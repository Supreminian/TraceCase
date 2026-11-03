"""End-to-end composition tests use real authorization, tools, JSONL and task storage."""

import os

import pytest

from sdlc.accounts import Accounts
from sdlc.agent import AssistantMessage, Decision, ToolCall, ToolResult, UserMessage
from sdlc.agent.composition import build_runtime
from sdlc.agent.application import ModelUnavailableError
from sdlc.secure_service import Service
from sdlc.testing_agent.domain import CaseBatch, CoveragePlan, RequirementAnalysis, RevisionProposal, EvidenceReview
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent import worker
from sdlc.testing_agent.chat import ChatService
from tests.test_testing_agent import TEXT, analysis, batch, plan, ready


class ScriptedClient:
    def __init__(self, *outputs, usage=None):
        self.outputs = list(outputs)
        self.contexts = []
        self.last_usage = {}
        self.last_metadata = {}
        self.usage = usage or {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10}

    def generate(self, context, settings):
        self.contexts.append(context)
        self.last_usage = {}
        self.last_metadata = {"request_id": f"request-{len(self.contexts)}"}
        assert self.outputs, "unexpected extra model invocation"
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, context.output_type), (type(result), context.output_type)
        self.last_usage = dict(self.usage)
        return result


def call(number, name, arguments=None):
    return AssistantMessage(tool_calls=[ToolCall(id=f"call-{number}", name=name,
                                                arguments=arguments or {})])


def full_script():
    return [
        call(1, "read_task"), Decision(should_allow=True),
        call(2, "analyze_requirements"), Decision(should_allow=True), RequirementAnalysis.model_validate(analysis()),
        call(3, "plan_coverage"), Decision(should_allow=True), CoveragePlan.model_validate(plan()),
        call(4, "generate_case_batch", {"module": "申请", "condition_ids": ["C1"], "batch_key": "submit"}),
        Decision(should_allow=True), CaseBatch.model_validate(batch()),
        call(5, "generate_case_batch", {"module": "审批", "condition_ids": ["C2"], "batch_key": "approval"}),
        Decision(should_allow=True), CaseBatch.model_validate(batch(2)),
        call(6, "inspect_coverage"), Decision(should_allow=True),
        call(7, "finalize_design"), Decision(should_allow=True),
        EvidenceReview(findings=[]),
        AssistantMessage(content="两条明确规则的用例已生成，请人工评审；测试尚未执行。"),
    ]


@pytest.fixture
def context(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("SDLC_AGENT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SDLC_AGENT_PROVIDER", "google")
    monkeypatch.setenv("SDLC_AGENT_ASSISTANT_MODEL", "test-assistant")
    monkeypatch.setenv("SDLC_AGENT_REVIEWER_MODEL", "test-reviewer")
    monkeypatch.setenv("SDLC_AGENT_SESSION_DIR", str(tmp_path / "sessions"))
    path = tmp_path / "composition.sqlite3"
    accounts = Accounts(path)
    accounts.register("composition-user", "Composition user", "disposable-composition-password")
    token = accounts.login("composition-user", "disposable-composition-password")
    workspace = accounts.spaces(token)[0]["id"]
    project = Service(path, token=token, workspace_id=workspace).create_project("Composition test", "")
    service = TestingService(path, token, workspace, project)
    task_id = service.create_task(TEXT)
    service.add_artifact(task_id, 'document_context', {'target_product': '虚构费用报销',
        'document_purpose': '定义用于测试准备的虚构报销业务', 'embedded_materials': []})
    return service, accounts, task_id


def runtime(service, task_id, client):
    return build_runtime(service.db_path, service.token, service.workspace_id,
                         service.project_id, task_id, model_client=client)


def test_full_runtime_uses_reviewed_real_tools_and_persists_reviewable_results(context):
    service, _, task_id = context
    client = ScriptedClient(*full_script())
    app = runtime(service, task_id, client)
    session = app.run(request_id="work-full")
    task = service.get_task(task_id)
    assert task["status"] == "waiting_review" and task["stage"] == "review"
    assert task["session_id"] == session.id
    assert len(client.contexts) == 20
    assert service.coverage(task_id)["missing_condition_ids"] == []
    run = service.legacy.get_run(task["run_id"])
    assert len(run["cases"]) == 2
    assert all(case["status"] == "pending_review" for case in run["cases"])
    assert len([message for message in session.messages if isinstance(message, ToolResult)]) == 7
    assert not [message.content for message in session.messages
                if isinstance(message, ToolResult) and message.is_error]
    assert session.metadata["agent_turn"]["status"] == "completed"
    assert app.agent.get_session(session.id) == session
    assert len([event for event in task["events"] if event["action"] == "model_call"]) == 20
    assert not task["baselines"]


def test_runtime_forwards_live_public_chunks_and_preserves_model_audit(context):
    service, _, task_id = context

    class StreamingClient(ScriptedClient):
        def generate_stream(self, model_context, settings, on_delta):
            assert model_context.output_type is AssistantMessage
            on_delta('已理解')
            on_delta('本轮目标。')
            return self.generate(model_context, settings)

    client = StreamingClient(AssistantMessage(content='已理解本轮目标。'))
    events = []
    app = build_runtime(service.db_path, service.token, service.workspace_id, service.project_id,
                        task_id, model_client=client, public_event_sink=events.append)
    app.run(request_id='streamed-main')
    assert ''.join(e['text'] for e in events if e['type'] == 'assistant_delta') == '已理解本轮目标。'
    assert len({e['message_id'] for e in events if e['type'] in {'assistant_start', 'assistant_delta', 'assistant_message'}}) == 1
    task = service.get_task(task_id)
    assert len([e for e in task['events'] if e['action'] == 'model_call']) == 1
    assert len([a for a in task['artifacts'] if a['kind'] == 'model_attempt']) == 1


def test_failed_second_batch_preserves_first_batch_and_asks_user_without_retry(context):
    service, _, task_id = context
    script = full_script()
    script[13] = ModelUnavailableError('PRIVATE upstream payload')
    client = ScriptedClient(*script)
    events = []
    app = build_runtime(service.db_path, service.token, service.workspace_id, service.project_id,
                        task_id, model_client=client, public_event_sink=events.append)
    app.run(request_id='partial-success')
    task = service.get_task(task_id)
    assert task['status'] == 'waiting_confirmation'
    assert len(service.case_drafts(task_id)) == 1 and task['run_id']
    assert len(service.legacy.get_run(task['run_id'])['cases']) == 1
    assert len(client.contexts) == 14
    assert any(e['type'] == 'waiting_confirmation' and e['tool'] == 'generate_case_batch' for e in events)
    assert 'PRIVATE' not in str(events) and 'PRIVATE' not in str(task)
    app.run(resume=True, request_id='resume-without-human')
    assert len(client.contexts) == 14


def test_runtime_failure_asks_confirmation_and_only_new_human_message_continues(context):
    service, _, task_id = context
    first = runtime(service, task_id, ScriptedClient(TimeoutError("api-key-PRIVATE")))
    first.run(request_id="work-failed")
    task = service.get_task(task_id)
    assert task["status"] == "waiting_confirmation"
    session = first.agent.get_session(task["session_id"])
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert "PRIVATE" not in str(task) and "PRIVATE" not in session.model_dump_json()
    resumed_client = ScriptedClient(AssistantMessage(content="已恢复，请补充范围。"))
    second = runtime(service, task_id, resumed_client)
    paused = second.run(resume=True, request_id="work-resume")
    assert resumed_client.contexts == [] and paused.messages == session.messages
    assert service.get_task(task_id)["status"] == "waiting_confirmation"
    recovered = second.run("确认重试刚才未完成的步骤", request_id="human-confirm-retry")
    assert [message.role for message in recovered.messages] == ["user", "assistant", "user", "assistant"]
    assert len(resumed_client.contexts) == 1
    assert service.get_task(task_id)["status"] == "partial"


def test_saved_human_question_survives_a_dropped_public_callback(context):
    service, _, task_id = context
    client = ScriptedClient(call(1, 'request_user_confirmation',
        {'question': '先确认本轮需要覆盖金额边界，可以继续吗？'}), Decision(should_allow=True))

    def dropped(event):
        raise RuntimeError('temporary UI delivery failure')

    app = build_runtime(service.db_path, service.token, service.workspace_id, service.project_id,
        task_id, model_client=client, public_event_sink=dropped)
    app.run(request_id='dropped-public-callback')
    chat = ChatService(service)
    pending = chat.get_pending(task_id)
    assert pending and pending['kind'] == 'generation_plan'
    assert pending['prompt'] == '先确认本轮需要覆盖金额边界，可以继续吗？'
    assert any(pending['prompt'] in m['content'] for m in chat.list_messages(task_id))
    assert len(client.contexts) == 2 and not client.outputs


@pytest.mark.parametrize("operation", ["start", "message"])
def test_repeated_work_delivery_does_not_append_or_call_model_twice(context, operation):
    service, _, task_id = context
    content = "请重点检查权限" if operation == "message" else ""
    first = runtime(service, task_id, ScriptedClient(AssistantMessage(content="本轮资料已整理。")))
    session = first.run(content, request_id="same-work")
    second_client = ScriptedClient()
    second = runtime(service, task_id, second_client)
    repeated = second.run(content, resume=True, request_id="same-work")
    assert repeated.messages == session.messages
    assert len([message for message in repeated.messages if isinstance(message, UserMessage)]) == 1
    assert second_client.contexts == []


def test_call_budget_is_reconstructed_and_not_reset_by_new_runtime(context, monkeypatch):
    service, _, task_id = context
    monkeypatch.setenv("SDLC_AGENT_MAX_MODEL_CALLS", "1")
    first = runtime(service, task_id, ScriptedClient(AssistantMessage(content="首轮结束。")))
    first.run(request_id="budget-first")
    second_client = ScriptedClient(AssistantMessage(content="不能调用到此响应"))
    second = runtime(service, task_id, second_client)
    assert second.agent.model_client.calls == 1
    second.run("继续", request_id="budget-second")
    assert second_client.contexts == []
    assert service.get_task(task_id)["status"] == "waiting_confirmation"
    assert ChatService(service).get_pending(task_id)['kind'] == 'budget_exhausted'


def test_reviewer_budget_stop_remains_a_budget_decision_instead_of_generic_failure(context, monkeypatch):
    service, _, task_id = context
    monkeypatch.setenv('SDLC_AGENT_MAX_MODEL_CALLS', '1')
    client = ScriptedClient(call(1, 'read_task'))
    runtime(service, task_id, client).run(request_id='reviewer-budget')
    assert len(client.contexts) == 1
    pending = ChatService(service).get_pending(task_id)
    assert pending and pending['kind'] == 'budget_exhausted'


def test_token_budget_reconstructs_input_output_usage_without_total_tokens(context, monkeypatch):
    service, _, task_id = context
    monkeypatch.setenv("SDLC_AGENT_ASSISTANT_MAX_TOKENS", "6")
    monkeypatch.setenv("SDLC_AGENT_MAX_TOTAL_TOKENS", "15")
    first = runtime(service, task_id, ScriptedClient(AssistantMessage(content="首轮结束。"),
                    usage={"input_tokens": 6, "output_tokens": 4}))
    first.run(request_id="tokens-first")
    second_client = ScriptedClient(AssistantMessage(content="不能调用到此响应"))
    second = runtime(service, task_id, second_client)
    assert second.agent.model_client.tokens == 10
    second.run("继续", request_id="tokens-second")
    assert second_client.contexts == []


@pytest.mark.parametrize("fails", [False, True])
def test_leased_worker_updates_job_from_actual_runtime_outcome(context, fails):
    service, accounts, task_id = context
    client = ScriptedClient(TimeoutError("private-provider-error")) if fails else ScriptedClient(*full_script())
    work_id = worker.enqueue(service, task_id, "start")
    job = worker.claim(service.db_path, "integration-worker")
    assert job["id"] == work_id

    def factory(path, token, workspace, project, task, *, execution_guard, capability, public_event_sink=None):
        return build_runtime(path, token, workspace, project, task,
                             execution_guard=execution_guard, model_client=client, capability=capability,
                             public_event_sink=public_event_sink)

    worker.execute(service.db_path, job, "integration-worker", runtime_factory=factory)
    pending = ChatService(service).get_pending(task_id)
    if fails:
        assert pending and pending['kind'] == 'retry_generation'
    else:
        assert pending is None
    work = worker.list_work(service, task_id)[0]
    assert work["status"] == "completed"
    assert service.get_task(task_id)["status"] == ("waiting_confirmation" if fails else "waiting_review")
    assert "private-provider-error" not in str(work)
    with accounts.db() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_assistant_can_choose_to_request_confirmation_and_continue_after_human_reply(context):
    service, _, task_id = context
    client = ScriptedClient(call(1, 'request_user_confirmation', {'question': '这轮优先验证权限和边界，可以按此重点继续吗？'}),
                            Decision(should_allow=True), AssistantMessage(content='已收到，将按确认的重点继续。'))

    def factory(path, token, workspace, project, task, *, execution_guard, capability, public_event_sink=None):
        return build_runtime(path, token, workspace, project, task, execution_guard=execution_guard,
            model_client=client, capability=capability, public_event_sink=public_event_sink)

    worker.enqueue(service, task_id, 'start')
    worker.execute(service.db_path, worker.claim(service.db_path, 'ask-worker'), 'ask-worker', runtime_factory=factory)
    pending = ChatService(service).get_pending(task_id)
    assert pending and pending['kind'] == 'generation_plan'
    assert len(client.contexts) == 2 and not service.case_drafts(task_id)
    ChatService(service).respond(task_id, pending['id'], 'retry', expected_revision=pending['revision'], request_key='human-reply')
    worker.execute(service.db_path, worker.claim(service.db_path, 'ask-worker'), 'ask-worker', runtime_factory=factory)
    assert ChatService(service).get_pending(task_id) is None
    assert len(client.contexts) == 3
    assert service.get_task(task_id)['status'] == 'partial'


def test_reviewer_message_runs_only_revision_tools_without_generation_permission(context):
    service, accounts, _ = context
    accounts.register("revision-reviewer", "Reviewer", "disposable-reviewer-password")
    token = accounts.login("revision-reviewer", "disposable-reviewer-password")
    team = accounts.create_team(service.token, "Revision team")
    invite = accounts.create_invite(service.token, team)
    request = accounts.request_join(token, invite, confirm=True)
    accounts.decide_join(service.token, team, request["request_id"], True, "reviewer")
    project = Service(service.db_path, token=service.token, workspace_id=team).create_project("Revision project", "")
    owner = TestingService(service.db_path, service.token, team, project)
    task_id = ready(owner)
    owner.finalize_design(task_id)
    reviewer = TestingService(service.db_path, token, team, project)
    proposal_id = reviewer.review_feedback(task_id, "TC1", "unclear_steps", "明确申请人的提交操作", 1, 1)
    assert "generate" not in accounts.access(token, team)["permissions"]
    work_id = worker.enqueue(reviewer, task_id, "message", "请处理指定退回反馈", request_key="reviewer-job")
    job = worker.claim(service.db_path, "revision-worker")
    assert job["id"] == work_id
    revised = batch()["cases"][0]["case"]
    revised["steps"] = [{"number": 1, "action": "申请人提交已准备的虚构报销单"}]
    client = ScriptedClient(
        call(2, "read_task"), Decision(should_allow=True),
        call(3, "propose_revision", {"case_id": "TC1", "feedback_proposal_id": proposal_id}),
        Decision(should_allow=True), RevisionProposal(case=revised, explanation="明确操作主体", unresolved=[]),
        AssistantMessage(content="修订已提出，请有编辑权限的成员检查后采纳。"),
    )
    seen_tools = []

    def factory(path, delegated, workspace, selected_project, task, *, execution_guard, capability,
                public_event_sink=None):
        app = build_runtime(path, delegated, workspace, selected_project, task,
            execution_guard=execution_guard, model_client=client, capability=capability,
            public_event_sink=public_event_sink)
        seen_tools.extend(tool.name for tool in app.agent.assistant.tools)
        assert capability == "regenerate"
        return app

    worker.execute(service.db_path, job, "revision-worker", runtime_factory=factory)
    assert set(seen_tools) == {"read_task", "read_project_context", "inspect_coverage", "propose_revision"}
    assert worker.list_work(reviewer, task_id)[0]["status"] == "completed"
    current = reviewer.get_task(task_id)
    revision = next(item for item in current["proposals"] if item["kind"] == "case_revision")
    assert revision["status"] == "pending"
    assert revision["content"]["case"]["steps"][0]["action"] == revised["steps"][0]["action"]
    original = reviewer.legacy.get_run(current["run_id"])["cases"][0]
    assert original["version"] == 1 and original["status"] == "returned"

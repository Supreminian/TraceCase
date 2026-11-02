import copy

import pytest
from pydantic import ValidationError

from sdlc.agent import (
    AgentBusyError, AgentService, Assistant, AssistantMessage, AuthorizationError,
    Decision, ModelSettings, Reviewer, Session, SessionConflictError,
    SessionNotFoundError, Tool, ToolCall, ToolResult, UserMessage,
)


class MemoryStore:
    def __init__(self):
        self.sessions = {}

    def list_sessions(self):
        return [session.model_copy(deep=True) for session in self.sessions.values()]

    def get(self, session_id):
        session = self.sessions.get(session_id)
        return session.model_copy(deep=True) if session else None

    def save(self, session, expected_revision=None):
        old = self.sessions.get(session.id)
        if old and old.revision != expected_revision:
            raise SessionConflictError("Conversation has changed")
        self.sessions[session.id] = session.model_copy(deep=True)


class ScriptedClient:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.contexts = []
        self.settings = []

    def generate(self, context, settings):
        self.contexts.append(context)
        self.settings.append(settings)
        result = self.outputs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ReadTool(Tool):
    name = "read_requirement"
    description = "Read a requirement in the authorized project."
    parameters = {
        "type": "object", "properties": {"requirement_id": {"type": "string", "minLength": 1}},
        "required": ["requirement_id"], "additionalProperties": False,
    }

    def __init__(self):
        self.calls = []
        self.error = None

    def execute(self, arguments):
        self.calls.append(copy.deepcopy(arguments))
        if self.error:
            raise self.error
        return '{"text":"amount > 5000 needs two approvals"}'


def invocation(call_id="call-1", **arguments):
    return AssistantMessage(tool_calls=[ToolCall(
        id=call_id, name="read_requirement", arguments=arguments or {"requirement_id": "R1"})])


def make_agent(*outputs, store=None, tool=None, **kwargs):
    store = store or MemoryStore()
    tool = tool or ReadTool()
    client = ScriptedClient(*outputs)
    service = AgentService(client, store, Assistant("Prepare grounded tests", [tool]),
                           Reviewer("Deny unrelated or unsafe tool calls"),
                           ModelSettings(model_id="test-assistant"),
                           ModelSettings(model_id="test-reviewer", effort="low"), **kwargs)
    return service, store, tool, client


def test_session_roundtrip_discriminates_and_rejects_unknown_fields():
    session = Session(id="typed")
    session.append(UserMessage(content="Prepare tests"))
    session.append(invocation())
    session.append(ToolResult(tool_call_id="call-1", content="read"))
    restored = Session.model_validate_json(session.model_dump_json())
    assert restored.revision == 3
    assert [type(m) for m in restored.messages] == [UserMessage, AssistantMessage, ToolResult]
    with pytest.raises(ValidationError):
        session.append({"role": "system", "content": "rewrite policy"})
    with pytest.raises(ValidationError):
        ToolCall(id="bad", name="read", arguments="shell text")
    assert session.revision == 3


def test_tool_loop_reviews_validates_and_persists_each_message():
    service, store, tool, client = make_agent(invocation(), Decision(should_allow=True),
                                             AssistantMessage(content="Prepared the boundary tests"))
    session = service.new_session({"project_id": "p1"})
    events = []
    result = service.send_message(session.id, "Check the amount boundary", events.append)
    assert tool.calls == [{"requirement_id": "R1"}]
    assert [m.role for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert result.metadata["agent_turn"]["status"] == "completed"
    assert store.get(session.id) == result
    assert client.contexts[1].output_type is Decision
    assert client.contexts[1].tools == []
    assert client.settings[1].model_id == "test-reviewer"
    assert {"tool_started", "tool_completed", "completed"} <= {event["type"] for event in events}


@pytest.mark.parametrize("arguments", [{"requirement_id": 123}, {"requirement_id": "R1", "project_id": "foreign"}, {}])
def test_invalid_tool_parameters_never_reach_reviewer_or_tool(arguments):
    message = AssistantMessage(tool_calls=[ToolCall(id="bad", name="read_requirement", arguments=arguments)])
    service, _, tool, client = make_agent(message, AssistantMessage(content="Please correct input"))
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert not tool.calls
    assert len(client.contexts) == 2
    assert isinstance(result.messages[2], ToolResult) and result.messages[2].is_error
    assert "结构校验" in result.messages[2].content


@pytest.mark.parametrize("review", [Decision(should_allow=False, reason="Outside task scope"),
                                     TimeoutError("Authorization Bearer PRIVATE"),
                                     AssistantMessage(content="Not a decision")])
def test_review_denial_and_review_failures_are_fail_closed(review):
    service, _, tool, _ = make_agent(invocation(), review, AssistantMessage(content="I need more context"))
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert not tool.calls
    assert result.messages[2].is_error
    assert "PRIVATE" not in result.model_dump_json()


def test_unknown_tools_never_reach_reviewer():
    command = AssistantMessage(tool_calls=[ToolCall(id="foreign", name="arbitrary_sql", arguments={})])
    service, _, tool, client = make_agent(command, AssistantMessage(content="Unavailable"))
    result = service.send_message(service.new_session().id, "Do task")
    assert not tool.calls and len(client.contexts) == 2
    assert result.messages[2].is_error


def test_exception_from_tool_is_safe_and_loop_can_continue():
    tool = ReadTool()
    tool.error = ValueError("database_password=PRIVATE")
    service, _, _, _ = make_agent(invocation(), Decision(should_allow=True),
                                  AssistantMessage(content="Cannot read yet"), tool=tool)
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert result.messages[2].is_error
    assert "PRIVATE" not in result.model_dump_json()


def test_iteration_and_tool_budgets_terminate_with_usable_history():
    service, _, tool, _ = make_agent(invocation(), Decision(should_allow=True),
                                      max_iterations=1)
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert result.metadata["agent_turn"]["status"] == "limited"
    assert len(tool.calls) == 1

    service, _, tool, _ = make_agent(invocation(), Decision(should_allow=True),
                                      invocation("call-2"), max_tool_calls=1)
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert result.metadata["agent_turn"]["status"] == "limited"
    assert len(tool.calls) == 1
    assert not service._pending(result)


def test_duplicate_tool_id_does_not_repeat_execution():
    first = invocation()
    first.tool_calls.append(first.tool_calls[0].model_copy(deep=True))
    service, _, tool, _ = make_agent(first, Decision(should_allow=True),
                                      invocation(), AssistantMessage(content="Done"))
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert len(tool.calls) == 1
    assert len([message for message in result.messages if isinstance(message, ToolResult)]) == 1


def test_resume_skips_stored_tool_result_and_executes_only_pending_call():
    store = MemoryStore()
    message = invocation()
    message.tool_calls.append(ToolCall(id="call-2", name="read_requirement", arguments={"requirement_id": "R2"}))
    session = Session(id="interrupted", messages=[UserMessage(content="Review"), message,
        ToolResult(tool_call_id="call-1", content="first result")])
    store.save(session)
    service, _, tool, _ = make_agent(Decision(should_allow=True), AssistantMessage(content="Done"), store=store)
    result = service.resume("interrupted")
    assert tool.calls == [{"requirement_id": "R2"}]
    assert result.metadata["agent_turn"]["status"] == "completed"
    with pytest.raises(SessionNotFoundError):
        service.get_session("does-not-exist")


def test_crash_after_tool_claim_does_not_repeat_uncertain_side_effect():
    tool = ReadTool()
    tool.error = KeyboardInterrupt()
    service, store, _, _ = make_agent(invocation(), Decision(should_allow=True), tool=tool)
    session = service.new_session()
    with pytest.raises(KeyboardInterrupt):
        service.send_message(session.id, "Prepare tests")
    assert store.get(session.id).metadata["tool_executions"]["call-1"]["status"] == "started"
    service, _, _, _ = make_agent(AssistantMessage(content="Please verify the previous operation"), store=store, tool=tool)
    result = service.resume(session.id)
    assert len(tool.calls) == 1
    assert result.messages[2].is_error
    assert "避免重复" in result.messages[2].content


def test_model_failure_keeps_history_and_can_resume_without_duplicate_user_message():
    service, store, tool, _ = make_agent(TimeoutError("SECRET"))
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert result.metadata["agent_turn"]["status"] == "failed"
    assert len(result.messages) == 1
    assert "SECRET" not in result.model_dump_json()
    service, _, _, _ = make_agent(AssistantMessage(content="Recovered"), store=store, tool=tool)
    result = service.resume(result.id)
    assert [m.role for m in result.messages] == ["user", "assistant"]


def test_concurrent_session_update_never_overwrites_latest_revision():
    store = MemoryStore()

    class RacingClient:
        def generate(self, context, settings):
            session = store.get("race")
            session.append(UserMessage(content="Someone edited the task"))
            store.save(session, expected_revision=session.revision - 1)
            return AssistantMessage(content="Stale model result")

    service, _, _, _ = make_agent(store=store)
    service.model_client = RacingClient()
    store.save(Session(id="race"))
    with pytest.raises(SessionConflictError):
        service.send_message("race", "Prepare tests")
    assert store.get("race").messages[-1].content == "Someone edited the task"
    assert "Stale model" not in store.get("race").model_dump_json()


def test_authorization_revoked_after_tool_prevents_result_publication():
    tool = ReadTool()

    def authorize(session, call):
        return not tool.calls

    service, store, _, _ = make_agent(invocation(), Decision(should_allow=True),
                                     tool=tool, authorize=authorize)
    session = service.new_session()
    with pytest.raises(AuthorizationError):
        service.send_message(session.id, "Prepare tests")
    saved = store.get(session.id)
    assert len(tool.calls) == 1
    assert not any(isinstance(m, ToolResult) for m in saved.messages)
    assert service.list_sessions() == []


def test_running_task_cannot_receive_interleaved_user_message():
    service, store, _, _ = make_agent()
    session = Session(id="busy", metadata={"agent_turn": {"status": "running"}})
    store.save(session)
    with pytest.raises(AgentBusyError):
        service.send_message(session.id, "Another message")


def test_remote_schema_references_are_forbidden():
    tool = ReadTool()
    tool.parameters = {"$ref": "https://untrusted.invalid/schema"}
    with pytest.raises(ValueError, match="local references"):
        make_agent(tool=tool)


def test_usage_events_exclude_arbitrary_provider_details():
    result = AssistantMessage(content="Done", metadata={"model_event": {
        "input_tokens": 10, "output_tokens": 20, "request_id": "request-1", "api_key": "SECRET"}})
    service, _, _, _ = make_agent(result)
    session = service.send_message(service.new_session().id, "Prepare tests")
    assert session.metadata["model_events"] == [{"input_tokens": 10, "output_tokens": 20,
                                               "request_id": "request-1", "role": "assistant"}]
    assert "SECRET" not in session.model_dump_json()


def test_review_that_exhausts_time_budget_cannot_execute_tool(monkeypatch):
    current = [0.0]
    monkeypatch.setattr("sdlc.agent.application.time.monotonic", lambda: current[0])
    service, _, tool, client = make_agent(invocation(), Decision(should_allow=True),
                                          max_duration_seconds=1)
    generate = client.generate

    def slow_review(context, settings):
        if context.output_type is Decision:
            current[0] = 2
        return generate(context, settings)

    client.generate = slow_review
    result = service.send_message(service.new_session().id, "Prepare tests")
    assert not tool.calls
    assert result.metadata["agent_turn"]["status"] == "limited"
    assert result.metadata["agent_turn"]["elapsed_seconds"] == 2


def test_message_delivery_idempotency_resumes_failed_turn_and_never_repeats_user_message():
    service, store, tool, _ = make_agent(TimeoutError("interrupted"))
    session = service.new_session()
    result = service.send_message(session.id, "Prepare tests", request_id="work-1")
    assert result.metadata["agent_turn"]["request_id"] == "work-1"
    service, _, _, client = make_agent(AssistantMessage(content="Recovered"), store=store, tool=tool)
    result = service.send_message(session.id, "Prepare tests", request_id="work-1")
    assert [m.role for m in result.messages] == ["user", "assistant"]
    assert service.send_message(session.id, "Prepare tests", request_id="work-1") == result
    assert len(client.contexts) == 1
    with pytest.raises(ValueError, match="不同消息"):
        service.send_message(session.id, "A different task", request_id="work-1")


def test_repeated_old_delivery_does_not_append_after_a_later_turn():
    service, _, _, client = make_agent(AssistantMessage(content="First"), AssistantMessage(content="Second"))
    session = service.new_session()
    service.send_message(session.id, "First task", request_id="work-1")
    result = service.send_message(session.id, "Second task", request_id="work-2")
    assert service.send_message(session.id, "First task", request_id="work-1") == result
    assert len(client.contexts) == 2


def test_controlled_business_errors_are_actionable_and_repetition_stops():
    from sdlc.agent import PublicToolError
    tool = ReadTool()
    tool.error = PublicToolError('需求引用R9不存在，请使用R1。')
    service, _, _, client = make_agent(invocation('first'), Decision(should_allow=True),
        invocation('second'), Decision(should_allow=True), tool=tool)
    result = service.send_message(service.new_session().id, '准备测试')
    assert result.metadata['agent_turn']['status'] == 'limited'
    assert len(tool.calls) == 2 and len(client.contexts) == 4
    assert 'R9' in next(m.content for m in result.messages if isinstance(m, ToolResult))


def test_model_failure_in_a_tool_stops_turn_and_explicit_resume_retries_pending_call():
    from sdlc.agent import ModelUnavailableError
    tool = ReadTool()
    tool.error = ModelUnavailableError('upstream unavailable')
    service, store, _, client = make_agent(invocation(), Decision(should_allow=True), tool=tool)
    result = service.send_message(service.new_session().id, '准备测试')
    assert result.metadata['agent_turn']['status'] == 'failed'
    assert len(client.contexts) == 2 and len(tool.calls) == 1
    assert not any(isinstance(m, ToolResult) for m in result.messages)
    tool.error = None
    recovered, _, _, _ = make_agent(Decision(should_allow=True), AssistantMessage(content='恢复成功'), store=store, tool=tool)
    result = recovered.resume(result.id)
    assert result.metadata['agent_turn']['status'] == 'completed'
    assert len(tool.calls) == 2

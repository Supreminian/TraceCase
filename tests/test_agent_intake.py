"""Intent routing uses original messages and durable, authorized receipts."""
import json

import pytest

from sdlc.accounts import Accounts
from sdlc.agent import ModelSettings
from sdlc.agent.composition import build_runtime
from sdlc.secure_service import Service
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.intake import MessageIntent, route_message
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent import worker
from tests.test_agent_composition import ScriptedClient
from tests.test_testing_agent import TEXT, analysis


@pytest.fixture
def intake_context(tmp_path, monkeypatch):
    monkeypatch.setenv('SDLC_AGENT_SESSION_DIR', str(tmp_path / 'sessions'))
    path = tmp_path / 'intake.sqlite3'
    accounts = Accounts(path)
    accounts.register('intake-user', 'Intake user', 'disposable-intake-password')
    token = accounts.login('intake-user', 'disposable-intake-password')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('Intake test', '')
    service = TestingService(path, token, workspace, project)
    return service, accounts, service.create_task(TEXT)


def route(service, task, content, client, key=None, sink=None):
    job = key or worker.enqueue(service, task, 'message', content)
    return route_message(service, task, content, job, client, ModelSettings(model_id='test-intent'), sink), job


@pytest.mark.parametrize('kind', ['requirements_replace', 'requirements_append', 'scope_change'])
def test_verbatim_change_creates_atomic_child_and_one_queue_entry(intake_context, kind):
    service, _, task = intake_context
    original = '# 新需求\n\n用户可以创建虚构笔记。\n\n相同标题不可重复。' if kind != 'scope_change' else '这轮只关注边界测试，不测试页面布局。'
    client = ScriptedClient(MessageIntent(intent=kind, confidence=.99))
    events = []
    result, job = route(service, task, original, client, sink=events.append)
    assert result.handled and result.child_task_id
    parent = service.get_task(task)
    child = service.get_task(result.child_task_id)
    assert parent['input']['original_text'] == TEXT and parent['status'] == 'partial'
    assert child['parent_task_id'] == task and child['document_version'] == 2
    if kind == 'requirements_replace':
        assert child['input']['original_text'] == original
    elif kind == 'requirements_append':
        assert child['input']['original_text'].endswith(original)
        assert TEXT in child['input']['original_text']
    else:
        assert child['input']['normalized_text'] == parent['input']['normalized_text']
        assert child['goal'] == original and child['scope'] == original
    assert any(a['kind'] == 'handoff' and a['content']['child_task_id'] == child['id'] for a in parent['artifacts'])
    queued = worker.list_work(service, child['id'])
    assert len(queued) == 1 and queued[0]['operation'] == 'start'
    assert any(e['type'] == 'task_handoff' for e in events)
    replay, _ = route(service, task, original, ScriptedClient(), key=job)
    assert replay == result and len(service.list_tasks()) == 2
    assert len(worker.list_work(service, child['id'])) == 1
    assert client.contexts[0].output_type is MessageIntent
    payload = json.loads(client.contexts[0].messages[0].content)
    assert payload['original_user_message'] == original


@pytest.mark.parametrize('intent', [MessageIntent(intent='conversation', confidence=.99),
                                    MessageIntent(intent='conversation', confidence=.5),
                                    MessageIntent(intent='clarification_answer', confidence=.99, question_id='INVENTED')])
def test_uncertain_or_unknown_answer_continues_conversation_without_effect(intake_context, intent):
    service, _, task = intake_context
    client = ScriptedClient(intent)
    result, job = route(service, task, '请说明接下来做什么', client)
    assert not result.handled and len(service.list_tasks()) == 1
    assert service.get_task(task)['questions'] == []
    replay, _ = route(service, task, '请说明接下来做什么', ScriptedClient(), key=job)
    assert replay == result


def test_confidence_is_recorded_without_programmatic_semantic_threshold(intake_context):
    service, _, task = intake_context
    result, _ = route(service, task, '# 新需求\n\n用户可以创建虚构笔记。',
                      ScriptedClient(MessageIntent(intent='requirements_replace', confidence=.2)))
    assert result.handled and result.child_task_id
    receipt = next(a['content'] for a in service.get_task(task)['artifacts'] if a['kind'] == 'message_intake')
    assert receipt['classification']['confidence'] == .2
    assert receipt['effective_intent'] == 'requirements_replace'


def test_answer_is_original_text_pending_confirmation_not_new_business_fact(intake_context):
    service, _, task = intake_context
    service.save_analysis(task, analysis(ambiguous=True))
    text = '关于 Q1：二级审批由指定的虚构主管负责。'
    result, job = route(service, task, text, ScriptedClient(MessageIntent(
        intent='clarification_answer', confidence=.99, question_id='Q1')))
    assert result.handled and result.child_task_id is None
    current = service.get_task(task)
    question = current['questions'][0]
    assert current['status'] == 'waiting_answer'
    assert question['status'] == 'answered' and question['answer']['text'] == text
    assert question['confirmed'] is None and len(service.list_tasks()) == 1
    revision = question['revision']
    route(service, task, text, ScriptedClient(), key=job)
    assert service.get_task(task)['questions'][0]['revision'] == revision


def test_start_and_confirmed_retry_do_not_classify(intake_context):
    service, _, task = intake_context
    start = worker.enqueue(service, task, 'start')
    empty = ScriptedClient()
    result = route_message(service, task, '', start, empty, ModelSettings(model_id='intent'))
    assert not result.handled and not empty.contexts
    with service.connection('view', True) as db:
        db.execute("UPDATE agent_work_items SET status='completed' WHERE id=?", (start,))
    chat = ChatService(service)
    pending = chat.request_confirmation(task, kind='generation_plan', plan_hash='p1')
    response = chat.respond(task, pending['id'], 'retry', expected_revision=pending['revision'], request_key='confirmed')
    with service.connection() as db:
        payload = json.loads(db.execute('SELECT payload FROM agent_work_items WHERE id=?', (response['job_id'],)).fetchone()[0])
    result = route_message(service, task, payload['content'], response['job_id'], empty, ModelSettings(model_id='intent'))
    assert not result.handled and not empty.contexts


@pytest.mark.parametrize('explicit,quote,expected', [(True, '继续生成', 'retry'),
                                                    (False, '继续生成', 'discuss'),
                                                    (True, '模型编造的同意', 'discuss')])
def test_natural_chat_consent_requires_prior_human_decision_and_verbatim_evidence(intake_context, explicit, quote, expected):
    service, _, task = intake_context
    chat = ChatService(service)
    pending = chat.request_confirmation(task, kind='generation_plan', plan_hash='exact-plan')
    message = '好的，继续生成这轮用例。'
    response = chat.respond(task, pending['id'], 'discuss', message, expected_revision=pending['revision'], request_key='discuss')
    client = ScriptedClient(MessageIntent(intent='continue_generation', confidence=.99,
                                         explicit_confirmation=explicit, evidence_quote=quote))
    result, _ = route(service, task, message, client, key=response['job_id'])
    assert not result.handled
    current = chat.list_confirmations(task)[0]
    assert current['choice'] == expected and current['plan_hash'] == 'exact-plan'
    assert service.get_task(task)['questions'] == []


def test_runtime_routes_regular_message_without_running_old_agent(intake_context, monkeypatch):
    service, _, task = intake_context
    monkeypatch.setenv('SDLC_AGENT_PROVIDER', 'google')
    monkeypatch.setenv('SDLC_AGENT_ASSISTANT_MODEL', 'test-assistant')
    monkeypatch.setenv('SDLC_AGENT_REVIEWER_MODEL', 'test-reviewer')
    text = '# 完整新需求\n\n申请人可提交虚构记录。'
    job = worker.enqueue(service, task, 'message', text)
    client = ScriptedClient(MessageIntent(intent='requirements_replace', confidence=.99))
    runtime = build_runtime(service.db_path, service.token, service.workspace_id, service.project_id,
                            task, model_client=client)
    session = runtime.run(text, request_id=job)
    assert session.messages == [] and len(client.contexts) == 1
    parent = service.get_task(task)
    assert parent['status'] == 'partial'
    assert len([event for event in parent['events'] if event['action'] == 'model_call']) == 1
    assert len([a for a in parent['artifacts'] if a['kind'] == 'model_attempt']) == 1


def test_clear_consent_after_a_separate_explanation_turn_is_still_processed(intake_context):
    service, _, task = intake_context
    chat = ChatService(service)
    pending = chat.request_confirmation(task, 'upstream timeout')
    explanation_job = worker.enqueue(service, task, 'message', '为什么上一次没有完成？')
    result, _ = route(service, task, '为什么上一次没有完成？',
        ScriptedClient(MessageIntent(intent='conversation', confidence=.99)), key=explanation_job)
    assert not result.handled
    with service.connection('view', True) as db:
        db.execute("UPDATE agent_work_items SET status='completed' WHERE id=?", (explanation_job,))
    service.set_state(task, 'partial')
    message = '了解了，现在继续刚才未完成的生成。'
    client = ScriptedClient(MessageIntent(intent='continue_generation', confidence=.99,
        explicit_confirmation=True, evidence_quote='继续刚才未完成的生成'))
    result, job = route(service, task, message, client)
    assert not result.handled
    saved = chat.list_confirmations(task)[0]
    assert saved['id'] == pending['id'] and saved['choice'] == 'retry'
    audit = next(e for e in service.get_task(task)['events'] if e['action'] == 'chat_generation_confirmed')
    assert audit['details']['request_id'] == job
    assert audit['details']['evidence_quote'] in message


def test_newer_stop_cannot_be_bypassed_by_older_discussion_context(intake_context):
    service, _, task = intake_context
    chat = ChatService(service)
    first = chat.request_confirmation(task)
    first_job = chat.respond(task, first['id'], 'discuss', '解释失败原因')['job_id']
    with service.connection('view', True) as db:
        db.execute("UPDATE agent_work_items SET status='completed' WHERE id=?", (first_job,))
    latest = chat.request_confirmation(task, request_key='newer-failure')
    chat.respond(task, latest['id'], 'stop')
    message = '继续'
    result, _ = route(service, task, message, ScriptedClient(MessageIntent(intent='continue_generation',
        confidence=1, explicit_confirmation=True, evidence_quote=message)))
    assert not result.handled
    assert all(c['choice'] != 'retry' for c in chat.list_confirmations(task))


def test_classification_does_not_write_for_reviewer_without_generation_permission(intake_context):
    service, accounts, _ = intake_context
    accounts.register('intake-reviewer', 'Reviewer', 'disposable-reviewer-password')
    reviewer_token = accounts.login('intake-reviewer', 'disposable-reviewer-password')
    team = accounts.create_team(service.token, 'Intake team')
    invite = accounts.create_invite(service.token, team)
    request = accounts.request_join(reviewer_token, invite, confirm=True)
    accounts.decide_join(service.token, team, request['request_id'], True, 'reviewer')
    project = Service(service.db_path, token=service.token, workspace_id=team).create_project('Team intake', '')
    owner = TestingService(service.db_path, service.token, team, project)
    task = owner.create_task(TEXT)
    reviewer = TestingService(service.db_path, reviewer_token, team, project)
    reviewer.execution_capability = 'regenerate'
    client = ScriptedClient()
    result, _ = route(reviewer, task, '这是新的需求文档', client)
    assert not result.handled and client.contexts == []
    assert len(owner.list_tasks()) == 1

"""First messages are model-routed chat, never implicitly accepted as a PRD.

These tests exercise the authenticated queue, runtime and durable business state.
Only model responses are substituted; no greeting keyword matcher is involved.
"""
import json
import os

import pytest

from sdlc.accounts import Accounts
from sdlc.agent import AssistantMessage, Decision, ToolCall
from sdlc.secure_service import Service
from sdlc.testing_agent import worker
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.intake import MessageIntent
from sdlc.testing_agent.service import TestingService
from tests.test_agent_chat_flow import ConversationModel, execute_next, assistant_replies
from tests.test_testing_agent import ready


PRD = '# 便签管理\n\n用户可以创建便签。\n\n便签标题最多20字。'


@pytest.fixture
def initial_chat(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith('SDLC_AGENT_'):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SDLC_AGENT_PROVIDER', 'google')
    monkeypatch.setenv('SDLC_AGENT_ASSISTANT_MODEL', 'scripted-assistant')
    monkeypatch.setenv('SDLC_AGENT_REVIEWER_MODEL', 'scripted-reviewer')
    monkeypatch.setenv('SDLC_AGENT_SESSION_DIR', str(tmp_path / 'sessions'))
    path = tmp_path / 'initial-chat.sqlite3'
    accounts = Accounts(path)
    accounts.register('first-message', 'First message', 'isolated-chat-password')
    token = accounts.login('first-message', 'isolated-chat-password')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('首次对话', '')
    return TestingService(path, token, workspace, project)


def assert_no_requirement_work(service, task_id):
    task = service.get_task(task_id)
    assert task['input']['kind'] == 'conversation'
    assert task['input']['original_text'] == task['input']['normalized_text'] == ''
    assert task['input']['paragraphs'] == []
    assert task['document_version'] == 0
    assert task['run_id'] is None
    assert task['questions'] == []
    assert task['requirement_identities'] == []
    assert task['case_identities'] == []
    assert not {'analysis', 'document_context', 'coverage_plan', 'case_batch'} & {
        artifact['kind'] for artifact in task['artifacts']}
    return task


@pytest.mark.parametrize('message', ['你好', 'Bonjour, tu peux m’aider ?', '你可以帮我做什么？'])
def test_first_conversation_message_streams_without_creating_requirement_evidence(initial_chat, message):
    service = initial_chat
    task_id = service.create_conversation(message)
    queued = worker.list_work(service, task_id)
    assert len(queued) == 1 and queued[0]['operation'] == 'message'
    assert_no_requirement_work(service, task_id)
    chat = ChatService(service)
    assert [item['content'] for item in chat.list_messages(task_id)] == [message]
    answer = '你好！可以和我讨论测试目标，也可以让我查看项目已有用例。'
    model = ConversationModel(MessageIntent(intent='conversation', confidence=.99),
                              AssistantMessage(content=answer))
    live = []

    def observe(event):
        if event['type'] == 'assistant_delta':
            reply = assistant_replies(ChatService(service), task_id)[-1]
            live.append((reply['content'], reply['state'], worker.list_work(service, task_id)[0]['status']))

    job = execute_next(service, model, task_id=task_id, observe=observe)
    assert live == [(answer[:2], 'streaming', 'running'), (answer, 'streaming', 'running')]
    task = assert_no_requirement_work(service, task_id)
    assert len(service.list_tasks()) == 1
    assert chat.get_pending(task_id) is None
    assert assistant_replies(chat, task_id)[-1]['content'] == answer
    intake = json.loads(model.contexts[0].messages[0].content)
    assert intake['original_user_message'] == message
    assert intake['current_requirement_excerpt'] == ''
    assert model.contexts[-1].messages[-1].content == message
    tools = {tool.name for tool in model.contexts[-1].tools}
    assert {'list_test_cases', 'get_test_case', 'ask_for_requirements'} <= tools
    assert not {'analyze_requirements', 'generate_case_batch', 'plan_coverage', 'finalize_design'} & tools
    calls = [event['details'] for event in task['events'] if event['action'] == 'model_call']
    assert all(call['budget_scope'] == 'conversation' and call['budget_request_id'] == job['id'] for call in calls)
    assert worker.claim(service.db_path, 'no-automatic-generation') is None


@pytest.mark.parametrize('kind', ['requirements_replace', 'requirements_append'])
def test_first_actual_requirements_are_classified_then_preserved_verbatim(initial_chat, kind):
    service = initial_chat
    initial = service.create_conversation(PRD)
    model = ConversationModel(MessageIntent(intent=kind, confidence=.99))
    execute_next(service, model, task_id=initial)
    parent = assert_no_requirement_work(service, initial)
    handoff = next(artifact['content'] for artifact in parent['artifacts'] if artifact['kind'] == 'handoff')
    child = service.get_task(handoff['child_task_id'])
    assert child['parent_task_id'] == initial
    assert child['document_version'] == 1
    assert child['input']['original_text'] == PRD
    assert child['input']['paragraphs'][1]['text'] == '用户可以创建便签。'
    assert all(paragraph['stable_id'] for paragraph in child['input']['paragraphs'])
    assert child['goal'] != '测试协作对话'
    assert len(worker.list_work(service, child['id'])) == 1
    assert worker.list_work(service, child['id'])[0]['operation'] == 'start'


def test_requirements_after_greeting_create_first_document_without_greeting_evidence(initial_chat):
    service = initial_chat
    initial = service.create_conversation('你好')
    execute_next(service, ConversationModel(MessageIntent(intent='conversation', confidence=1),
        AssistantMessage(content='你好，你希望验证哪些功能？')), task_id=initial)
    worker.enqueue(service, initial, 'message', PRD, request_key='actual-prd-after-greeting')
    model = ConversationModel(MessageIntent(intent='requirements_replace', confidence=1))
    execute_next(service, model, task_id=initial)
    parent = assert_no_requirement_work(service, initial)
    child_id = next(a['content']['child_task_id'] for a in parent['artifacts'] if a['kind'] == 'handoff')
    child = service.get_task(child_id)
    assert child['input']['original_text'] == PRD
    assert child['document_version'] == 1
    assert all('你好' not in block['text'] for block in child['input']['paragraphs'])
    assert any(m['content'] == '你好' for m in ChatService(service).list_messages(initial))
    assert len(service.list_tasks()) == 2


def test_scope_without_requirements_gets_model_question_and_later_input_still_routes(initial_chat):
    service = initial_chat
    initial = service.create_conversation('这次优先验证便签标题的边界')
    question = '便签标题允许多长，空标题应该如何处理？'
    model = ConversationModel(MessageIntent(intent='scope_change', confidence=.99),
        AssistantMessage(tool_calls=[ToolCall(id='ask-title-rule', name='ask_for_requirements',
            arguments={'question': question})]), Decision(should_allow=True))
    execute_next(service, model, task_id=initial)
    assert_no_requirement_work(service, initial)
    chat = ChatService(service)
    pending = chat.get_pending(initial)
    assert pending and pending['kind'] == 'requirements_input'
    assert question in pending['prompt']
    assert len(service.list_tasks()) == 1
    worker.enqueue(service, initial, 'message', PRD, request_key='answer-with-real-prd')
    execute_next(service, ConversationModel(MessageIntent(intent='requirements_replace', confidence=1)),
                 task_id=initial)
    child = next(task for task in service.list_tasks() if task['id'] != initial)
    assert child['input']['original_text'] == PRD
    assert child['document_version'] == 1


def test_first_message_can_query_approved_project_cases_without_a_prd(initial_chat):
    service = initial_chat
    source = ready(service)
    run_id = service.finalize_design(source)
    for case in service.legacy.get_run(run_id)['cases']:
        service.legacy.review(run_id, case['case_id'], case['version'], case['revision'],
            'approve', '', '符合需求', 'initial-query-' + case['case_id'])
    service.create_baseline(source)
    initial = service.create_conversation('直接展示这个项目全部已批准的测试用例')
    model = ConversationModel(MessageIntent(intent='conversation', confidence=1),
        AssistantMessage(content='我来查询本项目已批准的用例。', tool_calls=[ToolCall(
            id='approved-first-message', name='list_test_cases',
            arguments={'status': 'approved', 'scope': 'project', 'display_only': True})]))
    execute_next(service, model, task_id=initial)
    assert_no_requirement_work(service, initial)
    assert not any(context.output_type is Decision for context in model.contexts)
    card = next(message for message in ChatService(service).list_messages(initial)
                if message['kind'] == 'case_collection')
    payload = json.loads(card['content'])
    assert len(payload['shown_references']) == 2
    assert {item['run_id'] for item in payload['shown_references']} == {run_id}
    assert service.get_task(source)['status'] == 'completed'


@pytest.mark.parametrize('content,options', [('', {}), ('   ', {}), ('你好', {'due_at': 'bad-date'}),
                                           ('你好', {'owner_id': 'foreign-member'})])
def test_invalid_initial_message_or_options_leave_no_orphan_task(initial_chat, content, options):
    service = initial_chat
    with pytest.raises(ValueError):
        service.create_conversation(content, **options)
    assert service.list_tasks() == []


def test_queue_admission_failure_rolls_back_conversation_and_messages(initial_chat):
    service = initial_chat
    for message in ('第一条消息', '第二条消息'):
        service.create_conversation(message)
    with pytest.raises(ValueError, match='队列'):
        service.create_conversation('第三条消息')
    assert len(service.list_tasks()) == 2
    with service.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM testing_agent_chat_messages').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM agent_work_items').fetchone()[0] == 2


def test_new_chat_ui_queues_greeting_as_a_message_not_a_start_job(initial_chat):
    from tests.test_agent_ui import show
    service = initial_chat
    app = show(service)
    app.chat_input(key='agent_new_chat_' + service.project_id).set_value('你好').run()
    assert not app.exception
    tasks = service.list_tasks()
    assert len(tasks) == 1
    task_id = tasks[0]['id']
    assert_no_requirement_work(service, task_id)
    queued = worker.list_work(service, task_id)
    assert len(queued) == 1 and queued[0]['operation'] == 'message'
    assert [message['content'] for message in ChatService(service).list_messages(task_id)] == ['你好']
    assert not any('需求版本 0' in item.value for item in app.markdown)

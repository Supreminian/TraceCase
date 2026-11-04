"""Viewing through chat must not require generation or grant business writes."""
import json

import pytest

from sdlc.accounts import Accounts, AccessError
from sdlc.agent import AssistantMessage, ToolCall
from sdlc.agent.composition import build_runtime
from sdlc.secure_service import Service
from sdlc.service import ServiceError
from sdlc.testing_agent import worker
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.service import TestingService
from tests.test_agent_chat_flow import ConversationModel, execute_next
from tests.test_testing_agent import ready, TEXT, analysis


@pytest.fixture
def observer(tmp_path, monkeypatch):
    monkeypatch.setenv('SDLC_AGENT_PROVIDER', 'google')
    monkeypatch.setenv('SDLC_AGENT_ASSISTANT_MODEL', 'test-assistant')
    monkeypatch.setenv('SDLC_AGENT_REVIEWER_MODEL', 'test-reviewer')
    monkeypatch.setenv('SDLC_AGENT_SESSION_DIR', str(tmp_path / 'sessions'))
    path = tmp_path / 'view-chat.sqlite3'
    accounts = Accounts(path)
    accounts.register('owner', 'Owner', 'view-chat-password')
    accounts.register('observer', 'Observer', 'view-chat-password')
    owner_token = accounts.login('owner', 'view-chat-password')
    observer_token = accounts.login('observer', 'view-chat-password')
    workspace = accounts.create_team(owner_token, 'Shared view chat')
    invite = accounts.create_invite(owner_token, workspace)
    request = accounts.request_join(observer_token, invite, confirm=True)
    accounts.decide_join(owner_token, workspace, request['request_id'], True, 'observer')
    project = Service(path, token=owner_token, workspace_id=workspace).create_project('Shared project', '')
    return (TestingService(path, observer_token, workspace, project),
            TestingService(path, owner_token, workspace, project))


def test_observer_can_start_chat_and_read_project_cases_without_generation(observer):
    viewer, owner = observer
    source = ready(owner)
    run_id = owner.finalize_design(source)
    for case in owner.legacy.get_run(run_id)['cases']:
        owner.legacy.review(run_id, case['case_id'], case['version'], case['revision'],
                            'approve', '', '符合需求', 'view-approve-' + case['case_id'])
    owner.create_baseline(source)
    initial = viewer.create_conversation('展示这个项目已批准的测试用例')
    with viewer.connection() as db:
        payload = json.loads(db.execute('SELECT payload FROM agent_work_items WHERE task_id=?', (initial,)).fetchone()[0])
    assert payload['capability'] == 'view'
    model = ConversationModel(AssistantMessage(content='这是项目已批准的用例。', tool_calls=[ToolCall(
        id='view-cases', name='list_test_cases', arguments={'scope': 'project', 'status': 'approved', 'display_only': True})]))
    execute_next(viewer, model, task_id=initial)
    assert any(message['kind'] == 'case_collection' for message in ChatService(viewer).list_messages(initial))
    current = owner.get_task(source)
    assert current['status'] == 'completed'
    assert viewer.get_task(initial)['input']['paragraphs'] == []
    exposed = {tool.name for tool in model.contexts[0].tools}
    assert {'list_test_cases', 'get_test_case'} <= exposed
    assert not {'analyze_requirements', 'generate_case_batch', 'propose_revision', 'request_platform_action'} & exposed
    assert all(e['details']['budget_scope'] == 'conversation' for e in viewer.get_task(initial)['events'] if e['action'] == 'model_call')


def test_view_chat_cannot_write_business_artifacts_or_change_review_state(observer):
    viewer, owner = observer
    task = ready(owner)
    viewer.execution_capability = 'view'
    with pytest.raises((ServiceError, AccessError)):
        viewer.create_task(TEXT)
    with pytest.raises((ServiceError, AccessError)):
        viewer.save_analysis(task, analysis())
    with pytest.raises((ServiceError, AccessError)):
        viewer.add_artifact(task, 'analysis', analysis())
    with pytest.raises((ServiceError, AccessError)):
        viewer.set_state(task, 'partial')
    with pytest.raises((ServiceError, AccessError)):
        worker.enqueue(viewer, task, 'start')
    with pytest.raises((ServiceError, AccessError)):
        worker.enqueue(viewer, task, 'resume')
    with pytest.raises(ServiceError):
        viewer.create_conversation('查询', owner_id=owner.user['id'])


def test_observer_existing_task_query_does_not_change_business_status(observer):
    viewer, owner = observer
    task = ready(owner)
    run = owner.finalize_design(task)
    before = owner.get_task(task)
    worker.enqueue(viewer, task, 'message', '当前有哪些用例？')
    model = ConversationModel(AssistantMessage(tool_calls=[ToolCall(id='read', name='list_test_cases',
        arguments={'scope': 'task', 'status': 'all', 'display_only': True})]))
    execute_next(viewer, model, task_id=task)
    after = owner.get_task(task)
    assert (after['status'], after['stage'], after['run_id']) == (before['status'], before['stage'], run)
    assert after['questions'] == before['questions']
    assert after['baselines'] == before['baselines']


def test_observer_may_not_confirm_another_users_generation_plan(observer):
    viewer, owner = observer
    task = owner.create_task(TEXT)
    confirmation = ChatService(owner).request_confirmation(task, kind='generation_plan')
    with pytest.raises(ServiceError, match='不能确认'):
        ChatService(viewer).respond(task, confirmation['id'], 'retry', expected_revision=confirmation['revision'])
    assert ChatService(owner).get_pending(task)['status'] == 'pending'


def test_view_model_failure_is_visible_without_blocking_other_work(observer):
    viewer, _ = observer
    task = viewer.create_conversation('你好')
    with viewer.connection('view', True) as db:
        db.execute("UPDATE agent_work_items SET status='completed' WHERE task_id=?", (task,))
    viewer.execution_capability = 'view'
    chat = ChatService(viewer)
    chat.request_confirmation(task, reason='provider unavailable', request_key='read-only-failure')
    assert chat.get_pending(task) is None
    assert any(message['state'] == 'error' and message['content'] for message in chat.list_messages(task))
    runtime = build_runtime(viewer.db_path, viewer.token, viewer.workspace_id, viewer.project_id,
                            task, model_client=ConversationModel(), capability='view')
    assert runtime.service.execution_capability == 'view'


def test_team_members_share_cases_but_not_agent_transcripts_or_core_sessions(observer):
    viewer, owner = observer
    task = ready(owner)
    private = owner.create_conversation('Owner private opening')
    with owner.connection('view', True) as db:
        db.execute("UPDATE agent_work_items SET status='completed' WHERE task_id=?", (private,))
    assert private not in {item['id'] for item in viewer.list_tasks()}
    with pytest.raises(ServiceError):
        viewer.get_task(private)
    owner_chat = ChatService(owner)
    owner_chat.post_user(task, 'Owner private audit request')
    owner_chat.handle_event(task, {'type': 'assistant_message', 'message_id': 'private-answer', 'content': 'Restricted owner-only tool result'})
    owner.add_artifact(task, 'model_attempt', {'actor_id': owner.user['id'], 'messages': ['Restricted owner-only tool result']}, 'private-attempt')
    runtime = build_runtime(owner.db_path, owner.token, owner.workspace_id, owner.project_id, task,
        model_client=ConversationModel(AssistantMessage(content='Owner response')))
    owner_session = runtime.run('Owner question', request_id='owner-isolated')
    assert not ChatService(viewer).list_messages(task)
    assert not any(a['kind'] == 'model_attempt' for a in viewer.get_task(task)['artifacts'])
    worker.enqueue(viewer, task, 'message', '当前用例有哪些？')
    client = ConversationModel(AssistantMessage(content='Viewer response'))
    execute_next(viewer, client, task_id=task)
    assert all('Restricted owner-only' not in str(c.messages) + str(c.memory) for c in client.contexts)
    assert all('Owner private' not in m['content'] for m in ChatService(viewer).list_messages(task))
    assert all(m['content'] != 'Viewer response' for m in owner_chat.list_messages(task))
    with viewer.connection() as db:
        mappings = db.execute('SELECT actor_id,session_id FROM testing_agent_user_sessions WHERE task_id=?', (task,)).fetchall()
    assert len(mappings) == 2 and len({row['session_id'] for row in mappings}) == 2
    other_runtime = build_runtime(viewer.db_path, viewer.token, viewer.workspace_id, viewer.project_id, task,
        model_client=ConversationModel(), capability='view')
    with pytest.raises(Exception):
        other_runtime.agent.get_session(owner_session.id)


def test_legacy_shared_session_is_attributed_to_its_recorded_actor(observer):
    viewer, owner = observer
    task = ready(owner)
    old_runtime = build_runtime(owner.db_path, owner.token, owner.workspace_id, owner.project_id, task,
                                model_client=ConversationModel())
    old_session = old_runtime.agent.new_session({'task_id': task, 'workspace_id': owner.workspace_id,
                                                 'project_id': owner.project_id})
    viewer.execution_capability = 'view'
    viewer.bind_session(task, old_session.id)
    # Previous releases stored only one task-level pointer, last writer wins.
    with owner.connection('view', True) as db:
        db.execute('UPDATE testing_agent_tasks SET session_id=? WHERE id=?', (old_session.id, task))
    current = build_runtime(owner.db_path, owner.token, owner.workspace_id, owner.project_id, task,
                           model_client=ConversationModel(AssistantMessage(content='新的账号隔离回复')))
    with pytest.raises(Exception):
        current.agent.get_session(old_session.id)
    new_session = current.run('请说明测试范围', request_id='new-owner-turn')
    assert new_session.id != old_session.id

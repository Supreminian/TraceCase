import time

import pytest

from sdlc.accounts import Accounts
from sdlc.secure_service import Service
from sdlc.service import ServiceError
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent.worker import claim, enqueue, execute, list_work


@pytest.fixture
def context(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    accounts = Accounts(path)
    accounts.register('chat-user', 'Chat user', 'disposable-password')
    token = accounts.login('chat-user', 'disposable-password')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('Chat test', '')
    service = TestingService(path, token, workspace, project)
    task = service.create_task('# PRD\n\n申请人提交报销单。')
    return service, ChatService(service), task


def test_public_stream_updates_one_durable_message_and_hides_internal_events(context):
    service, chat, task = context
    chat.handle_event(task, {'type': 'private_reasoning', 'text': 'never-store'})
    chat.handle_event(task, {'type': 'assistant_start', 'message_id': 'm1'}, 'turn')
    chat.handle_event(task, {'type': 'assistant_delta', 'message_id': 'm1', 'text': '你好，'}, 'turn')
    assert chat.list_messages(task)[0]['state'] == 'streaming'
    chat.handle_event(task, {'type': 'assistant_delta', 'message_id': 'm1', 'text': '我正在分析。'}, 'turn')
    chat.handle_event(task, {'type': 'assistant_message', 'message_id': 'm1', 'content': '你好，我正在分析。'}, 'turn')
    saved = ChatService(service).list_messages(task)
    assert len(saved) == 1 and saved[0]['content'] == '你好，我正在分析。'
    assert saved[0]['state'] == 'completed' and 'never-store' not in str(saved)


def test_tool_progress_never_echoes_arguments_or_raw_error(context):
    _, chat, task = context
    chat.handle_event(task, {'type': 'tool_started', 'tool': 'generate_case_batch', 'tool_call_id': 't', 'arguments': {'secret': 'do-not-store'}}, 'turn')
    chat.handle_event(task, {'type': 'tool_completed', 'tool': 'generate_case_batch', 'tool_call_id': 't', 'is_error': True, 'error': 'provider-secret'}, 'turn')
    messages = chat.list_messages(task)
    assert len(messages) == 2 and messages[-1]['state'] == 'error'
    assert 'do-not-store' not in str(messages) and 'provider-secret' not in str(messages)


def test_user_message_idempotency_and_cross_project_isolation(context):
    service, chat, task = context
    first = chat.post_user(task, '请覆盖金额边界', 'user-request')
    assert chat.post_user(task, '请覆盖金额边界', 'user-request')['id'] == first['id']
    with pytest.raises(ServiceError):
        chat.post_user(task, '不同内容', 'user-request')
    other = service.legacy.create_project('Other project', '')
    scoped = ChatService(TestingService(service.db_path, service.token, service.workspace_id, other))
    with pytest.raises(ServiceError):
        scoped.list_messages(task)
    with pytest.raises(ServiceError):
        scoped.request_confirmation(task)


def test_failure_requires_explicit_confirmation_and_retry_is_exactly_once(context):
    service, chat, task = context
    chat.handle_event(task, {'type': 'assistant_start', 'message_id': 'partial'}, 'turn')
    pending = chat.request_confirmation(task, 'secret upstream body', request_key='failed-job')
    assert pending['kind'] == 'retry_generation'
    assert 'secret upstream body' not in str(chat.list_messages(task))
    assert chat.list_messages(task)[0]['state'] == 'interrupted'
    assert service.get_task(task)['status'] == 'waiting_confirmation'
    for operation in ('resume', 'start'):
        with pytest.raises(ValueError, match='确认'):
            enqueue(service, task, operation)
    result = chat.respond(task, pending['id'], 'retry', expected_revision=1, request_key='confirm-retry')
    again = chat.respond(task, pending['id'], 'retry', expected_revision=1, request_key='confirm-retry')
    assert result == again and result['job_id']
    assert len(list_work(service, task)) == 1
    with pytest.raises(ServiceError, match='已处理'):
        chat.respond(task, pending['id'], 'retry', request_key='different')


def test_confirmations_use_cas_and_stop_never_enqueues(context):
    service, chat, task = context
    pending = chat.request_confirmation(task)
    with pytest.raises(ServiceError, match='已更新'):
        chat.respond(task, pending['id'], 'stop', expected_revision=2)
    with pytest.raises(ServiceError, match='补充'):
        chat.respond(task, pending['id'], 'refine', content='')
    stopped = chat.respond(task, pending['id'], 'stop', request_key='stop')
    assert stopped['job_id'] is None and list_work(service, task) == []
    assert chat.get_pending(task) is None and service.get_task(task)['status'] == 'partial'


def test_generation_plan_confirmation_preserves_plan_version(context):
    service, chat, task = context
    record = chat.handle_event(task, {'type': 'waiting_confirmation', 'kind': 'generation_plan', 'plan_hash': 'hash-of-plan', 'prompt': '将覆盖提交与5000元边界，是否继续？'}, 'plan-turn')
    assert record['plan_hash'] == 'hash-of-plan'
    assert chat.list_messages(task)[0]['state'] == 'completed'
    result = chat.respond(task, record['id'], 'retry', request_key='accept-plan')
    assert result['confirmation']['choice'] == 'retry'
    assert result['confirmation']['plan_hash'] == 'hash-of-plan'
    work = list_work(service, task)[0]
    assert work['operation'] == 'message'
    assert service.get_task(task)['status'] == 'queued'


def test_worker_persists_initial_turn_and_uses_public_callback(context):
    service, chat, task = context
    enqueue(service, task, 'start', request_key='initial')
    assert '申请人提交' in chat.list_messages(task)[0]['content']
    assert service.artifact(task, 'interaction_policy', 'chat-v1')['plan_confirmation_required']
    job = claim(service.db_path, 'worker')

    def factory(path, token, workspace, project, task_id, *, execution_guard, capability, public_event_sink):
        class Runtime:
            def run(self, *args, **kwargs):
                public_event_sink({'type': 'assistant_start', 'message_id': 'answer'})
                public_event_sink({'type': 'assistant_delta', 'message_id': 'answer', 'text': '已完成需求分析。'})
                public_event_sink({'type': 'assistant_message', 'message_id': 'answer', 'content': '已完成需求分析。'})
        return Runtime()

    execute(service.db_path, job, 'worker', runtime_factory=factory)
    assert list_work(service, task)[0]['status'] == 'completed'
    assert chat.list_messages(task)[-1]['content'] == '已完成需求分析。'


def test_worker_failure_sends_confirmation_instead_of_background_retry(context):
    service, chat, task = context
    enqueue(service, task, request_key='fail')
    job = claim(service.db_path, 'worker')

    def factory(*args, **kwargs):
        raise RuntimeError('token=do-not-publish')

    execute(service.db_path, job, 'worker', runtime_factory=factory)
    assert list_work(service, task)[0]['status'] == 'failed'
    assert chat.get_pending(task)['kind'] == 'retry_generation'
    assert 'do-not-publish' not in str(chat.list_messages(task))
    assert claim(service.db_path, 'another-worker') is None


def test_expired_worker_requires_confirmation_before_model_is_called(context):
    service, chat, task = context
    enqueue(service, task, request_key='crash')
    first = claim(service.db_path, 'worker-one')
    with service.accounts.db(write=True) as db:
        db.execute('UPDATE agent_work_items SET lease_until=? WHERE id=?', (time.time() - 1, first['id']))
    recovered = claim(service.db_path, 'worker-two')
    assert recovered['requires_confirmation']

    def forbidden_factory(*args, **kwargs):
        pytest.fail('a crashed generation must wait for a user decision')

    execute(service.db_path, recovered, 'worker-two', runtime_factory=forbidden_factory)
    assert chat.get_pending(task)


def test_refinement_creates_one_child_and_preserves_original_confirmation(context):
    service, chat, task = context
    pending = chat.request_confirmation(task, kind='generation_plan', plan_hash='old-plan')
    result = chat.respond(task, pending['id'], 'refine', '只覆盖提交操作，暂不覆盖审批', request_key='refine-once')
    repeated = chat.respond(task, pending['id'], 'refine', '只覆盖提交操作，暂不覆盖审批', request_key='refine-once')
    assert result == repeated and result['task_id'] != task
    child = service.get_task(result['task_id'])
    assert child['parent_task_id'] == task and child['status'] == 'queued'
    assert '只覆盖提交操作' in child['scope']
    assert len(list_work(service, child['id'])) == 1
    assert len(service.list_tasks()) == 2
    assert service.get_task(task)['status'] == 'partial'
    assert chat.list_confirmations(task)[0]['choice'] == 'refine'
    assert '只覆盖提交操作' in str(chat.list_messages(child['id']))


def test_queue_admission_failure_rolls_back_confirmation_and_user_reply(context):
    service, chat, task = context
    pending = chat.request_confirmation(task)
    # Fill the workspace's two admitted running/queued slots with other tasks.
    for index in range(2):
        other = service.create_task('# PRD\n\n另一个独立需求' + str(index))
        enqueue(service, other)
    with pytest.raises(ValueError, match='额度'):
        chat.respond(task, pending['id'], 'retry', request_key='full-queue')
    assert chat.get_pending(task)['revision'] == 1
    assert all(message['role'] != 'user' for message in chat.list_messages(task))


@pytest.mark.parametrize(('error', 'expected'), [
    ('HTTP 429 token=hidden', '限流'), ('upstream timeout token=hidden', '超时'),
    ('JSON schema failed token=hidden', '校验'), ('permission denied token=hidden', '权限'),
])
def test_failure_reasons_are_fixed_classification_without_reflecting_details(context, error, expected):
    _, chat, task = context
    confirmation = chat.handle_event(task, {'type': 'waiting_confirmation', 'error': error})
    assert expected in confirmation['reason']
    assert 'hidden' not in str(chat.list_messages(task))



def test_wait_event_reuses_public_assistant_prompt_instead_of_duplicate_bubble(context):
    _, chat, task = context
    chat.handle_event(task, {'type': 'assistant_message', 'message_id': 'prompt', 'content': '请确认方案'}, 'turn')
    chat.handle_event(task, {'type': 'waiting_confirmation', 'message_id': 'prompt', 'kind': 'generation_plan', 'prompt': '请确认方案'}, 'turn')
    messages = chat.list_messages(task)
    assert len(messages) == 1
    assert messages[0]['kind'] == 'confirmation' and '请确认方案' in messages[0]['content']


def test_missing_requirements_is_normal_wait_and_cannot_be_blindly_retried(context):
    service, chat, task = context
    confirmation = chat.request_confirmation(task, kind='requirements_input', prompt='请提供报销提交功能的 PRD。')
    assert confirmation['kind'] == 'requirements_input'
    assert confirmation['reason'] == '我还需要一些需求资料，才能设计有依据的测试用例。'
    assert 'retry' not in confirmation['choices']
    assert 'discuss' in confirmation['choices']
    assert chat.list_messages(task)[0]['state'] == 'completed'
    assert service.get_task(task)['error'] == ''
    with pytest.raises(ServiceError, match='需求资料'):
        chat.respond(task, confirmation['id'], 'retry', request_key='blind-retry')
    assert chat.get_pending(task) and list_work(service, task) == []


@pytest.mark.parametrize('kind', ['generation_plan', 'requirements_input', 'retry_generation'])
def test_natural_discussion_retains_raw_text_without_approving_or_creating_child(context, kind):
    service, chat, task = context
    confirmation = chat.request_confirmation(task, kind=kind, plan_hash='current-plan')
    raw_reply = '\n# 我的补充\n\n我想先了解风险，暂时不要生成。\n'
    result = chat.respond(task, confirmation['id'], 'discuss', raw_reply, request_key='natural-reply')
    repeated = chat.respond(task, confirmation['id'], 'discuss', raw_reply, request_key='natural-reply')
    assert result == repeated and result['task_id'] == task
    assert len(service.list_tasks()) == 1 and len(list_work(service, task)) == 1
    assert chat.list_messages(task)[-1]['content'] == raw_reply
    saved = chat.list_confirmations(task)[0]
    assert saved['choice'] == 'discuss' and saved['content'] == raw_reply
    assert saved['status'] == 'resolved'
    assert not any(c['choice'] == 'retry' for c in chat.list_confirmations(task))
    with service.accounts.db() as db:
        import json
        job = db.execute('SELECT operation,payload FROM agent_work_items WHERE id=?', (result['job_id'],)).fetchone()
        assert job['operation'] == 'message'
        assert json.loads(job['payload'])['content'] == raw_reply
        assert json.loads(job['payload'])['confirmation_id'] == confirmation['id']


def test_discussion_requires_content_and_archived_projects_keep_read_only_history(context):
    service, chat, task = context
    confirmation = chat.request_confirmation(task, kind='generation_plan')
    with pytest.raises(ServiceError, match='填写'):
        chat.respond(task, confirmation['id'], 'discuss', '  \n')
    before = chat.list_messages(task)
    with service.accounts.db(write=True) as db:
        db.execute("UPDATE projects SET status='archived' WHERE id=?", (service.project_id,))
    assert chat.list_messages(task) == before
    with pytest.raises(ValueError, match='归档'):
        enqueue(service, task, 'message', '不能修改归档项目')
    for mutation in (
        lambda: chat.post_user(task, '新说明'),
        lambda: chat.respond(task, confirmation['id'], 'stop'),
        lambda: chat.request_confirmation(task),
        lambda: chat.handle_event(task, {'type': 'assistant_delta', 'text': '新文本'}),
    ):
        with pytest.raises(ServiceError, match='归档'):
            mutation()
    assert chat.list_messages(task) == before


@pytest.mark.parametrize('status', ['failed', 'waiting_confirmation'])
def test_legacy_interrupted_task_gets_one_actionable_question_without_running_model(context, status):
    service, chat, task = context
    service.set_state(task, status, next_action='请在对话中确认下一步。')
    before = service.get_task(task)
    pending = chat.recover_waiting_confirmation(task)
    again = chat.recover_waiting_confirmation(task)
    assert pending == again and pending['kind'] == 'retry_generation'
    current = service.get_task(task)
    assert current['status'] == 'waiting_confirmation'
    assert current['input'] == before['input']
    assert len(chat.list_messages(task)) == 1
    assert list_work(service, task) == []


def test_recovery_never_interrupts_active_message_job(context):
    service, chat, task = context
    enqueue(service, task, 'message', '请说明当前状态')
    service.set_state(task, 'failed')
    assert chat.recover_waiting_confirmation(task) is None
    assert chat.get_pending(task) is None


@pytest.mark.parametrize('legacy', [False, True])
def test_plain_chat_message_reaches_queue_while_waiting_without_implicit_retry(context, legacy):
    service, chat, task = context
    if legacy:
        service.set_state(task, 'failed', error='旧工作进程中断')
    else:
        chat.request_confirmation(task, kind='generation_plan', prompt='先覆盖提交操作，可以继续吗？')
    raw = '继续'
    job_id = enqueue(service, task, 'message', raw, request_key='normal-chat-reply')
    assert enqueue(service, task, 'message', raw, request_key='normal-chat-reply') == job_id
    assert len(list_work(service, task)) == 1
    assert chat.get_pending(task) is None
    assert service.get_task(task)['status'] == 'queued'
    record = chat.list_confirmations(task)[0]
    assert record['choice'] == 'discuss' and record['content'] == raw
    assert chat.list_messages(task)[-1]['content'] == raw
    with pytest.raises(ValueError, match='不同'):
        enqueue(service, task, 'message', '另一个消息', request_key='normal-chat-reply')


@pytest.mark.parametrize('existing_prompt', [False, True])
def test_spent_legacy_budget_has_new_round_action_instead_of_dead_end_retry(context, existing_prompt, monkeypatch):
    service, chat, task = context
    monkeypatch.setenv('SDLC_AGENT_MAX_TOTAL_TOKENS', '100')
    service.record_event(task, {'type': 'model_call', 'usage': {'total_tokens': 120}})
    service.set_state(task, 'failed')
    old = chat.request_confirmation(task) if existing_prompt else None
    pending = chat.recover_waiting_confirmation(task)
    assert pending['kind'] == 'budget_exhausted'
    assert '新一轮' in pending['prompt']
    assert chat.recover_waiting_confirmation(task) == pending
    if old:
        assert pending['id'] == old['id'] and pending['revision'] == old['revision'] + 1
    response = chat.respond(task, pending['id'], 'retry', expected_revision=pending['revision'], request_key='new-budget-round')
    assert chat.respond(task, pending['id'], 'retry', expected_revision=pending['revision'], request_key='new-budget-round') == response
    child = service.get_task(response['task_id'])
    parent = service.get_task(task)
    assert child['id'] != task and child['parent_task_id'] == task
    assert child['input']['original_text'] == parent['input']['original_text']
    assert child['goal'] == parent['goal'] and child['scope'] == parent['scope']
    assert len(list_work(service, child['id'])) == 1 and list_work(service, task) == []
    assert [e['details']['usage']['total_tokens'] for e in parent['events'] if e['action'] == 'model_call'] == [120]
    assert not [e for e in child['events'] if e['action'] == 'model_call']
    assert any(a['kind'] == 'handoff' and a['content']['child_task_id'] == child['id'] for a in parent['artifacts'])


def test_budget_exhausted_natural_message_gets_visible_reply_without_extra_provider_call(context, monkeypatch):
    from sdlc.agent.composition import build_runtime
    from tests.test_agent_composition import ScriptedClient
    service, chat, task = context
    monkeypatch.setenv('SDLC_AGENT_MAX_TOTAL_TOKENS', '100')
    service.record_event(task, {'type': 'model_call', 'usage': {'total_tokens': 120}})
    service.set_state(task, 'failed')
    enqueue(service, task, 'message', '继续')
    client = ScriptedClient()

    def factory(path, token, workspace, project, task_id, **options):
        return build_runtime(path, token, workspace, project, task_id, model_client=client, **options)

    execute(service.db_path, claim(service.db_path, 'budget-worker'), 'budget-worker', runtime_factory=factory)
    assert not client.contexts
    pending = chat.get_pending(task)
    assert pending and pending['kind'] == 'budget_exhausted'
    assert any('预算上限' in m['content'] and '新一轮' in m['content'] for m in chat.list_messages(task) if m['role'] == 'assistant')
    assert len(service.list_tasks()) == 1


@pytest.mark.parametrize('has_pending', [False, True])
def test_archived_legacy_task_can_be_viewed_without_attempting_recovery_write(context, has_pending):
    service, chat, task = context
    service.set_state(task, 'failed')
    pending = chat.request_confirmation(task) if has_pending else None
    before = chat.list_messages(task)
    with service.accounts.db(write=True) as db:
        db.execute("UPDATE projects SET status='archived' WHERE id=?", (service.project_id,))
    assert chat.recover_waiting_confirmation(task) == pending
    assert chat.list_messages(task) == before


def test_observer_can_read_legacy_task_without_recovery_permission(context):
    service, _, _ = context
    accounts = service.accounts
    accounts.register('chat-observer', 'Observer', 'disposable-observer-password')
    token = accounts.login('chat-observer', 'disposable-observer-password')
    team = accounts.create_team(service.token, 'Read-only recovery')
    invite = accounts.create_invite(service.token, team)
    request = accounts.request_join(token, invite, confirm=True)
    accounts.decide_join(service.token, team, request['request_id'], True, 'observer')
    project = Service(service.db_path, token=service.token, workspace_id=team).create_project('Legacy read-only', '')
    owner = TestingService(service.db_path, service.token, team, project)
    task = owner.create_task('# PRD\n\n用户创建虚构记录。')
    owner.set_state(task, 'failed')
    observer = ChatService(TestingService(service.db_path, token, team, project))
    assert observer.recover_waiting_confirmation(task) is None
    assert observer.list_messages(task) == []
    pending = ChatService(owner).request_confirmation(task)
    assert observer.recover_waiting_confirmation(task) == pending


@pytest.mark.parametrize('choice', ['retry', 'refine', 'discuss', 'stop'])
def test_confirmation_cannot_race_the_worker_that_is_still_saving_its_reply(context, choice):
    service, chat, task = context
    enqueue(service, task, 'start')
    pending = chat.request_confirmation(task, kind='budget_exhausted')
    before = chat.list_messages(task)
    with pytest.raises(ServiceError, match='还在保存'):
        chat.respond(task, pending['id'], choice, '只覆盖提交' if choice in {'refine', 'discuss'} else '')
    assert chat.get_pending(task)['revision'] == pending['revision']
    assert len(service.list_tasks()) == 1
    assert chat.list_messages(task) == before


def test_new_budget_round_rolls_back_completely_when_queue_is_full(context):
    service, chat, task = context
    pending = chat.request_confirmation(task, kind='budget_exhausted')
    for index in range(2):
        other = service.create_task('# PRD\n\n独立需求' + str(index))
        enqueue(service, other)
    with pytest.raises(ValueError, match='额度'):
        chat.respond(task, pending['id'], 'retry', request_key='budget-full-queue')
    assert chat.get_pending(task)['revision'] == pending['revision']
    assert len(service.list_tasks()) == 3
    assert not [a for a in service.get_task(task)['artifacts'] if a['kind'] == 'handoff']

"""Requirement confirmation commits one complete, resumable conversation turn."""
import copy

import pytest

from sdlc.service import ServiceError
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.service import fingerprint
from tests.test_agent_ui import setup, show
from tests.test_testing_agent import TEXT, analysis


@pytest.fixture
def ctx(tmp_path):
    return setup(tmp_path / 'requirement-continuation.sqlite3'), None


def questions(service):
    task = service.create_task(TEXT)
    content = analysis(ambiguous=True)
    for qid, prompt in [('Q2', '审批超时如何处理？'), ('Q3', '是否需要展示审批历史？')]:
        question = copy.deepcopy(content['clarifications'][0])
        question.update(id=qid, question=prompt, blocking=False, rationale='不影响明确场景的测试设计。')
        content['clarifications'].append(question)
    service.save_analysis(task, content)
    return task


def test_confirm_answers_creates_one_child_and_queue_without_losing_pending_questions(ctx):
    service, _ = ctx
    task = questions(service)
    original = service.get_task(task)['input']
    service.submit_answer(task, 'Q1', '财务负责人审批。', expected_revision=1)
    service.submit_answer(task, 'Q2', '审批超时后保持待审批，由申请人提醒。', expected_revision=1)
    chat = ChatService(service)
    pending = chat.request_confirmation(task, reason='timeout')
    result = service.confirm_answers_and_continue(task, {'Q1': 2, 'Q2': 2}, '负责人确认上述补充。')
    child = service.get_task(result['task_id'])
    assert child['document_version'] == 2 and child['status'] == 'queued'
    assert '财务负责人审批。' in child['input']['normalized_text']
    assert '审批超时后保持待审批，由申请人提醒。' in child['input']['normalized_text']
    assert service.get_task(task)['input'] == original
    assert [(q['id'], q['status']) for q in child['questions']] == [('Q3', 'open')]
    assert chat.get_pending(task) is None
    resolved = next(c for c in chat.list_confirmations(task) if c['id'] == pending['id'])
    assert resolved['status'] == 'resolved' and resolved['followup_task_id'] == child['id']
    with service.connection() as db:
        jobs = list(db.execute('SELECT * FROM agent_work_items WHERE task_id=?', (child['id'],)))
    assert len(jobs) == 1 and jobs[0]['id'] == result['job_id']
    assert service.confirm_answers_and_continue(task, {'Q2': 2, 'Q1': 2}, '重复点击') == result
    assert len(service.list_tasks()) == 2
    assert all(q['status'] == 'confirmed' for q in service.get_task(task)['questions'][:2])


def test_confirmation_rejects_stale_answer_without_partial_mutation(ctx):
    service, _ = ctx
    task = questions(service)
    service.submit_answer(task, 'Q1', '财务负责人审批。', expected_revision=1)
    service.submit_answer(task, 'Q2', '提醒一次。', expected_revision=1)
    service.submit_answer(task, 'Q2', '提醒两次。', expected_revision=2)
    chat = ChatService(service)
    pending = chat.request_confirmation(task)
    with pytest.raises(ServiceError, match='旧答复未被确认'):
        service.confirm_answers_and_continue(task, {'Q1': 2, 'Q2': 2}, '确认')
    assert len(service.list_tasks()) == 1
    assert chat.get_pending(task)['id'] == pending['id']
    assert service.get_task(task)['questions'][0]['status'] == 'answered'
    assert service.get_task(task)['questions'][1]['answer']['text'] == '提醒两次。'


def test_queue_admission_failure_rolls_back_facts_child_and_confirmation(ctx, monkeypatch):
    from sdlc.testing_agent import worker
    service, _ = ctx
    task = questions(service)
    service.submit_answer(task, 'Q1', '财务负责人审批。', expected_revision=1)
    chat = ChatService(service)
    pending = chat.request_confirmation(task)
    def unavailable(*args, **kwargs):
        raise ValueError('队列已满')
    monkeypatch.setattr(worker, '_enqueue_in_db', unavailable)
    with pytest.raises(ValueError, match='队列已满'):
        service.confirm_answers_and_continue(task, {'Q1': 2}, '确认')
    assert len(service.list_tasks()) == 1
    assert service.get_task(task)['questions'][0]['status'] == 'answered'
    assert chat.get_pending(task)['id'] == pending['id']
    assert service.artifact(task, 'handoff', 'requirements-' + fingerprint(['Q1'])[:40]) is None


def test_submitted_unconfirmed_answers_are_carried_without_becoming_facts(ctx):
    service, _ = ctx
    task = questions(service)
    service.submit_answer(task, 'Q1', '财务负责人审批。', expected_revision=1)
    service.submit_answer(task, 'Q2', '超时提醒细节还需要协作者确认。', 'needs_confirmation', expected_revision=1)
    result = service.confirm_answers_and_continue(task, {'Q1': 2}, '确认审批人')
    child = service.get_task(result['task_id'])
    assert '超时提醒细节还需要协作者确认。' not in child['input']['normalized_text']
    pending = next(q for q in child['questions'] if q['id'] == 'Q2')
    assert pending['status'] == 'answered' and pending['revision'] == 2
    assert pending['answer']['disposition'] == 'needs_confirmation'


def test_old_single_question_confirmation_retains_other_answers_and_prior_facts(ctx):
    service, _ = ctx
    task = questions(service)
    service.submit_answer(task, 'Q1', '财务负责人审批。')
    service.submit_answer(task, 'Q2', '超时后保留待审批。')
    first = service.confirm_answer(task, 'Q1', '确认审批人', 2)
    assert next(q for q in service.get_task(first)['questions'] if q['id'] == 'Q2')['status'] == 'answered'
    second = service.confirm_answer(task, 'Q2', '确认超时行为', 2)
    text = service.get_task(second)['input']['normalized_text']
    assert '财务负责人审批。' in text and '超时后保留待审批。' in text


def test_chat_keeps_multiple_answers_in_current_conversation_until_one_confirmation(ctx):
    service, _ = ctx
    task = questions(service)
    app = show(service)
    app.text_area(key='chat_question_answer_' + task + 'Q1_1').input('财务负责人审批。')
    app.text_area(key='chat_question_answer_' + task + 'Q2_1').input('超时后保留待审批。')
    assert not any(b.label == '提交这项答复' for b in app.button)
    next(b for b in app.button if b.label == '确认全部答复并继续').click().run()
    assert not app.exception and not app.error
    assert len(service.list_tasks()) == 2
    child = service.list_tasks()[0]
    assert child['parent_task_id'] == task
    assert '财务负责人审批。' in child['input']['normalized_text']
    assert '超时后保留待审批。' in child['input']['normalized_text']
    assert [(q['id'], q['status']) for q in service.get_task(child['id'])['questions']] == [('Q3', 'open')]
    assert app.session_state['agent_task_' + service.project_id] == child['id']


@pytest.mark.parametrize('status', ['completed', 'cancelled', 'failed', 'waiting_review'])
def test_replaying_legacy_confirmation_preserves_existing_child_state(ctx, status):
    service, _ = ctx
    task = questions(service)
    service.submit_answer(task, 'Q1', '财务负责人审批。')
    child = service.confirm_answer(task, 'Q1', '旧版确认', 2)
    # Older releases stored a single answer instead of the new batch envelope.
    with service.connection('manage', True) as db:
        import json
        row = db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='confirmed_supplement'", (child,)).fetchone()
        old = json.loads(row['content_json'])
        old.pop('answers')
        old.pop('question_ids')
        db.execute("UPDATE testing_agent_artifacts SET content_json=? WHERE task_id=? AND kind='confirmed_supplement'", (json.dumps(old), child))
        db.execute('UPDATE testing_agent_tasks SET status=? WHERE id=?', (status, child))
    result = service.confirm_answers_and_continue(task, {'Q1': 2}, '重复确认旧版本')
    assert result['task_id'] == child and result['job_id'] is None and not result['enqueued']
    assert service.get_task(child)['status'] == status
    with service.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM agent_work_items').fetchone()[0] == 0
    assert service.confirm_answers_and_continue(task, {'Q1': 3}, '刷新后再次确认') == result


def test_reanalysis_does_not_overwrite_carried_answer_awaiting_fact_confirmation(ctx):
    service, _ = ctx
    task = questions(service)
    service.submit_answer(task, 'Q1', '财务负责人审批。')
    service.submit_answer(task, 'Q2', '暂定提醒一次，等待负责人确认。', 'needs_confirmation')
    child = service.confirm_answers_and_continue(task, {'Q1': 2}, '确认审批人')['task_id']
    before = next(q for q in service.get_task(child)['questions'] if q['id'] == 'Q2')
    updated = analysis(ambiguous=True)
    updated['clarifications'] = [copy.deepcopy(before['content'])]
    service.save_analysis(child, updated)
    after = next(q for q in service.get_task(child)['questions'] if q['id'] == 'Q2')
    assert after == before
    assert '暂定提醒一次' not in service.get_task(child)['input']['normalized_text']


@pytest.mark.parametrize('workspace', ['workspace', 'legacy_workspace'])
def test_confirmation_from_structured_requirements_switches_only_on_next_render(ctx, workspace):
    from streamlit.testing.v1 import AppTest
    service, _ = ctx
    task = questions(service)
    service.submit_answer(task, 'Q1', '财务负责人审批。')
    app = AppTest.from_string(f'''
from sdlc.secure_service import Service
from sdlc.testing_agent.ui import {workspace}
s = Service({service.db_path!r}, token={service.token!r}, workspace_id={service.workspace_id!r}, project_id={service.project_id!r})
{workspace}(s)
''', default_timeout=15).run()
    assert not app.exception
    app.text_input(key='confirm_reason_' + task + 'Q1').input('负责人确认这条需求补充。')
    app.button(key='confirm_' + task + 'Q1').click().run()
    assert not app.exception and not app.error
    child = service.list_tasks()[0]
    assert child['parent_task_id'] == task
    assert app.session_state['agent_task_' + service.project_id] == child['id']
    with service.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM agent_work_items WHERE task_id=?', (child['id'],)).fetchone()[0] == 1


def test_background_status_update_keeps_conversation_identity_for_next_chat_turn(ctx):
    import json
    service, _ = ctx
    task = service.create_task(TEXT, '解释失败并继续测试设计')
    chat = ChatService(service)
    chat.request_confirmation(task, reason='timeout')
    app = show(service)
    key = 'agent_task_' + service.project_id
    labels = app.selectbox(key=key).options
    app.chat_input(key='agent_chat_input_' + task).set_value('为什么失败？暂时不要重试。').run()
    assert not app.exception and not app.error
    # Simulate the worker finishing while only the live chat fragment refreshes.
    # The outer selector in the browser still has its pre-completion wire label.
    with service.connection('generate', True) as db:
        db.execute("UPDATE agent_work_items SET status='completed' WHERE task_id=?", (task,))
    service.set_state(task, 'partial', next_action='等待继续沟通。')
    chat.handle_event(task, {'type': 'assistant_message', 'message_id': 'background-reply',
        'content': '上一次请求未完成，你可以继续或调整范围。'})
    app.run()
    assert not app.exception and not app.error
    assert app.selectbox(key=key).options == labels
    app.chat_input(key='agent_chat_input_' + task).set_value('继续').run()
    assert not app.exception and not app.error
    assert app.selectbox(key=key).value == task
    assert app.selectbox(key=key).options == labels
    with service.connection() as db:
        work = list(db.execute("SELECT task_id,payload FROM agent_work_items WHERE status='queued'"))
    assert len(work) == 1 and work[0]['task_id'] == task
    assert json.loads(work[0]['payload'])['content'] == '继续'


@pytest.mark.parametrize('bad_selection', ['stale_label', 'foreign_task'])
def test_conversation_recovers_invalid_selector_and_deferred_navigation(ctx, bad_selection):
    from streamlit.testing.v1 import AppTest
    service, _ = ctx
    task = service.create_task(TEXT, '当前项目的稳定对话')
    foreign = service.legacy.create_project('其他项目', '')
    from sdlc.testing_agent.service import TestingService
    foreign_service = TestingService(service.db_path, service.token, service.workspace_id, foreign)
    foreign_task = foreign_service.create_task(TEXT)
    key = 'agent_task_' + service.project_id
    app = AppTest.from_string(f'''
import streamlit as st
from sdlc.secure_service import Service
from sdlc.testing_agent.ui import workspace
s = Service({service.db_path!r}, token={service.token!r}, workspace_id={service.workspace_id!r}, project_id={service.project_id!r})
if st.session_state.pop('_inject_stale_selection', False):
    st.session_state[{key!r}] = '当前项目的稳定对话 · 已收到，准备开始'
workspace(s)
''', default_timeout=15).run()
    assert not app.exception
    if bad_selection == 'stale_label':
        app.session_state['_inject_stale_selection'] = True
    else:
        app.session_state['_chat_next_task_' + service.project_id] = foreign_task
    app.run()
    assert not app.exception and not app.error
    assert app.selectbox(key=key).value == task
    assert app.session_state['_chat_selected_task_' + service.project_id] == task
    assert foreign_task not in app.selectbox(key=key).options

"""One human confirmation for the whole blocking set; no partial facts or retries."""
import copy
import json

import pytest

from sdlc.service import ServiceError
from sdlc.testing_agent import worker
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.clarifications import edit_question, save_bundle_draft, submit_bundle
from sdlc.testing_agent.service import TestingService
from tests.test_agent_ui import setup, show
from tests.test_testing_agent import TEXT, analysis, batch, plan


@pytest.fixture
def context(tmp_path):
    service = setup(tmp_path / 'clarification-bundle.sqlite3')
    task = service.create_task(TEXT)
    output = analysis(ambiguous=True)
    second = copy.deepcopy(output['clarifications'][0])
    second.update(id='Q2', question='二级审批超时如何处理？')
    output['clarifications'].append(second)
    service.save_analysis(task, output)
    return service, task


def test_answer_and_confirm_bundle_commits_once_with_all_facts(context):
    service, task = context
    answers = {'Q1': '由财务负责人审批。', 'Q2': '超时保持待审批。'}
    result = submit_bundle(service, task, answers, {'Q1': 1, 'Q2': 1}, '已确认全部业务规则。')
    child = service.get_task(result['task_id'])
    assert all(answer in child['input']['normalized_text'] for answer in answers.values())
    assert child['questions'] == []
    assert len(worker.list_work(service, child['id'])) == 1
    assert all(q['status'] == 'confirmed' and q['revision'] == 3 for q in service.get_task(task)['questions'])
    assert submit_bundle(service, task, answers, {'Q1': 1, 'Q2': 1}, '重复点击') == result
    assert len(service.list_tasks()) == 2
    assert len(worker.list_work(service, child['id'])) == 1


def test_missing_blocker_cannot_trigger_partial_continuation(context):
    service, task = context
    with pytest.raises(ServiceError, match='全部阻断性澄清'):
        submit_bundle(service, task, {'Q1': '财务负责人。'}, {'Q1': 1}, '确认')
    assert all(q['answer'] is None and q['revision'] == 1 for q in service.get_task(task)['questions'])
    assert len(service.list_tasks()) == 1


def test_stale_bundle_rolls_back_every_answer(context):
    service, task = context
    service.submit_answer(task, 'Q2', '更新后的答复', expected_revision=1)
    with pytest.raises(ServiceError, match='整组答复尚未保存'):
        submit_bundle(service, task, {'Q1': '财务负责人。', 'Q2': '过期答复'}, {'Q1': 1, 'Q2': 1}, '确认')
    questions = service.get_task(task)['questions']
    assert questions[0]['answer'] is None
    assert questions[1]['answer']['text'] == '更新后的答复'
    assert len(service.list_tasks()) == 1


def test_queue_failure_rolls_back_submitted_answers_and_confirmation(context, monkeypatch):
    service, task = context
    def full(*args, **kwargs):
        raise ValueError('队列已满')
    monkeypatch.setattr(worker, '_enqueue_in_db', full)
    with pytest.raises(ValueError, match='队列已满'):
        submit_bundle(service, task, {'Q1': '财务负责人。', 'Q2': '保持待审批。'}, {'Q1': 1, 'Q2': 1}, '确认')
    assert all(q['answer'] is None and q['status'] == 'open' for q in service.get_task(task)['questions'])
    assert len(service.list_tasks()) == 1
    assert ChatService(service).list_messages(task) == []


def test_agent_answer_drafts_wait_for_one_human_confirmation(context):
    service, task = context
    saved = save_bundle_draft(service, task, {'Q1': '财务负责人。'}, {'Q1': 1})
    assert not saved['enqueued'] and not saved['confirmed']
    assert service.get_task(task)['questions'][0]['status'] == 'answered'
    assert len(service.list_tasks()) == 1
    saved = save_bundle_draft(service, task, {'Q2': '保持待审批。'}, {'Q2': 1})
    assert not saved['confirmed']
    result = submit_bundle(service, task, {'Q1': '财务负责人。', 'Q2': '保持待审批。'},
                           {'Q1': 2, 'Q2': 2}, '确认')
    assert result['enqueued'] and len(service.list_tasks()) == 2


def test_edit_question_invalidates_old_answer_and_requires_current_revision(context):
    service, task = context
    service.submit_answer(task, 'Q1', '旧审批人', expected_revision=1)
    edited = edit_question(service, task, 'Q1', '二级审批负责人及代理人是谁？', True, '澄清代理角色', 2)
    assert edited['revision'] == 3 and edited['status'] == 'open'
    assert edited['answer'] is None and edited['confirmed'] is None
    event = next(e for e in service.get_task(task)['events'] if e['action'] == 'question_edited')
    assert event['details']['previous']['answer']['text'] == '旧审批人'
    with pytest.raises(ServiceError, match='问题已更新'):
        edit_question(service, task, 'Q1', '过时修改', False, '过时请求', 2)
    assert service.get_task(task)['questions'][0]['content']['blocking'] is True


def test_edit_does_not_rewrite_confirmed_fact_or_another_project(context):
    service, task = context
    other_project = service.legacy.create_project('其他项目', '')
    other = TestingService(service.db_path, service.token, service.workspace_id, other_project)
    with pytest.raises(ServiceError, match='无权访问'):
        edit_question(other, task, 'Q1', '越界问题', False, '越界修改', 1)
    service.submit_answer(task, 'Q1', '财务负责人。', expected_revision=1)
    service.confirm_answer(task, 'Q1', '确认', expected_revision=2)
    with pytest.raises(ServiceError, match='已处理'):
        edit_question(service, task, 'Q1', '改变已确认事实', False, '修改', 3)


def test_edit_question_reversions_current_case_without_overwriting_approval(context):
    service, task = context
    for question in service.get_task(task)['questions']:
        edit_question(service, task, question['id'], question['content']['question'], False, '明确场景可执行，细节待补充', 1)
    service.save_plan(task, plan())
    service.save_batch(task, 'submit', batch())
    service.save_batch(task, 'approve', batch(2))
    run = service.finalize_design(task)
    first = service.legacy.get_run(run)['cases'][0]
    service.legacy.review(run, first['case_id'], first['version'], first['revision'], 'approve', '', '符合需求', 'approve-before-edit')
    before = service.legacy.get_run(run)
    edited = edit_question(service, task, 'Q1', '申请提交所需审批人是谁？', True, '澄清关联范围', 2,
                          requirement_ids=['R1'], paragraph_ids=['P002'], scope='case', affected_case_ids=['TC1'])
    assert edited['content']['affected_case_ids'] == ['TC1']
    after = service.legacy.get_run(run)
    case = next(c for c in after['cases'] if c['case_id'] == first['case_id'])
    assert case['version'] == first['version'] + 1 and case['status'] == 'pending_review'
    assert case['content']['readiness'] == 'needs_clarification'
    with service.connection() as db:
        old = json.loads(db.execute('SELECT content_json FROM case_versions WHERE run_id=? AND case_id=? AND version=?',
                                   (run, first['case_id'], first['version'])).fetchone()[0])
        assert old == first['content']
        assert db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND action='approve'", (run,)).fetchone()[0] == 1
    assert after['revision'] > before['revision']


def test_ui_single_confirmation_includes_all_blockers(context):
    service, task = context
    app = show(service)
    assert len([b for b in app.button if b.label == '确认全部答复并继续']) == 1
    assert not any(b.label == '提交这项答复' for b in app.button)
    app.text_area(key='chat_question_answer_' + task + 'Q1_1').input('财务负责人。')
    next(b for b in app.button if b.label == '确认全部答复并继续').click().run()
    assert app.error and len(service.list_tasks()) == 1
    app.text_area(key='chat_question_answer_' + task + 'Q2_1').input('保持待审批。')
    next(b for b in app.button if b.label == '确认全部答复并继续').click().run()
    assert not app.exception and not app.error
    child = next(t for t in service.list_tasks() if t['parent_task_id'] == task)
    assert len(worker.list_work(service, child['id'])) == 1

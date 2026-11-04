"""Authenticated, versioned clarification editing and one-shot answer bundles.

These functions are human action boundaries. Agent tools may prepare proposals,
but only the UI's authenticated confirmation invokes them.
"""
from __future__ import annotations

import json

from sdlc.domain import Clarification
from sdlc.service import ServiceError, dumps, now


def pending_questions(service, task_id):
    with service.connection() as db:
        service._task(db, task_id)
        return [service._question(row) for row in db.execute(
            "SELECT * FROM testing_agent_questions WHERE task_id=? AND status IN ('open','answered') ORDER BY id",
            (task_id,))]


def _check_idle(service, db, task_id):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_work_items'").fetchone():
        if db.execute("SELECT 1 FROM agent_work_items WHERE task_id=? AND status IN ('queued','running')",
                      (task_id,)).fetchone():
            raise ServiceError('Agent 正在处理该任务，请等待当前回复后再更改澄清。')


def _save_bundle_answers(service, db, task_id, answers, question_revisions):
    """Validate the entire submitted snapshot before changing any answer."""
    task = service._task(db, task_id)
    service._active(task)
    if (not isinstance(answers, dict) or not answers or not isinstance(question_revisions, dict)
            or set(answers) != set(question_revisions)):
        raise ServiceError('答复与问题版本不一致，请刷新后检查。')
    rows = {}
    for qid, text in answers.items():
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
            raise ServiceError('请填写本次提交中每项问题的答复，每项最多 4,000 字符。')
        revision = question_revisions[qid]
        if type(revision) is not int or revision < 1:
            raise ServiceError('问题版本无效，请刷新后检查。')
        row = db.execute('SELECT * FROM testing_agent_questions WHERE task_id=? AND id=?',
                         (task_id, qid)).fetchone()
        if not row or row['status'] not in {'open', 'answered'} or row['revision'] != revision:
            raise ServiceError('问题或答复已更新，整组答复尚未保存；请核对最新内容。')
        rows[qid] = row
    updated = {}
    for qid, text in answers.items():
        answer = {'text': text.strip(), 'disposition': 'answer', 'actor_id': service.user['id'],
                  'created_at': now(), 'assignee_id': None}
        db.execute("UPDATE testing_agent_questions SET answer_json=?,confirmed_json=NULL,status='answered',revision=revision+1 WHERE task_id=? AND id=?",
                   (dumps(answer), task_id, qid))
        updated[qid] = rows[qid]['revision'] + 1
        service._event(db, task_id, 'answer_submitted', {'question_id': qid, 'disposition': 'answer',
                                                       'source': 'clarification_bundle'})
    return updated


def submit_bundle(service, task_id, answers, question_revisions, reason, *, confirm=True):
    """Confirm all unresolved blockers together, or save the bundle for a confirmer.

No model job is enqueued until a permitted human confirms the entire blocking set.
The underlying continuation commits answers, facts, handoff and one job atomically.
"""
    if confirm:
        return service.confirm_answers_and_continue(task_id, question_revisions, reason,
            answers=answers, require_all_blocking=True)
    from sdlc.testing_agent.chat import ChatService
    chat = ChatService(service)
    with service.connection('generate', True) as db:
        service._task(db, task_id)
        _check_idle(service, db, task_id)
        blockers = {row['id'] for row in db.execute(
            "SELECT id,content_json FROM testing_agent_questions WHERE task_id=? AND status IN ('open','answered')",
            (task_id,)) if json.loads(row['content_json'])['blocking']}
        if not isinstance(answers, dict) or not blockers <= set(answers):
            raise ServiceError('请一并填写全部阻断性澄清，再统一提交。')
        revisions = _save_bundle_answers(service, db, task_id, answers, question_revisions)
        chat._post_user_db(db, task_id, '已统一提交澄清答复，等待需求确认人审核。\n\n'
                          + '\n\n'.join(qid + '：' + answer.strip() for qid, answer in answers.items()), None)
        service._event(db, task_id, 'clarification_bundle_submitted', {'question_ids': sorted(answers),
                                                                    'confirmed': False})
        return {'task_id': task_id, 'question_revisions': revisions, 'enqueued': False}


def save_bundle_draft(service, task_id, answers, question_revisions):
    """Save model-organized answer drafts for human review, never confirming facts.

    Partial drafts are allowed during conversation; the final human submit still
    requires every unresolved blocking question in the same version snapshot.
    """
    with service.connection('generate', True) as db:
        revisions = _save_bundle_answers(service, db, task_id, answers, question_revisions)
        service._event(db, task_id, 'clarification_bundle_drafted', {'question_ids': sorted(answers),
                                                                  'confirmed': False})
        return {'task_id': task_id, 'question_revisions': revisions, 'confirmed': False, 'enqueued': False}


def edit_question(service, task_id, question_id, question_text, blocking, reason, expected_revision, *,
                  paragraph_ids=None, requirement_ids=None, affected_case_ids=None, scope=None):
    """Edit an active clarification; clear old answers and reversion affected cases."""
    if (not isinstance(question_text, str) or not 1 <= len(question_text.strip()) <= 4000
            or type(blocking) is not bool or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000
            or type(expected_revision) is not int or expected_revision < 1):
        raise ServiceError('请提供问题内容、阻断标记、修改理由和当前版本。')
    with service.connection('manage', True) as db:
        task = service._task(db, task_id)
        service._active(task)
        _check_idle(service, db, task_id)
        if task['confirmer_id'] and task['confirmer_id'] != service.user['id']:
            raise ServiceError('仅本任务指定的需求确认人可以修改该澄清。')
        row = db.execute('SELECT * FROM testing_agent_questions WHERE task_id=? AND id=?',
                         (task_id, question_id)).fetchone()
        if not row or row['revision'] != expected_revision:
            raise ServiceError('问题已更新或不存在，请刷新后检查。')
        if row['status'] not in {'open', 'answered'}:
            raise ServiceError('该澄清已处理；请在当前需求版本提出新澄清，原确认记录保持不变。')
        previous = service._question(row)
        changes = {key: value for key, value in {'paragraph_ids': paragraph_ids, 'requirement_ids': requirement_ids,
                    'affected_case_ids': affected_case_ids, 'scope': scope}.items() if value is not None}
        content = Clarification.model_validate({**previous['content'], **changes,
                    'question': question_text.strip(), 'blocking': blocking, 'rationale': reason.strip()}).model_dump()
        known_paragraphs = {p['id'] for p in task['input']['paragraphs']}
        analysis_row = db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='analysis' AND artifact_key='default' AND input_hash=?",
                                  (task_id, task['input_hash'])).fetchone()
        known_rules = {r['id'] for r in json.loads(analysis_row['content_json'])['business_rules']} if analysis_row else set()
        known_cases = {c['case_id'] for c in service.legacy._cases(db, task['run_id'])} if task['run_id'] else set()
        for draft_row in db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='case_batch' AND input_hash=?",
                                    (task_id, task['input_hash'])):
            known_cases.update(d['case']['case_id'] for d in json.loads(draft_row['content_json'])['cases'])
        if (not set(content['paragraph_ids']) <= known_paragraphs or not set(content['requirement_ids']) <= known_rules
                or not set(content['affected_case_ids']) <= known_cases
                or (content['scope'] == 'case' and not content['affected_case_ids'])):
            raise ServiceError('澄清依据、业务规则或影响用例不属于当前任务，或局部范围缺少影响用例。')
        if content == previous['content']:
            return previous
        db.execute("UPDATE testing_agent_questions SET content_json=?,answer_json=NULL,confirmed_json=NULL,status='open',revision=revision+1 WHERE task_id=? AND id=?",
                   (dumps(content), task_id, question_id))
        if task['run_id']:
            run = service.legacy._run(db, task['run_id'])
            if run['project_id'] != service.project_id:
                raise ServiceError('关联运行不属于当前项目。')
            stored = db.execute('SELECT * FROM clarifications WHERE run_id=? AND id=?',
                                (task['run_id'], question_id)).fetchone()
            if stored:
                db.execute("UPDATE clarifications SET content_json=?,status='open',resolution_json=NULL,revision=revision+1 WHERE run_id=? AND id=?",
                           (dumps(content), task['run_id'], question_id))
                affected = {c['case_id'] for c in service.legacy._cases(db, task['run_id'])
                            if any(q['scope'] == 'run' or c['case_id'] in q['affected_case_ids']
                                   or set(c['content']['requirement_ids']) & set(q['requirement_ids'])
                                   for q in (previous['content'], content))}
                service.legacy._reversion_questions(db, task['run_id'], service.user['id'], reason.strip(), affected)
                service.legacy._event(db, task['run_id'], 'clarification_edited', service.user['id'], reason.strip(),
                    details={'clarification_id': question_id, 'previous': previous['content'], 'content': content})
        service._event(db, task_id, 'question_edited', {'question_id': question_id, 'previous': previous,
            'content': content, 'reason': reason.strip(), 'previous_revision': expected_revision,
            'revision': expected_revision + 1, 'previous_answer_invalidated': previous['answer'] is not None})
        db.execute('UPDATE testing_agent_tasks SET revision=revision+1,updated_at=? WHERE id=?', (now(), task_id))
        return service._question(db.execute('SELECT * FROM testing_agent_questions WHERE task_id=? AND id=?',
                                           (task_id, question_id)).fetchone())

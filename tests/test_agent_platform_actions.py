"""Conversation mutations require a human decision over current authorized data."""
import copy
import json

import pytest

from sdlc.accounts import Accounts
from sdlc.secure_service import Service
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent import platform_actions as actions
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent import worker
from tests.test_testing_agent import ready, TEXT, analysis


@pytest.fixture
def context(tmp_path):
    path = tmp_path / 'actions.sqlite3'
    accounts = Accounts(path)
    accounts.register('alice', 'Alice', 'platform-actions-password')
    token = accounts.login('alice', 'platform-actions-password')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('Actions', '')
    service = TestingService(path, token, workspace, project)
    task = ready(service)
    run = service.finalize_design(task)
    return service, accounts, task, run


def proposal(context, action, args):
    service, _, task, _ = context
    return actions.propose_action(service, task, action, args, '模型伪造的操作说明')


def confirm(context, record):
    service, _, task, _ = context
    return actions.confirm_action(service, task, record['id'], record['revision'])


def test_proposal_does_not_mutate_and_confirmation_is_idempotent(context):
    service, _, task, run = context
    record = proposal(context, 'case_review', {'run_id': run, 'case_id': 'TC1', 'decision': 'approve', 'reason': ''})
    assert '模型伪造' not in record['summary']
    assert record['snapshot']['version'] == 1
    assert service.legacy.get_run(run)['cases'][0]['status'] == 'pending_review'
    message = next(m for m in ChatService(service).list_messages(task) if m['kind'] == 'platform_action')
    assert json.loads(message['content']) == {'action_id': record['id']}
    again = proposal(context, 'case_review', record['arguments'])
    assert again['id'] == record['id'] and len(actions.list_actions(service, task)) == 1
    result = confirm(context, record)
    assert result['status'] == 'completed'
    assert confirm(context, record) == result
    case = service.legacy.get_run(run)['cases'][0]
    assert case['status'] == 'approved' and case['revision'] == 2
    assert len([e for e in service.legacy.get_run(run)['events'] if e['action'] == 'approve']) == 1


def test_stale_case_confirmation_does_not_apply_to_new_version(context):
    service, _, task, run = context
    record = proposal(context, 'case_review', {'run_id': run, 'case_id': 'TC1', 'decision': 'approve', 'reason': ''})
    case = service.legacy.get_run(run)['cases'][0]
    content = copy.deepcopy(case['content'])
    content['title'] = '修改后的用例'
    service.legacy.edit(run, 'TC1', 1, 1, content, '', '人工先修改', 'outside-action')
    with pytest.raises(ValueError, match='已变化'):
        confirm(context, record)
    assert service.legacy.get_run(run)['cases'][0]['status'] == 'pending_review'
    assert actions.get_action(service, task, record['id'])['status'] == 'pending'


def test_case_edit_preserves_old_version_and_approval(context):
    service, _, _, run = context
    service.legacy.review(run, 'TC1', 1, 1, 'approve', '', '人工确认通过', 'prior-approval')
    content = copy.deepcopy(service.legacy.get_run(run)['cases'][0]['content'])
    content['title'] = '更明确的提交场景'
    record = proposal(context, 'case_edit', {'run_id': run, 'case_id': 'TC1', 'content': content, 'reason': '改善可读性'})
    result = confirm(context, record)
    assert result['result']['version'] == 2
    case = service.legacy.get_run(run)['cases'][0]
    assert case['status'] == 'pending_review'
    with service.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM case_versions WHERE run_id=? AND case_id=?', (run, 'TC1')).fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND case_id=? AND action='approve' AND version=1", (run, 'TC1')).fetchone()[0] == 1


def test_project_creation_receipt_and_effect_share_transaction(context, monkeypatch):
    service, _, task, _ = context
    record = proposal(context, 'project_create', {'name': 'New confirmed project', 'description': 'Only after confirm'})
    original = service._event
    def fail_receipt(db, task_id, action, details):
        if action == 'platform_action_confirmed':
            raise RuntimeError('simulated receipt failure')
        return original(db, task_id, action, details)
    monkeypatch.setattr(service, '_event', fail_receipt)
    with pytest.raises(RuntimeError, match='receipt'):
        confirm(context, record)
    assert len(service.legacy.list_projects()) == 1
    assert actions.get_action(service, task, record['id'])['status'] == 'pending'
    monkeypatch.setattr(service, '_event', original)
    completed = confirm(context, record)
    assert confirm(context, record) == completed
    assert len(service.legacy.list_projects()) == 2


def test_cancelled_action_never_executes(context):
    service, _, task, _ = context
    record = proposal(context, 'project_create', {'name': 'Cancelled', 'description': ''})
    assert actions.cancel_action(service, task, record['id'])['status'] == 'cancelled'
    with pytest.raises(ValueError, match='已处理'):
        confirm(context, record)
    assert len(service.legacy.list_projects()) == 1


def test_cross_user_project_and_action_ids_cannot_escape_workspace(context):
    service, accounts, task, run = context
    record = proposal(context, 'case_review', {'run_id': run, 'case_id': 'TC1', 'decision': 'approve', 'reason': ''})
    accounts.register('bob', 'Bob', 'platform-actions-password')
    token = accounts.login('bob', 'platform-actions-password')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(service.db_path, token=token, workspace_id=workspace).create_project('Bob project', '')
    other = TestingService(service.db_path, token, workspace, project)
    other_task = other.create_task(TEXT)
    with pytest.raises(ValueError):
        actions.propose_action(other, other_task, 'case_review', record['arguments'])
    with pytest.raises(ValueError):
        actions.confirm_action(other, task, record['id'])
    with pytest.raises(ValueError):
        actions.get_action(other, other_task, record['id'])
    assert service.legacy.get_run(run)['cases'][0]['status'] == 'pending_review'


def test_action_confirm_rechecks_revoked_capability_and_actor(context):
    service, accounts, _, _ = context
    accounts.register('reviewer', 'Reviewer', 'platform-actions-password')
    reviewer_token = accounts.login('reviewer', 'platform-actions-password')
    team = accounts.create_team(service.token, 'Team')
    invite = accounts.create_invite(service.token, team)
    request = accounts.request_join(reviewer_token, invite, confirm=True)
    accounts.decide_join(service.token, team, request['request_id'], True, 'reviewer')
    project = Service(service.db_path, token=service.token, workspace_id=team).create_project('Team project', '')
    owner = TestingService(service.db_path, service.token, team, project)
    task = ready(owner)
    run = owner.finalize_design(task)
    reviewer = TestingService(service.db_path, reviewer_token, team, project)
    record = actions.propose_action(reviewer, task, 'case_review', {'run_id': run, 'case_id': 'TC1', 'decision': 'approve', 'reason': ''})
    with pytest.raises(ValueError):
        actions.confirm_action(owner, task, record['id'])
    reviewer_id = accounts.me(reviewer_token)['id']
    accounts.set_member(service.token, team, reviewer_id, 'observer')
    with pytest.raises(ValueError):
        actions.confirm_action(reviewer, task, record['id'])
    assert owner.legacy.get_run(run)['cases'][0]['status'] == 'pending_review'


def test_approved_export_never_bypasses_gate_and_is_repeatable(context):
    service, _, task, run = context
    record = proposal(context, 'approved_export', {'run_id': run, 'format': 'csv'})
    with pytest.raises(ValueError, match='批准'):
        confirm(context, record)
    assert not service.legacy.list_exports()
    for case in service.legacy.get_run(run)['cases']:
        service.legacy.review(run, case['case_id'], case['version'], case['revision'], 'approve', '', '人工确认通过', 'approve-' + case['case_id'])
    fresh = proposal(context, 'approved_export', record['arguments'])
    result = confirm(context, fresh)
    assert service.get_task(task)['status'] == 'completed'
    assert confirm(context, fresh) == result
    assert len(service.legacy.list_exports()) == 1
    assert result['result']['filename'].endswith('.csv')


def test_regenerate_creates_one_targeted_job_without_changing_approved_case(context):
    service, _, task, run = context
    service.legacy.review(run, 'TC1', 1, 1, 'approve', '', '人工确认通过', 'approve-first')
    record = proposal(context, 'case_regenerate', {'run_id': run, 'case_id': 'TC1', 'reason': '请把步骤拆分得更清晰'})
    result = confirm(context, record)['result']
    assert result['target_task_id'] == task
    assert service.legacy.get_run(run)['cases'][0]['status'] == 'approved'
    assert len(worker.list_work(service, task)) == 1
    assert confirm(context, record)['result'] == result
    with service.connection() as db:
        payload = json.loads(db.execute('SELECT payload FROM agent_work_items WHERE id=?', (result['job_id'],)).fetchone()[0])
    assert payload['platform_action_id'] == record['id']
    assert payload['regeneration_case_id'] == 'TC1'
    feedback = next(p for p in service.get_task(task)['proposals'] if p['id'] == result['feedback_proposal_id'])
    assert feedback['source_version'] == 1 and feedback['source_revision'] == 2


def test_regenerate_completed_source_creates_unapproved_linked_round(context):
    service, _, task, run = context
    for case in service.legacy.get_run(run)['cases']:
        service.legacy.review(run, case['case_id'], case['version'], case['revision'], 'approve', '', '人工确认通过', 'approve-' + case['case_id'])
    service.create_baseline(task)
    original = service.legacy.get_run(run)['cases']
    record = proposal(context, 'case_regenerate', {'run_id': run, 'case_id': 'TC1', 'reason': '优化步骤'})
    result = confirm(context, record)['result']
    assert result['target_task_id'] != task
    child = service.get_task(result['target_task_id'])
    assert child['parent_task_id'] == task
    assert service.get_task(task)['status'] == 'completed'
    assert service.legacy.get_run(run)['cases'] == original
    assert all(c['status'] == 'pending_review' for c in service.legacy.get_run(child['run_id'])['cases'])
    assert len(worker.list_work(service, child['id'])) == 1


def test_clarification_edit_requires_reviewed_revision(context):
    service, _, _, _ = context
    task = service.create_task(TEXT)
    service.save_analysis(task, analysis(ambiguous=True))
    record = actions.propose_action(service, task, 'clarification_edit', {'task_id': task, 'question_id': 'Q1',
        'question_text': '请确认二级审批角色', 'blocking': False, 'reason': '明确问题范围'})
    actions.confirm_action(service, task, record['id'])
    question = service.get_task(task)['questions'][0]
    assert question['content']['question'] == '请确认二级审批角色'
    assert question['content']['blocking'] is False


def test_resource_actions_keep_immutable_versions(context):
    service, _, task, _ = context
    created = confirm(context, proposal(context, 'resource_create', {'kind': 'test_data', 'title': '虚构数据', 'body': '费用100元'}))['result']
    update = proposal(context, 'resource_update', {'resource_id': created['resource_id'], 'title': '更新数据', 'body': '费用200元'})
    assert confirm(context, update)['result']['revision'] == 2
    with service.connection() as db:
        assert db.execute('SELECT body FROM testing_shared_resource_versions WHERE resource_id=? AND revision=1',
            (created['resource_id'],)).fetchone()[0] == '费用100元'
    archive = proposal(context, 'resource_archive', {'resource_id': created['resource_id'], 'archived': True})
    assert confirm(context, archive)['result']['archived']


def test_join_team_proposal_uses_invite_and_does_not_grant_membership(context):
    service, accounts, task, _ = context
    accounts.register('team-host', 'Host', 'platform-actions-password')
    host = accounts.login('team-host', 'platform-actions-password')
    team = accounts.create_team(host, 'Invitation target')
    code = accounts.create_invite(host, team)
    record = proposal(context, 'team_join_request', {'code': code, 'note': '申请协作'})
    assert all(space['id'] != team for space in accounts.spaces(service.token))
    result = confirm(context, record)['result']
    assert result['request_id'] and result['pending']
    assert confirm(context, record)['result'] == result
    assert all(space['id'] != team for space in accounts.spaces(service.token))

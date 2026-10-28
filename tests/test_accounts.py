"""Authorization tests use disposable accounts and the actual authenticated application boundary."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import sqlite3
from pathlib import Path
import time

import pytest
from starlette.testclient import TestClient

from sdlc.accounts import Accounts, AccessError, digest
from sdlc.secure_service import Service
from sdlc.service import Service as Engine, ServiceError
from sdlc import jobs
from tests.fixture_provider import FixtureProvider
from tests.test_live_generation import Model

PASSWORD = 'disposable-password-123'


@pytest.fixture
def context(tmp_path):
    path = tmp_path / 'accounts.sqlite3'
    store = Accounts(path)
    store.register('alice', 'Alice', PASSWORD)
    store.register('bob', 'Bob', PASSWORD)
    a, b = store.login('alice', PASSWORD), store.login('bob', PASSWORD)
    wa, wb = store.spaces(a)[0]['id'], store.spaces(b)[0]['id']
    return path, store, a, b, wa, wb


def team_member(context, role='reviewer'):
    path, store, a, b, _, _ = context
    team = store.create_team(a, '共享团队')
    code = store.create_invite(a, team)
    request = store.request_join(b, code, confirm=True)
    store.decide_join(a, team, request['request_id'], True, role)
    return team


def seeded(context, team=None):
    path, _, a, _, wa, _ = context
    service = Service(path, token=a, workspace_id=team or wa)
    pid = service.create_project('测试项目', '', 'spoofed-user')
    service = Service(path, token=a, workspace_id=team or wa, project_id=pid)
    text = Path('tests/fixtures/generation/normal.md').read_text()
    run = service.generate_from_prd(text=text, operator='spoofed-user', provider=FixtureProvider())
    return service, run


def test_password_sessions_and_legacy_quarantine(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    legacy = Engine(path)
    project = legacy.create_project('历史', '', 'legacy-operator')
    store = Accounts(path)
    user = store.register('alice', 'Alice', PASSWORD)
    token = store.login('alice', PASSWORD)
    space = store.spaces(token)[0]['id']
    service = Service(path, token=token, workspace_id=space)
    assert service.list_projects() == []
    with pytest.raises(ServiceError):
        service.get_project(project)
    with store.db() as db:
        hashed = db.execute('SELECT password_hash FROM users WHERE id=?', (user,)).fetchone()[0]
        assert hashed.startswith('$argon2id$') and PASSWORD not in hashed
        assert db.execute('SELECT token_hash FROM sessions').fetchone()[0] == digest(token)
    assert list(tmp_path.glob('*.before-accounts-*.sqlite3'))
    with pytest.raises(AccessError):
        Service(path, workspace_id=space)
    store.change_password(token, PASSWORD, PASSWORD + '-new')
    with pytest.raises(AccessError):
        store.me(token)
    with pytest.raises(AccessError):
        store.login('alice', PASSWORD)
    token = store.login('alice', PASSWORD + '-new')
    store.logout(token)
    with pytest.raises(AccessError):
        store.me(token)


def test_workspace_object_isolation_and_actor_derived_from_session(context):
    path, store, a, b, wa, wb = context
    service, run = seeded(context)
    other = Service(path, token=b, workspace_id=wb)
    assert other.create_project('测试项目', '', 'alice')
    assert len(other.list_projects()) == 1
    with pytest.raises(AccessError):
        Service(path, token=b, workspace_id=wa)
    with pytest.raises(ServiceError):
        other.get_project(service.project_id)
    actor = store.me(a)['id']
    assert service.get_run(run)['operator'] == actor
    assert all(e['operator'] == actor for e in service.get_run(run)['events'])
    with store.db() as db:
        assert db.execute("SELECT display_name FROM account_events WHERE action='generation_completed'").fetchone()[0] == 'Alice'


def test_invite_only_requests_and_atomic_admission(context):
    _, store, a, b, _, _ = context
    team = store.create_team(a, '团队')
    code = store.create_invite(a, team, max_uses=1)
    preview = store.request_join(b, code)
    assert preview['name'] == '团队' and not preview['pending']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.request_join(b, code, confirm=True), range(2)))
    assert results[0]['request_id'] == results[1]['request_id']
    assert team not in {s['id'] for s in store.spaces(b)}
    with pytest.raises(AccessError):
        store.access(b, team)
    # Expiration/revocation after submission does not invalidate a valid pending application.
    with store.db(write=True) as db:
        db.execute('UPDATE invites SET expires_at=0,revoked=1')
    request_id = results[0]['request_id']
    def approve(_):
        try:
            store.decide_join(a, team, request_id, True, 'reviewer')
            return True
        except AccessError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(approve, range(2))) == 1
    assert store.access(b, team)['role'] == 'reviewer'


@pytest.mark.parametrize('role,forbidden', [('observer', 'generate'), ('editor', 'review'), ('reviewer', 'edit')])
def test_preset_role_capabilities_and_immediate_revocation(context, role, forbidden):
    path, store, a, b, _, _ = context
    team = team_member(context, role)
    service, run_id = seeded(context, team)
    member = Service(path, token=b, workspace_id=team, project_id=service.project_id)
    assert member.get_run(run_id)['id'] == run_id
    case = member.get_run(run_id)['cases'][0]
    with pytest.raises(ServiceError):
        if forbidden == 'generate':
            member.generate_from_prd(text='\n'.join(['# PRD', '测试内容足够二十个字符长度。']), operator='Alice')
        elif forbidden == 'review':
            member.review(run_id, case['case_id'], 1, 1, 'approve', 'Alice', '通过', 'replay')
        else:
            member.edit(run_id, case['case_id'], 1, 1, case['content'], 'Alice', '修改', 'edit')
    store.set_member(a, team, store.me(b)['id'], 'observer', remove=True)
    with pytest.raises(ServiceError):
        member.get_run(run_id)


def test_owner_admin_bounds_transfer_and_overrides(context):
    _, store, a, b, _, _ = context
    team = team_member(context, 'admin')
    bob, alice = store.me(b)['id'], store.me(a)['id']
    with pytest.raises(AccessError):
        store.set_member(b, team, alice, 'observer')
    with pytest.raises(AccessError):
        store.set_member(b, team, bob, 'admin')
    with pytest.raises(AccessError):
        store.leave(a, team)
    store.transfer(a, team, bob)
    assert store.access(b, team)['role'] == 'owner'
    assert store.access(a, team)['role'] == 'admin'
    store.set_member(b, team, alice, 'reviewer', {'export': False})
    assert 'export' not in store.access(a, team)['permissions']
    with pytest.raises(AccessError):
        store.access(a, team, 'export')


def test_login_limit_and_session_expiry(context):
    _, store, a, _, _, _ = context
    for _ in range(10):
        with pytest.raises(AccessError):
            store.login('missing', 'wrong-password')
    with pytest.raises(AccessError, match='频繁'):
        store.login('missing', PASSWORD)
    with store.db(write=True) as db:
        db.execute('UPDATE sessions SET expires_at=? WHERE token_hash=?', (time.time() - 1, digest(a)))
    with pytest.raises(AccessError):
        store.me(a)


def return_case(service, run_id):
    case = service.get_run(run_id)['cases'][0]
    service.review(run_id, case['case_id'], case['version'], case['revision'], 'return', 'spoof', '明确原文步骤角色', 'return')
    return next(c for c in service.get_run(run_id)['cases'] if c['case_id'] == case['case_id'])


def test_targeted_job_preserves_other_approvals_and_original_evidence(context):
    service, run_id = seeded(context)
    original = service.get_run(run_id)
    for case in original['cases'][1:]:
        service.review(run_id, case['case_id'], 1, 1, 'approve', 'spoof', '通过', case['case_id'])
    case = return_case(service, run_id)
    job = jobs.enqueue(service, run_id, case['case_id'])
    assert jobs.enqueue(service, run_id, case['case_id']) == job
    model = Model(['{"unfinished":', json.dumps({**case['content'], 'title': '修订后的标题'})])
    jobs.execute(service.db_path, service.token, service.workspace_id, service.project_id, job, model)
    current = service.get_run(run_id)
    assert current['cases'][0]['version'] == 2 and current['cases'][0]['status'] == 'pending_review'
    assert all(c['version'] == 1 and c['status'] == 'approved' for c in current['cases'][1:])
    assert service.gate(run_id)
    with service.accounts.db() as db:
        record = dict(db.execute('SELECT * FROM generation_jobs WHERE id=?', (job,)).fetchone())
    assert record['status'] == 'completed' and len(json.loads(record['attempts_json'])) == 2
    assert len(model.calls) == 2 and len(current['case_versions']) == len(original['cases']) + 1


def test_late_model_output_cannot_overwrite_manual_edit(context):
    service, run_id = seeded(context)
    case = return_case(service, run_id)
    job = jobs.enqueue(service, run_id, case['case_id'])
    manual = copy.deepcopy(case['content'])
    manual['title'] = '人工修订优先'
    service.edit(run_id, case['case_id'], case['version'], case['revision'], manual, 'spoof', '人工修订', 'manual')
    model = Model([json.dumps(case['content'])])
    jobs.execute(service.db_path, service.token, service.workspace_id, service.project_id, job, model)
    current = service.get_run(run_id)['cases'][0]
    assert current['version'] == 2 and current['content']['title'] == '人工修订优先'
    assert jobs.list_jobs(service, run_id)[0]['status'] == 'conflict'


def test_worker_downgrade_preserves_raw_without_applying(context):
    _, store, a, b, _, _ = context
    team = team_member(context, 'reviewer')
    owner, run = seeded(context, team)
    member = Service(owner.db_path, token=b, workspace_id=team, project_id=owner.project_id)
    case = return_case(member, run)
    job = jobs.enqueue(member, run, case['case_id'])
    class RevokeDuringCall(Model):
        def generate(self, request):
            store.set_member(a, team, store.me(b)['id'], 'observer')
            return super().generate(request)
    jobs.execute(member.db_path, b, team, member.project_id, job, RevokeDuringCall([json.dumps(case['content'])]))
    assert owner.get_run(run)['cases'][0]['version'] == 1
    with store.db() as db:
        row = db.execute('SELECT status,attempts_json FROM generation_jobs WHERE id=?', (job,)).fetchone()
        assert row['status'] == 'failed' and json.loads(row['attempts_json'])[0]['raw_output']


def test_gateway_http_only_cookie_csrf_and_download_reauthorization(context, monkeypatch):
    import sdlc.gateway as gateway
    path, store, a, b, _, _ = context
    monkeypatch.setattr(gateway, 'DB_PATH', str(path))
    with TestClient(gateway.app) as client:
        response = client.get('/', follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/auth/login'
        page = client.get('/auth/login')
        assert 'HttpOnly' in page.headers['set-cookie'] and 'SameSite=strict' in page.headers['set-cookie']
        csrf = client.cookies['tracecase_csrf']
        response = client.post('/auth/login', data={'username': 'alice', 'password': PASSWORD, 'csrf': 'bad'})
        assert '页面已过期' in response.text
        csrf = client.cookies['tracecase_csrf']
        response = client.post('/auth/login', data={'username': 'alice', 'password': PASSWORD, 'csrf': csrf}, follow_redirects=False)
        assert response.status_code == 303 and 'HttpOnly' in response.headers['set-cookie']
        assert response.headers['location'] == '/' and 'token' not in response.headers['location']
        team = team_member(context, 'reviewer')
        service, run = seeded(context, team)
        for case in service.get_run(run)['cases']:
            service.review(run, case['case_id'], 1, 1, 'approve', 'spoof', '通过', case['case_id'])
        export = service.export(run, 'csv', 'spoof', service.get_run(run)['revision'], 'export')
        url = '/auth/download/' + export['id'] + '?workspace=' + team + '&project=' + service.project_id
        client.cookies.set('tracecase_session', b)
        assert client.get(url).status_code == 200
        store.set_member(a, team, store.me(b)['id'], 'observer')
        assert client.get(url).status_code == 403
        assert client.get('/media/arbitrary').status_code == 403


def test_pending_revision_blocks_export_even_after_manual_review(context):
    service, run = seeded(context)
    case = return_case(service, run)
    jobs.enqueue(service, run, case['case_id'])
    service.edit(run, case['case_id'], case['version'], case['revision'], case['content'], 'spoof', '人工修订', 'edit')
    for current in service.get_run(run)['cases']:
        service.review(run, current['case_id'], current['version'], current['revision'], 'approve', 'spoof', '通过', current['case_id'])
    assert any('正在重新生成' in issue for issue in service.gate(run))
    with pytest.raises(ServiceError):
        service.export(run, 'csv', 'spoof', service.get_run(run)['revision'], 'export')


def test_initial_job_is_background_idempotent_and_scoped(context):
    path, _, a, _, wa, _ = context
    service = Service(path, token=a, workspace_id=wa)
    project = service.create_project('初始生成', '', 'spoof')
    service = Service(path, token=a, workspace_id=wa, project_id=project)
    text = Path('tests/fixtures/generation/normal.md').read_text()
    job = jobs.enqueue_initial(service, text=text)
    assert jobs.enqueue_initial(service, text=text) == job
    assert not service.list_runs()
    jobs.execute_initial(service, job, FixtureProvider())
    result = jobs.initial_jobs(service)[0]
    assert result['status'] == 'completed' and service.get_run(result['run_id'])['cases']
    jobs.execute_initial(service, job, FixtureProvider())
    assert len(service.list_runs()) == 1


def test_schema_ownership_is_immutable_and_legacy_assignment_explicit(context):
    from scripts.assign_legacy import assign
    path, store, a, _, wa, wb = context
    legacy = Engine(path)
    project = legacy.create_project('旧项目', '', 'old-label')
    assign(path, project, wa)
    assert Service(path, token=a, workspace_id=wa).get_project(project)['operator'] == 'old-label'
    with pytest.raises(ValueError):
        assign(path, project, wb)
    with pytest.raises(sqlite3.IntegrityError):
        with store.db(write=True) as db:
            db.execute('UPDATE project_workspaces SET workspace_id=? WHERE project_id=?', (wb, project))


def test_initial_job_revoked_before_start_and_changed_prd_not_silently_dropped(context):
    path, store, a, _, wa, _ = context
    service = Service(path, token=a, workspace_id=wa)
    project = service.create_project('初始任务', '', 'spoof')
    service = Service(path, token=a, workspace_id=wa, project_id=project)
    text = Path('tests/fixtures/generation/normal.md').read_text()
    job = jobs.enqueue_initial(service, text=text)
    with pytest.raises(ServiceError, match='另一份需求'):
        jobs.enqueue_initial(service, text=text + '\n新需求')
    store.logout(a)
    jobs.execute_initial(service, job, FixtureProvider())
    with store.db() as db:
        assert db.execute('SELECT status FROM generation_jobs WHERE id=?', (job,)).fetchone()[0] == 'failed'
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0


def test_gateway_registration_and_multiple_tabs_share_csrf(context, monkeypatch):
    import sdlc.gateway as gateway
    path, store, _, _, _, _ = context
    monkeypatch.setattr(gateway, 'DB_PATH', str(path))
    with TestClient(gateway.app) as client:
        client.get('/auth/login')
        original = client.cookies['tracecase_csrf']
        client.get('/auth/login')  # Opening a second tab must not invalidate the first form.
        assert client.cookies['tracecase_csrf'] == original
        assert client.get('/_stcore/health').status_code == 401
        response = client.post('/auth/register', data={
            'username': 'new-user', 'display_name': 'New', 'password': PASSWORD,
            'confirmation': PASSWORD, 'csrf': original})
        assert '注册成功' in response.text
        token = store.login('new-user', PASSWORD)
        assert len(store.spaces(token)) == 1 and store.spaces(token)[0]['kind'] == 'personal'


def test_invite_reset_team_settings_and_account_schema_upgrade(context):
    path, store, a, b, _, _ = context
    team = store.create_team(a, '原名')
    code = store.create_invite(a, team)
    invite = store.manage_data(a, team)['invites'][0]
    replacement = store.reset_invite(a, team, invite['id'])
    with pytest.raises(AccessError):
        store.request_join(b, code)
    assert store.request_join(b, replacement)['name'] == '原名'
    store.update_team(a, team, '新名', '团队简介')
    assert store.request_join(b, replacement)['name'] == '新名'
    with store.db(write=True) as db:
        db.execute('UPDATE account_meta SET version=1')
    Accounts(path)
    with store.db() as db:
        assert db.execute('SELECT version FROM account_meta').fetchone()[0] == 4
    with store.db(write=True) as db:
        db.execute('UPDATE account_meta SET version=99')
    with pytest.raises(AccessError, match='版本'):
        Accounts(path)


def test_targeted_repair_failure_is_bounded_and_keeps_returned_version(context):
    service, run = seeded(context)
    case = return_case(service, run)
    job = jobs.enqueue(service, run, case['case_id'])
    model = Model(['{"unfinished":'])
    jobs.execute(service.db_path, service.token, service.workspace_id, service.project_id, job, model)
    current = service.get_run(run)['cases'][0]
    assert len(model.calls) == 2 and current['version'] == 1 and current['status'] == 'returned'
    assert jobs.list_jobs(service, run)[0]['status'] == 'failed'
    retry = jobs.enqueue(service, run, case['case_id'])
    assert retry != job


def test_initial_jobs_in_different_workspaces_do_not_share_unique_slot(context):
    path, _, a, b, wa, wb = context
    text = Path('tests/fixtures/generation/normal.md').read_text()
    jobs_created = []
    for token, workspace in ((a, wa), (b, wb)):
        service = Service(path, token=token, workspace_id=workspace)
        project = service.create_project('同名项目', '', 'spoof')
        service = Service(path, token=token, workspace_id=workspace, project_id=project)
        jobs_created.append(jobs.enqueue_initial(service, text=text))
    assert len(set(jobs_created)) == 2


def test_authenticated_auto_repair_replays_scoped_result(context):
    path, _, a, _, wa, _ = context
    service = Service(path, token=a, workspace_id=wa)
    project = service.create_project('修复幂等', '', 'spoof')
    service = Service(path, token=a, workspace_id=wa, project_id=project)
    text = Path('tests/fixtures/generation/normal.md').read_text()
    raw = Path('tests/fixtures/generation/normal.output.json').read_text()
    model = Model(['{"unfinished":', raw])
    run = service.generate_from_prd(text=text, operator='spoof', request_id='repair-once', provider=model)
    assert service.generate_from_prd(text=text, operator='spoof', request_id='repair-once', provider=model) == run
    assert len(model.calls) == 2 and service.get_run(run)['status'] == 'valid'


def test_archived_project_prevents_queued_model_call(context):
    service, run = seeded(context)
    case = return_case(service, run)
    job = jobs.enqueue(service, run, case['case_id'])
    service.set_project_archived(service.project_id, True, 'spoof', service.get_project()['revision'])
    with pytest.raises(ServiceError, match='归档'):
        jobs.enqueue(service, run, case['case_id'])
    model = Model([json.dumps(case['content'])])
    jobs.execute(service.db_path, service.token, service.workspace_id, service.project_id, job, model)
    assert not model.calls and jobs.list_jobs(service, run)[0]['status'] == 'failed'

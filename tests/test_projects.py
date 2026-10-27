import copy
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from sdlc.providers import list_samples
from sdlc.service import Service, ServiceError
from sdlc.storage import LEGACY_PROJECT


def sample():
    return next(s['text'] for s in list_samples() if s['id'] == 'normal')


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / 'test.sqlite3'
    admin = Service(path)
    a = admin.create_project('项目 A', 'A description', 'tester')
    b = admin.create_project('项目 B', '', 'tester')
    return admin, Service(path, project_id=a), Service(path, project_id=b)


def create_run(service):
    return service.generate(text=sample(), operator='tester')


def approve_all(service, run_id):
    for c in service.get_run(run_id)['cases']:
        service.review(run_id, c['case_id'], c['version'], c['revision'], 'approve', 'tester', '通过', uuid4().hex)


def export(service, run_id):
    return service.export(run_id, 'csv', 'tester', service.get_run(run_id)['revision'], uuid4().hex)


def test_no_project_means_no_run_access(workspace):
    admin, a, _ = workspace
    run_id = create_run(a)
    with pytest.raises(ServiceError, match='选择项目'):
        admin.generate(text=sample(), operator='tester')
    with pytest.raises(ServiceError, match='选择项目'):
        admin.list_runs()
    with pytest.raises(ServiceError, match='选择项目'):
        admin.get_run(run_id)


def test_project_names_revision_and_audit(workspace):
    admin, a, _ = workspace
    with pytest.raises(ServiceError):
        admin.create_project('  ', '', 'tester')
    with pytest.raises(ServiceError, match='已存在'):
        admin.create_project(' 项目 A ', '', 'tester')
    old = a.get_project()
    admin.update_project(a.project_id, '新名称', 'new description', 'editor', old['revision'])
    updated = a.get_project()
    assert updated['name'] == '新名称' and updated['id'] == old['id']
    assert updated['revision'] == old['revision'] + 1
    assert updated['events'][-1]['operator'] == 'editor'
    with pytest.raises(ServiceError, match='变化'):
        admin.update_project(a.project_id, '再改', '', 'editor', old['revision'])


def test_project_run_case_export_isolation_and_immutable_binding(workspace):
    _, a, b = workspace
    ra, rb = create_run(a), create_run(b)
    assert [r['id'] for r in a.list_runs()] == [ra]
    assert [r['id'] for r in b.list_run_summaries()] == [rb]
    assert a.project_stats()['cases'] == 11
    with pytest.raises(ServiceError, match='不属于'):
        b.get_run(ra)
    with pytest.raises(ServiceError, match='不属于'):
        b.generate(text=sample(), operator='tester', parent_run_id=ra)
    approve_all(a, ra)
    saved = export(a, ra)
    assert a.list_exports()[0]['id'] == saved['id']
    assert not b.list_exports()
    with pytest.raises(ServiceError, match='不属于'):
        b.get_export(saved['id'])
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with a.connection(write=True) as db:
            db.execute('UPDATE runs SET project_id=? WHERE id=?', (b.project_id, ra))
    with pytest.raises(sqlite3.IntegrityError, match='another project'):
        with a.connection(write=True) as db:
            db.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (ra, rb))


@pytest.mark.parametrize('operation', ['review', 'edit', 'clarification', 'resolve', 'export', 'interrupt', 'revision'])
def test_cross_project_mutations_are_rejected(workspace, operation):
    _, a, b = workspace
    run_id = create_run(a)
    c = a.get_run(run_id)['cases'][0]
    with pytest.raises(ServiceError, match='不属于'):
        if operation == 'review':
            b.review(run_id, c['case_id'], 1, c['revision'], 'approve', 'tester', '通过', uuid4().hex)
        elif operation == 'edit':
            b.edit(run_id, c['case_id'], 1, c['revision'], c['content'], 'tester', '修改', uuid4().hex)
        elif operation == 'clarification':
            rule = a.get_run(run_id)['output']['business_rules'][0]
            q = {'id': 'Q-H', 'question': '问题', 'paragraph_ids': rule['paragraph_ids'],
                 'requirement_ids': [rule['id']], 'affected_case_ids': [], 'scope': 'run', 'blocking': True}
            b.add_clarification(run_id, q, 1, 'tester', '问题', uuid4().hex)
        elif operation == 'resolve':
            b.resolve_clarification(run_id, 'Q', 1, 'false_positive', ['P001'], 'tester', '误报', uuid4().hex)
        elif operation == 'export':
            b.export(run_id, 'csv', 'tester', 1, uuid4().hex)
        elif operation == 'interrupt':
            b.mark_interrupted(run_id, 'tester', '中断')
        else:
            b.revise_returned(run_id, 1, 'tester', None, uuid4().hex)


def test_archive_read_only_restore_and_rename_snapshot(workspace):
    admin, a, _ = workspace
    run_id = create_run(a)
    approve_all(a, run_id)
    saved = export(a, run_id)
    assert '项目 A' in saved['content'].decode('utf-8-sig')
    p = a.get_project()
    admin.update_project(a.project_id, '项目新名称', '', 'tester', p['revision'])
    assert a.get_export(saved['id'])['content'] == saved['content']
    assert a.get_export(saved['id'])['project_name'] == '项目 A'
    new = export(a, run_id)
    assert new['project_name'] == '项目新名称'
    p = a.get_project()
    admin.set_project_archived(a.project_id, True, 'tester', p['revision'])
    assert a.get_run(run_id)['cases'] and a.get_export(saved['id'])['content']
    with pytest.raises(ServiceError, match='归档'):
        create_run(a)
    with pytest.raises(ServiceError, match='归档'):
        export(a, run_id)
    c = a.get_run(run_id)['cases'][0]
    with pytest.raises(ServiceError, match='归档'):
        a.edit(run_id, c['case_id'], c['version'], c['revision'], copy.deepcopy(c['content']), 'tester', '改动', uuid4().hex)
    admin.set_project_archived(a.project_id, False, 'tester', a.get_project()['revision'])
    assert create_run(a)


def test_cannot_archive_generating_run_or_delete_project(workspace):
    admin, a, _ = workspace
    run_id = create_run(a)
    with a.connection(write=True) as db:
        db.execute("UPDATE runs SET status='generating' WHERE id=?", (run_id,))
    with pytest.raises(ServiceError, match='生成中'):
        admin.set_project_archived(a.project_id, True, 'tester', a.get_project()['revision'])
    a.mark_interrupted(run_id, 'tester', '确认旧调用已结束')
    admin.set_project_archived(a.project_id, True, 'tester', a.get_project()['revision'])
    with pytest.raises(sqlite3.IntegrityError, match='archive'):
        with a.connection(write=True) as db:
            db.execute('DELETE FROM projects WHERE id=?', (a.project_id,))


def legacy_database(path):
    db = sqlite3.connect(path)
    db.executescript((Path(__file__).parent / 'fixtures/schema_v1.sql').read_text())
    output = json.loads(Path('samples/normal.output.json').read_text())
    from sdlc.domain import prepare_input
    from sdlc.prompts import build_prompt
    prepared = prepare_input(text=sample())
    for run_id, parent in [('old-parent', None), ('old-child', 'old-parent')]:
        db.execute('INSERT INTO runs (id,parent_run_id,created_at,operator,status,input_json,config_json,prompt_json,output_json) VALUES (?,?,?,?,?,?,?,?,?)',
                   (run_id, parent, '2026-10-26T00:00:00+00:00', 'old-reviewer', 'valid',
                    json.dumps(prepared), '{}', json.dumps(build_prompt('V2', prepared['paragraphs'])), json.dumps(output)))
        for c in output['test_cases']:
            db.execute('INSERT INTO case_versions VALUES (?,?,?,?,?,?)', (run_id, c['case_id'], 1, json.dumps(c), '2026-10-26T00:00:00+00:00', 'old-reviewer'))
            db.execute('INSERT INTO case_heads VALUES (?,?,?,?,?)', (run_id, c['case_id'], 1, 1, 'approved'))
    content = 'legacy,批准快照\n不可修改'.encode('utf-8-sig')
    db.execute('INSERT INTO exports VALUES (?,?,?,?,?,?,?,?,?,?)', ('old-export', 'old-parent', 'csv', 'old.csv', 'text/csv', 'old-reviewer', '2026-10-26T00:00:00+00:00', content, hashlib.sha256(content).hexdigest(), '[]'))
    db.commit()
    db.close()
    return content


def test_migration_preserves_relationships_versions_and_export_bytes(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    content = legacy_database(path)
    admin = Service(path)
    assert len(admin.list_projects()) == 1
    project = admin.get_project(LEGACY_PROJECT)
    assert project['name'] == '历史项目'
    scoped = Service(path, project_id=LEGACY_PROJECT)
    assert len(scoped.list_runs()) == 2
    assert scoped.get_run('old-child')['parent_run_id'] == 'old-parent'
    assert len(scoped.get_run('old-parent')['cases']) == 11
    assert scoped.get_export('old-export')['content'] == content
    backups = list(tmp_path.glob('*.before-projects-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 1
        assert backup.execute('SELECT content FROM exports').fetchone()[0] == content
    Service(path)
    assert len(list(tmp_path.glob('*.before-projects-*.sqlite3'))) == 1
    with scoped.connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert db.execute('PRAGMA foreign_key_list(case_versions)').fetchone()['table'] == 'runs'


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import sdlc.storage as storage
    path = tmp_path / 'legacy.sqlite3'
    content = legacy_database(path)
    original = storage.statements
    def fail(db, script):
        original(db, script)
        raise RuntimeError('simulated migration failure')
    monkeypatch.setattr(storage, 'statements', fail)
    with pytest.raises(RuntimeError):
        Service(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 2
        assert db.execute('SELECT content FROM exports').fetchone()[0] == content
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone()


def test_newer_database_is_not_downgraded(tmp_path):
    path = tmp_path / 'newer.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError, match='高于'):
        Service(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 99


def test_inflight_generation_keeps_original_project_and_blocks_archive(workspace):
    import threading
    from sdlc.providers import FixtureProvider
    admin, a, b = workspace
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    class SlowProvider:
        config = {'provider': 'test', 'model': 'slow-test'}
        def generate(self, request):
            entered.set()
            assert release.wait(5)
            return FixtureProvider().generate(request)
    def generate_a():
        try:
            results.append(a.generate(text=sample(), operator='tester', provider=SlowProvider()))
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=generate_a)
    thread.start()
    assert entered.wait(5)
    try:
        rb = create_run(b)
        with pytest.raises(ServiceError, match='生成中'):
            admin.set_project_archived(a.project_id, True, 'tester', a.get_project()['revision'])
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert len(results) == 1
    assert a.get_run(results[0])['project_id'] == a.project_id
    assert a.get_run(results[0])['status'] == 'valid'
    assert [r['id'] for r in b.list_runs()] == [rb]
    with pytest.raises(ServiceError, match='不属于'):
        b.get_run(results[0])

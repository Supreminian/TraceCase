import time

import pytest

from sdlc.accounts import Accounts, AccessError, digest
from sdlc.secure_service import Service
from sdlc.testing_agent.worker import claim, delegated_identity, initialize


def context(path):
    store = Accounts(path)
    user = store.register('worker-user', 'Worker user', 'disposable-password')
    token = store.login('worker-user', 'disposable-password')
    workspace = store.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('Worker test', '', None)
    return store, user, token, workspace, project


def test_lease_expiry_recovers_without_duplicate_live_claim(tmp_path):
    path = tmp_path / 'worker.sqlite3'
    store, user, token, workspace, project = context(path)
    initialize(path)
    with store.db(write=True) as db:
        db.execute("INSERT INTO agent_work_items(id,workspace_id,project_id,task_id,actor_id,operation,payload,created_at,updated_at) VALUES (?,?,?,?,?,'start','{}',?,?)",
                   ('job', workspace, project, 'task', user, time.time(), time.time()))
    first = claim(path, 'worker-a', lease_seconds=60)
    assert first['attempts'] == 1
    assert claim(path, 'worker-b') is None
    with store.db(write=True) as db:
        db.execute('UPDATE agent_work_items SET lease_until=?', (time.time() - 1,))
    recovered = claim(path, 'worker-b')
    assert recovered['id'] == first['id'] and recovered['attempts'] == 2
    assert recovered['lease_owner'] == 'worker-b'


def test_delegation_does_not_store_plaintext_or_depend_on_browser_session(tmp_path):
    path = tmp_path / 'worker.sqlite3'
    store, user, token, workspace, project = context(path)
    store.logout(token)
    job = {'actor_id': user, 'workspace_id': workspace, 'project_id': project}
    with delegated_identity(path, job) as delegated:
        assert store.access(delegated, workspace, 'generate')['id'] == user
        with store.db() as db:
            assert db.execute('SELECT COUNT(*) FROM sessions WHERE token_hash=?', (delegated,)).fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM sessions WHERE token_hash=?', (digest(delegated),)).fetchone()[0] == 1
    with pytest.raises(AccessError):
        store.me(delegated)


def test_disabled_actor_cannot_start_delegated_work(tmp_path):
    path = tmp_path / 'worker.sqlite3'
    store, user, token, workspace, project = context(path)
    with store.db(write=True) as db:
        db.execute('UPDATE users SET enabled=0 WHERE id=?', (user,))
    with pytest.raises(AccessError):
        with delegated_identity(path, {'actor_id': user, 'workspace_id': workspace, 'project_id': project}):
            pytest.fail('must not yield')

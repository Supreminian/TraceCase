"""Shared project resources have immutable snapshots and real ACL boundaries."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from sdlc.accounts import Accounts
from sdlc.secure_service import Service
from sdlc.service import ServiceError
from sdlc.testing_agent.resources import ResourceLibrary


@pytest.fixture
def resource_context(tmp_path):
    path = tmp_path / 'resources.sqlite3'
    accounts = Accounts(path)
    for username in ('alice', 'bob'):
        accounts.register(username, username.title(), 'resource-test-password')
    alice = accounts.login('alice', 'resource-test-password')
    bob = accounts.login('bob', 'resource-test-password')
    workspace = accounts.spaces(alice)[0]['id']
    legacy = Service(path, token=alice, workspace_id=workspace)
    project = legacy.create_project('资源项目', '')
    return path, accounts, alice, bob, workspace, project


def test_resource_versions_events_and_detached_task_snapshots(resource_context):
    path, accounts, token, _, workspace, project = resource_context
    library = ResourceLibrary(path, token, workspace, project)
    created = library.create('precondition', '虚构申请数据', '准备一份尚未提交的虚构报销单。')
    assert created['revision'] == 1 and created['archived'] is False
    snapshot = library.snapshot([created['id']])
    updated = library.update(created['id'], 1, '新版数据准备', '准备两份不同发票号的虚构报销单。')
    assert updated['revision'] == 2
    assert snapshot[0]['body'] == '准备一份尚未提交的虚构报销单。'
    assert snapshot[0]['revision'] == 1
    assert snapshot[0]['business_fact_confirmed'] is False
    assert library.snapshot([{'id': created['id'], 'revision': 1}]) == snapshot
    with accounts.db() as db:
        versions = db.execute('SELECT revision,body FROM testing_shared_resource_versions ORDER BY revision').fetchall()
        assert [v['revision'] for v in versions] == [1, 2]
        assert versions[0]['body'] == snapshot[0]['body']
        events = db.execute('SELECT * FROM testing_shared_resource_events ORDER BY revision').fetchall()
        assert [e['action'] for e in events] == ['created', 'updated']
        assert all(e['actor_id'] == accounts.me(token)['id'] and e['created_at'] for e in events)


def test_resource_cross_project_and_workspace_isolation(resource_context):
    path, accounts, alice, bob, workspace, project = resource_context
    library = ResourceLibrary(path, alice, workspace, project)
    created = library.create('steps', '提交步骤', '点击提交并观察页面状态。')
    second = Service(path, token=alice, workspace_id=workspace).create_project('其他项目', '')
    other = ResourceLibrary(path, alice, workspace, second)
    assert other.list() == []
    with pytest.raises(ServiceError, match='无权访问'):
        other.snapshot([created['id']])
    with pytest.raises(ServiceError, match='无权访问'):
        other.update(created['id'], 1, '越权', '越权修改')
    bob_space = accounts.spaces(bob)[0]['id']
    bob_project = Service(path, token=bob, workspace_id=bob_space).create_project('Bob项目', '')
    bob_library = ResourceLibrary(path, bob, bob_space, bob_project)
    with pytest.raises(ServiceError, match='无权访问'):
        bob_library.snapshot([created['id']])


def test_resource_roles_and_revocation_are_rechecked(resource_context):
    path, accounts, alice, bob, _, _ = resource_context
    team = accounts.create_team(alice, '资源协作')
    invite = accounts.create_invite(alice, team)
    request = accounts.request_join(bob, invite, confirm=True)
    accounts.decide_join(alice, team, request['request_id'], True, 'editor')
    project = Service(path, token=alice, workspace_id=team).create_project('团队资源', '')
    owner = ResourceLibrary(path, alice, team, project)
    member = ResourceLibrary(path, bob, team, project)
    item = member.create('test_data', '发票数据', '仅使用虚构发票号。')
    member.update(item['id'], 1, '发票数据更新', '使用两个不同的虚构发票号。')
    with pytest.raises(ServiceError, match='无此操作权限'):
        member.archive(item['id'], 2)
    bob_id = accounts.me(bob)['id']
    accounts.set_member(alice, team, bob_id, 'reviewer')
    assert member.snapshot([item['id']])[0]['revision'] == 2
    with pytest.raises(ServiceError, match='无此操作权限'):
        member.create('steps', '步骤', '提交')
    with pytest.raises(ServiceError, match='无此操作权限'):
        member.update(item['id'], 2, '修改', '任意编辑')
    owner.archive(item['id'], 2)
    accounts.set_member(alice, team, bob_id, 'observer', remove=True)
    with pytest.raises(ServiceError, match='无权访问'):
        member.list()


def test_resource_archive_restore_and_stale_cas(resource_context):
    path, _, token, _, workspace, project = resource_context
    library = ResourceLibrary(path, token, workspace, project)
    item = library.create('steps', '操作步骤', '填写后提交。')
    saved_snapshot = library.snapshot([item['id']])
    archived = library.archive(item['id'], 1)
    assert archived['revision'] == 2 and archived['archived'] is True
    assert library.list() == [] and len(library.list(include_archived=True)) == 1
    with pytest.raises(ServiceError, match='归档资源'):
        library.snapshot([item['id']])
    with pytest.raises(ServiceError, match='归档资源'):
        library.snapshot([{'id': item['id'], 'revision': 1}])
    assert saved_snapshot[0]['body'] == '填写后提交。'
    with pytest.raises(ServiceError, match='请先恢复'):
        library.update(item['id'], 2, '新标题', '新内容')
    restored = library.archive(item['id'], 2, archived=False)
    assert restored['revision'] == 3 and restored['archived'] is False
    assert library.snapshot([{'id': item['id'], 'revision': 1}])[0]['body'] == '填写后提交。'
    with pytest.raises(ServiceError, match='其他协作者'):
        library.update(item['id'], 1, '旧请求', '不能覆盖')
    with pytest.raises(ServiceError, match='已更新'):
        library.archive(item['id'], 2)


def test_resource_concurrent_updates_only_one_wins(resource_context):
    path, _, token, _, workspace, project = resource_context
    library = ResourceLibrary(path, token, workspace, project)
    item = library.create('test_data', '初始数据', '虚构数据V1')
    def update(number):
        try:
            return library.update(item['id'], 1, '并发更新', f'虚构数据{number}')['revision']
        except ServiceError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, [2, 3]))
    assert outcomes.count(2) == 1 and outcomes.count(None) == 1
    assert library.snapshot([{'id': item['id'], 'revision': 1}])[0]['body'] == '虚构数据V1'


@pytest.mark.parametrize('kind,title,body', [('arbitrary', '标题', '内容'),
    ('steps', '', '内容'), ('steps', 'x' * 101, '内容'), ('test_data', '标题', ''),
    ('precondition', '标题', 'x' * 8001), ('steps', '标题', 'bad\x00text')])
def test_resource_input_constraints(resource_context, kind, title, body):
    path, _, token, _, workspace, project = resource_context
    library = ResourceLibrary(path, token, workspace, project)
    with pytest.raises(ServiceError):
        library.create(kind, title, body)
    assert library.list() == []


def test_resource_snapshot_count_and_total_context_budget(resource_context):
    path, _, token, _, workspace, project = resource_context
    library = ResourceLibrary(path, token, workspace, project)
    resources = [library.create('test_data', 'x' * 100, 'x' * 8000) for _ in range(6)]
    assert len(library.snapshot([r['id'] for r in resources[:5]])) == 5
    with pytest.raises(ServiceError, match='40,000'):
        library.snapshot([r['id'] for r in resources])
    with pytest.raises(ServiceError, match='50'):
        library.snapshot(['id'] * 51)
    with pytest.raises(ServiceError, match='重复'):
        library.snapshot([resources[0]['id'], resources[0]['id']])

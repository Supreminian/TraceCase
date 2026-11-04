"""Authenticated audit queries and data boundaries across real users/workspaces."""
import json

import pytest

from sdlc.accounts import Accounts, AccessError
from sdlc.secure_service import Service
from sdlc.service import ServiceError
from sdlc.testing_agent.audit_queries import case_review_history, team_join_history
from sdlc.testing_agent.case_browser import get_case, list_cases
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.resources import ResourceLibrary
from sdlc.testing_agent.service import TestingService
from tests.test_testing_agent import ready


@pytest.fixture
def users(tmp_path):
    path = tmp_path / 'audit.sqlite3'
    accounts = Accounts(path)
    tokens = {}
    services = {}
    for name in ('alice', 'bob', 'charlie'):
        accounts.register(name, name.title(), 'audit-disposable-password')
        token = accounts.login(name, 'audit-disposable-password')
        tokens[name] = token
        workspace = accounts.spaces(token)[0]['id']
        project = Service(path, token=token, workspace_id=workspace).create_project(name + ' private', '')
        services[name] = TestingService(path, token, workspace, project)
    return accounts, tokens, services


def approved(service):
    task = ready(service)
    run = service.finalize_design(task)
    case = service.legacy.get_run(run)['cases'][0]
    event = service.legacy.review(run, case['case_id'], case['version'], case['revision'],
                                  'approve', 'spoofed-actor', '符合需求', 'approve-for-audit')
    return task, run, case, event


def team(users):
    accounts, tokens, private = users
    workspace = accounts.create_team(tokens['alice'], 'Shared audit team')
    code = accounts.create_invite(tokens['alice'], workspace)
    request = accounts.request_join(tokens['bob'], code, confirm=True)
    accounts.decide_join(tokens['alice'], workspace, request['request_id'], True, 'reviewer')
    project = Service(accounts.path, token=tokens['alice'], workspace_id=workspace).create_project('Shared work', '')
    owner = TestingService(accounts.path, tokens['alice'], workspace, project)
    member = TestingService(accounts.path, tokens['bob'], workspace, project)
    return owner, member, request


def test_case_review_proves_actor_and_preserves_snapshot_after_display_name_change(users):
    accounts, tokens, services = users
    service = services['alice']
    task, run, case, event_id = approved(service)
    with accounts.db(write=True) as db:
        db.execute('UPDATE users SET display_name=? WHERE id=?', ('New name', accounts.me(tokens['alice'])['id']))
    result = case_review_history(service, run, case['case_id'])
    decision = result['events'][0]
    assert decision['event_id'] == event_id
    assert decision['actor_id'] == accounts.me(tokens['alice'])['id']
    assert decision['actor_name'] == 'Alice' and decision['actor_verified']
    assert decision['recorded_operator'] != 'spoofed-actor'
    assert result['current_status'] == 'approved' and decision['is_current_version']
    current = get_case(service, run, case['case_id'])['case']
    service.legacy.edit(run, current['case_id'], current['version'], current['revision'],
                        {**current['content'], 'title': 'Updated case'}, '', '修正标题', 'audit-edit')
    result = case_review_history(service, run, case['case_id'])
    assert result['current_status'] == 'pending_review'
    assert not result['events'][0]['is_current_version']
    assert result['events'][0]['case_version'] == 1 and result['current_version'] == 2
    assert not {'password_hash', 'token_hash', 'config_json', 'raw_output'} & result.keys()


def test_other_personal_data_cannot_be_read_by_forged_object_ids(users):
    accounts, tokens, services = users
    alice, bob = services['alice'], services['bob']
    task, run, case, _ = approved(alice)
    chat = ChatService(alice)
    chat.post_user(task, 'Alice private chat')
    library = ResourceLibrary(alice.db_path, alice.token, alice.workspace_id, alice.project_id)
    resource = library.create('precondition', 'Alice private source', 'private material')
    assert list_cases(bob)['total'] == 0
    for action in (lambda: bob.get_task(task), lambda: get_case(bob, run, case['case_id']),
                   lambda: case_review_history(bob, run, case['case_id']),
                   lambda: ChatService(bob).list_messages(task),
                   lambda: ResourceLibrary(bob.db_path, bob.token, bob.workspace_id, bob.project_id).snapshot([resource['id']])):
        with pytest.raises(ServiceError):
            action()
    with pytest.raises((AccessError, ServiceError)):
        TestingService(accounts.path, tokens['bob'], alice.workspace_id, alice.project_id)
    with pytest.raises(ServiceError):
        TestingService(accounts.path, tokens['bob'], bob.workspace_id, alice.project_id)


def test_personal_owner_boundary_survives_erroneous_membership(users):
    accounts, tokens, services = users
    alice, bob = services['alice'], services['bob']
    bob_id = accounts.me(tokens['bob'])['id']
    with accounts.db(write=True) as db:
        db.execute("INSERT INTO memberships(workspace_id,user_id,role) VALUES (?,?,'admin')", (alice.workspace_id, bob_id))
        db.execute('INSERT INTO workspace_preferences VALUES (?,?)', (bob_id, alice.workspace_id))
    assert alice.workspace_id not in {space['id'] for space in accounts.spaces(tokens['bob'])}
    assert accounts.preferred_space(tokens['bob']) is None
    with pytest.raises(AccessError):
        accounts.access(tokens['bob'], alice.workspace_id)
    with pytest.raises((AccessError, ServiceError)):
        TestingService(accounts.path, tokens['bob'], alice.workspace_id, alice.project_id)
    assert accounts.access(tokens['bob'], bob.workspace_id)['id'] == bob_id


def test_team_join_history_returns_real_decider_with_restricted_fields(users):
    accounts, tokens, services = users
    owner, member, request = team(users)
    bob_id = accounts.me(tokens['bob'])['id']
    from_owner = team_join_history(owner, username='bob')
    from_member = team_join_history(member)
    assert from_owner == from_member
    assert from_owner['subject']['id'] == bob_id
    decision = from_owner['requests'][0]
    assert decision['request_id'] == request['request_id']
    assert decision['decision_actor_id'] == accounts.me(tokens['alice'])['id']
    assert decision['decision_actor_name'] == 'Alice'
    assert decision['status'] == 'approved' and decision['actor_verified'] and decision['decided_at']
    rendered = json.dumps(from_owner)
    assert all(secret not in rendered for secret in ('password', 'token_hash', 'invite_id', 'note', 'code_hash'))
    with pytest.raises(ServiceError, match='管理者'):
        team_join_history(member, username='charlie')
    assert team_join_history(owner, username='charlie')['subject'] is None
    assert team_join_history(owner, username="bob' OR 1=1 --")['total'] == 0
    with pytest.raises(ServiceError, match='团队'):
        team_join_history(services['alice'], user_id=bob_id)


def test_nonmembers_and_removed_members_lose_access_to_existing_agent_objects(users):
    accounts, tokens, services = users
    owner, member, request = team(users)
    task, run, case, _ = approved(owner)
    assert list_cases(member)['total'] == 2
    assert case_review_history(member, run, case['case_id'])['total'] == 1
    with pytest.raises((AccessError, ServiceError)):
        TestingService(accounts.path, tokens['charlie'], owner.workspace_id, owner.project_id)
    bob_id = accounts.me(tokens['bob'])['id']
    accounts.set_member(tokens['alice'], owner.workspace_id, bob_id, 'reviewer', remove=True)
    for action in (lambda: member.get_task(task), lambda: list_cases(member),
                   lambda: case_review_history(member, run, case['case_id']),
                   lambda: team_join_history(member), lambda: ChatService(member).list_messages(task)):
        with pytest.raises(ServiceError):
            action()
    # A manager can still inspect the historical decision, without giving the
    # former member access to the shared workspace again.
    assert team_join_history(owner, user_id=bob_id)['requests'][0]['request_id'] == request['request_id']


def test_foreign_team_request_is_not_discoverable_through_current_team_query(users):
    accounts, tokens, services = users
    owner, member, _ = team(users)
    other = accounts.create_team(tokens['charlie'], 'Unrelated team')
    code = accounts.create_invite(tokens['charlie'], other)
    request = accounts.request_join(tokens['bob'], code, confirm=True)
    accounts.decide_join(tokens['charlie'], other, request['request_id'], True, 'editor')
    result = team_join_history(owner, username='bob')
    assert result['total'] == 1
    assert request['request_id'] not in json.dumps(result)
    assert 'Charlie' not in json.dumps(result)


@pytest.mark.parametrize('limit,offset', [(True, 0), (0, 0), (101, 0), (50, -1), (50, True)])
def test_audit_queries_reject_unbounded_paging(users, limit, offset):
    _, _, services = users
    with pytest.raises(ServiceError):
        case_review_history(services['alice'], 'run', 'case', limit=limit, offset=offset)

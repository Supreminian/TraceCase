"""Workspace entry prioritizes memberships and keeps new team actions on demand."""
import pytest
from streamlit.testing.v1 import AppTest

from sdlc.accounts import Accounts


@pytest.fixture
def context(tmp_path):
    store = Accounts(tmp_path / 'workspaces.sqlite3')
    store.register('alice', 'Alice', 'disposable-password')
    token = store.login('alice', 'disposable-password')
    return store, token


def picker(store, token):
    app = AppTest.from_string(f'''
import streamlit as st
from sdlc.accounts import Accounts
from sdlc.account_ui import workspace_picker
from sdlc.ui import apply_style
apply_style()
store = Accounts({str(store.path)!r})
token = {token!r}
if st.session_state.get('active_workspace'):
    st.text('已进入工作空间')
else:
    workspace_picker(store, token, store.spaces(token))
''', default_timeout=10).run()
    assert not app.exception
    return app


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_no_team_shows_first_team_form_and_creates_owner(context):
    store, token = context
    app = picker(store, token)
    assert not app.text_input
    app.button(key='open_create_team').click().run()
    next(t for t in app.text_input if t.label == '团队名称').input('第一个团队')
    button(app, '创建团队并进入').click().run()
    assert not app.exception
    team = next(s for s in store.spaces(token) if s['kind'] == 'team')
    assert team['name'] == '第一个团队' and team['role'] == 'owner'
    assert app.session_state['active_workspace'] == team['id']


def test_existing_teams_show_cards_and_creation_dialog(context):
    store, token = context
    team = store.create_team(token, '已有团队', '协作说明')
    app = picker(store, token)
    assert app.button(key='space_' + team)
    assert not app.text_input and not app.radio
    app.button(key='open_create_team').click().run()
    next(t for t in app.text_input if t.label == '团队名称').input('新团队')
    button(app, '创建团队并进入').click().run()
    assert not app.exception
    selected = app.session_state['active_workspace']
    assert selected != team
    assert store.access(token, selected)['role'] == 'owner'
    assert store.preferred_space(token) == selected


def test_join_other_team_requires_approval_before_showing_card(context):
    store, token = context
    current = store.create_team(token, '当前团队')
    store.register('owner', 'Owner', 'disposable-password')
    owner = store.login('owner', 'disposable-password')
    target = store.create_team(owner, '目标团队')
    code = store.create_invite(owner, target)
    app = picker(store, token)
    app.button(key='open_join_team').click().run()
    assert not any(t.label == '团队名称' for t in app.text_input)
    next(t for t in app.text_input if t.label == '邀请码').input(code)
    button(app, '核对邀请的团队').click().run()
    assert any('目标团队' in info.value for info in app.info)
    button(app, '确认提交加入申请').click().run()
    assert not app.exception
    assert any('申请已提交' in result.value for result in app.success)
    assert [s['id'] for s in store.spaces(token) if s['kind'] == 'team'] == [current]
    assert store.applications(token)[0]['status'] == 'pending'


def test_team_card_enters_existing_space(context):
    store, token = context
    team = store.create_team(token, '已有团队')
    app = picker(store, token)
    app.button(key='space_' + team).click().run()
    assert not app.exception
    assert app.session_state['active_workspace'] == team
    assert store.preferred_space(token) == team


def test_team_management_button_opens_and_returns_without_losing_project():
    app = AppTest.from_string('''
import streamlit as st
from sdlc.account_ui import team_navigation
st.session_state.setdefault('current_project', 'existing-project')
team_navigation()
if st.session_state.get('show_team_management'):
    st.title('团队管理内容')
else:
    st.title('项目工作台')
''').run()
    assert not app.toggle
    app.button(key='open_team_management').click().run()
    assert app.title[0].value == '团队管理内容'
    app.button(key='close_team_management').click().run()
    assert app.title[0].value == '项目工作台'
    assert app.session_state['current_project'] == 'existing-project'


def test_work_mode_picker_has_docked_profile_and_no_separate_account_actions(context):
    store, token = context
    app = AppTest.from_string(f'''
from unittest.mock import patch
from sdlc.account_ui import enter
from sdlc.accounts import COOKIE
with patch('sdlc.account_ui.st.context') as browser_context:
    browser_context.cookies = {{COOKIE: {token!r}}}
    enter({str(store.path)!r})
''', default_timeout=10).run()
    assert not app.exception
    assert app.title[0].value == '选择工作方式'
    assert app.sidebar.button(key='switch_workspace')
    labels = [item.proto.label for item in app.sidebar.get('link_button')]
    assert labels == []
    assert not app.get('html')
    dock = [item.value for item in app.sidebar.markdown if 'tc-sidebar-footer' in item.value]
    assert len(dock) == 1 and '查看个人资料' in dock[0]
    assert 'action="/auth/logout"' not in dock[0]
    assert not any(widget.key == 'page' for widget in app.radio)


def test_team_grid_paginates_without_losing_entry_access(context):
    store, token = context
    for index in range(5):
        store.create_team(token, f'团队 {index + 1}')
    app = picker(store, token)
    assert len([b for b in app.button if (b.key or '').startswith('space_')]) == 4
    assert app.button(key='teams_previous').disabled
    app.button(key='teams_next').click().run()
    assert not app.exception
    assert len([b for b in app.button if (b.key or '').startswith('space_')]) == 1
    assert app.button(key='teams_next').disabled
    button(app, '进入团队').click().run()
    assert not app.exception
    assert store.preferred_space(token) == app.session_state['active_workspace']

"""Account/workspace navigation, separate from extracted PRD business roles."""
import streamlit as st

from sdlc.accounts import Accounts, AccessError, COOKIE, ROLE_NAMES, ROLES, OVERRIDES, logout_proof
from sdlc.web_ui import user_identity
from sdlc.ui import page_header, section_title, workspace_mode

PERMISSION_NAMES = {'generate': '创建项目与生成', 'edit': '人工编辑', 'review': '审核与澄清',
                    'regenerate': '退回用例重新生成', 'export': '批准导出与下载'}


def logout_control(token):
    # Native same-tab POST: one click, no intermediate page and no JavaScript credential access.
    proof = logout_proof(token)
    st.html('<form method="post" action="/auth/logout" target="_self">'
            '<input type="hidden" name="logout_proof" value="' + proof + '">'
            '<button type="submit" class="tc-logout-button">退出登录</button></form>')


def reset_space():
    # Keep drafts per space; discard all review selections, rendered data and download bytes.
    drafts = st.session_state.setdefault('_space_drafts', {})
    old = st.session_state.get('_rendered_workspace')
    project = st.session_state.get('current_project')
    if old:
        project_drafts = st.session_state.get('_project_drafts', {})
        if project:
            project_drafts.setdefault(project, {}).update({k: st.session_state[k] for k in
                ('prd_text', 'parent_run_id', 'input_source') if k in st.session_state})
        drafts[old] = {'_project_drafts': project_drafts, 'current_project': project}
    keep = {k: st.session_state[k] for k in ('auth_user', 'active_workspace', '_space_drafts', '_workspace_initialized') if k in st.session_state}
    target = st.session_state.get('active_workspace')
    st.session_state.clear()
    st.session_state.update(keep)
    if target and target in drafts:
        st.session_state.update(drafts[target])


def enter(path):
    store = Accounts(path)
    token = st.context.cookies.get(COOKIE)
    try:
        user = store.me(token)
    except AccessError:
        st.session_state.clear()
        page_header('欢迎使用 TraceCase', '请先登录，再进入自己的个人空间或团队。', 'workspace', '账号')
        st.html('<a href="/auth/login" target="_self">登录 / 注册</a>')
        st.stop()
    if st.session_state.get('auth_user') != user['id']:
        st.session_state.clear()
        st.session_state['auth_user'] = user['id']
    with st.sidebar:
        st.markdown(user_identity(user), unsafe_allow_html=True)
    spaces = store.spaces(token)
    ids = {s['id'] for s in spaces}
    if not st.session_state.get('_workspace_initialized'):
        st.session_state['_workspace_initialized'] = True
        preferred = store.preferred_space(token)
        if preferred in ids:
            st.session_state['active_workspace'] = preferred
    current = st.session_state.get('active_workspace')
    if current not in ids:
        st.session_state.pop('active_workspace', None)
        current = None
    if current:
        chosen = next(s for s in spaces if s['id'] == current)
        st.session_state['_rendered_workspace'] = current
        st.session_state['_space_archived'] = bool(chosen['archived'])
        with st.sidebar:
            workspace_mode(chosen, sidebar=True)
            if chosen['kind'] == 'team':
                st.caption('当前角色：' + ROLE_NAMES[chosen['role']])
        workspace_mode(chosen)
        if chosen['kind'] == 'team' and st.session_state.get('show_team_management'):
            with st.sidebar:
                st.divider()
                space_navigation(user, token, chosen)
            # Retain navigation widgets while the management page hides project controls.
            for key in ('current_project', 'page'):
                if key in st.session_state:
                    st.session_state[key] = st.session_state[key]
            team_management(store, token, chosen)
            st.stop()
        return token, current, user
    with st.sidebar:
        st.caption(user['display_name'])
        st.divider()
        space_navigation(user, token)
    workspace_picker(store, token, spaces)
    for item in store.applications(token):
        status = {'pending': '待管理员审核', 'approved': '已批准，可进入团队', 'rejected': '已拒绝'}[item['status']]
        st.info(item['name'] + ' · ' + status + (' · ' + item['reason'] if item['reason'] else ''))
    st.caption('升级前尚无账号归属的项目由本机维护者明确分配，不会自动归入新注册账号。')
    st.stop()


def space_navigation(user, token, chosen=None):
    st.caption('团队功能' if chosen and chosen['kind'] == 'team' else '空间功能')
    with st.container(key='workspace_navigation'):
        if st.button('切换个人 / 团队空间', key='switch_workspace', width='stretch',
                     type='secondary' if chosen else 'primary'):
            st.session_state.pop('active_workspace', None)
            reset_space()
            st.rerun()
        if chosen and chosen['kind'] == 'team':
            team_navigation()
        st.link_button('个人资料', '/auth/profile', width='stretch')
        st.link_button('登录账号与绑定', '/auth/account', width='stretch')
        if user.get('password_login', True):
            st.link_button('修改密码', '/auth/password', width='stretch')
        logout_control(token)


def set_team_management(enabled):
    st.session_state['show_team_management'] = enabled


def team_navigation():
    active = st.session_state.get('show_team_management', False)
    st.button('团队管理', key='open_team_management', width='stretch',
              type='primary' if active else 'secondary', on_click=set_team_management, args=(True,))
    if active:
        st.button('返回项目工作台', key='close_team_management', width='stretch',
                  on_click=set_team_management, args=(False,))


def select_workspace(store, token, workspace_id):
    store.choose_space(token, workspace_id)
    st.session_state['active_workspace'] = workspace_id
    reset_space()
    st.rerun()


def team_entry_form(store, token, mode):
    if mode == 'create':
        with st.form('create_team'):
            name = st.text_input('团队名称', max_chars=80)
            description = st.text_area('团队简介（可选）', max_chars=500)
            if st.form_submit_button('创建团队并进入', type='primary'):
                try:
                    workspace = store.create_team(token, name, description)
                    select_workspace(store, token, workspace)
                except AccessError as exc:
                    st.error(str(exc))
    else:
        code = st.text_input('邀请码', max_chars=128, type='password')
        note = st.text_input('申请说明（可选）', max_chars=1000)
        if st.button('核对邀请的团队'):
            try:
                result = store.request_join(token, code)
                st.session_state['join_preview'] = {'code': code, **result}
            except AccessError as exc:
                st.error(str(exc))
        preview = st.session_state.get('join_preview')
        if preview and preview['code'] == code:
            st.info('申请加入：' + preview['name'] + '。管理员审核通过后才可访问团队数据。')
            if st.button('确认提交加入申请'):
                try:
                    store.request_join(token, code, note, confirm=True)
                    st.success('申请已提交，请等待管理员审核。')
                except AccessError as exc:
                    st.error(str(exc))


def close_team_entry():
    st.session_state.pop('_team_entry', None)
    st.session_state.pop('join_preview', None)


@st.dialog('团队协作', on_dismiss=close_team_entry)
def team_entry_dialog(store, token, mode):
    section_title('创建团队' if mode == 'create' else '加入其他团队',
                  '创建后你将成为团队所有者。' if mode == 'create' else '输入邀请码，核对团队后提交申请，等待管理员审核。', 'analysis')
    team_entry_form(store, token, mode)


def personal_entry(store, token, spaces):
    with st.container(border=True, key='workspace_personal'):
        section_title('个人工作', '生成 → 逐条审核 → 定向修订 → 批准导出', 'review')
        if st.button('进入我的个人空间', type='primary', width='stretch'):
            select_workspace(store, token, next(s['id'] for s in spaces if s['kind'] == 'personal'))


def workspace_picker(store, token, spaces):
    page_header('选择工作方式', '个人工作独立保存；团队协作共享项目，由管理员分配权限。', 'workspace', '工作空间')
    teams = [space for space in spaces if space['kind'] == 'team']
    if not teams:
        team_entry, personal = st.columns([1.618, 1], gap='large')
        with team_entry, st.container(border=True, key='workspace_team_empty'):
            section_title('创建或加入一个团队', '你还没有加入团队。创建团队，或通过邀请码申请加入。', 'analysis')
            choice = st.radio('团队入口', ['创建团队', '加入团队'], horizontal=True)
            team_entry_form(store, token, 'create' if choice == '创建团队' else 'join')
        with personal:
            personal_entry(store, token, spaces)
        return
    team_list, actions = st.columns([1.618, 1], gap='large')
    with team_list:
        section_title('我的团队', f'共 {len(teams)} 个团队，选择团队继续协作。', 'analysis')
        for space in teams:
            with st.container(border=True, key='workspace_team_card_' + space['id']):
                st.text(space['name'])
                st.caption(ROLE_NAMES[space['role']] + (' · 已归档，仅可查阅' if space['archived'] else ' · 协作中'))
                if space['description']:
                    st.text(space['description'])
                if st.button('进入团队', key='space_' + space['id']):
                    select_workspace(store, token, space['id'])
    with actions:
        personal_entry(store, token, spaces)
        with st.container(border=True, key='workspace_team_actions'):
            section_title('更多团队', '创建新团队，或申请加入其他团队。', 'analysis')
            if st.button('创建团队', key='open_create_team', width='stretch'):
                close_team_entry()
                st.session_state['_team_entry'] = 'create'
            if st.button('加入其他团队', key='open_join_team', width='stretch'):
                close_team_entry()
                st.session_state['_team_entry'] = 'join'
    mode = st.session_state.get('_team_entry')
    if mode in {'create', 'join'}:
        team_entry_dialog(store, token, mode)


def team_management(store, token, space):
    page_header('团队管理', space['name'], 'workspace', '团队')
    actor = store.access(token, space['id'])
    if space['archived']:
        st.info('团队已归档，历史项目仍可查看；团队管理已停止。')
        return
    if 'manage' not in actor['permissions']:
        st.info('你可以查看团队工作；邀请与成员权限由管理员管理。')
        if st.button('退出此团队'):
            store.leave(token, space['id'])
            st.session_state.pop('active_workspace', None)
            reset_space()
            st.rerun()
        return
    data = store.manage_data(token, space['id'])
    requests, members, invites, settings = st.tabs(['加入申请', '成员与权限', '邀请码', '团队设置'])
    with requests:
        pending = [r for r in data['requests'] if r['status'] == 'pending']
        if not pending:
            st.info('暂无待审核加入申请。')
        for row in pending:
            with st.container(border=True):
                st.text(row['display_name'] + '（' + row['username'] + '）')
                st.text(row['note'] or '未填写申请说明')
                options = ['admin', 'editor', 'reviewer', 'observer'] if actor['role'] == 'owner' else ['editor', 'reviewer', 'observer']
                role = st.selectbox('批准后的角色', options, index=len(options)-1, format_func=ROLE_NAMES.get, key='joinrole_' + row['id'])
                reason = st.text_input('拒绝原因', key='joinreason_' + row['id'], max_chars=1000)
                a, b = st.columns(2)
                if a.button('批准加入', key='approvejoin_' + row['id']):
                    store.decide_join(token, space['id'], row['id'], True, role)
                    st.rerun()
                if b.button('拒绝申请', key='rejectjoin_' + row['id']):
                    store.decide_join(token, space['id'], row['id'], False, role, reason)
                    st.rerun()
    with members:
        for member in data['members']:
            with st.container(border=True):
                st.text(member['display_name'] + ' · ' + ROLE_NAMES[member['role']])
                if member['role'] == 'owner' or member['user_id'] == actor['id'] or (member['role'] == 'admin' and actor['role'] != 'owner'):
                    continue
                with st.form('member_' + member['user_id']):
                    options = ['admin', 'editor', 'reviewer', 'observer'] if actor['role'] == 'owner' else ['editor', 'reviewer', 'observer']
                    role = st.selectbox('预设角色', options, index=options.index(member['role']), format_func=ROLE_NAMES.get)
                    st.caption('保存角色时重置为该角色的默认权限；需要细调时在下方展开设置。')
                    custom = st.checkbox('自定义业务权限')
                    overrides = {}
                    import json
                    existing = json.loads(member['overrides_json'])
                    for permission in sorted(OVERRIDES):
                        enabled = st.checkbox(PERMISSION_NAMES[permission], value=existing.get(permission, permission in ROLES[member['role']]),
                                              disabled=permission not in actor['permissions'])
                        if custom:
                            overrides[permission] = enabled
                    remove = st.checkbox('移除此成员')
                    if st.form_submit_button('保存成员设置'):
                        store.set_member(token, space['id'], member['user_id'], role, overrides, remove)
                        st.rerun()
    with invites:
        uses = st.number_input('最多申请次数', min_value=1, max_value=1000, value=20)
        days = st.number_input('有效天数', min_value=1, max_value=30, value=7)
        if st.button('创建新的邀请码'):
            code = store.create_invite(token, space['id'], uses, days)
            st.code(code, language=None)
            st.warning('邀请码仅本次展示，请复制保存。使用后仍需管理员审核。')
        for invite in data['invites']:
            if st.button('重置此邀请码', key='reset_' + invite['id']):
                code = store.reset_invite(token, space['id'], invite['id'], uses, days)
                st.code(code, language=None)
                st.info('旧邀请码已停用，新邀请码仅本次展示。已提交的申请仍可正常审核。')
            st.caption(f"已使用 {invite['uses']} / {invite['max_uses']} · {'已停用' if invite['revoked'] else '有效期内可申请'}")
            if not invite['revoked'] and st.button('停用此邀请码', key='revoke_' + invite['id']):
                store.revoke_invite(token, space['id'], invite['id'])
                st.rerun()
    with settings:
        with st.form('team_profile'):
            name = st.text_input('团队名称', value=space['name'], max_chars=80)
            description = st.text_area('团队简介', value=space['description'], max_chars=500)
            if st.form_submit_button('保存团队资料'):
                store.update_team(token, space['id'], name, description)
                st.rerun()
        if actor['role'] == 'owner':
            targets = [m for m in data['members'] if m['user_id'] != actor['id']]
            if targets:
                target = st.selectbox('转移所有权给', [m['user_id'] for m in targets], format_func=lambda v: next(m['display_name'] for m in targets if m['user_id'] == v))
                confirm = st.checkbox('确认转移最高管理权限，我将成为管理员')
                if st.button('转移所有权', disabled=not confirm):
                    store.transfer(token, space['id'], target)
                    st.rerun()
            if st.checkbox('确认归档团队，保留历史并停止写入') and st.button('归档此团队'):
                store.archive_team(token, space['id'])
                st.rerun()
        elif st.button('退出团队'):
            store.leave(token, space['id'])
            st.session_state.pop('active_workspace', None)
            reset_space()
            st.rerun()


def download_link(service, exported, label):
    # IDs identify resources, never authenticate. Every click is authorized again by the gateway.
    st.link_button(label, '/auth/download/' + exported['id'] + '?workspace=' + service.workspace_id + '&project=' + service.project_id)

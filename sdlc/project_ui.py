"""Project workspace views; mutations remain guarded by the application service."""
import streamlit as st

from sdlc.service import ServiceError
from sdlc.ui import format_time, page_header, section_title


DRAFT_FIELDS = ('prd_text', 'parent_run_id', 'input_source', 'selected_run')


def project_changed():
    old = st.session_state.get('_active_project')
    target = st.session_state.get('current_project')
    drafts = st.session_state.setdefault('_project_drafts', {})
    if old:
        drafts.setdefault(old, {}).update({k: st.session_state[k] for k in DRAFT_FIELDS if k in st.session_state})
    for key in DRAFT_FIELDS:
        st.session_state.pop(key, None)
    for key in ('ready_export', 'input_preview', '_review_next', '_detail_target', '_expand_edit',
                '_selected_run', '_draft', '_parent', '_navigate_to', '_notice', 'model_confirm', '_project_ready_export'):
        st.session_state.pop(key, None)
    if target:
        st.session_state.update(drafts.get(target, {}))
    st.session_state['_active_project'] = target
    st.session_state['page'] = '项目概览' if target else '项目列表'


def upload_changed(project_id):
    uploaded = st.session_state.get(f'prd_file_{project_id}')
    draft = st.session_state.setdefault('_project_drafts', {}).setdefault(project_id, {})
    if uploaded is None:
        draft.pop('upload', None)
    else:
        draft['upload'] = {'filename': uploaded.name, 'data': uploaded.getvalue()}


def remember_draft():
    project_id = st.session_state.get('current_project')
    if project_id:
        drafts = st.session_state.setdefault('_project_drafts', {})
        drafts.setdefault(project_id, {}).update({k: st.session_state[k] for k in DRAFT_FIELDS if k in st.session_state})


def open_project(project_id):
    st.session_state['_switch_project'] = project_id
    st.rerun()


def projects_view(admin, operator):
    page_header('项目列表', '按项目组织需求、用例与审核记录，从这里开始新的测试工作。', 'workspace', '项目管理')
    section_title('创建项目', '为一组独立需求建立专属工作空间。', 'workspace')
    if not operator.strip():
        st.caption('请在左上角导航菜单填写操作者标识，之后即可创建或修改项目。')
    with st.form('create_project'):
        name = st.text_input('项目名称', key='new_project_name', max_chars=80)
        description = st.text_area('项目简介', key='new_project_description', max_chars=500)
        submitted = st.form_submit_button('新建项目', disabled=not operator.strip(), type='primary')
    if submitted:
        try:
            project_id = admin.create_project(name, description, operator)
            open_project(project_id)
        except (ServiceError, ValueError) as error:
            st.error(str(error))
    section_title('已有项目', '进入项目查看生成进度、用例和批准快照。', 'workspace')
    page = st.number_input('项目列表页码', min_value=1, value=1, step=1, key='projects_page')
    projects = admin.list_projects(limit=25, offset=(page - 1) * 25)
    st.caption('每页最多 25 个项目；归档项目也保留在列表中。')
    if not projects:
        st.info('还没有项目。填写操作者标识并新建项目，即可生成和管理该项目的测试用例。')
        return
    for p in projects:
        with st.container(border=True):
            st.text(p['name'])
            st.text(f"{'已归档' if p['status'] == 'archived' else '进行中'} · {p['run_count']} 个批次")
            st.text(p['description'])
            if st.button('进入项目', key=f"enter_project_{p['id']}"):
                open_project(p['id'])
    with st.expander('项目设置与归档', expanded=False):
        selected = st.selectbox('管理项目', [p['id'] for p in projects],
                                format_func=lambda pid: next(p['name'] for p in projects if p['id'] == pid),
                                key='manage_project')
        project = admin.get_project(selected)
        if project['status'] == 'active':
            with st.form(f"edit_project_{selected}_{project['revision']}"):
                name = st.text_input('修改项目名称', value=project['name'], max_chars=80)
                description = st.text_area('修改项目简介', value=project['description'], max_chars=500)
                submitted = st.form_submit_button('保存项目信息', disabled=not operator.strip())
            if submitted:
                try:
                    admin.update_project(selected, name, description, operator, project['revision'])
                    st.session_state['_notice'] = '项目信息已保存，历史快照内容不变。'
                    st.rerun()
                except (ServiceError, ValueError) as error:
                    st.error(str(error))
        if st.button('恢复项目' if project['status'] == 'archived' else '归档项目', key='archive_project',
                     disabled=not operator.strip()):
            try:
                admin.set_project_archived(selected, project['status'] != 'archived', operator, project['revision'])
                st.session_state['_notice'] = '项目状态已更新。归档项目可查阅和下载已有快照。'
                st.rerun()
            except (ServiceError, ValueError) as error:
                st.error(str(error))
        with st.expander('项目维护记录'):
            st.json(project['events'])


def overview(service, go_to):
    project = service.get_project()
    page_header('项目概览', project['description'] or '查看当前项目的测试准备与审核进度。', 'workspace', project['name'])
    stats = service.project_stats()
    columns = st.columns(4)
    for col, label, value in zip(columns, ('生成批次', '待审核', '已退回', '失败或校验未通过'),
                                  (stats['runs'], stats['pending'], stats['returned'], stats['failed']), strict=True):
        col.metric(label, value)
    st.caption('统计各批次的当前用例版本，表示工作量，不代表项目整体审批结论。')
    if project['status'] == 'active' and st.button('为此项目生成用例', key='project_generate', type='primary'):
        go_to('新建运行')
    latest = service.list_run_summaries(limit=5)
    valid = service.list_run_summaries(limit=1, valid_only=True)
    if latest and valid and latest[0]['id'] != valid[0]['id']:
        st.warning('最近批次尚未校验通过；项目用例默认展示较早的有效批次，请同时检查最近批次状态。')
    section_title('最近的生成批次', '继续审核，或回看需求分析结果。', 'review')
    for r in latest:
        with st.container(border=True):
            st.text(format_time(r['created_at']))
            status = {'valid': '可继续审核', 'invalid': '结果待修复', 'failed': '生成失败', 'generating': '正在生成'}.get(r['status'], r['status'])
            st.caption(f"{status} · {r['case_count']} 条用例")
            if st.button('查看此批次', key=f"open_recent_{r['id']}"):
                go_to('运行详情与审核', r['id'])
    if not latest:
        st.info('本项目还没有生成批次。下一步：点击“为此项目生成用例”，输入本项目的 PRD。')


def project_cases(service, go_to):
    page_header('项目测试用例', '按状态、优先级和分类筛选，定位需要处理的测试内容。', 'review', '用例库')
    recent = service.list_run_summaries(limit=100)
    valid = service.list_run_summaries(limit=1, valid_only=True)
    if not recent:
        st.info('本项目还没有用例，请先生成。')
        return
    if valid and valid[0]['id'] not in {r['id'] for r in recent}:
        recent.append(valid[0])
    preferred = valid[0]['id'] if valid else recent[0]['id']
    if recent[0]['id'] != preferred:
        st.warning('有更新的批次尚未通过校验。当前默认显示最近一个校验通过批次，请按需切换。')
    ids = [r['id'] for r in recent]
    selected = st.selectbox('用例所属批次', ids, index=ids.index(preferred),
                            format_func=lambda rid: next(f"{format_time(r['created_at'])} · {r['case_count']} 条用例" for r in recent if r['id'] == rid),
                            key=f'cases_run_{service.project_id}')
    run = service.get_run(selected)
    status = st.multiselect('审核状态', ['pending_review', 'approved', 'returned'],
                            default=['pending_review', 'approved', 'returned'],
                            format_func=lambda x: {'pending_review': '待审核', 'approved': '已通过', 'returned': '已退回'}[x])
    priorities = st.multiselect('优先级', ['P0', 'P1', 'P2'], default=['P0', 'P1', 'P2'])
    category_options = sorted({c['content']['category'] for c in run['cases']})
    categories = st.multiselect('用例分类', category_options, default=category_options,
                                format_func=lambda x: {'normal': '正常', 'boundary': '边界', 'exception': '异常', 'security': '安全'}.get(x, x))
    filtered = [c for c in run['cases'] if c['status'] in status and c['content']['priority'] in priorities
                and c['content']['category'] in categories]
    st.caption(f"筛选后 {len(filtered)} / {len(run['cases'])} 条 · 仅展示所选批次")
    for c in filtered:
        with st.container(border=True):
            st.text(f"{c['case_id']} · v{c['version']} · {c['content']['title']}")
            st.caption(f"{ {'pending_review': '待审核', 'approved': '已通过', 'returned': '已退回'}[c['status']]} · {c['content']['priority']} · {c['content']['category']}")
            if st.button('查看与审核此用例', key=f"open_case_{selected}_{c['case_id']}"):
                st.session_state['_review_next'] = {'run_id': selected, 'case_id': c['case_id']}
                st.session_state['_detail_target'] = {'run_id': selected, 'tab': '用例审核'}
                go_to('运行详情与审核', selected)
    if not filtered:
        st.info('没有匹配的当前用例。若批次校验失败，请打开批次详情查看原因。')
    if st.button('查看所选批次详情', key='cases_run_detail'):
        go_to('运行详情与审核', selected)


def export_history(service):
    page_header('项目导出记录', '下载已保存的批准快照，历史文件不会被后续修改覆盖。', 'export', '批准交付 / 04')
    page = st.number_input('导出记录页码', min_value=1, value=1, step=1, key=f'export_page_{service.project_id}')
    exports = service.list_exports(limit=25, offset=(page - 1) * 25)
    st.caption('每页 25 条，仅列出当前项目的历史批准快照。')
    if not exports:
        st.info('本页没有导出记录。完成某一批次审核后，可在该批次的导出页生成批准快照。')
    for item in exports:
        with st.container(border=True):
            st.text(f"{format_time(item['created_at'])} · {item['filename']} · {item['project_name'] or '迁移前历史快照'}")
            ready = st.session_state.get('_project_ready_export')
            data = ready if ready and ready['project_id'] == service.project_id and ready['id'] == item['id'] else None
            if st.button('准备下载', key=f"project_export_{item['id']}"):
                data = service.get_export(item['id'])
                st.session_state['_project_ready_export'] = data
            if data:
                st.download_button('下载此快照', data['content'], file_name=data['filename'],
                                   mime=data['mime'], key=f"download_project_{item['id']}")

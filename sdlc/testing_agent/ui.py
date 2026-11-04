"""Task-first testing workspace. Public actions remain explicit and authorized."""
from __future__ import annotations

import json
from uuid import uuid4

import streamlit as st

from sdlc.account_ui import download_link
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent import worker
from sdlc.ui import page_header, section_title


STATES = {'queued': '等待处理', 'running': '处理中', 'waiting_answer': '等待答复',
          'waiting_review': '等待评审', 'partial': '部分完成', 'completed': '准备完成',
          'failed': '可恢复失败', 'cancelled': '已取消'}
STAGES = {'analysis': '需求分析', 'planning': '覆盖规划', 'generation': '分批生成',
          'review': '人工评审', 'delivery': '批准交付'}
FEEDBACK = {'步骤不清': 'unclear_steps', '预期不可判断': 'unclear_expected', '遗漏边界': 'missing_boundary',
            '遗漏权限': 'missing_permission', '遗漏状态': 'missing_state', '无依据规则': 'unsupported_rule',
            '重复用例': 'duplicate', '需求已经变化': 'requirement_changed', '表达偏好': 'style'}
RESOURCE_KINDS = {'precondition': '共享前置条件', 'steps': '共享步骤', 'test_data': '测试数据准备'}


def testing_service(legacy):
    return TestingService(legacy.db_path, legacy.token, legacy.workspace_id, legacy.project_id)


def notice_action(action):
    try:
        result = action()
        st.session_state['_agent_notice'] = '已保存，相关历史和版本均已保留。'
        return result
    except (ValueError, RuntimeError) as exc:
        st.error(str(exc))
        return None


@st.fragment(run_every='3s')
def progress(service, task_id):
    task = service.get_task(task_id)
    jobs = worker.list_work(service, task_id)
    stage = STAGES.get(task['stage'], task['stage'])
    active = any(j['status'] in {'queued', 'running'} for j in jobs)
    st.caption(('◌ ' if active else '● ') + STATES.get(task['status'], task['status']) + ' · ' + stage)
    if active:
        st.info('Agent 正在处理。可以离开此页，稍后从当前任务继续。')
    for job in jobs[:1]:
        if job.get('error'):
            st.warning(job['error'])
    signature = (task['status'], task['stage'], len(task['artifacts']), tuple((j['id'], j['status']) for j in jobs))
    key = '_agent_progress_' + task_id
    previous = st.session_state.get(key)
    st.session_state[key] = signature
    if previous is not None and previous != signature:
        st.rerun(scope='app')


def new_task_form(service, tasks):
    from sdlc.testing_agent.resources import ResourceLibrary
    resources = ResourceLibrary(service.db_path, service.token, service.workspace_id, service.project_id).list()
    with st.form('agent_new_task_' + service.project_id, clear_on_submit=False):
        section_title('新建测试任务', '输入需求即可开始；目标、范围和协作者可按需要补充。', 'input')
        text = st.text_area('需求正文（Markdown）', height=230, key='agent_prd_text_' + service.project_id)
        uploaded = st.file_uploader('或上传需求文档', type=['md', 'markdown'], key='agent_prd_file_' + service.project_id)
        goal = st.text_input('测试目标（可选）', placeholder='例如：重点验证审批权限、金额边界和重复提交')
        scope = st.text_input('本次范围（可选）', placeholder='默认处理本次需求的全部明确内容')
        parents = [None] + [t['id'] for t in tasks]
        parent = st.selectbox('基于已有任务分析需求变更（可选）', parents,
            format_func=lambda x: '独立新需求' if x is None else next(t['goal'][:50] + f" · 需求 v{t['document_version']}" for t in tasks if t['id'] == x))
        with st.expander('复用共享测试资料（可选）'):
            resource_policy = st.selectbox('资源选用方式', ['未新选资源时沿用原任务固定版本', '只使用本次选择，可以为空'])
            resource_ids = st.multiselect('复用项目共享资源（可选）', [r['id'] for r in resources],
                format_func=lambda rid: next(RESOURCE_KINDS[r['kind']] + ' · ' + r['title'] + f" · 第 {r['revision']} 版" for r in resources if r['id'] == rid))
            st.caption('本次选用的资源保存固定版本；资源后续修改不会改动已有任务。')
        with st.expander('协作与截止时间（可选）'):
            with service.connection() as db:
                members = [dict(r) for r in db.execute('SELECT u.id,u.display_name,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?', (service.workspace_id,))]
            owner = st.selectbox('任务负责人', [None] + [m['id'] for m in members],
                format_func=lambda uid: '当前登录用户' if uid is None else next(m['display_name'] for m in members if m['id'] == uid))
            confirmer = st.selectbox('需求确认人', [None] + [m['id'] for m in members if m['role'] in {'owner', 'admin'}],
                format_func=lambda uid: '空间管理者' if uid is None else next(m['display_name'] for m in members if m['id'] == uid))
            due = st.text_input('截止日期（YYYY-MM-DD，可选）')
        st.caption('需求将发送至维护者配置的模型服务。请使用已授权、脱敏的资料。')
        submitted = st.form_submit_button('开始测试准备', type='primary', width='stretch')
    if submitted:
        def create():
            source = text
            if uploaded:
                if text.strip():
                    raise ValueError('粘贴正文与上传文档请选择一种。')
                if uploaded.size > 102400:
                    raise ValueError('文件超过 100 KiB。')
                source = uploaded.getvalue().decode('utf-8')
            if due:
                from datetime import date
                date.fromisoformat(due)
            selected_resources = resource_ids if resource_ids or resource_policy.startswith('只使用') else None
            task_id = service.create_task(source, goal, scope, parent, owner_id=owner, confirmer_id=confirmer, due_at=due or None, resource_ids=selected_resources)
            worker.enqueue(service, task_id, request_key=uuid4().hex)
            return task_id
        task_id = notice_action(create)
        if task_id:
            st.session_state['_chat_next_task_' + service.project_id] = task_id
            st.session_state['_agent_create'] = False
            st.rerun()


def evidence(task, paragraph_ids):
    for paragraph in task['input']['paragraphs']:
        if paragraph['id'] in paragraph_ids:
            st.caption(paragraph['id'] + f" · 原文 {paragraph['start_line']}–{paragraph['end_line']} 行")
            st.text(paragraph['text'])


def requirements_view(service, task):
    bound = service.artifact(task['id'], 'project_resources')
    if bound and bound['resources']:
        with st.expander('本任务选用的共享准备资料'):
            st.caption(bound['notice'])
            for resource in bound['resources']:
                st.markdown('**' + resource['title'] + f" · 第 {resource['revision']} 版**")
                st.text(resource['body'])
    analysis = service.artifact(task['id'], 'analysis')
    if analysis:
        for kind, label in [('features', '功能'), ('roles', '角色'), ('business_rules', '业务规则')]:
            with st.expander(label, expanded=kind == 'business_rules'):
                for item in analysis.get(kind, []):
                    st.markdown('**' + item['id'] + '**')
                    st.text(item.get('statement', item.get('description', item.get('name', ''))))
                    evidence(task, item.get('paragraph_ids', []))
        for risk in analysis.get('risks', []):
            st.warning('待核对风险：' + risk)
    else:
        st.info('需求分析成果会在此处出现。')
    section_title('待确认问题', '答复经过确认后形成需求补充，并继续处理受影响内容。', 'clarify')
    for question in task['questions']:
        q, qid = question['content'], question['id']
        with st.container(border=True):
            st.markdown('**' + q['question'] + '**')
            st.caption(('影响执行或判断' if q['blocking'] else '补充核对项') + ' · ' + question['status'])
            with st.expander('查看问题依据'):
                evidence(task, q['paragraph_ids'])
                st.caption('相关规则：' + '、'.join(q['requirement_ids']))
            if question.get('answer'):
                st.text('当前答复：' + question['answer']['text'])
            if question['status'] != 'confirmed' and task['status'] not in {'completed', 'cancelled'}:
                with st.form('agent_answer_' + task['id'] + qid):
                    answer = st.text_area('答复或原文依据', key='agent_answer_text_' + task['id'] + qid)
                    disposition = st.selectbox('处理方式', ['answer', 'unknown', 'needs_confirmation', 'false_positive'],
                        format_func=lambda x: {'answer': '提供答案，待确认', 'unknown': '暂时不知道', 'needs_confirmation': '需要协作者确认', 'false_positive': '原文已有规定，指出误报'}[x])
                    sent = st.form_submit_button('提交答复')
                if sent:
                    result = notice_action(lambda: service.submit_answer(task['id'], qid, answer, disposition, expected_revision=question['revision']))
                    if result is not None:
                        st.rerun()
                if question['status'] == 'answered' and question.get('answer', {}).get('disposition') == 'answer' and 'manage' in service.legacy.user['permissions']:
                    reason = st.text_input('需求确认说明', key='confirm_reason_' + task['id'] + qid)
                    if st.button('确认补充并继续相关测试', key='confirm_' + task['id'] + qid):
                        result = notice_action(lambda: service.confirm_answers_and_continue(task['id'],
                            {qid: question['revision']}, reason))
                        if result:
                            st.session_state['_chat_next_task_' + service.project_id] = result['task_id']
                            st.rerun()
                if question['status'] == 'answered' and question.get('answer', {}).get('disposition') == 'false_positive':
                    sources = st.multiselect('支持误报判断的原文段落', q['paragraph_ids'], key='false_positive_sources_' + task['id'] + qid)
                    if st.button('按已有原文处置误报', key='false_positive_' + task['id'] + qid):
                        def resolve():
                            service.resolve_question(task['id'], qid, question['answer']['text'], sources)
                            return True
                        if notice_action(resolve):
                            st.rerun()


def coverage_view(service, task):
    plan = service.artifact(task['id'], 'coverage_plan')
    report = service.coverage(task['id'])
    st.caption('覆盖统计基于已识别需求与测试条件；需要人工核对是否完整理解了原文。')
    if plan:
        st.dataframe([{'模块': c['module'], '条件': c['description'], '规则': '、'.join(c['requirement_ids']),
                       '方法': c['method'], '优先级': c['priority'], '状态': c['status']} for c in plan['conditions']], hide_index=True, width='stretch')
    else:
        st.info('覆盖计划尚未生成。')
    if report.get('missing_condition_ids') or report.get('missing_rule_ids'):
        st.warning('尚有覆盖缺口：' + '、'.join(report.get('missing_condition_ids', []) + report.get('missing_rule_ids', [])))
    if plan and 'manage' in service.legacy.user['permissions'] and task['status'] not in {'completed', 'cancelled'}:
        with st.expander('确认调整本次测试范围'):
            ids = st.multiselect('排除的测试条件', [c['id'] for c in plan['conditions']], key='exclude_conditions_' + task['id'])
            excluded_rules = st.multiselect('排除的需求（包括尚未设计的范围外条目）',
                [r['id'] for r in (service.artifact(task['id'], 'analysis') or {}).get('business_rules', [])], key='exclude_rules_' + task['id'])
            reason = st.text_area('排除理由与影响说明', key='exclude_reason_' + task['id'])
            st.caption('将创建关联的新范围任务；原有退回内容与历史记录保留。')
            if st.button('确认范围调整', key='exclude_scope_' + task['id']):
                child = notice_action(lambda: service.exclude_condition(task['id'], ids, reason, excluded_rules))
                if child:
                    st.session_state['_chat_next_task_' + service.project_id] = child
                    st.rerun()
    suggestions = service.reuse_suggestions(task['id'])
    with st.expander('项目已有用例与复用建议'):
        st.json(suggestions)
    with st.expander('导入已有用例 CSV'):
        imported = st.file_uploader('选择 CSV', type=['csv'], key='agent_import_' + task['id'])
        st.caption('将现有列映射为标题、前置条件、步骤、预期。导入内容作为待核对资料，不自动批准。')
        mapping = {}
        if imported:
            import csv
            import io
            try:
                columns = next(csv.reader(io.StringIO(imported.getvalue().decode('utf-8-sig'))))
                if len(columns) != len(set(columns)) or not columns:
                    raise ValueError('CSV 列名不能重复或为空。')
                for field, label in [('title', '标题列'), ('preconditions', '前置条件列'), ('steps', '步骤列'), ('expected_results', '预期结果列')]:
                    mapping[field] = st.selectbox(label, columns, index=columns.index(field) if field in columns else 0,
                        key='csv_' + field + '_' + task['id'])
            except (UnicodeDecodeError, StopIteration, ValueError) as exc:
                st.error('CSV 读取失败：' + str(exc))
            if mapping and st.button('校验并导入', key='import_' + task['id']):
                result = notice_action(lambda: service.import_cases_csv(task['id'], imported.getvalue(), mapping))
                if result:
                    st.success(f"已导入 {result['count']} 条未验证资料；继续任务时 Agent 可以参考，仍需关联依据和人工评审。")
    if task['parent_task_id']:
        with st.expander('需求差异与影响', expanded=True):
            impact = service.compare_tasks(task['parent_task_id'], task['id'])
            st.json(impact)
            st.caption('未发现直接关联不代表没有间接影响，请人工复核影响范围。')
            if task['run_id'] and 'review' in service.legacy.user['permissions']:
                old_task = service.get_task(task['parent_task_id'])
                if old_task['run_id']:
                    previous_cases = service.legacy.get_run(old_task['run_id'])['cases']
                    candidates = [c['case_id'] for c in previous_cases if c['status'] == 'approved']
                    if candidates:
                        chosen = st.selectbox('申请沿用批准的用例', candidates, key='carry_case_' + task['id'])
                        carry_reason = st.text_input('沿用判断依据', key='carry_reason_' + task['id'])
                        st.caption('服务端仍会检查内容、规则和来源是否等价；不确定间接影响时不要沿用。')
                        if st.button('核对并记录批准沿用', key='carry_' + task['id']):
                            if notice_action(lambda: service.carry_approval(task['id'], task['parent_task_id'], chosen, carry_reason)) is not None:
                                st.rerun()


def cases_view(service, task):
    drafts = service.case_drafts(task['id'])
    if not task['run_id']:
        st.info('正在按模块准备草稿；已有成果如下，汇总完成后可正式评审。')
        for draft in drafts:
            with st.expander(draft['case']['title']):
                st.text('\n'.join(draft['case']['expected_results']))
        return
    run = service.legacy.get_run(task['run_id'])
    cases = run['cases']
    if not cases:
        return
    ids = [c['case_id'] for c in cases]
    selector_key = 'agent_case_' + task['id']
    pending = st.session_state.pop('_agent_next_case_' + task['id'], None)
    if pending in ids:
        st.session_state[selector_key] = pending
    elif selector_key not in st.session_state:
        st.session_state[selector_key] = next((c['case_id'] for c in cases if c['status'] == 'pending_review'), ids[0])
    selected = st.selectbox('当前用例', ids, key=selector_key,
        format_func=lambda cid: next(c['content']['title'] + ' · ' + {'approved': '评审通过', 'pending_review': '待评审', 'returned': '已退回'}.get(c['status'], c['status']) for c in cases if c['case_id'] == cid))
    case = next(c for c in cases if c['case_id'] == selected)
    content = case['content']
    with st.container(border=True):
        st.subheader(content['title'])
        st.caption(f"{selected} · v{case['version']} · {content['priority']} · {content['category']} · 测试未执行")
        st.markdown('**前置条件**')
        st.text('\n'.join(content['preconditions']))
        st.markdown('**操作步骤**')
        st.text('\n'.join(f"{step['number']}. {step['action']}" for step in content['steps']))
        st.markdown('**预期结果**')
        st.text('\n'.join(content['expected_results']))
        draft = next((d for d in drafts if d['case']['case_id'] == selected), {})
        if draft.get('test_data'):
            st.markdown('**测试数据**')
            st.text('\n'.join(draft['test_data']))
        if draft.get('cleanup'):
            st.markdown('**清理与恢复**')
            st.text('\n'.join(draft['cleanup']))
        with st.expander('查看需求依据'):
            evidence(task, content['paragraph_ids'])
    permissions = service.legacy.user['permissions']
    if 'review' in permissions and case['status'] == 'pending_review':
        action = st.selectbox('评审决定', ['评审通过', '退回修改'], key='agent_review_action_' + task['id'] + selected)
        with st.form('agent_review_' + task['id'] + selected + str(case['version'])):
            if action == '退回修改':
                category = st.selectbox('退回分类', list(FEEDBACK))
                reason = st.text_area('退回意见（必填）')
            else:
                category = '步骤不清'
                reason = st.selectbox('通过说明', ['通过，无补充意见', '与需求一致', '步骤和预期清晰', '边界覆盖符合要求'])
            submitted = st.form_submit_button('提交并查看下一条', type='primary')
        if submitted:
            def review():
                if action == '评审通过':
                    service.legacy.review(task['run_id'], selected, case['version'], case['revision'], 'approve', '', reason or '与需求一致，无补充意见', uuid4().hex)
                else:
                    service.review_feedback(task['id'], selected, FEEDBACK[category], reason, case['version'], case['revision'])
                    if FEEDBACK[category] not in {'duplicate', 'requirement_changed'} and 'regenerate' in permissions:
                        try:
                            worker.enqueue(service, task['id'], 'message', '请处理该任务最新的用例反馈，调用 propose_revision 提出修订差异，不修改需求事实。')
                        except ValueError:
                            st.session_state['_agent_notice'] = '退回已保存。当前任务结束后，可继续处理修订。'
                return True
            if notice_action(review):
                remaining = [c['case_id'] for c in cases if c['case_id'] != selected and c['status'] == 'pending_review']
                returned = [c['case_id'] for c in cases if c['case_id'] != selected and c['status'] == 'returned']
                if remaining:
                    st.session_state['_agent_next_case_' + task['id']] = remaining[0]
                elif returned:
                    st.session_state['_agent_next_case_' + task['id']] = returned[0]
                st.session_state['_agent_target_tab_' + task['id']] = '用例评审' if remaining or returned or action == '退回修改' else '交付'
                st.rerun()
    for proposal in task['proposals']:
        if proposal['case_id'] != selected or proposal['status'] != 'pending':
            continue
        with st.expander('修订或处理建议 · ' + proposal['kind'], expanded=True):
            proposed = proposal['content']
            if 'case' in proposed:
                st.text(proposed.get('explanation', ''))
                import difflib
                st.code('\n'.join(difflib.unified_diff(json.dumps(content, ensure_ascii=False, indent=2).splitlines(),
                    json.dumps(proposed['case'], ensure_ascii=False, indent=2).splitlines(), fromfile='当前版本', tofile='修订提案', lineterm='')), language='diff')
                if 'regenerate' in permissions and st.button('采纳为新版本并重新评审', key='apply_' + proposal['id']):
                    if notice_action(lambda: service.apply_revision(task['id'], proposal['id'])):
                        st.rerun()
            else:
                st.text(proposed.get('reason', '请确认需求变更或合并范围。'))
                if proposal['kind'] == 'requirement_confirmation' and st.button('创建需求变更任务', key='new_requirement_' + proposal['id']):
                    st.session_state['_agent_create'] = True
                    st.session_state['agent_prd_text_' + service.project_id] = task['input']['normalized_text']
                    st.rerun()
                if proposal['kind'] == 'merge_proposal' and 'manage' in permissions:
                    decision = st.selectbox('处理重复用例', ['dismiss', 'merge', 'deactivate'], key='merge_decision_' + proposal['id'],
                        format_func=lambda x: {'dismiss': '保留，驳回合并建议', 'merge': '合并到另一用例', 'deactivate': '从新范围中停用'}[x])
                    targets = [c['case_id'] for c in cases if c['case_id'] != selected]
                    target = st.selectbox('合并目标', [None] + targets, key='merge_target_' + proposal['id'])
                    why = st.text_input('确认理由', key='merge_reason_' + proposal['id'])
                    if st.button('确认处理建议', key='resolve_' + proposal['id']):
                        child = notice_action(lambda: service.resolve_proposal(task['id'], proposal['id'], decision, why, target))
                        if child:
                            st.session_state['_chat_next_task_' + service.project_id] = child
                            st.rerun()
    if 'edit' in permissions and task['status'] not in {'completed', 'cancelled'}:
        with st.expander('人工修订当前用例'):
            with st.form('agent_manual_' + task['id'] + selected + str(case['version'])):
                title = st.text_input('用例标题', value=content['title'])
                conditions = st.text_area('前置条件（每行一项）', value='\n'.join(content['preconditions']))
                steps = st.text_area('操作步骤（每行一步）', value='\n'.join(s['action'] for s in content['steps']))
                expected = st.text_area('预期结果（每行一项）', value='\n'.join(content['expected_results']))
                reason = st.text_input('修订理由')
                saved = st.form_submit_button('保存新版本并重新评审')
            if saved:
                revised = {**content, 'title': title,
                    'preconditions': [v.strip() for v in conditions.splitlines() if v.strip()],
                    'steps': [{'number': i + 1, 'action': v} for i, v in enumerate([s.strip() for s in steps.splitlines() if s.strip()])],
                    'expected_results': [v.strip() for v in expected.splitlines() if v.strip()]}
                if notice_action(lambda: service.legacy.edit(task['run_id'], selected, case['version'], case['revision'], revised, '', reason, uuid4().hex)):
                    st.rerun()
    pending_count = sum(c['status'] == 'pending_review' for c in cases)
    returned_count = sum(c['status'] == 'returned' for c in cases)
    if pending_count == 0:
        if returned_count:
            st.warning('本轮评审完成，存在退回用例。请采纳修订提案，或确认需求变化后进入下一轮。')
        else:
            st.success('当前用例已完成设计评审。请到交付区域建立批准基线并导出；测试执行尚未开展。')


def delivery_view(service, task):
    st.info('这里交付的是测试设计成果。用例评审通过后，实际执行状态仍为“未执行”。')
    if task['run_id']:
        for issue in service.legacy.gate(task['run_id']):
            st.warning(issue)
        if 'export' in service.legacy.user['permissions']:
            if not task['baselines'] and st.button('建立批准用例基线', key='baseline_' + task['id'], type='primary'):
                if notice_action(lambda: service.create_baseline(task['id'])):
                    st.rerun()
            if task['baselines']:
                st.success('批准基线已保存，适用需求版本 v' + str(task['document_version']) + '。测试未执行。')
                fmt = st.selectbox('交付格式', ['csv', 'md'], key='agent_fmt_' + task['id'], format_func=lambda value: 'CSV' if value == 'csv' else 'Markdown')
                baseline = task['baselines'][-1]['snapshot']
                current = service.legacy.get_run(task['run_id'])
                matches = [(c['case_id'], c['version'], c['revision']) for c in current['cases']] == [(c['case_id'], c['version'], c['revision']) for c in baseline['cases']]
                if not matches:
                    st.warning('当前用例已偏离批准基线，请创建变更任务重新建立基线。')
                if st.button('生成批准导出文件', key='export_agent_' + task['id'], disabled=not matches):
                    run = service.legacy.get_run(task['run_id'])
                    exported = notice_action(lambda: service.legacy.export(task['run_id'], fmt, '', run['revision'], uuid4().hex))
                    if exported:
                        download_link(service.legacy, exported, '下载批准用例基线')
    if st.button('生成回归范围建议', key='regression_' + task['id']):
        plan = notice_action(lambda: service.regression_plan(task['id'], task['parent_task_id']))
        if plan:
            st.dataframe(plan['cases'], hide_index=True, width='stretch')
            st.caption('范围需要人工确认。间接影响尚不确定的部分应继续核对。')


def resources_view(service):
    from sdlc.testing_agent.resources import ResourceLibrary
    library = ResourceLibrary(service.db_path, service.token, service.workspace_id, service.project_id)
    permissions = service.legacy.user['permissions']
    active = service.legacy.get_project()['status'] == 'active'
    with st.expander('项目共享资源 · 前置条件、步骤与测试数据'):
        st.caption('维护可复用的测试准备模板。它们不代替需求依据，引用后仍需核对本次需求并评审用例。')
        resources = library.list(include_archived=True)
        for resource in resources:
            st.markdown('**' + resource['title'] + '**')
            st.caption(RESOURCE_KINDS[resource['kind']] + f" · 第 {resource['revision']} 版" + (' · 已归档' if resource['archived'] else ''))
            st.text(resource['body'])
            if active and 'edit' in permissions and not resource['archived']:
                with st.form('resource_edit_' + resource['id'] + str(resource['revision'])):
                    title = st.text_input('资源标题', resource['title'])
                    body = st.text_area('准备内容', resource['body'])
                    if st.form_submit_button('保存资源新版本'):
                        if notice_action(lambda: library.update(resource['id'], resource['revision'], title, body)):
                            st.rerun()
            if active and 'manage' in permissions:
                label = '恢复使用' if resource['archived'] else '归档资源'
                if st.button(label, key='resource_archive_' + resource['id']):
                    if notice_action(lambda: library.archive(resource['id'], resource['revision'], not resource['archived'])):
                        st.rerun()
        if active and 'generate' in permissions:
            with st.form('resource_new_' + service.project_id):
                st.markdown('**添加共享资源**')
                kind = st.selectbox('资源类型', list(RESOURCE_KINDS), format_func=RESOURCE_KINDS.get)
                title = st.text_input('新资源标题', max_chars=100)
                body = st.text_area('新资源准备内容', max_chars=8000)
                if st.form_submit_button('添加到项目资源库'):
                    if notice_action(lambda: library.create(kind, title, body)):
                        st.rerun()


def legacy_workspace(legacy):
    service = testing_service(legacy)
    page_header('项目测试任务', '让 Agent 围绕测试目标整理依据、发现缺口并推进到可评审的成果。', 'workspace', '测试 Agent')
    resources_view(service)
    if '_agent_notice' in st.session_state:
        st.success(st.session_state.pop('_agent_notice'))
    page = st.session_state.setdefault('agent_task_page_' + service.project_id, 1)
    tasks = service.list_tasks(limit=20, offset=(page - 1) * 20)
    permissions = legacy.user['permissions']
    if tasks:
        section_title('项目待办', '继续未完成工作，按目标和需求版本组织测试准备。', 'workspace')
        counts = st.columns(4)
        for col, label, value in zip(counts, ['我负责的未完成任务', '等待答复', '等待评审', '待恢复任务'],
                [sum(t['owner_id'] == legacy.user['id'] and t['status'] not in {'completed', 'cancelled'} for t in tasks),
                 sum(t['status'] == 'waiting_answer' for t in tasks), sum(t['status'] in {'waiting_review', 'partial'} for t in tasks),
                 sum(t['status'] == 'failed' for t in tasks)]):
            col.metric(label, value)
        st.caption('以上统计当前页任务；任务列表按创建时间排序。')
    if 'generate' in permissions:
        if not tasks or st.session_state.get('_agent_create'):
            new_task_form(service, service.list_tasks(limit=100))
        elif st.button('新建测试任务 / 分析需求变更', type='primary', key='agent_create'):
            st.session_state['_agent_create'] = True
            st.rerun()
    if not tasks:
        st.caption('任务开始后，可在这里继续需求澄清、用例评审和版本维护。')
        return
    key = 'agent_task_' + service.project_id
    next_task = st.session_state.pop('_chat_next_task_' + service.project_id, None)
    if next_task:
        st.session_state[key] = next_task
    ids = [t['id'] for t in tasks]
    # A just-created child may not be in the current page.
    selected = st.session_state.get(key)
    if selected and selected not in ids:
        try:
            tasks.insert(0, service.get_task(selected))
            ids.insert(0, selected)
        except ValueError:
            st.session_state.pop(key, None)
    selected = st.selectbox('当前测试任务', ids, key=key,
        format_func=lambda tid: next(t['goal'][:50] + f" · v{t['document_version']} · {t['id'][:8]}" for t in tasks if t['id'] == tid))
    previous, counter, following = st.columns([1, 2, 1])
    if previous.button('上一页任务', disabled=page == 1):
        st.session_state['agent_task_page_' + service.project_id] = page - 1
        st.session_state.pop(key, None)
        st.rerun()
    counter.caption(f'第 {page} 页 · 每页 20 项任务')
    if following.button('下一页任务', disabled=len(tasks) < 20):
        st.session_state['agent_task_page_' + service.project_id] = page + 1
        st.session_state.pop(key, None)
        st.rerun()
    task = service.get_task(selected)
    progress(service, selected)
    st.markdown('**目标：** ' + task['goal'])
    st.text('范围：' + task['scope'])
    st.caption(f"需求 v{task['document_version']} · {task['next_action']} · 测试执行：未执行")
    if task['error']:
        st.error(task['error'])
    summary = service.task_summary(selected)
    cols = st.columns(4)
    for col, label, value in zip(cols, ['当前用例', '待评审', '已退回', '未确认问题'],
                                  [summary['case_count'], summary['pending_review'], summary['returned'], summary['open_questions']]):
        col.metric(label, value)
    if {'generate', 'regenerate'} & permissions and task['status'] not in {'completed', 'cancelled'}:
        with st.expander('继续任务或调整目标'):
            message = st.text_area('补充目标或反馈', key='agent_message_' + selected)
            left, right = st.columns(2)
            if left.button('继续 / 恢复任务', key='resume_' + selected):
                def resume():
                    if 'generate' in permissions:
                        service.resume_task(selected)
                        return worker.enqueue(service, selected, 'message' if message.strip() else 'resume', message)
                    return worker.enqueue(service, selected, 'message', message.strip() or '请仅处理已记录的用例反馈并提出修订，不修改需求事实。')
                if notice_action(resume):
                    st.rerun()
            if right.button('取消当前任务', key='cancel_' + selected, disabled='generate' not in permissions):
                worker.cancel_work(service, selected)
                service.cancel_task(selected)
                st.rerun()
    tab_key = 'agent_tabs_' + selected
    target = st.session_state.pop('_agent_target_tab_' + selected, None)
    if target:
        st.session_state[tab_key] = target
    tabs = st.tabs(['需求与问题', '测试覆盖', '用例评审', '交付'], key=tab_key, on_change='rerun')
    with tabs[0]:
        requirements_view(service, task)
    with tabs[1]:
        coverage_view(service, task)
    with tabs[2]:
        cases_view(service, task)
    with tabs[3]:
        delivery_view(service, task)
    with st.expander('任务活动与诊断记录', expanded=False):
        for event in reversed(task['events'][-30:]):
            st.caption(event['created_at'] + ' · ' + event['action'])
            if 'manage' in permissions:
                st.json(event['details'], expanded=False)


def workspace(legacy):
    from sdlc.testing_agent.chat_ui import workspace as chat_workspace
    return chat_workspace(legacy)

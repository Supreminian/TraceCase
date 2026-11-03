"""Human-operated case cards inside chat; no model grants review authority."""
from __future__ import annotations

import difflib
import json
from uuid import uuid4

import streamlit as st

from sdlc.testing_agent import worker
from sdlc.testing_agent.case_browser import get_case, list_cases

LABELS = {'approved': '已批准', 'pending_review': '待审核', 'returned': '已退回'}


def action(callback):
    try:
        callback()
    except ValueError as exc:
        st.error(str(exc))
        return
    st.rerun(scope='app')


def render_collection(service, message, active=False):
    try:
        payload = json.loads(message['content'])
        key = 'cards_' + message['id']
        reference = payload.get('reference')
        if reference:
            detail = get_case(service, **reference)
            items, result = [detail['case']], {'total': 1, 'page': 1, 'pages': 1}
        else:
            query = dict(payload['query'])
            query['page'] = st.session_state.get(key + '_page', query.get('page', 1))
            result = list_cases(service, **query)
            items = result['items']
        status = payload.get('query', {}).get('status', 'all')
        scope = '当前任务' if payload.get('query', {}).get('task_id') else '当前项目'
        st.markdown(f'**{scope} · {LABELS.get(status, "全部")}用例 · 共 {result["total"]} 条**')
        st.caption('以下为实时数据；批准仅表示评审通过，不表示测试已执行。')
        if not items:
            st.info('没有符合条件的用例。可以调整筛选条件，或先完成用例审核。')
        for case in items:
            _card(service, message, case, active)
        if result['pages'] > 1:
            previous, counter, following = st.columns([1, 2, 1])
            if previous.button('上一页', key=key + '_prev', disabled=result['page'] <= 1):
                st.session_state[key + '_page'] = result['page'] - 1
                st.rerun(scope='app')
            counter.caption(f'第 {result["page"]} / {result["pages"]} 页')
            if following.button('下一页', key=key + '_next', disabled=result['page'] >= result['pages']):
                st.session_state[key + '_page'] = result['page'] + 1
                st.rerun(scope='app')
    except (ValueError, KeyError, TypeError):
        st.warning('用例列表暂不可用，请刷新或检查当前项目权限。')


def _card(service, message, case, active):
    key = 'case_card_' + message['id'] + '_' + case['run_id'] + '_' + case['case_id']
    content = case['content']
    permissions = service.legacy.user['permissions']
    with st.container(border=True, key=key):
        st.markdown('**' + content['title'] + '**')
        st.caption(f'{case["case_id"]} · {LABELS.get(case["status"], case["status"])} · 第 {case["version"]} 版 · {content["priority"]} · {content["category"]}')
        old = next((r for r in json.loads(message['content']).get('shown_references', [])
                    if r['run_id'] == case['run_id'] and r['case_id'] == case['case_id']), None)
        if old and (old['version'], old['revision']) != (case['version'], case['revision']):
            st.caption('此用例在本条消息后已有更新，当前显示最新版本和状态。')
        detail = get_case(service, case['run_id'], case['case_id'])
        st.markdown('**前置条件**')
        st.text('\n'.join(content['preconditions']) or '无')
        st.markdown('**操作步骤**')
        st.text('\n'.join(f'{step["number"]}. {step["action"]}' for step in content['steps']))
        st.markdown('**预期结果**')
        st.text('\n'.join(content['expected_results']))
        with st.expander('查看需求依据'):
            st.markdown('**关联需求：** ' + '、'.join(content['requirement_ids']))
            for paragraph in detail['paragraphs']:
                st.caption(paragraph['id'])
                st.text(paragraph['text'])
        with st.expander('审核与其他操作'):
            with st.expander('查看审批记录'):
                events = service.legacy.get_run(case['run_id'])['events']
                related = [e for e in events if e.get('case_id') == case['case_id']]
                if not related:
                    st.caption('暂无该用例的审批或修改记录。')
                for event in related:
                    st.caption(str(event.get('created_at', '')) + ' · ' + str(event.get('action', '')))
                    st.text(event.get('reason', '') or '无补充说明')
            task_id = detail['task_id']
            locked = detail['task_status'] in {'completed', 'cancelled'}
            if case['status'] == 'pending_review' and 'review' in permissions and not locked:
                with st.form(key + f'_review_v{case["version"]}_r{case["revision"]}'):
                    decision = st.selectbox('审核决定', ['通过', '退回'], key=key + '_decision')
                    reason = st.text_input('说明（退回时必填）', key=key + '_reason')
                    submitted = st.form_submit_button('提交审核', disabled=active)
                if submitted:
                    def review():
                        if decision == '通过':
                            service.legacy.review(case['run_id'], case['case_id'], case['version'], case['revision'],
                                                  'approve', '', reason or '通过，无补充意见', uuid4().hex)
                        elif task_id:
                            service.review_feedback(task_id, case['case_id'], 'unclear_steps', reason,
                                                    case['version'], case['revision'])
                        else:
                            service.legacy.review(case['run_id'], case['case_id'], case['version'], case['revision'],
                                                  'return', '', reason, uuid4().hex)
                    action(review)
            if 'edit' in permissions and not locked:
                with st.expander('人工修改为新版本'):
                    with st.form(key + f'_edit_v{case["version"]}_r{case["revision"]}'):
                        title = st.text_input('用例标题', content['title'])
                        preconditions = st.text_area('前置条件（每行一条）', '\n'.join(content['preconditions']))
                        steps = st.text_area('操作步骤（每行一步）', '\n'.join(s['action'] for s in content['steps']))
                        expected = st.text_area('预期结果（每行一条）', '\n'.join(content['expected_results']))
                        reason = st.text_input('修改理由')
                        save = st.form_submit_button('保存新版本并重新审核', disabled=active)
                    if save:
                        changed = {**content, 'title': title, 'preconditions': preconditions.splitlines(),
                                   'steps': [{'number': i + 1, 'action': x} for i, x in enumerate(steps.splitlines())],
                                   'expected_results': expected.splitlines()}
                        action(lambda: service.legacy.edit(case['run_id'], case['case_id'], case['version'],
                            case['revision'], changed, '', reason, uuid4().hex))
            if task_id and case['status'] == 'returned' and 'regenerate' in permissions and not locked:
                task = service.get_task(task_id)
                feedback = [p for p in task['proposals'] if p['case_id'] == case['case_id'] and p['status'] == 'pending']
                if st.button('请求 Agent 修订', key=key + '_revise', disabled=active):
                    action(lambda: worker.enqueue(service, task_id, 'message',
                        '请处理用例 ' + case['case_id'] + ' 已记录的退回意见，提出修订供人工采纳，不批准用例。', request_key=uuid4().hex))
                for proposal in feedback:
                    if 'case' not in proposal['content']:
                        st.caption('退回意见：' + proposal['content'].get('reason', '待处理'))
                        continue
                    with st.expander('查看修订差异'):
                        st.code('\n'.join(difflib.unified_diff(json.dumps(content, ensure_ascii=False, indent=2).splitlines(),
                            json.dumps(proposal['content']['case'], ensure_ascii=False, indent=2).splitlines(),
                            fromfile='当前版本', tofile='修订提案')), language='diff')
                        if st.button('采纳为新版本', key=key + '_apply_' + proposal['id'], disabled=active):
                            action(lambda: service.apply_revision(task_id, proposal['id']))
                        if 'manage' in permissions:
                            rejection = st.text_input('拒绝修订的理由', key=key + '_reject_reason_' + proposal['id'])
                            if st.button('拒绝此修订', key=key + '_reject_' + proposal['id'], disabled=active):
                                action(lambda: service.resolve_proposal(task_id, proposal['id'], 'dismiss', rejection))
            if task_id and case['status'] == 'approved' and 'generate' in permissions:
                with st.expander('提出修改'):
                    with st.form(key + '_change'):
                        change = st.text_area('说明新的测试目标或需求变化')
                        requested = st.form_submit_button('交给 Agent 处理', disabled=active)
                    if requested:
                        def request_change():
                            if not change.strip():
                                raise ValueError('请说明需要修改的内容。')
                            worker.enqueue(service, task_id, 'message',
                                '请针对已批准用例 ' + case['case_id'] + ' 处理以下变更，保留原批准记录：\n' + change,
                                request_key=uuid4().hex)
                            st.session_state['_chat_next_task_' + service.project_id] = task_id
                            st.session_state['_chat_auto_follow_' + task_id] = True
                        action(request_change)
            if task_id and case['status'] == 'approved' and 'export' in permissions:
                if st.button('查看批准交付与导出', key=key + '_export', disabled=active):
                    st.session_state['_chat_next_task_' + service.project_id] = task_id
                    st.session_state['_agent_target_tab_' + task_id] = '交付'
                    st.session_state['_chat_artifacts_open_' + task_id] = True
                    st.rerun(scope='app')
        if locked and case['status'] == 'approved':
            st.caption('批准版本保留。如需修改，请在聊天中说明新的需求或测试范围，建立关联任务重新审核。')

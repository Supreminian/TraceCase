"""Human confirmations embedded in the Agent conversation."""
from __future__ import annotations

import json

import streamlit as st

LABELS = {'name': '名称', 'description': '简介', 'reason': '原因', 'question_text': '澄清问题',
          'blocking': '阻断性', 'role': '角色', 'decision': '审核决定', 'format': '导出格式',
          'task_id': '任务', 'case_id': '用例', 'run_id': '运行', 'question_id': '问题',
          'paragraph_ids': '需求段落', 'requirement_ids': '关联规则', 'affected_case_ids': '影响用例',
          'scope': '影响范围', 'body': '资料正文', 'title': '标题', 'archived': '归档',
          'user_id': '成员', 'request_id': '申请', 'resource_id': '资料', 'overrides': '权限调整',
          'approve': '是否同意加入', 'kind': '资料类型'}


def _fields(arguments):
    for name, value in arguments.items():
        if name == 'changes':
            for change in value:
                with st.container(border=True):
                    st.markdown('**' + change['question_id'] + '**')
                    _fields({key: item for key, item in change.items() if key != 'question_id'})
        elif name == 'content':
            st.markdown('**修改后的用例内容**')
            st.write(value.get('title', ''))
            for field, label in [('preconditions', '前置条件'), ('steps', '操作步骤'), ('expected_results', '预期结果')]:
                st.markdown('**' + label + '**')
                st.text('\n'.join(str(entry.get('action', entry)) if isinstance(entry, dict) else str(entry)
                                  for entry in value.get(field, [])))
        else:
            shown = ('是' if value else '否') if type(value) is bool else '、'.join(str(item) for item in value) if isinstance(value, list) else json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
            st.markdown('**' + LABELS.get(name, name) + '**')
            st.text(shown)


def render_action(service, chat_task_id, message, active):
    from sdlc.testing_agent.platform_actions import ACTIONS, get_action, confirm_action, cancel_action
    try:
        action_id = json.loads(message['content'])['action_id']
        record = get_action(service, chat_task_id, action_id)
        spec = ACTIONS[record['action']]
        st.markdown('**' + spec['description'] + '**')
        st.write(record['summary'])
        snapshot = record['snapshot']
        previous = snapshot.get('question')
        if previous:
            with st.expander('修改前的澄清'):
                st.write(previous.get('question', ''))
                st.caption('阻断性：' + ('是' if previous.get('blocking') else '否'))
        _fields(record['arguments'])
        key = 'platform_action_' + record['id']
        if record['status'] == 'pending':
            is_actor = service.user['id'] == record['actor_id']
            st.caption('确认后执行以上变更；执行时会再次校验权限和当前版本。')
            yes, no = st.columns(2)
            if yes.button('确认执行', key=key + '_confirm', type='primary', disabled=active or not is_actor):
                completed = confirm_action(service, chat_task_id, record['id'], expected_revision=record['revision'])
                st.session_state['_agent_notice'] = '已完成：' + spec['description']
                result = completed.get('result') or {}
                if record['action'] in {'clarification_edit', 'clarification_edit_batch'}:
                    from sdlc.testing_agent.platform_tools import publish_clarifications
                    source_id = record['arguments'].get('task_id') or record['arguments']['changes'][0]['task_id']
                    publish_clarifications(service, chat_task_id, source_id)
                if result.get('target_task_id'):
                    st.session_state['_chat_next_task_' + service.project_id] = result['target_task_id']
                st.rerun(scope='app')
            if no.button('取消这项操作', key=key + '_cancel', disabled=active or not is_actor):
                cancel_action(service, chat_task_id, record['id'], expected_revision=record['revision'])
                st.rerun(scope='app')
            if not is_actor:
                st.caption('等待提出本次操作的账号确认。')
        elif record['status'] == 'completed':
            st.success('操作已完成')
            result = record.get('result') or {}
            if record['action'] == 'approved_export' and result.get('id'):
                exported = service.legacy.get_export(result['id'])
                st.download_button('下载批准文件', data=exported['content'], file_name=exported['filename'],
                                   mime=exported['mime'], key=key + '_download')
            if record['action'] == 'project_create' and result.get('project_id'):
                if st.button('进入新项目', key=key + '_project'):
                    st.session_state['_switch_project'] = result['project_id']
                    st.rerun(scope='app')
        else:
            st.caption('此操作已取消。')
    except (ValueError, KeyError, TypeError) as exc:
        st.error(str(exc) if isinstance(exc, ValueError) else '操作记录暂不可用，请检查当前账号权限并刷新。')

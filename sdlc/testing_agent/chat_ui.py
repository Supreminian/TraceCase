"""Conversation-first testing workbench with public, live model output."""
from __future__ import annotations

from contextlib import contextmanager
import html
import json
import re
from uuid import uuid4

import streamlit as st

from sdlc.domain import prepare_input
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent import worker


CSS = '''<style>
.st-key-agent_chat_workspace {height:calc(100dvh - 212px)!important;flex:0 0 auto!important;min-height:0}
.st-key-agent_chat_workspace > div:has(> .st-key-agent_chat_shell) {flex:1 1 0!important;min-height:0;height:auto!important}
.st-key-agent_chat_workspace .st-key-agent_chat_shell {min-height:0;overflow:hidden}
.st-key-agent_chat_shell > div:has([class*="st-key-chat_history_"]),
.st-key-agent_chat_shell > div:has(.st-key-chat_welcome) {flex:1 1 0!important;min-height:0;overflow:hidden}
.tc-chat-heading {display:flex;align-items:center;gap:14px;margin:0 0 4px}
.tc-chat-logo {width:44px;height:44px;display:grid;place-items:center;border-radius:15px;background:#243454;color:#fff;font-size:23px;box-shadow:0 7px 22px #24345418}
.tc-chat-heading h1 {font-size:27px!important;margin:0!important;padding:0!important;line-height:1.35!important}
.tc-chat-heading p {color:#778398;margin:4px 0 0;font-size:13px}
.st-key-agent_chat_shell {background:#fff;border:1px solid #e2e7ef;border-radius:20px;padding:10px 20px 18px;box-shadow:0 12px 40px #26365005}
.st-key-agent_chat_shell [data-testid="stChatMessage"] {border-radius:6px 18px 18px 18px;padding:16px 18px;margin:4px 0 12px;background:#f5f7fb;max-width:92%;border:1px solid #e7ebf2;align-self:flex-start}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--user) {width:fit-content!important;max-width:80%;margin-left:auto;align-self:flex-end;flex-direction:row-reverse;border:1px solid #d6e1fb;border-radius:18px 6px 18px 18px;background:#edf3ff}
.st-key-agent_chat_shell [data-testid="stChatMessageContent"] {min-width:0;overflow-wrap:anywhere}
.st-key-agent_chat_shell [data-testid="stChatMessageContent"] > [data-testid="stVerticalBlock"] {gap:6px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--user) [data-testid="stChatMessageContent"] {flex:1}
.tc-message-identity {display:flex;align-items:center;gap:7px;margin:0 0 4px;font-size:12px;font-weight:700;color:#5263a5;letter-spacing:.02em}
.tc-message--user {justify-content:flex-end;color:#365eab}
.tc-agent-indicator {width:6px;height:6px;border-radius:50%;background:#6879d3;box-shadow:0 0 0 3px #e5e9ff}
.st-key-agent_chat_shell [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] {overflow-wrap:anywhere}
.st-key-agent_chat_shell [data-testid="stChatMessage"] p {line-height:1.75;font-size:14px}
/* Readable rich text spacing, scoped to Agent replies only. */
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) {padding:20px 22px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) [data-testid="stChatMessageContent"] > [data-testid="stVerticalBlock"] {gap:18px!important}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) [data-testid="stMarkdownContainer"] {line-height:1.85;font-size:14px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) :is(p,li) {line-height:1.85}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) p {margin:0 0 14px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) :is(h1,h2,h3,h4,h5,h6) {margin:22px 0 10px!important;padding:0!important;line-height:1.5!important}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) :is(ul,ol) {margin:10px 0 16px;padding-left:24px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) li {margin:7px 0;padding-left:3px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) li > p {margin:0 0 8px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) li > :is(ul,ol) {margin:6px 0 10px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) :is(pre,blockquote,table) {margin:16px 0}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) :is(th,td) {padding:10px 12px;line-height:1.7}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) [data-testid="stMarkdownContainer"] > :first-child {margin-top:0!important}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--agent) [data-testid="stMarkdownContainer"] > :last-child {margin-bottom:0}
.st-key-agent_chat_shell [data-testid="stChatInput"] {border:1px solid #cfd8e8;border-radius:15px;background:#f8faff;box-shadow:0 3px 12px #26365006}
.st-key-agent_chat_shell div:has(> [class*="st-key-chat_history_"]) {height:auto!important;flex:1 1 0!important;min-height:0}
.st-key-agent_chat_shell [class*="st-key-chat_history_"] {min-height:0;overscroll-behavior:contain}
.st-key-agent_chat_shell div:has(> .st-key-chat_welcome) {height:auto!important;flex:1 1 0!important;min-height:0}
.st-key-chat_welcome {min-height:0;gap:0!important;overflow-y:auto!important}
.st-key-chat_welcome [data-testid="stElementContainer"]:has(.tc-chat-welcome) {margin-block:auto;flex-shrink:0;width:100%}
@media(min-width:1000px){.st-key-agent_chat_shell{width:calc(100% + 64px)!important;max-width:none!important;margin-inline:-32px;padding-inline:24px}}
/* The active composer swaps the send affordance for a real cancel action. */
.st-key-chat_composer {position:relative;flex-shrink:0}
.st-key-chat_composer:has(.st-key-chat_stop_generation) [data-testid="stChatInputSubmitButton"] {visibility:hidden;pointer-events:none}
.st-key-chat_composer:has(.st-key-chat_stop_generation) [data-testid="stChatInput"] textarea {padding-right:52px}
.st-key-chat_composer > div:has(> .st-key-chat_stop_generation) {position:absolute!important;right:12px;bottom:12px;width:34px!important;z-index:2}
.st-key-chat_stop_generation button {width:34px!important;min-height:34px!important;height:34px;padding:0!important;border-radius:50%!important;background:#243454!important;border:0!important;color:white!important}
.st-key-chat_stop_generation button [data-testid="stMarkdownContainer"] {position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.st-key-chat_stop_generation button:hover {background:#354b72!important}
.st-key-chat_stop_generation button:focus-visible {outline:3px solid #9bade5;outline-offset:3px}
.tc-chat-state {font-size:12px;color:#778398;padding:8px 2px;border-bottom:1px solid #edf0f5;display:flex;gap:8px;align-items:center;min-width:0;flex-wrap:wrap}
.tc-chat-source {overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1 1 120px;color:#8b95a6}
.tc-state-dot {width:7px;height:7px;border-radius:100%;background:#64748b;display:inline-block}
.tc-state-dot.active {background:#5b6bec;animation:tc-breathe 1.6s infinite}
.tc-thinking {display:flex;align-items:center;gap:11px;padding:12px 16px;border:1px solid #e5e9f5;border-radius:14px;background:linear-gradient(110deg,#f2f4ff,#fafbff,#f2f8fa);background-size:200% 100%;animation:tc-shimmer 3s linear infinite;margin:8px 0}
.tc-thinking-orb {color:#5263ce;font-size:22px;animation:tc-breathe 1.7s ease-in-out infinite}
.tc-thinking-label {color:#62708b;font-size:13px}
.tc-thinking-dots span {display:inline-block;width:4px;height:4px;margin-left:3px;border-radius:50%;background:#7d8adc;animation:tc-hop 1.2s infinite}
.tc-thinking-dots span:nth-child(2){animation-delay:.15s}.tc-thinking-dots span:nth-child(3){animation-delay:.3s}
.tc-chat-welcome {padding:22px 10px;text-align:center;color:#718096}
.tc-chat-welcome strong {display:block;font-size:20px;color:#2d3b55;margin:14px 0 8px}
.tc-chat-welcome p {font-size:14px;line-height:1.8;max-width:480px;margin:auto}
.tc-chat-tag {display:inline-block;padding:4px 10px;margin:14px 3px 0;background:#f0f3f9;border:1px solid #e5eaf1;border-radius:20px;font-size:12px;color:#64748b}
.tc-confirm-tag {font-size:12px;color:#6b61ae;letter-spacing:.06em;margin:0;padding:8px 0;line-height:1.7}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-confirm-tag) {padding-block:24px}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-confirm-tag) [data-testid="stChatMessageContent"] > [data-testid="stVerticalBlock"] {gap:20px!important}
.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-confirm-tag) [data-testid="stCaptionContainer"] {padding-top:8px;line-height:1.8}
.tc-chat-footnote {color:#94a0b3;font-size:11px;margin-top:7px}
@keyframes tc-hop {0%,70%,100%{transform:translateY(0);opacity:.4}35%{transform:translateY(-4px);opacity:1}}
@keyframes tc-breathe {0%,100%{opacity:.45;transform:scale(.94)}50%{opacity:1;transform:scale(1.05)}}
@keyframes tc-shimmer {0%{background-position:200% 0}100%{background-position:-200% 0}}
@media(prefers-reduced-motion:reduce){.tc-thinking,.tc-thinking-orb,.tc-thinking-dots span,.tc-state-dot.active{animation:none!important}}
@media(max-width:700px){.st-key-agent_chat_workspace{height:calc(100dvh - 250px)!important}.st-key-agent_chat_shell{padding:7px 9px}.tc-chat-heading h1{font-size:23px!important}.st-key-agent_chat_shell [data-testid="stChatMessage"]{max-width:98%;padding:12px 10px}.st-key-agent_chat_shell [data-testid="stChatMessage"]:has(.tc-message--user){max-width:90%}}
</style>'''

LABELS = {'queued': '已收到，准备开始', 'running': 'Agent 正在处理', 'waiting_confirmation': '等待你的确认',
          'waiting_answer': '等待补充需求', 'waiting_review': '等待用例审核', 'partial': '等待你的消息',
          'completed': '测试设计已交付', 'failed': '需要确认下一步', 'cancelled': '本轮已取消'}
STAGES = {'analysis': '正在理解需求与角色', 'planning': '正在整理覆盖计划', 'generation': '正在编写测试用例',
          'review': '正在复核结果与需求依据', 'delivery': '正在整理交付内容'}


@contextmanager
def _message(role):
    """Keep explicit speaker identities on streamed and interactive messages."""
    user = role == 'user'
    with st.chat_message(role, avatar=':material/person:' if user else ':material/auto_awesome:'):
        st.markdown('<div class="tc-message-identity ' + ('tc-message--user' if user else 'tc-message--agent') + '">'
            + ('你' if user else '<span class="tc-agent-indicator" aria-hidden="true"></span>TraceCase Agent')
            + '</div>', unsafe_allow_html=True)
        yield


def _jobs(service, task_id):
    with service.connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_work_items'").fetchone():
            return []
        return [dict(r) for r in db.execute('SELECT id,status,operation FROM agent_work_items WHERE task_id=? ORDER BY created_at DESC LIMIT 5', (task_id,))]


def _select_next(service, task_id):
    st.session_state['_chat_next_task_' + service.project_id] = task_id
    st.session_state['_agent_create'] = False
    st.session_state['_chat_auto_follow_' + task_id] = True
    st.rerun()


def _safe_action(action):
    try:
        return action()
    except (ValueError, RuntimeError) as exc:
        st.error(str(exc))
        return None


def _start(service, text, instruction='', parent_task_id=None, options=None):
    options = options or {}
    task_id = service.create_task(text, instruction[:2000], parent_task_id=parent_task_id, **options)
    if instruction.strip() and instruction.strip() != text.strip():
        ChatService(service).post_user(task_id, instruction[:20_000], request_key='initial-goal-' + task_id)
    worker.enqueue(service, task_id, request_key=uuid4().hex)
    return task_id


def _read_input(value):
    if isinstance(value, str):
        return value.strip(), None
    text, files = value.text.strip(), value.files
    if len(files) > 1:
        raise ValueError('每轮请先选择一份 Markdown 需求文档。')
    if not files:
        return text, None
    uploaded = files[0]
    prepared = prepare_input(filename=uploaded.name, data=uploaded.getvalue())
    return text, prepared['normalized_text']


def _confirmation(service, chat, task_id, pending, active=False):
    kind = pending['kind']
    plan = kind == 'generation_plan'
    needs_document = kind == 'requirements_input'
    with _message('assistant'):
        st.markdown('<div class="tc-confirm-tag">下一步由你决定</div>', unsafe_allow_html=True)
        if plan:
            details = service.artifact(task_id, 'coverage_plan') or {}
            with st.expander('查看本轮覆盖计划'):
                for condition in details.get('conditions', []):
                    st.markdown('**' + condition['module'] + '** · ' + condition['description'])
                    st.caption(condition['priority'] + ' · ' + '、'.join(condition['requirement_ids']))
        permissions = service.legacy.user['permissions']
        allowed = bool({'generate', 'regenerate'} & permissions) and not active
        if kind == 'budget_exhausted':
            allowed = 'generate' in permissions and not active
        first, second = st.columns([2, 1])
        label = '创建新一轮继续' if kind == 'budget_exhausted' else '确认并继续' if plan else '重试未完成部分'
        if not needs_document and first.button(label, key='chat_retry_' + pending['id'], type='primary', disabled=not allowed):
            result = _safe_action(lambda: chat.respond(task_id, pending['id'], 'retry', expected_revision=pending['revision'], request_key=uuid4().hex))
            if result:
                _select_next(service, result['task_id'])
        if second.button('暂不继续', key='chat_stop_' + pending['id'], disabled=not allowed):
            result = _safe_action(lambda: chat.respond(task_id, pending['id'], 'stop', expected_revision=pending['revision'], request_key=uuid4().hex))
            if result:
                st.rerun()
        if needs_document:
            st.info('直接在下方输入需求正文或附上 Markdown 文档，我会以新需求版本继续。')
        if 'generate' in permissions and not needs_document and not active:
            with st.expander('调整目标或范围后继续'):
                with st.form('chat_refine_' + pending['id']):
                    content = st.text_area('告诉我这次需要调整什么', max_chars=2000,
                        placeholder='例如：这轮只设计审批权限用例，金额边界放到下一轮。')
                    submitted = st.form_submit_button('按补充说明继续')
                if submitted:
                    result = _safe_action(lambda: chat.respond(task_id, pending['id'], 'refine', content,
                        expected_revision=pending['revision'], request_key=uuid4().hex))
                    if result:
                        _select_next(service, result['task_id'])
        st.caption('这次确认只决定生成流程；需求事实确认、用例审核和批准导出仍由你单独操作。')


def _questions_in_chat(service, chat, task, active, historical=False):
    with service.connection() as db:
        questions = [service._question(row) for row in db.execute("SELECT * FROM testing_agent_questions WHERE task_id=? AND status IN ('open','answered') ORDER BY id", (task['id'],))]
    if not questions:
        return
    if historical:
        with st.expander('上一轮未解决的问题与答复'):
            st.caption('原记录完整保留。创建新一轮后，Agent 会核对这些问题是否仍然适用；无需在已耗尽预算的旧任务中重复填写。')
            for question in questions:
                st.markdown(question['content']['question'])
                if question.get('answer'):
                    st.text('已保存答复：' + question['answer']['text'])
        return
    with _message('assistant'):
        st.markdown('**这些需求还需要你补充**')
        for question in questions:
            q = question['content']
            st.markdown(q['question'])
            with st.expander('相关需求依据', key='chat_question_sources_' + task['id'] + q['id']):
                for paragraph in task['input']['paragraphs']:
                    if paragraph['id'] in q['paragraph_ids']:
                        st.caption(paragraph['id'])
                        st.text(paragraph['text'])
            permissions = service.legacy.user['permissions']
            if question['status'] == 'open' and 'generate' in permissions:
                with st.form('chat_question_' + task['id'] + q['id']):
                    answer = st.text_area('你的答复', max_chars=4000, key='chat_question_answer_' + task['id'] + q['id'])
                    submitted = st.form_submit_button('提交这项答复', disabled=active)
                if submitted:
                    def submit():
                        service.submit_answer(task['id'], q['id'], answer, expected_revision=question['revision'])
                        chat.post_user(task['id'], '针对「' + q['question'] + '」的答复：\n\n' + answer, request_key=uuid4().hex)
                        return True
                    if _safe_action(submit):
                        st.rerun(scope='app')
            elif question['status'] == 'answered':
                answer = question.get('answer') or {}
                st.info('已收到答复：' + answer.get('text', ''))
                if answer.get('disposition') != 'answer' or 'manage' not in permissions:
                    st.caption('等待指定的需求确认人确认；这条答复还没有成为业务规则。')
        ready = {q['id']: q['revision'] for q in questions
                 if q['status'] == 'answered' and (q.get('answer') or {}).get('disposition') == 'answer'}
        if ready and 'manage' in service.legacy.user['permissions']:
            st.caption(f'本轮有 {len(ready)} 项答复待确认。确认后一起交给 Agent 继续；其他未解决的问题仍会保留。')
            if st.button('确认本轮答复并继续', key='chat_confirm_answers_' + task['id'], disabled=active):
                result = _safe_action(lambda: service.confirm_answers_and_continue(task['id'], ready,
                    '在聊天中确认已提交的答复作为本轮需求补充。'))
                if result:
                    _select_next(service, result['task_id'])


@st.fragment(run_every=0.4)
def live_conversation(service, chat, task_id):
    with service.connection() as db:
        task = service._task(db, task_id)
        handoff = db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='handoff' ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
    if handoff and st.session_state.pop('_chat_auto_follow_' + task_id, False):
        _select_next(service, json.loads(handoff['content_json'])['child_task_id'])
    jobs = _jobs(service, task_id)
    active = any(j['status'] in {'queued', 'running'} for j in jobs)
    pending = chat.recover_waiting_confirmation(task_id)
    messages = chat.list_messages(task_id)
    pending_message_id = next((message['id'] for message in reversed(messages)
                               if message['kind'] == 'confirmation'), None)
    live_key = '_chat_live_state_' + task_id
    previous = st.session_state.get(live_key)
    current = (task['status'], task['run_id'], bool(pending), active)
    st.session_state[live_key] = current
    # Refresh result cards only on a real state transition, never for every token.
    if previous is not None and previous != current and not active:
        st.rerun(scope='app')
    status = LABELS.get(task['status'], '继续当前对话')
    document_context = service.artifact(task_id, 'document_context')
    title = document_context['target_product'] if document_context else task['input']['summary']['title']
    st.markdown('<div class="tc-chat-state"><span class="tc-state-dot ' + ('active' if active else '') + '"></span>'
                + html.escape(status) + '<span>·</span>需求版本 ' + str(task['document_version'])
                + '<span class="tc-chat-source" title="' + html.escape(title, quote=True) + '">本轮需求：'
                + html.escape(title) + '</span></div>', unsafe_allow_html=True)
    with st.container(height=360, border=False, autoscroll=True, key='chat_history_' + task_id):
        if not messages:
            with _message('assistant'):
                st.markdown('我会在这里和你推进测试准备。你可以补充目标、查看需求依据，或审核已有用例。')
                if task['run_id']:
                    st.caption('这是一项已有任务，已保存成果和审核记录可以继续使用。')
        progress = []
        for message in messages:
            if message['kind'] == 'progress':
                progress.append(message['content'])
                continue
            content = message['content']
            if pending and pending['kind'] == 'budget_exhausted' and message['id'] == pending_message_id:
                content = pending['prompt']
            if not content.strip():
                continue
            with _message(message['role']):
                if message['role'] == 'user' and len(content) > 1600:
                    st.markdown(content[:600] + '\n\n…')
                    with st.expander('查看本次提交的完整内容'):
                        st.text(content)
                else:
                    st.markdown(content + (' ▍' if message['state'] == 'streaming' else ''))
                if message['state'] == 'interrupted':
                    st.caption('这段回复已中断，已有内容保留。')
        if active and not pending:
            stage = STAGES.get(task['stage'], '正在整理下一步')
            st.markdown('<div class="tc-thinking" role="status" aria-live="polite"><span class="tc-thinking-orb">✦</span>'
                '<span class="tc-thinking-label">' + html.escape(stage) + '</span><span class="tc-thinking-dots"><span></span><span></span><span></span></span></div>', unsafe_allow_html=True)
        if progress and active:
            st.caption(' · '.join(progress[-2:]))
        if pending:
            _confirmation(service, chat, task_id, pending, active)
        if task['status'] not in {'completed', 'cancelled'}:
            _questions_in_chat(service, chat, task, active,
                historical=bool(pending and pending['kind'] == 'budget_exhausted'))
        if task['run_id'] and not active and not pending:
            with _message('assistant'):
                with service.connection() as db:
                    cases = service.legacy._cases(db, task['run_id'])
                approved = sum(c['status'] == 'approved' for c in cases)
                st.markdown(f'当前有 **{len(cases)} 条用例**，已审核通过 **{approved} 条**。测试尚未执行。')
                target = '交付' if approved == len(cases) and cases else '用例评审'
                if st.button('查看批准交付' if target == '交付' else '开始审核用例', key='chat_open_results_' + task_id):
                    st.session_state['_chat_artifacts_open_' + task_id] = True
                    st.session_state['_agent_target_tab_' + task_id] = target
                    st.rerun(scope='app')


def _new_conversation(service, options):
    with st.container(border=True, key='agent_chat_shell'):
        with st.container(height=230, border=False, key='chat_welcome'):
            st.markdown('<div class="tc-chat-welcome"><span style="font-size:34px;color:#5263ce">✦</span>'
                '<strong>从一个测试目标开始</strong><p>告诉我你要验证什么，粘贴需求或附上 Markdown 文档。'
                '我会结合你的需求推进测试设计，遇到不明确的地方再与你讨论。</p>'
                '<span class="tc-chat-tag">需求分析</span><span class="tc-chat-tag">测试设计</span>'
                '<span class="tc-chat-tag">用例修订</span></div>', unsafe_allow_html=True)
        value = st.chat_input('描述测试需求，或粘贴 / 上传 PRD…', key='agent_new_chat_' + service.project_id,
            max_chars=20_000, accept_file=True, file_type=['md', 'markdown'], max_upload_size=1,
            disabled='generate' not in service.legacy.user['permissions'])
        if value:
            def create():
                content, document = _read_input(value)
                return _start(service, document or content, content if document else '', options=options)
            task_id = _safe_action(create)
            if task_id:
                _select_next(service, task_id)
        st.markdown('<div class="tc-chat-footnote">支持 Markdown，文件 ≤100 KiB、需求正文 ≤20,000 字符。资料发送至已配置的模型服务。</div>', unsafe_allow_html=True)


def _details(service, task):
    from sdlc.testing_agent.ui import requirements_view, coverage_view, cases_view, delivery_view
    key = 'agent_tabs_' + task['id']
    target = st.session_state.pop('_agent_target_tab_' + task['id'], None)
    if target:
        st.session_state[key] = target
        st.session_state['_chat_artifacts_open_' + task['id']] = True
    with st.expander('测试成果与人工审核', expanded=st.session_state.get('_chat_artifacts_open_' + task['id'], False)):
        st.caption('对话负责推进工作；原文、覆盖、审核与交付在这里保留清晰的结构。')
        tabs = st.tabs(['需求与问题', '测试覆盖', '用例评审', '交付'], key=key, on_change='rerun')
        with tabs[0]:
            requirements_view(service, task)
        with tabs[1]:
            coverage_view(service, task)
        with tabs[2]:
            cases_view(service, task)
        with tabs[3]:
            delivery_view(service, task)


def workspace(legacy):
    from sdlc.testing_agent.ui import testing_service
    service = testing_service(legacy)
    chat = ChatService(service)
    st.html(CSS)
    with st.container(key='agent_chat_workspace'):
        selected = _workspace_contents(legacy, service, chat)
    if selected:
        _details(service, service.get_task(selected))


def _workspace_contents(legacy, service, chat):
    from sdlc.testing_agent.ui import resources_view
    st.markdown('<div class="tc-chat-heading"><div class="tc-chat-logo">✦</div><div><h1>测试协作对话</h1>'
                '<p>需求、计划、用例与反馈，在同一个对话中逐步完成。</p></div></div>', unsafe_allow_html=True)
    if '_agent_notice' in st.session_state:
        st.success(st.session_state.pop('_agent_notice'))
    tasks = service.list_tasks(limit=100)
    key = 'agent_task_' + service.project_id
    canonical_key = '_chat_selected_task_' + service.project_id
    known = {task['id']: task for task in tasks}

    def available_task(value):
        if value is None:
            return True
        if not isinstance(value, str):
            return False
        if value in known:
            return True
        # Selectbox wire values are display strings. Old tabs can send a stale
        # label after background updates; a label must never become a task ID.
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
            return False
        try:
            task = service.get_task(value)
        except ValueError:
            return False
        tasks.insert(0, task)
        known[value] = task
        return True

    def fallback_task():
        previous = st.session_state.get(canonical_key)
        return previous if previous and available_task(previous) else tasks[0]['id'] if tasks else None

    next_task = st.session_state.pop('_chat_next_task_' + service.project_id, None)
    if next_task:
        st.session_state[key] = next_task if available_task(next_task) else fallback_task()
    if st.session_state.pop('_agent_create', False):
        st.session_state[key] = None
    selected = st.session_state.get(key)
    if selected and not available_task(selected):
        st.session_state[key] = fallback_task()
    if key not in st.session_state:
        st.session_state[key] = tasks[0]['id'] if tasks else None
    ids = [None] + [t['id'] for t in tasks]
    # Goals and document versions are immutable within a task. Status belongs to
    # the live conversation header, where updating it cannot change wire values.
    labels = {task['id']: task['goal'][:45] + f" · 需求 {task['document_version']} · {task['id'][:8]}" for task in tasks}
    left, right, setting_column = st.columns([4, 1, 1])
    selected = left.selectbox('当前对话', ids, key=key, label_visibility='collapsed',
        placeholder='新对话', format_func=lambda tid: '新对话' if tid is None else labels.get(tid, str(tid)))
    if selected not in ids:
        # A stale browser widget can be deserialized during selectbox creation,
        # after the pre-render check above. Keep handling this submitted chat turn
        # with the canonical ID, then repair the widget on the next render.
        selected = fallback_task()
        st.session_state['_chat_next_task_' + service.project_id] = selected
    st.session_state[canonical_key] = selected
    if right.button('新对话', key='chat_new_conversation', width='stretch', disabled='generate' not in legacy.user['permissions']):
        st.session_state['_agent_create'] = True
        st.rerun()
    options = {}
    with setting_column.popover('资料与设置', width='stretch'):
        resources_view(service)
        from sdlc.testing_agent.resources import ResourceLibrary
        resources = ResourceLibrary(service.db_path, service.token, service.workspace_id, service.project_id).list()
        selected_resources = st.multiselect('本次选用的共享准备资料', [r['id'] for r in resources],
            format_func=lambda rid: next(r['title'] + f" · 第{r['revision']}版" for r in resources if r['id'] == rid),
            key='chat_resources_' + service.project_id)
        options['resource_ids'] = selected_resources or None
        with service.connection() as db:
            members = [dict(r) for r in db.execute('SELECT u.id,u.display_name,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?', (service.workspace_id,))]
        options['owner_id'] = st.selectbox('任务负责人', [None] + [m['id'] for m in members],
            format_func=lambda uid: '当前登录用户' if uid is None else next(m['display_name'] for m in members if m['id'] == uid))
        options['confirmer_id'] = st.selectbox('需求确认人', [None] + [m['id'] for m in members if m['role'] in {'owner', 'admin'}],
            format_func=lambda uid: '空间管理者' if uid is None else next(m['display_name'] for m in members if m['id'] == uid))
        options['due_at'] = st.text_input('截止日期（YYYY-MM-DD，可选）') or None
    if not selected:
        _new_conversation(service, options)
        return
    task = service.get_task(selected)
    permissions = legacy.user['permissions']
    with st.container(border=True, key='agent_chat_shell'):
        live_conversation(service, chat, selected)
        pending = chat.get_pending(selected)
        active = any(j['status'] in {'queued', 'running'} for j in _jobs(service, selected))
        if task['parent_task_id']:
            st.caption('本轮保留了上轮的需求与讨论关联，原记录可在对话列表中查看。')
        with st.container(key='chat_composer'):
            value = st.chat_input('告诉我需要调整的地方，或补充下一步需求…', key='agent_chat_input_' + selected,
                max_chars=20_000, accept_file=True, file_type=['md', 'markdown'], max_upload_size=1,
                disabled=active or not bool({'generate', 'regenerate'} & permissions) or task['status'] == 'cancelled')
            if active and 'generate' in permissions:
                with st.container(key='chat_stop_generation'):
                    if st.button('停止生成', icon=':material/stop:', key='chat_cancel_' + selected,
                                 help='停止生成，保留已生成的内容'):
                        worker.cancel_work(service, selected)
                        if task['status'] != 'completed':
                            service.set_state(selected, 'partial',
                                next_action='生成已停止，已有内容已保留；可以继续发送消息。')
                        st.rerun()
        if value:
            def send():
                content, document = _read_input(value)
                if document:
                    if 'generate' not in permissions:
                        raise ValueError('当前角色不能创建需求新版本。')
                    if pending:
                        chat.respond(selected, pending['id'], 'stop', expected_revision=pending['revision'], request_key=uuid4().hex)
                    return _start(service, document, content or task['goal'], selected, options)
                if pending:
                    return chat.respond(selected, pending['id'], 'discuss', content,
                        expected_revision=pending['revision'], request_key=uuid4().hex)['task_id']
                worker.enqueue(service, selected, 'message', content, request_key=uuid4().hex)
                return selected
            followup = _safe_action(send)
            if followup:
                _select_next(service, followup)
    return selected

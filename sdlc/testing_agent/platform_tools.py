"""Authenticated platform capabilities; the model selects tools, never its identity.

Mutations create reviewable proposals. Only an authenticated human application
callback can execute them. Queries expose bounded, current-scope records.
"""
from __future__ import annotations

from sdlc.accounts import Accounts
from sdlc.agent.application import ToolDelivered
from sdlc.service import ServiceError, dumps, identifier, now


def publish_clarifications(service, chat_task_id, target_task_id):
    from sdlc.testing_agent.chat import ChatService
    from sdlc.testing_agent.clarifications import pending_questions
    ChatService(service)
    questions = pending_questions(service, target_task_id)
    key = 'clarifications-' + target_task_id
    payload = dumps({'target_task_id': target_task_id})
    with service.connection('view', True) as db:
        service._task(db, chat_task_id)
        service._task(db, target_task_id)
        key += '-' + service.user['id']
        row = db.execute("SELECT id FROM testing_agent_chat_messages WHERE task_id=? AND actor_id='agent' AND request_key=?",
                         (chat_task_id, key)).fetchone()
        if not row:
            db.execute('INSERT INTO testing_agent_chat_messages(id,task_id,actor_id,role,kind,content,state,request_key,created_at,updated_at,audience_user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identifier(), chat_task_id, 'agent', 'assistant', 'clarification_bundle', payload, 'completed', key, now(), now(), service.user['id']))
    return {'target_task_id': target_task_id, 'displayed_in_chat': True,
            'questions': questions, 'notice': '所有阻断问题在同一消息中收集答复，统一人工确认后才交给模型。'}


def build_platform_tools(service, chat_task_id):
    from sdlc.testing_agent.tools import BusinessTool, parameters
    from sdlc.testing_agent.platform_actions import ACTIONS, propose_action
    from sdlc.testing_agent.audit_queries import case_review_history, team_join_history
    string = {'type': 'string', 'minLength': 1, 'maxLength': 200}

    def list_items(kind, task_id=None, keyword='', page=1):
        if type(page) is not int or page < 1 or len(keyword) > 200:
            raise ServiceError('查询页码或关键词无效。')
        offset, size = (page - 1) * 20, 20
        task_id = task_id or chat_task_id
        with service.connection() as db:
            user = service.user
            if kind == 'projects':
                items = service.legacy.list_projects(limit=500)
            elif kind == 'tasks':
                items = [{k: t[k] for k in ('id', 'goal', 'scope', 'status', 'stage', 'run_id', 'document_version')}
                         for t in service.list_tasks(limit=500)]
            elif kind == 'runs':
                items = service.legacy.list_runs(limit=500)
            elif kind == 'exports':
                items = service.legacy.list_exports()
            elif kind == 'resources':
                from sdlc.testing_agent.resources import ResourceLibrary
                items = ResourceLibrary(service.db_path, service.token, service.workspace_id, service.project_id).list()
            elif kind == 'questions':
                task = service._task(db, task_id)
                items = [service._question(row) for row in db.execute('SELECT * FROM testing_agent_questions WHERE task_id=? ORDER BY id', (task['id'],))]
            elif kind == 'team_members':
                items = [dict(row) for row in db.execute('SELECT u.id,u.username,u.display_name,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY u.display_name,u.id', (service.workspace_id,))]
            elif kind == 'join_requests':
                Accounts.authorize(db, service.token, service.workspace_id, 'manage')
                items = [dict(row) for row in db.execute('SELECT r.id,r.user_id,u.username,u.display_name,r.status,r.created_at,r.decided_by,r.decided_at FROM join_requests r JOIN users u ON u.id=r.user_id WHERE r.workspace_id=? ORDER BY r.created_at DESC', (service.workspace_id,))]
            elif kind == 'profile':
                items = [{k: user[k] for k in ('id', 'username', 'display_name', 'role', 'kind')}
                         | {'permissions': sorted(user['permissions'])}]
            else:
                raise ServiceError('查询类型无效。')
        if keyword:
            items = [item for item in items if keyword.casefold() in dumps(item).casefold()]
        return {'kind': kind, 'items': items[offset:offset + size], 'total': len(items),
                'page': page, 'page_size': size, 'workspace_id': service.workspace_id,
                'project_id': service.project_id}

    def read_record(kind, record_id, case_id=None):
        if kind == 'case_approval':
            if not case_id:
                raise ServiceError('请提供查询结果中的用例编号。')
            return case_review_history(service, record_id, case_id)
        if kind == 'user_approval':
            return team_join_history(service, user_id=record_id)
        if kind == 'task':
            task = service.get_task(record_id)
            return {k: task[k] for k in ('id', 'goal', 'scope', 'status', 'stage', 'run_id', 'revision', 'questions', 'proposals')}
        if kind == 'resource':
            from sdlc.testing_agent.resources import ResourceLibrary
            library = ResourceLibrary(service.db_path, service.token, service.workspace_id, service.project_id)
            result = library.snapshot([record_id])
            return {'resources': result}
        raise ServiceError('记录类型无效。')

    def propose(action, arguments, summary):
        result = propose_action(service, chat_task_id, action, arguments, summary)
        # The human confirmation card is durable before this turn finishes.
        raise ToolDelivered(dumps({'action_id': result['id'], 'action': action,
            'status': result['status'], 'summary': result['summary'],
            'notice': '具体变更已在聊天中展示，等待用户确认执行。'}))

    def show_questions(target_task_id=None):
        result = publish_clarifications(service, chat_task_id, target_task_id or chat_task_id)
        raise ToolDelivered(dumps(result))

    def draft_answers(target_task_id, answers):
        from sdlc.testing_agent.clarifications import save_bundle_draft
        if len({a['question_id'] for a in answers}) != len(answers):
            raise ServiceError('答复中的问题编号不能重复。')
        result = save_bundle_draft(service, target_task_id,
            {a['question_id']: a['answer'] for a in answers},
            {a['question_id']: a['expected_revision'] for a in answers})
        publish_clarifications(service, chat_task_id, target_task_id)
        raise ToolDelivered(dumps({'result': result,
            'notice': '答复草稿已填入统一确认消息；尚未作为业务事实发送给生成模型。'}))

    def account_page(section):
        # Account secrets and uploaded images belong in the authenticated form.
        from sdlc.testing_agent.chat import ChatService
        labels = {'profile': ('个人资料与头像', '/auth/profile'),
                  'password': ('修改密码', '/auth/profile?section=password'),
                  'bindings': ('登录账号与绑定', '/auth/profile?section=account')}
        label, url = labels[section]
        ChatService(service).handle_event(chat_task_id, {'type': 'assistant_message',
            'message_id': identifier(), 'content': f'可以在这里完成：[{label}]({url})。'})
        raise ToolDelivered(dumps({'opened_in_chat': True, 'section': section}))

    catalog = [{'action': name, **spec} for name, spec in ACTIONS.items()]
    return [
        BusinessTool('list_platform_items', '查询当前授权空间/项目的项目、任务、运行、用例资料、澄清、团队成员及入队申请。先查真实对象ID，再选择后续操作；不能查询其他用户个人空间。',
            parameters({'kind': {'type': 'string', 'enum': ['projects', 'tasks', 'runs', 'exports', 'resources', 'questions', 'team_members', 'join_requests', 'profile']},
                        'task_id': string, 'keyword': {'type': 'string', 'maxLength': 200}, 'page': {'type': 'integer', 'minimum': 1}}, ['kind']), list_items),
        BusinessTool('read_platform_record', '读取任务及其全部澄清、准备资料、指定用例审批历史或当前团队用户入队审批人。没有记录时明确说明；禁止从历史讨论猜测审批人。',
            parameters({'kind': {'type': 'string', 'enum': ['task', 'resource', 'case_approval', 'user_approval']}, 'record_id': string, 'case_id': string}, ['kind', 'record_id']), read_record),
        BusinessTool('platform_action_catalog', '读取平台可通过聊天办理的操作、参数及限制。操作由你根据用户目标选择；若目标不明确先询问，禁止猜测ID或用导航按钮代替已支持的操作。', parameters(), lambda: {'actions': catalog}),
        BusinessTool('propose_platform_action', '准备平台操作供用户确认，包括用例修改/重生成、澄清修改、审批、导出和团队管理。此工具只保存提案和确认卡片，不执行批准；用户确认后由应用按其真实权限执行。参数必须来自catalog，目标ID来自授权查询。',
            parameters({'action': {'type': 'string', 'enum': list(ACTIONS)}, 'arguments': {'type': 'object'},
                        'summary': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}, ['action', 'arguments', 'summary']), propose),
        BusinessTool('show_blocking_clarifications', '在聊天中集中展示指定任务的全部待处理阻断澄清，统一收集与确认；可从用例关联的task_id定位。',
            parameters({'target_task_id': string}), show_questions),
        BusinessTool('prepare_clarification_answers', '根据用户本轮与历史原话整理多项澄清答复草稿，一次填入同一确认消息；未知内容保留未知，禁止补造业务规则。只保存草稿，所有阻断项须由人统一确认才继续生成。',
            parameters({'target_task_id': string, 'answers': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                'items': parameters({'question_id': string, 'answer': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                                     'expected_revision': {'type': 'integer', 'minimum': 1}}, ['question_id', 'answer', 'expected_revision'])}}, ['target_task_id', 'answers']), draft_answers),
        BusinessTool('open_account_page', '在聊天中提供当前登录账号的个人资料/头像、修改密码或账号绑定入口；密码、密钥和图片在专用表单操作，不能要求用户发到聊天里。',
            parameters({'section': {'type': 'string', 'enum': ['profile', 'password', 'bindings']}}, ['section']), account_page),
    ]

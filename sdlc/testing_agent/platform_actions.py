"""Durable, human-confirmed actions over the authenticated application services.

The model can propose an action, but never receives a confirmation tool. A user's
confirmation binds identity, target and the reviewed versions. Domain effects and
the receipt commit together; retries cannot duplicate a project, revision or job.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json

from jsonschema import Draft202012Validator

from sdlc.accounts import Accounts
from sdlc.service import ServiceError, dumps, identifier, now


def _schema(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


_ID = {'type': 'string', 'minLength': 1, 'maxLength': 128}
_TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
_CASE = {'run_id': _ID, 'case_id': _ID}
ACTIONS = {
    'project_create': {'description': '在当前工作空间创建项目', 'capability': 'generate', 'parameters': _schema({
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
        'description': {'type': 'string', 'maxLength': 500}}, ['name', 'description'])},
    'project_update': {'description': '修改当前项目名称与简介', 'capability': 'edit', 'parameters': _schema({
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
        'description': {'type': 'string', 'maxLength': 500}}, ['name', 'description'])},
    'project_archive': {'description': '归档当前项目或恢复归档', 'capability': 'manage', 'parameters': _schema({
        'archived': {'type': 'boolean'}}, ['archived'])},
    'case_review': {'description': '人工确认通过或退回指定用例当前版本', 'capability': 'review', 'parameters': _schema({
        **_CASE, 'decision': {'type': 'string', 'enum': ['approve', 'return']},
        'reason': {'type': 'string', 'maxLength': 2000}}, ['run_id', 'case_id', 'decision', 'reason'])},
    'case_edit': {'description': '保存指定用例的新版本，重新等待人工评审', 'capability': 'edit', 'parameters': _schema({
        **_CASE, 'content': {'type': 'object'}, 'reason': _TEXT}, ['run_id', 'case_id', 'content', 'reason'])},
    'case_regenerate': {'description': '依据反馈定向重新生成指定用例的修订建议，保留原版本与审批记录',
        'capability': 'regenerate', 'parameters': _schema({**_CASE, 'reason': _TEXT}, ['run_id', 'case_id', 'reason'])},
    'task_cancel': {'description': '停止指定测试任务，保留已有成果与历史', 'capability': 'generate',
        'parameters': _schema({'task_id': _ID}, ['task_id'])},
    'approved_export': {'description': '通过全部交付检查后建立批准基线并生成导出文件', 'capability': 'export',
        'parameters': _schema({'run_id': _ID, 'format': {'type': 'string', 'enum': ['csv', 'md']}}, ['run_id', 'format'])},
    'clarification_edit': {'description': '修改指定澄清内容或阻断性，清除旧答复并重新评审受影响用例',
        'capability': 'manage', 'parameters': _schema({
            'task_id': _ID, 'question_id': _ID, 'question_text': _TEXT, 'blocking': {'type': 'boolean'},
            'reason': _TEXT, 'paragraph_ids': {'type': 'array', 'items': _ID},
            'requirement_ids': {'type': 'array', 'items': _ID},
            'affected_case_ids': {'type': 'array', 'items': _ID},
            'scope': {'type': 'string', 'enum': ['run', 'case']}},
            ['task_id', 'question_id', 'question_text', 'blocking', 'reason'])},
    'team_create': {'description': '创建团队，当前账号成为团队所有者', 'capability': 'view',
        'parameters': _schema({'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                              'description': {'type': 'string', 'maxLength': 500}}, ['name', 'description'])},
    'team_update': {'description': '修改当前团队名称与简介', 'capability': 'manage',
        'parameters': _schema({'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                              'description': {'type': 'string', 'maxLength': 500}}, ['name', 'description'])},
    'team_join_decide': {'description': '审核当前团队的加入申请并指定系统角色', 'capability': 'manage',
        'parameters': _schema({'request_id': _ID, 'approve': {'type': 'boolean'},
                              'role': {'type': 'string', 'enum': ['admin', 'editor', 'reviewer', 'observer']},
                              'reason': {'type': 'string', 'maxLength': 1000}}, ['request_id', 'approve', 'role', 'reason'])},
    'team_member_role': {'description': '修改当前团队其他成员的系统角色与业务权限', 'capability': 'manage',
        'parameters': _schema({'user_id': _ID, 'role': {'type': 'string', 'enum': ['admin', 'editor', 'reviewer', 'observer']},
                              'overrides': {'type': 'object', 'additionalProperties': {'type': 'boolean'}}}, ['user_id', 'role'])},
    'resource_create': {'description': '创建当前项目的共享测试准备资料', 'capability': 'generate',
        'parameters': _schema({'kind': {'type': 'string', 'enum': ['precondition', 'steps', 'test_data']},
                              'title': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                              'body': {'type': 'string', 'minLength': 1, 'maxLength': 8000}}, ['kind', 'title', 'body'])},
    'resource_update': {'description': '保存共享资料新版本，已有任务快照保持不变', 'capability': 'edit',
        'parameters': _schema({'resource_id': _ID, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                              'body': {'type': 'string', 'minLength': 1, 'maxLength': 8000}}, ['resource_id', 'title', 'body'])},
    'resource_archive': {'description': '归档或恢复当前项目共享资料', 'capability': 'manage',
        'parameters': _schema({'resource_id': _ID, 'archived': {'type': 'boolean'}}, ['resource_id', 'archived'])},
}

ACTIONS['clarification_edit_batch'] = {
    'description': '统一确认并修改同一任务的多项澄清，逐项保留旧内容与修改记录',
    'capability': 'manage',
    'parameters': _schema({'changes': {'type': 'array', 'minItems': 1, 'maxItems': 100,
        'items': ACTIONS['clarification_edit']['parameters']}}, ['changes'])}
ACTIONS['team_join_request'] = {
    'description': '使用用户提供的邀请码提交加入团队申请，等待该团队管理员审核',
    'capability': 'view', 'parameters': _schema({
        'code': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        'note': {'type': 'string', 'maxLength': 1000}}, ['code', 'note'])}

SCHEMA = '''CREATE TABLE IF NOT EXISTS testing_agent_platform_actions (
 id TEXT PRIMARY KEY, chat_task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id),
 workspace_id TEXT NOT NULL, project_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 action TEXT NOT NULL, summary TEXT NOT NULL, arguments_json TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 revision INTEGER NOT NULL DEFAULT 1, result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)'''


def initialize(service):
    from sdlc.testing_agent.chat import ChatService
    from sdlc.testing_agent.worker import initialize as initialize_worker
    initialize_worker(service.db_path)
    ChatService(service)
    from sdlc.testing_agent.resources import ResourceLibrary
    ResourceLibrary(service.db_path, service.token, service.workspace_id, service.project_id)
    with service.connection('view', True) as db:
        db.execute(SCHEMA)


def _resources(service):
    from sdlc.testing_agent.resources import ResourceLibrary
    library = object.__new__(ResourceLibrary)
    library.db_path, library.token = service.db_path, service.token
    library.workspace_id, library.project_id = service.workspace_id, service.project_id
    library.service, library.accounts = service.legacy, service.accounts
    return library


def _record(row):
    if row is None:
        raise ServiceError('操作不存在或无权访问。')
    result = dict(row)
    for name in ('arguments', 'snapshot', 'result'):
        value = result.pop(name + '_json')
        result[name] = json.loads(value) if value else None
    return result


def _owned_action(service, db, chat_task_id, action_id):
    service._task(db, chat_task_id)
    return _record(db.execute('''SELECT * FROM testing_agent_platform_actions
        WHERE id=? AND chat_task_id=? AND workspace_id=? AND project_id=? AND actor_id=?''',
        (action_id, chat_task_id, service.workspace_id, service.project_id, service.user['id'])).fetchone())


def list_actions(service, chat_task_id):
    initialize(service)
    with service.connection() as db:
        service._task(db, chat_task_id)
        return [_record(row) for row in db.execute('''SELECT * FROM testing_agent_platform_actions
            WHERE chat_task_id=? AND workspace_id=? AND project_id=? AND actor_id=?
            ORDER BY created_at,id''', (chat_task_id, service.workspace_id, service.project_id, service.user['id']))]


def get_action(service, chat_task_id, action_id):
    initialize(service)
    with service.connection() as db:
        return _owned_action(service, db, chat_task_id, action_id)


@contextmanager
def _transaction_service(service, db):
    """Isolated instance adapters let existing domain methods share one unit of work.

    No class/global monkeypatching, SQL authorization bypass or nested commit. The
    normal secure method wrappers and every service ACL check still execute.
    """
    scoped = copy.copy(service)
    scoped.user = dict(service.user)
    scoped.accounts = copy.copy(service.accounts)
    scoped.legacy = copy.copy(service.legacy)
    scoped.legacy.accounts = scoped.accounts

    @contextmanager
    def database(write=False):
        yield db

    @contextmanager
    def legacy_connection(write=False, initialize=False):
        if initialize:
            raise ServiceError('操作事务内不能重新初始化数据库。')
        scoped.legacy.user = Accounts.authorize(db, scoped.token, scoped.workspace_id, scoped.legacy._capability)
        if scoped.project_id:
            scoped.legacy._owned(db, scoped.project_id)
        yield db

    scoped.accounts.db = database
    scoped.legacy.connection = legacy_connection
    yield scoped


def _snapshot(service, action, arguments):
    capability = ACTIONS[action]['capability']
    with service.connection() as db:
        service.user = Accounts.authorize(db, service.token, service.workspace_id, capability)
        project = service.legacy._read_project(db, service.project_id)
        if project['status'] != 'active' and not (action == 'project_archive' and not arguments['archived']):
            raise ServiceError('项目已归档，不能执行此操作。')
        if action == 'clarification_edit_batch':
            changes = arguments['changes']
            identities = {(item['task_id'], item['question_id']) for item in changes}
            if len(identities) != len(changes) or len({item['task_id'] for item in changes}) != 1:
                raise ServiceError('请一次修改同一任务中的不同澄清问题。')
            return {'questions': [_snapshot(service, 'clarification_edit', item) for item in changes]}
        if action == 'team_join_request':
            return service.accounts.request_join(service.token, arguments['code'], confirm=False)
        if action.startswith('case_') or action == 'approved_export':
            run = service.legacy.get_run(arguments['run_id'])
            if action == 'approved_export':
                return {'run_revision': run['revision'], 'cases': [
                    {k: case[k] for k in ('case_id', 'version', 'revision', 'status')} for case in run['cases']]}
            case = next((case for case in run['cases'] if case['case_id'] == arguments['case_id']), None)
            if not case:
                raise ServiceError('用例不存在或不属于当前项目。')
            if action == 'case_review' and arguments['decision'] == 'return' and not arguments['reason'].strip():
                raise ServiceError('退回需要填写原因。')
            if action == 'case_edit' and arguments['content'].get('case_id') != arguments['case_id']:
                raise ServiceError('不能修改用例编号。')
            source = db.execute('SELECT id,status FROM testing_agent_tasks WHERE run_id=? AND workspace_id=? AND project_id=?',
                (arguments['run_id'], service.workspace_id, service.project_id)).fetchone()
            if source and (source['status'] == 'cancelled' or (action == 'case_review' and source['status'] == 'completed')):
                raise ServiceError('该任务已取消或交付，不能再审核原用例；请创建关联修订轮次。')
            if action == 'case_regenerate' and not source:
                raise ServiceError('此用例没有 Agent 任务关联，请先在运行详情中修订。')
            return {'version': case['version'], 'revision': case['revision'], 'status': case['status'],
                    'title': case['content']['title'], 'content': case['content'],
                    'source_task_id': source['id'] if source else None,
                    'source_locked': source['status'] in {'completed', 'cancelled'} if source else False}
        if action in {'project_update', 'project_archive'}:
            return {k: project[k] for k in ('id', 'name', 'description', 'revision', 'status')}
        if action == 'task_cancel':
            task = service._task(db, arguments['task_id'])
            service._active(task)
            return {k: task[k] for k in ('id', 'goal', 'input_hash')}
        if action == 'clarification_edit':
            task = service._task(db, arguments['task_id'])
            service._active(task)
            question = db.execute('SELECT * FROM testing_agent_questions WHERE task_id=? AND id=?',
                                  (task['id'], arguments['question_id'])).fetchone()
            if not question:
                raise ServiceError('澄清问题不存在或无权访问。')
            return {'question_revision': question['revision'],
                    'question': json.loads(question['content_json'])}
        if action in {'resource_update', 'resource_archive'}:
            library = _resources(service)
            head = library._head(db, arguments['resource_id'])
            return library._version(db, head)
        if action.startswith('team_') and action != 'team_create':
            workspace = db.execute('SELECT kind,name,description FROM workspaces WHERE id=?',
                                   (service.workspace_id,)).fetchone()
            if not workspace or workspace['kind'] != 'team':
                raise ServiceError('此操作仅适用于当前团队空间。')
            if action == 'team_update':
                return dict(workspace)
            if action == 'team_join_decide':
                request = db.execute('SELECT id,user_id,status FROM join_requests WHERE id=? AND workspace_id=?',
                    (arguments['request_id'], service.workspace_id)).fetchone()
                if not request or request['status'] != 'pending':
                    raise ServiceError('加入申请不存在、已处理或不属于当前团队。')
                return dict(request)
            member = db.execute('SELECT user_id,role,overrides_json FROM memberships WHERE workspace_id=? AND user_id=?',
                (service.workspace_id, arguments['user_id'])).fetchone()
            if not member:
                raise ServiceError('成员不存在或不属于当前团队。')
            return dict(member)
        return {'workspace_id': service.workspace_id}


def _summary(action, args, snapshot):
    label = ACTIONS[action]['description']
    if action.startswith('case_'):
        return f"{label}：{args['case_id']} · {snapshot['title']} · 第 {snapshot['version']} 版"
    if action == 'clarification_edit':
        return f"{label}：{args['question_id']} · {'阻断' if args['blocking'] else '非阻断'}"
    return label


def propose_action(service, chat_task_id, action, arguments, summary=''):
    if action not in ACTIONS:
        raise ServiceError('此操作尚未开放对话执行。')
    if not isinstance(arguments, dict) or len(dumps(arguments)) > 40_000:
        raise ServiceError('操作参数无效或过长。')
    if list(Draft202012Validator(ACTIONS[action]['parameters']).iter_errors(arguments)):
        raise ServiceError('操作参数不符合接口约束，请提供完整且有效的参数。')
    initialize(service)
    with service.connection('view', True) as db:
        service._task(db, chat_task_id)
        with _transaction_service(service, db) as scoped:
            snapshot = _snapshot(scoped, action, arguments)
        # Model prose cannot obscure the server-selected operation or target.
        summary = _summary(action, arguments, snapshot)
        prior = db.execute('''SELECT * FROM testing_agent_platform_actions WHERE chat_task_id=?
            AND actor_id=? AND action=? AND arguments_json=? AND snapshot_json=? AND status='pending' ''',
            (chat_task_id, service.user['id'], action, dumps(arguments), dumps(snapshot))).fetchone()
        if prior:
            return _record(prior)
        action_id, stamp = identifier(), now()
        db.execute('''INSERT INTO testing_agent_platform_actions
            (id,chat_task_id,workspace_id,project_id,actor_id,action,summary,arguments_json,snapshot_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (action_id, chat_task_id, service.workspace_id, service.project_id, service.user['id'], action,
             summary, dumps(arguments), dumps(snapshot), stamp, stamp))
        db.execute('''INSERT INTO testing_agent_chat_messages
            (id,task_id,actor_id,role,kind,content,state,request_key,created_at,updated_at,audience_user_id)
            VALUES(?,?,?,'assistant','platform_action',?,'completed',?,?,?,?)''',
            (identifier(), chat_task_id, service.user['id'], dumps({'action_id': action_id}),
             'platform-action-' + action_id, stamp, stamp, service.user['id']))
        service._event(db, chat_task_id, 'platform_action_proposed', {'action_id': action_id, 'action': action})
        return _owned_action(service, db, chat_task_id, action_id)


def _revision_target(service, db, record):
    from sdlc.domain import prepare_input
    args, snapshot = record['arguments'], record['snapshot']
    if not snapshot.get('source_task_id'):
        return None
    source = service.get_task(snapshot['source_task_id'])
    target_id = source['id']
    # A restricted regeneration capability may clone existing evidence, never
    # introduce new business facts or inherit an approval into a changed version.
    service.execution_capability = 'regenerate'
    if source['status'] == 'completed':
        target_id = service._insert_task(db, prepare_input(text=source['input']['original_text']),
            source['goal'], source['scope'], source['id'], source['owner_id'], source['confirmer_id'], source['due_at'])
        analysis = service.artifact(source['id'], 'analysis')
        plan = service.artifact(source['id'], 'coverage_plan')
        if not analysis or not plan:
            raise ServiceError('源任务缺少需求分析或覆盖计划，无法安全创建修订轮次。')
        service.save_analysis(target_id, analysis)
        # Save without implicit draft reuse, then copy current reviewed versions.
        service.add_artifact(target_id, 'coverage_plan', plan)
        current = {case['case_id']: case['content'] for case in service.legacy.get_run(args['run_id'])['cases']}
        drafts = [{**draft, 'case': current.get(draft['case']['case_id'], draft['case'])}
                  for draft in service.case_drafts(source['id'])]
        service.save_batch(target_id, 'confirmed-regeneration-source', {'cases': drafts})
        service.publish_partial(target_id)
    return target_id


def _regenerate(service, db, record):
    from sdlc.testing_agent.worker import _enqueue_in_db
    args = record['arguments']
    target_id = _revision_target(service, db, record)
    target = service.get_task(target_id)
    service._active(target)
    case = next(case for case in service.legacy.get_run(target['run_id'])['cases'] if case['case_id'] == args['case_id'])
    feedback = service.add_proposal(target_id, 'revision',
        {'category': 'unclear_expected', 'reason': args['reason'], 'business_fact_confirmed': False,
         'platform_action_id': record['id']}, args['case_id'], case['version'], case['revision'], capability='regenerate')
    content = ('用户已确认定向修订用例 ' + args['case_id'] + '。请读取反馈提案 ' + feedback
               + '，依据现有需求调用 propose_revision，为这一个用例提出新版本建议，保留其他用例和原审批记录。'
               + '\n用户反馈：' + args['reason'])
    job_id = _enqueue_in_db(service, db, target_id, 'message', content,
                            'platform-action-' + record['id'], record_chat=True)
    payload = json.loads(db.execute('SELECT payload FROM agent_work_items WHERE id=?', (job_id,)).fetchone()[0])
    payload.update(platform_action_id=record['id'], platform_chat_task_id=record['chat_task_id'],
                   regeneration_case_id=args['case_id'], feedback_proposal_id=feedback)
    db.execute('UPDATE agent_work_items SET payload=? WHERE id=?', (dumps(payload), job_id))
    return {'target_task_id': target_id, 'job_id': job_id, 'feedback_proposal_id': feedback,
            'case_id': args['case_id'], 'requires_review': True}


def _execute(service, db, record):
    action, args, snap = record['action'], record['arguments'], record['snapshot']
    key = 'platform-action-' + record['id']
    if action == 'project_create':
        return {'project_id': service.legacy.create_project(args['name'], args['description'])}
    if action == 'project_update':
        service.legacy.update_project(service.project_id, args['name'], args['description'], '', snap['revision'])
        return {'project_id': service.project_id}
    if action == 'project_archive':
        service.legacy.set_project_archived(service.project_id, args['archived'], '', snap['revision'])
        return {'project_id': service.project_id, 'archived': args['archived']}
    if action == 'case_review':
        event = service.legacy.review(args['run_id'], args['case_id'], snap['version'], snap['revision'],
                                     args['decision'], '', args['reason'].strip() or '人工确认通过，未补充说明。', key)
        return {'event_id': event, 'run_id': args['run_id'], 'case_id': args['case_id']}
    if action == 'case_edit':
        target_id = _revision_target(service, db, record) if snap.get('source_locked') else None
        target_run = service.get_task(target_id)['run_id'] if target_id else args['run_id']
        current = next(case for case in service.legacy.get_run(target_run)['cases'] if case['case_id'] == args['case_id'])
        version = service.legacy.edit(target_run, args['case_id'], current['version'], current['revision'],
                                      args['content'], '', args['reason'], key)
        return {'version': version, 'run_id': target_run, 'case_id': args['case_id'],
                'target_task_id': target_id, 'requires_review': True}
    if action == 'case_regenerate':
        return _regenerate(service, db, record)
    if action == 'task_cancel':
        service.cancel_task(args['task_id'])
        db.execute("UPDATE agent_work_items SET status='cancelled',lease_until=NULL WHERE task_id=? AND status IN ('queued','running')", (args['task_id'],))
        return {'task_id': args['task_id'], 'status': 'cancelled'}
    if action == 'approved_export':
        source = db.execute('SELECT id,status FROM testing_agent_tasks WHERE run_id=? AND workspace_id=? AND project_id=?',
            (args['run_id'], service.workspace_id, service.project_id)).fetchone()
        if source and source['status'] != 'completed':
            service.create_baseline(source['id'])
        exported = service.legacy.export(args['run_id'], args['format'], '', snap['run_revision'], key)
        return {name: exported[name] for name in ('id', 'filename', 'mime', 'content_hash')}
    if action == 'clarification_edit':
        from sdlc.testing_agent.clarifications import edit_question
        result = edit_question(service, expected_revision=snap['question_revision'], **args)
        return result if isinstance(result, dict) else {'task_id': args['task_id'], 'question_id': args['question_id']}
    if action == 'clarification_edit_batch':
        from sdlc.testing_agent.clarifications import edit_question
        return {'task_id': args['changes'][0]['task_id'], 'questions': [
            edit_question(service, expected_revision=version['question_revision'], **change)
            for change, version in zip(args['changes'], snap['questions'], strict=True)]}
    if action == 'team_create':
        return {'workspace_id': service.accounts.create_team(service.token, args['name'], args['description'])}
    if action == 'team_join_request':
        return service.accounts.request_join(service.token, args['code'], args['note'], confirm=True)
    if action.startswith('resource_'):
        library = _resources(service)
        if action == 'resource_create':
            result = library.create(**args)
        elif action == 'resource_update':
            result = library.update(expected_revision=snap['revision'], **args)
        else:
            result = library.archive(expected_revision=snap['revision'], **args)
        return {'resource_id': result['id'], 'revision': result['revision'], 'archived': result['archived']}
    if action == 'team_update':
        service.accounts.update_team(service.token, service.workspace_id, args['name'], args['description'])
    elif action == 'team_join_decide':
        service.accounts.decide_join(service.token, service.workspace_id, **args)
    elif action == 'team_member_role':
        service.accounts.set_member(service.token, service.workspace_id, **args)
    else:
        raise ServiceError('不支持的操作。')
    return {'workspace_id': service.workspace_id}


def confirm_action(service, chat_task_id, action_id, expected_revision=1):
    initialize(service)
    with service.connection('view', True) as db:
        record = _owned_action(service, db, chat_task_id, action_id)
        if record['status'] == 'completed':
            return record
        if record['status'] != 'pending' or record['revision'] != expected_revision:
            raise ServiceError('操作已处理或版本已变化，请查看最新消息。')
        with _transaction_service(service, db) as scoped:
            snapshot = _snapshot(scoped, record['action'], record['arguments'])
            if snapshot != record['snapshot']:
                raise ServiceError('目标内容或审核状态已变化，请让 Agent 基于最新版本重新准备操作。')
            result = _execute(scoped, db, record)
        db.execute("UPDATE testing_agent_platform_actions SET status='completed',revision=revision+1,result_json=?,updated_at=? WHERE id=?",
                   (dumps(result), now(), action_id))
        service._event(db, chat_task_id, 'platform_action_confirmed', {'action_id': action_id, 'action': record['action'],
            'snapshot_hash': hashlib.sha256(dumps(record['snapshot']).encode()).hexdigest()})
        return _owned_action(service, db, chat_task_id, action_id)


def cancel_action(service, chat_task_id, action_id, expected_revision=1):
    initialize(service)
    with service.connection('view', True) as db:
        record = _owned_action(service, db, chat_task_id, action_id)
        if record['status'] == 'cancelled':
            return record
        if record['status'] != 'pending' or record['revision'] != expected_revision:
            raise ServiceError('操作已处理或版本已变化。')
        db.execute("UPDATE testing_agent_platform_actions SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (now(), action_id))
        service._event(db, chat_task_id, 'platform_action_cancelled', {'action_id': action_id})
        return _owned_action(service, db, chat_task_id, action_id)


def confirmed_regeneration(service, task_id, request_id):
    """Validate the queued human decision; arbitrary message text grants nothing."""
    if not request_id:
        return None
    with service.connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_work_items'").fetchone():
            return None
        job = db.execute('SELECT * FROM agent_work_items WHERE id=? AND task_id=? AND workspace_id=? AND project_id=? AND actor_id=?',
            (request_id, task_id, service.workspace_id, service.project_id, service.user['id'])).fetchone()
        if not job:
            return None
        payload = json.loads(job['payload'])
        if not payload.get('platform_action_id'):
            return None
        record = _owned_action(service, db, payload.get('platform_chat_task_id'), payload['platform_action_id'])
        result = record['result'] or {}
        if (record['status'] != 'completed' or record['action'] != 'case_regenerate'
                or result.get('job_id') != request_id or result.get('target_task_id') != task_id
                or result.get('case_id') != payload.get('regeneration_case_id')
                or result.get('feedback_proposal_id') != payload.get('feedback_proposal_id')):
            raise ServiceError('重生成请求缺少对应的人工确认或已发生变化。')
        return result

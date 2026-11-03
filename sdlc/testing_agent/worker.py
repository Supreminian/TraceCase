"""Durable, leased local worker. Credentials are minted in memory, never queued."""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import json
import secrets
import threading
import time
from uuid import uuid4

from sdlc.accounts import Accounts, AccessError, digest


SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_work_items(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL,
 task_id TEXT NOT NULL, actor_id TEXT NOT NULL, operation TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 created_at REAL NOT NULL, updated_at REAL NOT NULL, lease_owner TEXT,
 lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
 request_key TEXT, UNIQUE(workspace_id,project_id,actor_id,request_key));
CREATE UNIQUE INDEX IF NOT EXISTS agent_work_active ON agent_work_items(task_id)
 WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS agent_work_poll ON agent_work_items(status,lease_until,created_at);
'''


def initialize(path):
    store = Accounts(path)
    with store.db(write=True) as db:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
    return store


def _enqueue_in_db(service, db, task_id, operation='start', content='', request_key=None,
                   *, confirmation_id=None, record_chat=True):
    """The shared transaction is also used by explicit confirmation responses."""
    from sdlc.testing_agent.chat import ChatService
    if operation not in {'start', 'resume', 'message'}:
        raise ValueError('未知任务操作。')
    if not isinstance(content, str) or len(content) > 20_000:
        raise ValueError('补充信息过长。')
    task = service._task(db, task_id)
    project = db.execute('SELECT status FROM projects WHERE id=?', (service.project_id,)).fetchone()
    if not project or project['status'] != 'active':
        raise ValueError('项目已归档，不能继续任务；历史记录仍可查看。')
    if not (operation == 'message' and task['status'] == 'completed'):
        service._active(task)
    user = Accounts.authorize(db, service.token, service.workspace_id)
    capability = 'generate' if 'generate' in user['permissions'] else 'regenerate'
    if capability == 'regenerate' and operation != 'message':
        raise AccessError('当前角色只能发起已有用例的修订。')
    Accounts.authorize(db, service.token, service.workspace_id, capability)
    service.user = user
    db.execute('INSERT OR IGNORE INTO testing_agent_artifacts(id,task_id,kind,artifact_key,input_hash,content_json,created_at) VALUES(?,?,?,?,?,?,?)',
               (uuid4().hex, task_id, 'interaction_policy', 'chat-v1', task['input_hash'],
                json.dumps({'mode': 'chat', 'plan_confirmation_required': True}), task['updated_at']))
    pending = db.execute("SELECT id FROM testing_agent_chat_confirmations WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
    if pending:
        raise ValueError('Agent 正在等待你的确认，请先选择下一步。')
    if confirmation_id is not None:
        resolution = db.execute("SELECT * FROM testing_agent_chat_confirmations WHERE id=? AND task_id=? AND status='resolved'", (confirmation_id, task_id)).fetchone()
        if not resolution or resolution['choice'] not in {'retry', 'refine', 'discuss'} or resolution['response_actor_id'] != user['id']:
            raise AccessError('生成确认不存在、已暂停或不属于当前账号。')
    elif task['status'] == 'waiting_confirmation':
        raise ValueError('请先通过 Agent 确认消息选择下一步。')
    if request_key:
        if not isinstance(request_key, str) or len(request_key) > 128:
            raise ValueError('请求标识过长。')
        old = db.execute('SELECT * FROM agent_work_items WHERE workspace_id=? AND project_id=? AND actor_id=? AND request_key=?',
                         (service.workspace_id, service.project_id, user['id'], request_key)).fetchone()
        if old:
            if json.loads(old['payload']).get('content') != content or old['operation'] != operation or old['task_id'] != task_id:
                raise ValueError('请求标识已用于不同任务。')
            return old['id']
    old = db.execute("SELECT * FROM agent_work_items WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchone()
    if old:
        if operation == 'message' or confirmation_id:
            raise ValueError('任务正在处理，请完成当前步骤后补充信息。')
        return old['id']
    active = db.execute("SELECT COUNT(*) FROM agent_work_items WHERE workspace_id=? AND status IN ('queued','running')", (service.workspace_id,)).fetchone()[0]
    global_count = db.execute("SELECT COUNT(*) FROM agent_work_items WHERE status IN ('queued','running')").fetchone()[0]
    admitted = db.execute("SELECT COUNT(*) FROM agent_work_items WHERE workspace_id=? AND created_at>?", (service.workspace_id, time.time() - 86400)).fetchone()[0]
    if active >= 2 or global_count >= 20 or admitted >= 100:
        raise ValueError('任务队列或本日任务额度已满，请稍后重试。')
    job_id = uuid4().hex
    db.execute('INSERT INTO agent_work_items(id,workspace_id,project_id,task_id,actor_id,operation,payload,created_at,updated_at,request_key) VALUES (?,?,?,?,?,?,?,?,?,?)',
               (job_id, service.workspace_id, service.project_id, task_id, user['id'], operation,
                json.dumps({'content': content, 'capability': capability, 'confirmation_id': confirmation_id}, ensure_ascii=False), time.time(), time.time(), request_key))
    if record_chat:
        # Construct without opening another SQLite transaction.
        chat = object.__new__(ChatService)
        chat.service = service
        initial = content if operation == 'message' else task['input'].get('original_text', '') if operation == 'start' else '请继续当前测试任务。'
        if initial:
            chat._post_user_db(db, task_id, initial, 'job-' + job_id)
    return job_id


def enqueue(service, task_id, operation='start', content='', request_key=None, *, confirmation_id=None):
    from sdlc.testing_agent.chat import ChatService
    service.get_task(task_id)
    initialize(service.db_path)
    chat = ChatService(service)
    if operation == 'message' and confirmation_id is None:
        # All chat entry points accept the original user message while waiting.
        # The model decides whether it is consent, a question or new material.
        # Sending text alone never grants generation permission.
        if request_key:
            with service.connection() as db:
                previous = db.execute("SELECT * FROM testing_agent_chat_confirmations WHERE task_id=? AND response_actor_id=? AND response_key=?",
                    (task_id, service.user['id'], request_key)).fetchone()
                if previous:
                    if previous['content'] != content or previous['choice'] not in {'discuss', 'retry'}:
                        raise ValueError('请求标识已用于不同的对话内容。')
                    return previous['job_id']
        pending = chat.recover_waiting_confirmation(task_id)
        if pending:
            return chat.respond(task_id, pending['id'], 'discuss', content,
                expected_revision=pending['revision'], request_key=request_key)['job_id']
    with service.connection('view', True) as db:
        return _enqueue_in_db(service, db, task_id, operation, content, request_key, confirmation_id=confirmation_id)


def list_work(service, task_id):
    service.get_task(task_id)
    store = initialize(service.db_path)
    with store.db() as db:
        return [dict(r) for r in db.execute('SELECT id,status,operation,created_at,updated_at,attempts,error FROM agent_work_items WHERE workspace_id=? AND project_id=? AND task_id=? ORDER BY created_at DESC LIMIT 20',
                                           (service.workspace_id, service.project_id, task_id))]


def cancel_work(service, task_id):
    service.get_task(task_id)
    store = initialize(service.db_path)
    with store.db(write=True) as db:
        Accounts.authorize(db, service.token, service.workspace_id, 'generate')
        db.execute("UPDATE agent_work_items SET status='cancelled',lease_until=NULL,updated_at=? WHERE task_id=? AND workspace_id=? AND project_id=? AND status IN ('queued','running')",
                   (time.time(), task_id, service.workspace_id, service.project_id))


def claim(path, owner, lease_seconds=120):
    store = initialize(path)
    with store.db(write=True) as db:
        # The task row itself is a durable submission intent. Recover a crash
        # between task creation and queue insertion without storing credentials.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='testing_agent_tasks'").fetchone():
            orphan = db.execute("""SELECT t.*,(SELECT actor_id FROM testing_agent_events e
                WHERE e.task_id=t.id AND e.action='created' ORDER BY seq LIMIT 1) AS submitter
                FROM testing_agent_tasks t WHERE t.status='queued'
                AND NOT EXISTS(SELECT 1 FROM agent_work_items j WHERE j.task_id=t.id)
                AND (SELECT COUNT(*) FROM agent_work_items j WHERE j.workspace_id=t.workspace_id AND j.status IN ('queued','running'))<2
                AND (SELECT COUNT(*) FROM agent_work_items j WHERE j.workspace_id=t.workspace_id AND j.created_at>?)<100
                AND (SELECT COUNT(*) FROM agent_work_items j WHERE j.status IN ('queued','running'))<20
                ORDER BY t.created_at LIMIT 1""", (time.time() - 86400,)).fetchone()
            if orphan:
                db.execute("INSERT INTO agent_work_items(id,workspace_id,project_id,task_id,actor_id,operation,payload,created_at,updated_at) VALUES(?,?,?,?,?,'start','{}',?,?)",
                           (uuid4().hex, orphan['workspace_id'], orphan['project_id'], orphan['id'], orphan['submitter'] or orphan['owner_id'], time.time(), time.time()))
        row = db.execute("SELECT * FROM agent_work_items WHERE status='queued' OR (status='running' AND lease_until<?) ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
        if not row:
            db.execute("UPDATE agent_work_items SET status='failed',error='多次恢复未完成，请人工检查后重试。',updated_at=? WHERE status='running' AND lease_until<? AND attempts>=4", (time.time(), time.time()))
            return None
        db.execute("UPDATE agent_work_items SET status='running',lease_owner=?,lease_until=?,updated_at=?,attempts=attempts+1 WHERE id=?",
                   (owner, time.time() + lease_seconds, time.time(), row['id']))
        result = dict(db.execute('SELECT * FROM agent_work_items WHERE id=?', (row['id'],)).fetchone())
        result['requires_confirmation'] = row['status'] == 'running'
        return result


@contextmanager
def delegated_identity(path, job):
    """A short-lived, non-exported token for the recorded actor; current ACL still applies."""
    store = Accounts(path)
    token = secrets.token_urlsafe(48)
    with store.db(write=True) as db:
        actor = db.execute('SELECT * FROM users WHERE id=? AND enabled=1', (job['actor_id'],)).fetchone()
        if not actor:
            raise AccessError('任务发起账号已失效。')
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), actor['id'], time.time() + 3600, time.time()))
        capability = json.loads(job.get('payload', '{}')).get('capability', 'generate')
        if capability not in {'generate', 'regenerate'}:
            raise AccessError('无效执行权限。')
        Accounts.authorize(db, token, job['workspace_id'], capability)
        if not db.execute('SELECT 1 FROM project_workspaces WHERE workspace_id=? AND project_id=?', (job['workspace_id'], job['project_id'])).fetchone():
            raise AccessError('任务项目无权访问。')
    try:
        yield token
    finally:
        with store.db(write=True) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))


def execute(path, job, owner, *, runtime_factory=None):
    from sdlc.agent.composition import build_runtime
    runtime_factory = runtime_factory or build_runtime
    store = initialize(path)
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(20):
            with store.db(write=True) as db:
                updated = db.execute("UPDATE agent_work_items SET lease_until=?,updated_at=? WHERE id=? AND lease_owner=? AND status='running'",
                                     (time.time() + 120, time.time(), job['id'], owner))
                if not updated.rowcount:
                    return

    def guard():
        with store.db() as db:
            current = db.execute('SELECT status,lease_owner,lease_until FROM agent_work_items WHERE id=?', (job['id'],)).fetchone()
            if not current or current['status'] != 'running' or current['lease_owner'] != owner or current['lease_until'] < time.time():
                raise AccessError('任务已取消或执行租约失效，结果未继续发布。')

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with delegated_identity(path, job) as token:
            from sdlc.testing_agent.chat import ChatService
            from sdlc.testing_agent.service import TestingService
            operation, payload = job['operation'], json.loads(job['payload'])
            service = TestingService(path, token, job['workspace_id'], job['project_id'])
            service.execution_capability = payload.get('capability', 'generate')
            chat = ChatService(service)
            with service.connection('generate', True) as db:
                task = service._task(db, job['task_id'])
                db.execute('INSERT OR IGNORE INTO testing_agent_artifacts(id,task_id,kind,artifact_key,input_hash,content_json,created_at) VALUES(?,?,?,?,?,?,?)',
                           (uuid4().hex, task['id'], 'interaction_policy', 'chat-v1', task['input_hash'],
                            json.dumps({'mode': 'chat', 'plan_confirmation_required': True}), task['updated_at']))
            if job.get('requires_confirmation'):
                raise RuntimeError('Interrupted worker requires explicit user confirmation')
            # Recovery of an orphan submission also creates its initial public turn.
            if operation == 'start' and not chat.list_messages(job['task_id']):
                task = service.get_task(job['task_id'])
                chat.post_user(job['task_id'], task['input']['original_text'], 'job-' + job['id'])

            def public_event_sink(event):
                guard()
                return chat.handle_event(job['task_id'], event, request_key=job['id'])

            options = {'execution_guard': guard, 'capability': payload.get('capability', 'generate')}
            parameters = inspect.signature(runtime_factory).parameters
            if 'public_event_sink' in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                options['public_event_sink'] = public_event_sink
            runtime = runtime_factory(path, token, job['workspace_id'], job['project_id'], job['task_id'], **options)
            runtime.run(payload.get('content', '') if operation == 'message' else '',
                        resume=operation == 'resume' or job['attempts'] > 1, request_id=job['id'])
            guard()
            if not chat.get_pending(job['task_id']):
                chat.handle_event(job['task_id'], {'type': 'completed'}, request_key=job['id'])
        with store.db(write=True) as db:
            db.execute("UPDATE agent_work_items SET status='completed',lease_until=NULL,updated_at=? WHERE id=? AND lease_owner=? AND status='running'", (time.time(), job['id'], owner))
    except Exception as exc:
        # No provider exception text is accepted as public chat or queue content.
        message = '本轮任务暂未完成，已有成果已保留；请在对话中确认下一步。'
        with store.db(write=True) as db:
            changed = db.execute("UPDATE agent_work_items SET status='failed',error=?,lease_until=NULL,updated_at=? WHERE id=? AND lease_owner=? AND status='running'", (message, time.time(), job['id'], owner)).rowcount
        if changed:
            try:
                from sdlc.testing_agent.chat import ChatService
                from sdlc.testing_agent.service import TestingService
                with delegated_identity(path, job) as token:
                    service = TestingService(path, token, job['workspace_id'], job['project_id'])
                    service.execution_capability = json.loads(job['payload']).get('capability', 'generate')
                    service.record_event(job['task_id'], {'type': 'worker_request_failed',
                        'request_id': job['id'], 'error_class': type(exc).__name__[:80]})
                    ChatService(service).request_confirmation(job['task_id'], request_key=job['id'])
            except Exception:
                # Revoked access must not be bypassed to publish a confirmation.
                pass
    finally:
        stopped.set()
        thread.join(timeout=2)


def run_forever(path, *, stop=None, poll_seconds=2):
    stop = stop or threading.Event()
    owner = uuid4().hex
    while not stop.is_set():
        job = claim(path, owner)
        if job:
            execute(path, job, owner)
        else:
            stop.wait(poll_seconds)

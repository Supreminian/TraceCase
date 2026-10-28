"""Authenticated application boundary. The legacy Service is an internal persistence engine."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import time

from sdlc.accounts import Accounts, AccessError
from sdlc.service import Service as Engine, ServiceError, identifier, now, dumps

CAPABILITIES = {
    'list_projects': 'view', 'count_projects': 'view', 'get_project': 'view', 'project_stats': 'view',
    'list_runs': 'view', 'list_run_summaries': 'view', 'get_run': 'view', 'gate': 'view',
    'create_project': 'generate', 'update_project': 'edit', 'set_project_archived': 'manage',
    'generate': 'generate', 'generate_from_prd': 'generate', 'repair_validation': 'generate',
    'revalidate_existing': 'generate', 'revise_returned': 'regenerate',
    'review': 'review', 'edit': 'edit', 'add_clarification': 'review',
    'resolve_clarification': 'review', 'mark_interrupted': 'generate',
    'export': 'export', 'list_exports': 'export', 'get_export': 'export',
}


class Service(Engine):
    def __init__(self, db_path='data/sdlc.sqlite3', *, project_id=None, token=None, workspace_id=None):
        self.token, self.workspace_id = token, workspace_id
        self._context_capability = ContextVar('capability', default='view')
        self.accounts = Accounts(db_path)
        super().__init__(db_path, project_id=project_id)
        self.user = self.accounts.access(token, workspace_id)

    @property
    def _capability(self):
        return self._context_capability.get()

    @_capability.setter
    def _capability(self, value):
        self._context_capability.set(value)

    @contextmanager
    def connection(self, write=False, initialize=False):
        with super().connection(write, initialize) as db:
            if not initialize:
                try:
                    self.user = Accounts.authorize(db, self.token, self.workspace_id, self._capability)
                    if self.project_id:
                        self._owned(db, self.project_id)
                except AccessError as exc:
                    raise ServiceError(str(exc)) from None
            yield db

    def _owned(self, db, project_id):
        if not db.execute('SELECT 1 FROM project_workspaces WHERE project_id=? AND workspace_id=?',
                          (project_id, self.workspace_id)).fetchone():
            raise ServiceError('项目不存在或无权访问。')

    def _read_project(self, db, project_id):
        self._owned(db, project_id)
        return super()._read_project(db, project_id)

    def _request(self, db, request_id, payload):
        # Keys and payloads are both bound to authenticated identity and current workspace.
        from sdlc.accounts import digest
        if not request_id or len(request_id) > 128:
            raise ServiceError('缺少有效请求标识。')
        scoped = digest(f'{self.user["id"]}:{self.workspace_id}:{request_id}')
        return super()._request(db, scoped, payload)

    def _cached_request(self, db, request_id):
        from sdlc.accounts import digest
        return super()._cached_request(db, digest(f'{self.user["id"]}:{self.workspace_id}:{request_id}'))

    def _remember(self, db, request_id, fingerprint, result):
        from sdlc.accounts import digest
        return super()._remember(db, digest(f'{self.user["id"]}:{self.workspace_id}:{request_id}'), fingerprint, result)

    def _event(self, db, run_id, action, operator, reason, *args, **kwargs):
        event = super()._event(db, run_id, action, self.user['id'], reason, *args, **kwargs)
        Accounts._audit(db, self.user, action, self.workspace_id, event_id=event, run_id=run_id,
                        reason=reason, case_id=kwargs.get('case_id', args[0] if args else None))
        return event

    def _project_event(self, db, project_id, action, operator, details):
        super()._project_event(db, project_id, action, self.user['id'], details)
        Accounts._audit(db, self.user, 'project_' + action, self.workspace_id, project_id=project_id, **details)

    def list_projects(self, limit=200, offset=0):
        if not 1 <= limit <= 500 or offset < 0:
            raise ServiceError('分页参数无效。')
        with self.connection() as db:
            return [dict(r) for r in db.execute('''SELECT p.*,
                (SELECT COUNT(*) FROM runs r WHERE r.project_id=p.id) AS run_count
                FROM projects p JOIN project_workspaces pw ON pw.project_id=p.id
                WHERE pw.workspace_id=? ORDER BY p.created_at DESC,p.id LIMIT ? OFFSET ?''',
                (self.workspace_id, limit, offset))]

    def count_projects(self):
        with self.connection() as db:
            return db.execute('SELECT COUNT(*) FROM project_workspaces WHERE workspace_id=?',
                              (self.workspace_id,)).fetchone()[0]

    def create_project(self, name, description, operator=None):
        name, description = self._project_fields(name, description)
        project_id = identifier()
        with self.connection(write=True) as db:
            key = self.workspace_id + ':' + name.casefold()
            if db.execute('SELECT 1 FROM projects WHERE name_key=?', (key,)).fetchone():
                raise ServiceError('项目名称已存在，请使用其他名称。')
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)',
                       (project_id, name, key, description, 'active', now(), now(), self.user['id'], 1))
            db.execute('INSERT INTO project_workspaces VALUES (?,?)', (project_id, self.workspace_id))
            self._project_event(db, project_id, 'create', self.user['id'], {'name': name})
        return project_id

    def update_project(self, project_id, name, description, operator, expected_revision):
        name, description = self._project_fields(name, description)
        with self.connection(write=True) as db:
            old = self._read_project(db, project_id)
            if old['revision'] != expected_revision or old['status'] != 'active':
                raise ServiceError('项目已更新或归档，请刷新后重试。')
            key = self.workspace_id + ':' + name.casefold()
            if db.execute('SELECT 1 FROM projects WHERE name_key=? AND id!=?', (key, project_id)).fetchone():
                raise ServiceError('项目名称已存在。')
            db.execute('UPDATE projects SET name=?,name_key=?,description=?,operator=?,updated_at=?,revision=revision+1 WHERE id=?',
                       (name, key, description, self.user['id'], now(), project_id))
            self._project_event(db, project_id, 'update', self.user['id'], {'name': name})

    def get_run(self, run_id):
        run = super().get_run(run_id)
        with self.connection() as db:
            names = {row['id']: row['display_name'] for row in db.execute(
                "SELECT u.id,u.display_name FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.workspace_id=?",
                (self.workspace_id,))}
            snapshots = {}
            for row in db.execute("SELECT user_id,display_name,details_json FROM account_events WHERE workspace_id=? ORDER BY created_at", (self.workspace_id,)):
                import json
                details = json.loads(row['details_json'])
                if details.get('event_id'):
                    snapshots[details['event_id']] = row['display_name']
                names.setdefault(row['user_id'], row['display_name'])
        run['operator_display'] = names.get(run['operator'], run['operator'] + '（历史自报身份）')
        for event in run['events']:
            event['operator_display'] = snapshots.get(event['id'], event['operator'] + '（历史自报身份）')
        return run

    def _gate(self, db, run_id):
        issues = super()._gate(db, run_id)
        if db.execute("SELECT 1 FROM generation_jobs WHERE workspace_id=? AND run_id=? AND status IN ('queued','running')",
                      (self.workspace_id, run_id)).fetchone():
            issues.append('仍有用例正在重新生成，请等待完成或人工处理冲突后导出。')
        return issues

    def _admit_generation(self, db):
        active = db.execute("""SELECT COUNT(*) FROM runs r JOIN project_workspaces p ON p.project_id=r.project_id
            WHERE p.workspace_id=? AND r.status='generating' """, (self.workspace_id,)).fetchone()[0]
        # Initial tasks count their own reservation; at most two tasks per space are admitted.
        active += db.execute("SELECT COUNT(*) FROM generation_jobs WHERE workspace_id=? AND status IN ('queued','running') AND kind!='initial'", (self.workspace_id,)).fetchone()[0]
        today = db.execute("SELECT COUNT(*) FROM account_events WHERE workspace_id=? AND action='model_admitted' AND created_at>?",
                           (self.workspace_id, time.time() - 86400)).fetchone()[0]
        if active >= 2 or today >= 100:
            raise ServiceError('此空间已达到生成并发或每日额度，请稍后重试。')
        Accounts._audit(db, self.user, 'model_admitted', self.workspace_id)

    def _capture_response(self, run_id, response, latency):
        # Preserve evidence even if permission was removed while the model was running.
        denied = False
        with self.accounts.db(write=True) as db:
            try:
                Accounts.authorize(db, self.token, self.workspace_id, self._capability)
            except AccessError:
                denied = True
            db.execute("UPDATE runs SET raw_output=?,usage_json=?,latency_ms=? WHERE id=? AND status='generating'",
                       (response.raw_output, dumps(response.usage), latency, run_id))
            if denied:
                db.execute("UPDATE runs SET status='failed',error='生成期间权限或会话失效，原始响应已保存，未写入用例。' WHERE id=? AND status='generating'", (run_id,))
        if denied:
            raise ServiceError('生成期间权限或会话失效，响应已保留，请重新登录或联系管理员。')


def guarded(name, method):
    signature = inspect.signature(method)

    @wraps(method)
    def call(self, *args, **kwargs):
        previous = self._capability
        self._capability = CAPABILITIES[name]
        try:
            self.user = self.accounts.access(self.token, self.workspace_id, self._capability)
            bound = signature.bind(self, *args, **kwargs)
            if 'operator' in signature.parameters:
                bound.arguments['operator'] = self.user['id']
            if name == 'generate_from_prd':
                bound.arguments.setdefault('kwargs', {})['operator'] = self.user['id']
            # Metadata values in caller input cannot override the authenticated actor.
            return method(*bound.args, **bound.kwargs)
        except AccessError as exc:
            raise ServiceError(str(exc)) from None
        finally:
            self._capability = previous
    return call


for _name in CAPABILITIES:
    setattr(Service, _name, guarded(_name, getattr(Service, _name)))

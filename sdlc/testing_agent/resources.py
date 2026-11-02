"""Versioned project resources; snapshots are material, never confirmed rules."""
from __future__ import annotations

import json
from contextlib import contextmanager

from sdlc.accounts import Accounts, AccessError
from sdlc.secure_service import Service
from sdlc.service import ServiceError, dumps, identifier, now


KINDS = {'precondition', 'steps', 'test_data'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS testing_shared_resources (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
 revision INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id));
CREATE INDEX IF NOT EXISTS testing_resources_scope
 ON testing_shared_resources(workspace_id,project_id,archived,updated_at);
CREATE TABLE IF NOT EXISTS testing_shared_resource_versions (
 resource_id TEXT NOT NULL REFERENCES testing_shared_resources(id), revision INTEGER NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, archived INTEGER NOT NULL,
 actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
 PRIMARY KEY(resource_id,revision));
CREATE TABLE IF NOT EXISTS testing_shared_resource_events (
 id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES testing_shared_resources(id),
 action TEXT NOT NULL, revision INTEGER NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, details_json TEXT NOT NULL);
'''


class ResourceLibrary:
    """Every operation rechecks workspace authority and project ownership.

    update/archive use compare-and-swap against the current revision. Every state
    transition creates an immutable version; existing task snapshots stay intact.
    """

    def __init__(self, db_path, token, workspace_id, project_id):
        if not project_id:
            raise ServiceError('请先选择项目。')
        self.db_path, self.token = str(db_path), token
        self.workspace_id, self.project_id = workspace_id, project_id
        self.service = Service(db_path, token=token, workspace_id=workspace_id, project_id=project_id)
        self.accounts = self.service.accounts
        self.service.get_project()
        with self.accounts.db(write=True) as db:
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def _connection(self, capability='view', write=False):
        with self.accounts.db(write=write) as db:
            try:
                self.user = Accounts.authorize(db, self.token, self.workspace_id, capability)
            except AccessError as exc:
                raise ServiceError(str(exc)) from None
            project = db.execute('''SELECT p.status FROM projects p JOIN project_workspaces w
                ON w.project_id=p.id WHERE p.id=? AND w.workspace_id=?''',
                (self.project_id, self.workspace_id)).fetchone()
            if not project or (capability != 'view' and project['status'] != 'active'):
                raise ServiceError('项目不存在、已归档或无权访问。')
            yield db

    def _head(self, db, resource_id):
        row = db.execute('''SELECT * FROM testing_shared_resources
            WHERE id=? AND workspace_id=? AND project_id=?''',
            (resource_id, self.workspace_id, self.project_id)).fetchone()
        if not row:
            raise ServiceError('共享资源不存在或无权访问。')
        return dict(row)

    @staticmethod
    def _fields(title, body):
        if not isinstance(title, str) or not isinstance(body, str):
            raise ServiceError('资源标题和内容必须是文本。')
        title, body = title.strip(), body.strip()
        if not 1 <= len(title) <= 100 or not 1 <= len(body) <= 8000:
            raise ServiceError('资源标题须为 1–100 字符，内容须为 1–8,000 字符。')
        if '\x00' in title or '\x00' in body:
            raise ServiceError('资源包含不允许的 NUL 字符。')
        try:
            body.encode('utf-8')
            title.encode('utf-8')
        except UnicodeEncodeError:
            raise ServiceError('资源包含无效 Unicode 字符。') from None
        return title, body

    @staticmethod
    def _revision(value):
        if type(value) is not int or value < 1:
            raise ServiceError('资源版本无效，请刷新。')

    def _version(self, db, head, revision=None):
        revision = head['revision'] if revision is None else revision
        self._revision(revision)
        row = db.execute('SELECT * FROM testing_shared_resource_versions WHERE resource_id=? AND revision=?',
                         (head['id'], revision)).fetchone()
        if not row:
            raise ServiceError('共享资源版本不存在。')
        return {**head, **dict(row), 'id': head['id'], 'archived': bool(row['archived'])}

    def _event(self, db, resource_id, action, revision, details=None):
        db.execute('INSERT INTO testing_shared_resource_events VALUES(?,?,?,?,?,?,?)',
            (identifier(), resource_id, action, revision, self.user['id'], now(), dumps(details or {})))
        Accounts._audit(db, self.user, 'shared_resource_' + action, self.workspace_id,
                        project_id=self.project_id, resource_id=resource_id, revision=revision)

    def list(self, include_archived=False):
        if type(include_archived) is not bool:
            raise ServiceError('归档筛选参数无效。')
        with self._connection() as db:
            heads = db.execute('''SELECT * FROM testing_shared_resources WHERE workspace_id=?
                AND project_id=? AND (? OR archived=0) ORDER BY updated_at DESC,id''',
                (self.workspace_id, self.project_id, int(include_archived))).fetchall()
            return [self._version(db, dict(head)) for head in heads]

    def create(self, kind, title, body):
        if not isinstance(kind, str) or kind not in KINDS:
            raise ServiceError('资源类型只能为共享前置条件、步骤或测试数据准备。')
        title, body = self._fields(title, body)
        resource_id, stamp = identifier(), now()
        with self._connection('generate', True) as db:
            db.execute('INSERT INTO testing_shared_resources VALUES(?,?,?,?,?,?,?,?,?)',
                (resource_id, self.workspace_id, self.project_id, kind, 1, 0, stamp, stamp, self.user['id']))
            db.execute('INSERT INTO testing_shared_resource_versions VALUES(?,?,?,?,?,?,?)',
                (resource_id, 1, title, body, 0, self.user['id'], stamp))
            self._event(db, resource_id, 'created', 1, {'kind': kind})
            return self._version(db, self._head(db, resource_id))

    def update(self, resource_id, expected_revision, title, body):
        self._revision(expected_revision)
        title, body = self._fields(title, body)
        with self._connection('edit', True) as db:
            head = self._head(db, resource_id)
            if head['revision'] != expected_revision:
                raise ServiceError('资源已被其他协作者更新，请刷新后重试。')
            if head['archived']:
                raise ServiceError('请先恢复归档资源，再修改。')
            revision, stamp = head['revision'] + 1, now()
            db.execute('INSERT INTO testing_shared_resource_versions VALUES(?,?,?,?,?,?,?)',
                (resource_id, revision, title, body, 0, self.user['id'], stamp))
            updated = db.execute('UPDATE testing_shared_resources SET revision=?,updated_at=? WHERE id=? AND revision=?',
                                 (revision, stamp, resource_id, expected_revision))
            if updated.rowcount != 1:
                raise ServiceError('资源刚发生变化，请刷新。')
            self._event(db, resource_id, 'updated', revision, {'previous_revision': expected_revision})
            return self._version(db, self._head(db, resource_id))

    def archive(self, resource_id, expected_revision, archived=True):
        self._revision(expected_revision)
        if type(archived) is not bool:
            raise ServiceError('归档状态无效。')
        with self._connection('manage', True) as db:
            head = self._head(db, resource_id)
            if head['revision'] != expected_revision:
                raise ServiceError('资源已更新，请刷新后重试。')
            if bool(head['archived']) == archived:
                return self._version(db, head)
            current = self._version(db, head)
            revision, stamp = head['revision'] + 1, now()
            db.execute('INSERT INTO testing_shared_resource_versions VALUES(?,?,?,?,?,?,?)',
                (resource_id, revision, current['title'], current['body'], int(archived), self.user['id'], stamp))
            updated = db.execute('UPDATE testing_shared_resources SET revision=?,archived=?,updated_at=? WHERE id=? AND revision=?',
                                 (revision, int(archived), stamp, resource_id, expected_revision))
            if updated.rowcount != 1:
                raise ServiceError('资源刚发生变化，请刷新。')
            self._event(db, resource_id, 'archived' if archived else 'restored', revision,
                        {'previous_revision': expected_revision})
            return self._version(db, self._head(db, resource_id))

    def snapshot(self, ids):
        """Return detached version snapshots suitable for a task artifact.

        Strings select current versions. {'id': ..., 'revision': ...} explicitly
        retrieves a historical version for reproducibility. The resource and
        version must both be active; existing snapshots survive later archive.
        """
        if not isinstance(ids, list) or len(ids) > 50:
            raise ServiceError('每次最多选择 50 个共享资源版本。')
        snapshots, seen, character_count = [], set(), 0
        with self._connection() as db:
            for item in ids:
                if isinstance(item, str):
                    resource_id, revision = item, None
                elif isinstance(item, dict) and set(item) <= {'id', 'revision'} and isinstance(item.get('id'), str):
                    resource_id, revision = item['id'], item.get('revision')
                else:
                    raise ServiceError('共享资源选择格式无效。')
                head = self._head(db, resource_id)
                if head['archived']:
                    raise ServiceError('归档资源不能用于新任务，请选择有效资源。')
                version = self._version(db, head, revision)
                pair = (resource_id, version['revision'])
                if pair in seen:
                    raise ServiceError('同一共享资源版本不能重复选择。')
                seen.add(pair)
                if version['archived']:
                    raise ServiceError('归档资源不能用于新任务，请选择有效版本。')
                character_count += len(version['body'])
                if character_count > 40000:
                    raise ServiceError('所选共享资源正文合计超过 40,000 字符，请减少选择。')
                snapshots.append({key: version[key] for key in (
                    'id', 'kind', 'title', 'body', 'revision', 'actor_id', 'created_at')})
            for item in snapshots:
                item['business_fact_confirmed'] = False
                item['usage_notice'] = '共享测试资料；仍须核对当前需求适用性，不能自动作为业务事实。'
        # JSON detaches nested objects and makes the persistence contract explicit.
        return json.loads(dumps(snapshots))

"""Server-side identities, expiring sessions and transaction-bound workspace authorization."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError


class AccessError(ValueError):
    pass


ROLES = {
    'owner': {'view', 'generate', 'edit', 'review', 'regenerate', 'export', 'manage'},
    'admin': {'view', 'generate', 'edit', 'review', 'regenerate', 'export', 'manage'},
    'editor': {'view', 'generate', 'edit', 'regenerate'},
    'reviewer': {'view', 'review', 'regenerate', 'export'},
    'observer': {'view'},
}
ROLE_NAMES = {'owner': '所有者', 'admin': '管理员', 'editor': '用例编辑者',
              'reviewer': '审核者', 'observer': '观察者'}
OVERRIDES = {'generate', 'edit', 'review', 'regenerate', 'export'}
HASHER = PasswordHasher()
COOKIE = 'tracecase_session'
SESSION_SECONDS = 12 * 3600
SCHEMA = '''
CREATE TABLE IF NOT EXISTS account_meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL, created_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 expires_at REAL NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('personal','team')),
 name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', owner_id TEXT NOT NULL REFERENCES users(id),
 archived INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS personal_owner ON workspaces(owner_id) WHERE kind='personal';
CREATE TABLE IF NOT EXISTS workspace_preferences(
 user_id TEXT PRIMARY KEY REFERENCES users(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id));
CREATE TABLE IF NOT EXISTS memberships(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), user_id TEXT NOT NULL REFERENCES users(id),
 role TEXT NOT NULL CHECK(role IN ('owner','admin','editor','reviewer','observer')),
 overrides_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(workspace_id,user_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_workspace_owner ON memberships(workspace_id) WHERE role='owner';
CREATE TABLE IF NOT EXISTS invites(
 id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 expires_at REAL NOT NULL, max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
 revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS join_requests(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 user_id TEXT NOT NULL REFERENCES users(id), invite_id TEXT NOT NULL REFERENCES invites(id),
 status TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL, created_at REAL NOT NULL,
 reason TEXT, decided_by TEXT, decided_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_join ON join_requests(workspace_id,user_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS project_workspaces(
 project_id TEXT PRIMARY KEY REFERENCES projects(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id));
CREATE TRIGGER IF NOT EXISTS immutable_project_workspace_update BEFORE UPDATE ON project_workspaces
 BEGIN SELECT RAISE(ABORT, 'project workspace is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_project_workspace_delete BEFORE DELETE ON project_workspaces
 BEGIN SELECT RAISE(ABORT, 'project workspace is immutable'); END;
CREATE TABLE IF NOT EXISTS account_events(
 id TEXT PRIMARY KEY, workspace_id TEXT, user_id TEXT NOT NULL, display_name TEXT NOT NULL,
 action TEXT NOT NULL, created_at REAL NOT NULL, details_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rate_attempts(bucket TEXT NOT NULL, stamp REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rate_bucket ON rate_attempts(bucket,stamp);
CREATE TABLE IF NOT EXISTS generation_jobs(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL, user_id TEXT NOT NULL,
 run_id TEXT NOT NULL, case_id TEXT NOT NULL, version INTEGER NOT NULL, revision INTEGER NOT NULL,
 status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 kind TEXT NOT NULL DEFAULT 'case_revision' CHECK(kind IN ('initial','case_revision')),
 reason TEXT NOT NULL, prompt_json TEXT, config_json TEXT, attempts_json TEXT, error TEXT,
 result_version INTEGER);
CREATE TRIGGER IF NOT EXISTS immutable_account_events_update BEFORE UPDATE ON account_events
 BEGIN SELECT RAISE(ABORT, 'immutable account history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_account_events_delete BEFORE DELETE ON account_events
 BEGIN SELECT RAISE(ABORT, 'immutable account history'); END;
'''


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def uid():
    return uuid.uuid4().hex


class Accounts:
    def __init__(self, path):
        self.path = str(path)
        # Initialize the existing versioned business schema first, without touching its history.
        from sdlc.service import Service
        Service(path)
        with self.db() as db:
            has_accounts = db.execute("SELECT 1 FROM sqlite_master WHERE name='account_meta'").fetchone()
            if has_accounts and db.execute('SELECT version FROM account_meta').fetchone()[0] > 3:
                raise AccessError('账号数据库版本不兼容，请使用匹配版本的应用。')
            if has_accounts and db.execute('SELECT version FROM account_meta').fetchone()[0] == 3:
                return
            if not has_accounts or db.execute('SELECT version FROM account_meta').fetchone()[0] < 3:
                backup = Path(self.path + '.before-accounts-' + uid()[:8] + '.sqlite3')
                backup.touch(mode=0o600, exist_ok=False)
                with sqlite3.connect(backup) as target:
                    db.backup(target)
                backup.chmod(0o600)
            # executescript owns its transaction; never execute it inside a caller's transaction.
            db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
            if not db.execute('SELECT 1 FROM account_meta').fetchone():
                db.execute('INSERT INTO account_meta VALUES (3)')
            if 'kind' not in {r[1] for r in db.execute('PRAGMA table_info(generation_jobs)')}:
                db.execute("ALTER TABLE generation_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'case_revision'")
                db.execute("UPDATE generation_jobs SET kind='initial' WHERE case_id='__initial__'")
            db.execute('DROP INDEX IF EXISTS active_case_job')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_case_revision_job ON generation_jobs(run_id,case_id) WHERE kind='case_revision' AND status IN ('queued','running')")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_initial_job ON generation_jobs(project_id,user_id) WHERE kind='initial' AND status IN ('queued','running')")
            db.execute('UPDATE account_meta SET version=3')
        Path(self.path).chmod(0o600)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def identity(db, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 128:
            raise AccessError('请先登录。')
        row = db.execute('''SELECT u.id,u.username,u.display_name FROM sessions s JOIN users u
            ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1''',
            (digest(token or ''), time.time())).fetchone()
        if not row:
            raise AccessError('登录已失效，请重新登录。')
        return dict(row)

    @staticmethod
    def authorize(db, token, workspace_id, capability='view'):
        user = Accounts.identity(db, token)
        row = db.execute('''SELECT m.*,w.archived,w.kind FROM memberships m JOIN workspaces w
            ON w.id=m.workspace_id WHERE m.user_id=? AND m.workspace_id=?''',
            (user['id'], workspace_id)).fetchone()
        if not row:
            raise AccessError('无权访问此工作空间。')
        permissions = set(ROLES[row['role']])
        for key, enabled in json.loads(row['overrides_json']).items():
            if key in OVERRIDES:
                (permissions.add if enabled else permissions.discard)(key)
        if capability not in permissions or (capability != 'view' and row['archived']):
            raise AccessError('当前角色无此操作权限，或工作空间已归档。')
        return {**user, 'role': row['role'], 'permissions': permissions, 'kind': row['kind']}

    def me(self, token):
        with self.db() as db:
            return self.identity(db, token)

    def access(self, token, workspace_id, capability='view'):
        with self.db() as db:
            return self.authorize(db, token, workspace_id, capability)

    @staticmethod
    def _audit(db, user, action, workspace=None, **details):
        db.execute('INSERT INTO account_events VALUES (?,?,?,?,?,?,?)',
                   (uid(), workspace, user['id'], user['display_name'], action, time.time(),
                    json.dumps(details, ensure_ascii=False)))

    @staticmethod
    def _password(password):
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise AccessError('密码须为 10–128 个字符。')

    def throttle(self, bucket, limit=10, seconds=900):
        # Commit attempts even when the following credential check fails.
        with self.db(write=True) as db:
            db.execute('DELETE FROM rate_attempts WHERE stamp<?', (time.time() - 86400,))
            count = db.execute('SELECT COUNT(*) FROM rate_attempts WHERE bucket=? AND stamp>?',
                               (bucket, time.time() - seconds)).fetchone()[0]
            if count >= limit:
                raise AccessError('尝试过于频繁，请稍后再试。')
            db.execute('INSERT INTO rate_attempts VALUES (?,?)', (bucket, time.time()))

    def register(self, username, display_name, password, source='local'):
        self.throttle('register:' + source, 10, 3600)
        username, display_name = username.strip().casefold(), display_name.strip()
        if not re.fullmatch(r'[a-z0-9_\-.]{3,40}', username):
            raise AccessError('账号须为 3–40 位英文字母、数字、下划线、短横线或点。')
        if not 1 <= len(display_name) <= 40:
            raise AccessError('显示名称须为 1–40 个字符。')
        self._password(password)
        hashed = HASHER.hash(password)
        user_id, workspace_id = uid(), uid()
        try:
            with self.db(write=True) as db:
                db.execute('INSERT INTO users VALUES (?,?,?,?,?,1)',
                           (user_id, username, display_name, hashed, time.time()))
                db.execute("INSERT INTO workspaces(id,kind,name,owner_id) VALUES (?,'personal',?,?)",
                           (workspace_id, '个人工作', user_id))
                db.execute("INSERT INTO memberships(workspace_id,user_id,role) VALUES (?,?,'owner')",
                           (workspace_id, user_id))
                self._audit(db, {'id': user_id, 'display_name': display_name}, 'register', workspace_id)
        except sqlite3.IntegrityError:
            raise AccessError('此账号已注册，请使用其他账号或登录。') from None
        return user_id

    def login(self, username, password, source='local', previous_token=None):
        username = username.strip().casefold()[:40]
        self.throttle('login-ip:' + source, 30)
        self.throttle('login-user:' + digest(username), 10)
        with self.db() as db:
            user = db.execute('SELECT * FROM users WHERE username=? AND enabled=1', (username,)).fetchone()
        # Same expensive verification for unknown accounts, without storing a shared credential.
        encoded = user['password_hash'] if user else DUMMY_HASH
        try:
            if len(password) > 128 or not HASHER.verify(encoded, password):
                raise AccessError('账号或密码错误。')
        except (VerificationError, InvalidHashError):
            raise AccessError('账号或密码错误。') from None
        if not user:
            raise AccessError('账号或密码错误。')
        token = secrets.token_urlsafe(48)
        with self.db(write=True) as db:
            db.execute('DELETE FROM sessions WHERE expires_at<=? OR token_hash=?',
                       (time.time(), digest(previous_token or '')))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                       (digest(token), user['id'], time.time() + SESSION_SECONDS, time.time()))
            self._audit(db, dict(user), 'login')
        return token

    def logout(self, token):
        with self.db(write=True) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token or ''),))

    def change_password(self, token, old_password, new_password):
        self._password(new_password)
        user = self.me(token)
        self.throttle('password:' + user['id'], 10)
        with self.db() as db:
            row = db.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
        try:
            HASHER.verify(row[0], old_password)
        except (VerificationError, InvalidHashError):
            raise AccessError('当前密码不正确。') from None
        hashed = HASHER.hash(new_password)
        with self.db(write=True) as db:
            self.identity(db, token)
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed, user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
            self._audit(db, user, 'password_changed')

    def spaces(self, token):
        with self.db() as db:
            user = self.identity(db, token)
            return [dict(r) for r in db.execute('''SELECT w.*,m.role FROM workspaces w JOIN memberships m
                ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.kind,w.name,w.id''', (user['id'],))]

    def preferred_space(self, token):
        with self.db() as db:
            user = self.identity(db, token)
            row = db.execute("""SELECT p.workspace_id FROM workspace_preferences p JOIN memberships m
                ON m.workspace_id=p.workspace_id AND m.user_id=p.user_id WHERE p.user_id=?""", (user['id'],)).fetchone()
            return row[0] if row else None

    def choose_space(self, token, workspace):
        with self.db(write=True) as db:
            user = self.authorize(db, token, workspace)
            db.execute('INSERT INTO workspace_preferences VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET workspace_id=excluded.workspace_id',
                       (user['id'], workspace))

    def create_team(self, token, name, description=''):
        name, description = name.strip(), description.strip()
        if not 1 <= len(name) <= 80 or len(description) > 500:
            raise AccessError('团队名称须为 1–80 字符，简介最多 500 字符。')
        with self.db(write=True) as db:
            user = self.identity(db, token)
            workspace = uid()
            db.execute("INSERT INTO workspaces VALUES (?,'team',?,?,?,0)",
                       (workspace, name, description, user['id']))
            db.execute("INSERT INTO memberships(workspace_id,user_id,role) VALUES (?,?,'owner')",
                       (workspace, user['id']))
            self._audit(db, user, 'team_created', workspace, name=name)
            return workspace

    def update_team(self, token, workspace, name, description):
        name, description = name.strip(), description.strip()
        if not 1 <= len(name) <= 80 or len(description) > 500:
            raise AccessError('团队名称须为 1–80 字符，简介最多 500 字符。')
        with self.db(write=True) as db:
            actor = self.authorize(db, token, workspace, 'manage')
            if actor['kind'] != 'team':
                raise AccessError('请选择团队。')
            db.execute('UPDATE workspaces SET name=?,description=? WHERE id=?', (name, description, workspace))
            self._audit(db, actor, 'team_updated', workspace, name=name, description=description)

    def reset_invite(self, token, workspace, invite_id, max_uses=20, days=7):
        if type(max_uses) is not int or not 1 <= max_uses <= 1000 or not 1 <= days <= 30:
            raise AccessError('邀请码使用次数须为 1–1000，有效期为 1–30 天。')
        code = secrets.token_urlsafe(24)
        with self.db(write=True) as db:
            actor = self.authorize(db, token, workspace, 'manage')
            old = db.execute('SELECT id FROM invites WHERE id=? AND workspace_id=?', (invite_id, workspace)).fetchone()
            if not old:
                raise AccessError('邀请码不存在。')
            db.execute('UPDATE invites SET revoked=1 WHERE id=?', (invite_id,))
            db.execute('INSERT INTO invites VALUES (?,?,?,?,?,0,0)',
                       (uid(), digest(code), workspace, time.time() + days * 86400, max_uses))
            self._audit(db, actor, 'invite_reset', workspace, previous_invite_id=invite_id)
        return code

    def create_invite(self, token, workspace, max_uses=20, days=7):
        if type(max_uses) is not int or not 1 <= max_uses <= 1000 or not 1 <= days <= 30:
            raise AccessError('邀请码使用次数须为 1–1000，有效期为 1–30 天。')
        code = secrets.token_urlsafe(24)
        with self.db(write=True) as db:
            user = self.authorize(db, token, workspace, 'manage')
            if user['kind'] != 'team':
                raise AccessError('个人空间不支持邀请。')
            db.execute('INSERT INTO invites VALUES (?,?,?,?,?,0,0)',
                       (uid(), digest(code), workspace, time.time() + days * 86400, max_uses))
            self._audit(db, user, 'invite_created', workspace, max_uses=max_uses, days=days)
        return code

    def revoke_invite(self, token, workspace, invite_id):
        with self.db(write=True) as db:
            user = self.authorize(db, token, workspace, 'manage')
            db.execute('UPDATE invites SET revoked=1 WHERE id=? AND workspace_id=?', (invite_id, workspace))
            self._audit(db, user, 'invite_revoked', workspace, invite_id=invite_id)

    def request_join(self, token, code, note='', confirm=False):
        user = self.me(token)
        self.throttle('invite:' + user['id'], 10)
        if len(note) > 1000 or len(code) > 128:
            raise AccessError('申请说明最多 1000 字符，邀请码格式无效。')
        with self.db(write=True) as db:
            self.identity(db, token)
            invite = db.execute('''SELECT i.*,w.name,w.archived FROM invites i JOIN workspaces w
                ON w.id=i.workspace_id WHERE i.code_hash=? AND w.kind='team' ''', (digest(code.strip()),)).fetchone()
            if not invite or invite['revoked'] or invite['archived'] or invite['expires_at'] <= time.time():
                raise AccessError('邀请码无效、已停用或已过期。')
            workspace = invite['workspace_id']
            if db.execute('SELECT 1 FROM memberships WHERE user_id=? AND workspace_id=?', (user['id'], workspace)).fetchone():
                raise AccessError('你已是该团队成员。')
            pending = db.execute("SELECT id FROM join_requests WHERE user_id=? AND workspace_id=? AND status='pending'",
                                 (user['id'], workspace)).fetchone()
            if pending:
                return {'name': invite['name'], 'request_id': pending[0], 'pending': True}
            if invite['uses'] >= invite['max_uses']:
                raise AccessError('邀请码已达到使用次数上限。')
            if not confirm:
                return {'name': invite['name'], 'pending': False}
            request_id = uid()
            db.execute('INSERT INTO join_requests(id,workspace_id,user_id,invite_id,note,created_at) VALUES (?,?,?,?,?,?)',
                       (request_id, workspace, user['id'], invite['id'], note.strip(), time.time()))
            db.execute('UPDATE invites SET uses=uses+1 WHERE id=?', (invite['id'],))
            self._audit(db, user, 'join_requested', workspace, request_id=request_id)
            return {'name': invite['name'], 'request_id': request_id, 'pending': True}

    def applications(self, token):
        with self.db() as db:
            user = self.identity(db, token)
            return [dict(r) for r in db.execute('''SELECT j.id,j.status,j.created_at,j.reason,w.name FROM join_requests j
                JOIN workspaces w ON w.id=j.workspace_id WHERE j.user_id=? ORDER BY j.created_at DESC''', (user['id'],))]

    def manage_data(self, token, workspace):
        with self.db() as db:
            self.authorize(db, token, workspace, 'manage')
            return {name: [dict(r) for r in db.execute(sql, (workspace,))] for name, sql in {
                'members': '''SELECT m.*,u.username,u.display_name FROM memberships m JOIN users u ON u.id=m.user_id WHERE workspace_id=?''',
                'requests': '''SELECT j.*,u.username,u.display_name FROM join_requests j JOIN users u ON u.id=j.user_id WHERE workspace_id=? ORDER BY created_at DESC''',
                'invites': 'SELECT id,expires_at,max_uses,uses,revoked FROM invites WHERE workspace_id=?',
            }.items()}

    @staticmethod
    def _role_allowed(actor, old_role, new_role):
        if new_role not in ROLES or new_role == 'owner' or old_role == 'owner':
            raise AccessError('所有者变更必须使用所有权转移。')
        if actor['role'] != 'owner' and (old_role == 'admin' or new_role == 'admin'):
            raise AccessError('只有所有者可以任命或变更管理员。')

    def decide_join(self, token, workspace, request_id, approve, role='observer', reason=''):
        if not approve and not 1 <= len(reason.strip()) <= 1000:
            raise AccessError('拒绝申请须填写原因（最多 1000 字符）。')
        with self.db(write=True) as db:
            actor = self.authorize(db, token, workspace, 'manage')
            self._role_allowed(actor, None, role)
            request = db.execute("SELECT * FROM join_requests WHERE id=? AND workspace_id=? AND status='pending'",
                                 (request_id, workspace)).fetchone()
            if not request:
                raise AccessError('申请已处理或不存在，请刷新。')
            if approve:
                if not ROLES[role] <= actor['permissions']:
                    raise AccessError('不能授予超出自身权限的能力。')
                db.execute('INSERT INTO memberships(workspace_id,user_id,role) VALUES (?,?,?)',
                           (workspace, request['user_id'], role))
            db.execute('UPDATE join_requests SET status=?,reason=?,decided_by=?,decided_at=? WHERE id=?',
                       ('approved' if approve else 'rejected', reason.strip(), actor['id'], time.time(), request_id))
            self._audit(db, actor, 'join_decided', workspace, request_id=request_id, approved=approve, role=role)

    def set_member(self, token, workspace, user_id, role, overrides=None, remove=False):
        overrides = overrides or {}
        if set(overrides) - OVERRIDES or any(type(v) is not bool for v in overrides.values()):
            raise AccessError('仅支持系统定义的业务权限开关。')
        with self.db(write=True) as db:
            actor = self.authorize(db, token, workspace, 'manage')
            target = db.execute('SELECT * FROM memberships WHERE workspace_id=? AND user_id=?', (workspace, user_id)).fetchone()
            if not target or actor['id'] == user_id:
                raise AccessError('不能修改自己或不存在的成员；所有者请使用所有权转移。')
            self._role_allowed(actor, target['role'], role)
            effective = (ROLES[role] | {k for k, v in overrides.items() if v}) - {k for k, v in overrides.items() if not v}
            if not effective <= actor['permissions']:
                raise AccessError('不能授予超出自身权限的能力。')
            if remove:
                db.execute('DELETE FROM memberships WHERE workspace_id=? AND user_id=?', (workspace, user_id))
            else:
                db.execute('UPDATE memberships SET role=?,overrides_json=? WHERE workspace_id=? AND user_id=?',
                           (role, json.dumps(overrides), workspace, user_id))
            self._audit(db, actor, 'member_removed' if remove else 'member_changed', workspace,
                        target=user_id, role=role, overrides=overrides)

    def transfer(self, token, workspace, target_id):
        with self.db(write=True) as db:
            actor = self.authorize(db, token, workspace, 'manage')
            if actor['role'] != 'owner' or actor['kind'] != 'team' or target_id == actor['id']:
                raise AccessError('只有团队所有者可以转移给其他现有成员。')
            if not db.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?', (workspace, target_id)).fetchone():
                raise AccessError('新所有者必须是团队现有成员。')
            db.execute("UPDATE memberships SET role='admin',overrides_json='{}' WHERE workspace_id=? AND user_id=?", (workspace, actor['id']))
            db.execute("UPDATE memberships SET role='owner',overrides_json='{}' WHERE workspace_id=? AND user_id=?", (workspace, target_id))
            db.execute('UPDATE workspaces SET owner_id=? WHERE id=?', (target_id, workspace))
            self._audit(db, actor, 'ownership_transferred', workspace, target=target_id)

    def leave(self, token, workspace):
        with self.db(write=True) as db:
            actor = self.authorize(db, token, workspace)
            if actor['role'] == 'owner':
                raise AccessError('所有者须先转移所有权，不能直接退出。')
            db.execute('DELETE FROM memberships WHERE workspace_id=? AND user_id=?', (workspace, actor['id']))
            self._audit(db, actor, 'team_left', workspace)

    def archive_team(self, token, workspace):
        with self.db(write=True) as db:
            actor = self.authorize(db, token, workspace, 'manage')
            if actor['role'] != 'owner' or actor['kind'] != 'team':
                raise AccessError('只有团队所有者可归档团队。')
            db.execute('UPDATE workspaces SET archived=1 WHERE id=?', (workspace,))
            self._audit(db, actor, 'team_archived', workspace)


DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))

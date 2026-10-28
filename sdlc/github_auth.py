"""GitHub authorization-code login with state, browser binding and S256 PKCE."""
import base64
from dataclasses import dataclass, field
import hashlib
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx

from sdlc.accounts import AccessError, Accounts, HASHER, SESSION_SECONDS, digest, uid

FLOW_COOKIE = 'tracecase_github_flow'
FLOW_TTL = 600


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str

    @classmethod
    def load(cls):
        values = [os.environ.get(key, '').strip() for key in
                  ('GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET', 'GITHUB_REDIRECT_URI')]
        if not all(values):
            raise AccessError('GitHub 登录尚未启用，请使用账号密码登录，或联系维护者完成配置。')
        parts = urlsplit(values[2])
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment
                or parts.path != '/auth/github/callback'
                or (parts.scheme == 'http' and parts.hostname not in {'127.0.0.1', 'localhost', '::1'})):
            raise AccessError('GitHub 登录回调配置无效，请联系维护者。')
        return cls(*values)


def configured():
    try:
        Config.load()
        return True
    except AccessError:
        return False


def start(store, config, previous_token=None, link=False):
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    with store.db(write=True) as db:
        target = Accounts.identity(db, previous_token)['id'] if link else None
        db.execute('DELETE FROM oauth_flows WHERE expires_at<=?', (time.time(),))
        db.execute('INSERT INTO oauth_flows VALUES (?,?,?,?,?,?,?,?)',
                   (digest(state), digest(browser), verifier, time.time() + FLOW_TTL,
                    target, digest(previous_token or ''), config.client_id, config.redirect_uri))
    url = 'https://github.com/login/oauth/authorize?' + urlencode({
        'client_id': config.client_id, 'redirect_uri': config.redirect_uri, 'state': state,
        'scope': '', 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'prompt': 'select_account'})
    return url, browser


def consume(store, config, state, browser):
    if not isinstance(state, str) or not isinstance(browser, str) or not state or not browser or len(state) > 128 or len(browser) > 128:
        raise AccessError('GitHub 登录校验失败，请从登录页重新发起。')
    with store.db(write=True) as db:
        row = db.execute('SELECT * FROM oauth_flows WHERE state_hash=?', (digest(state),)).fetchone()
        if (not row or row['expires_at'] <= time.time() or row['browser_hash'] != digest(browser)
                or row['client_id'] != config.client_id or row['redirect_uri'] != config.redirect_uri):
            raise AccessError('GitHub 登录已过期或校验失败，请重新发起。')
        db.execute('DELETE FROM oauth_flows WHERE state_hash=?', (digest(state),))
        return dict(row)


async def _json(client, method, url, **kwargs):
    # Fixed upstream hosts, no redirects, bounded responses, sanitized errors.
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 128 * 1024:
                raise AccessError('GitHub 返回内容异常，请稍后重试。')
        import json
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise AccessError('GitHub 返回内容异常，请稍后重试。')
        return parsed


async def profile(config, code, verifier):
    if not isinstance(code, str) or not 1 <= len(code) <= 512:
        raise AccessError('GitHub 未返回有效授权码，请重试。')
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            token = await _json(client, 'POST', 'https://github.com/login/oauth/access_token',
                               headers={'Accept': 'application/json'}, data={
                                   'client_id': config.client_id, 'client_secret': config.client_secret,
                                   'code': code, 'redirect_uri': config.redirect_uri, 'code_verifier': verifier})
            bearer = token.get('access_token')
            token_type = token.get('token_type')
            if (not isinstance(bearer, str) or not bearer or len(bearer) > 4096
                    or not isinstance(token_type, str) or token_type.lower() != 'bearer'):
                raise AccessError('GitHub 授权未完成或授权码已失效，请重新登录。')
            result = await _json(client, 'GET', 'https://api.github.com/user', headers={
                'Authorization': 'Bearer ' + bearer, 'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'TraceCase-Login'})
        # Discard upstream tokens and all unrelated profile fields; never persist email or repository access.
        if type(result.get('id')) is not int or result['id'] <= 0 or not isinstance(result.get('login'), str) or not result['login'].strip():
            raise AccessError('GitHub 用户身份无效，请重试。')
        return {'id': str(result['id']), 'login': result['login'].strip()[:100],
                'name': str(result.get('name') or result['login']).strip()[:40]}
    except (httpx.HTTPError, ValueError, TypeError):
        raise AccessError('GitHub 登录服务暂不可用，请稍后重试；也可使用账号密码登录。') from None


def finish(store, identity, flow):
    subject, login, name = identity['id'], identity['login'], identity['name']
    token = secrets.token_urlsafe(48)
    # OAuth-only accounts have no usable password login; retain the existing NOT NULL hash schema.
    unused_hash = HASHER.hash(secrets.token_urlsafe(48))
    with store.db(write=True) as db:
        bound = db.execute("SELECT user_id FROM external_identities WHERE provider='github' AND subject=?", (subject,)).fetchone()
        target = flow['target_user_id']
        if target:
            valid = db.execute('''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>? AND u.enabled=1''',
                (flow['previous_session_hash'], target, time.time())).fetchone()
            if not valid:
                raise AccessError('绑定前的登录已失效，请重新登录后绑定。')
            if bound and bound['user_id'] != target:
                raise AccessError('此 GitHub 账号已绑定其他账号，不能合并或转移。')
            existing = db.execute("SELECT subject FROM external_identities WHERE provider='github' AND user_id=?", (target,)).fetchone()
            if existing and existing['subject'] != subject:
                raise AccessError('当前账号已绑定其他 GitHub 账号。')
            user_id = target
        elif bound:
            user_id = bound['user_id']
        else:
            user_id, workspace = uid(), uid()
            # No account merging by username, display name or email.
            db.execute('INSERT INTO users(id,username,display_name,password_hash,created_at,enabled,password_login) VALUES (?,?,?,?,?,1,0)',
                       (user_id, 'gh.' + uid(), name or login[:40], unused_hash, time.time()))
            db.execute("INSERT INTO workspaces(id,kind,name,owner_id) VALUES (?,'personal','个人工作',?)", (workspace, user_id))
            db.execute("INSERT INTO memberships(workspace_id,user_id,role) VALUES (?,?,'owner')", (workspace, user_id))
        user = db.execute('SELECT * FROM users WHERE id=? AND enabled=1', (user_id,)).fetchone()
        if not user:
            raise AccessError('账号已停用，请联系维护者。')
        if not target and not bound:
            store._audit(db, dict(user), 'github_registered', workspace, github_subject=subject)
        db.execute("INSERT INTO external_identities VALUES ('github',?,?,?,?) ON CONFLICT(provider,subject) DO UPDATE SET login=excluded.login",
                   (subject, user_id, login, time.time()))
        db.execute('DELETE FROM sessions WHERE expires_at<=? OR token_hash=?', (time.time(), flow['previous_session_hash']))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), user_id, time.time() + SESSION_SECONDS, time.time()))
        store._audit(db, dict(user), 'github_linked' if target else 'github_login', github_subject=subject)
    return token

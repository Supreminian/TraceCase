"""GitHub login integration, with the external provider replaced at the HTTP boundary."""
import asyncio
import base64
import hashlib
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from sdlc.accounts import Accounts, AccessError, COOKIE
from sdlc import github_auth as github
import sdlc.gateway as gateway

PASSWORD = 'disposable-oauth-password'


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    path = tmp_path / 'oauth.sqlite3'
    store = Accounts(path)
    monkeypatch.setattr(gateway, 'DB_PATH', str(path))
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'test-client')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'never-log-this-secret')
    monkeypatch.setenv('GITHUB_REDIRECT_URI', 'http://127.0.0.1:8501/auth/github/callback')
    return store


def begin(client):
    response = client.get('/auth/github/start', follow_redirects=False)
    assert response.status_code == 303
    location = response.headers['location']
    parsed = urlsplit(location)
    assert parsed.scheme == 'https' and parsed.netloc == 'github.com'
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert query['scope'] == [''] and query['code_challenge_method'] == ['S256']
    assert 'never-log-this-secret' not in location
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=lax' in response.headers['set-cookie']
    return query


def callback(client, query, code='test-code'):
    return client.get('/auth/github/callback', params={'state': query['state'][0], 'code': code})


def fake_profile(monkeypatch, user_id='100', login='cat-user', name='Summer'):
    async def profile(config, code, verifier):
        assert code == 'test-code' and len(verifier) >= 43
        return {'id': user_id, 'login': login, 'name': name}
    monkeypatch.setattr(github, 'profile', profile)


def test_first_login_creates_personal_space_repeat_login_tracks_id_not_name(oauth, monkeypatch):
    fake_profile(monkeypatch)
    with TestClient(gateway.app) as client:
        query = begin(client)
        with oauth.db() as db:
            verifier = db.execute('SELECT verifier FROM oauth_flows').fetchone()[0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        assert query['code_challenge'] == [challenge]
        response = callback(client, query)
        assert 'GitHub 登录成功' in response.text
        assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
        token = client.cookies[COOKIE]
        user = oauth.me(token)
        assert not user['password_login'] and len(oauth.spaces(token)) == 1
        assert oauth.spaces(token)[0]['kind'] == 'personal'
        assert '没有本地密码' in client.get('/auth/password').text
        fake_profile(monkeypatch, login='renamed-account', name='New Name')
        assert 'GitHub 登录成功' in callback(client, begin(client)).text
        assert oauth.me(client.cookies[COOKIE])['id'] == user['id']
        with pytest.raises(AccessError):
            oauth.me(token)  # Previous local session rotated.
        with oauth.db() as db:
            assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM oauth_flows').fetchone()[0] == 0
            assert db.execute('SELECT login FROM external_identities').fetchone()[0] == 'renamed-account'
            assert 'never-log-this-secret' not in str(db.execute('SELECT * FROM account_events').fetchall())
        with pytest.raises(AccessError):
            oauth.login(user['username'], PASSWORD)


def test_invalid_state_browser_cookie_replay_and_expiry_never_create_accounts(oauth, monkeypatch):
    calls = []
    async def forbidden(*args):
        calls.append(args)
        raise AssertionError('Invalid state must never contact GitHub')
    monkeypatch.setattr(github, 'profile', forbidden)
    with TestClient(gateway.app) as client:
        query = begin(client)
        assert '校验失败' in client.get('/auth/github/callback?state=wrong&code=test-code').text
        # An invalid callback clears the transient binding; a fresh flow is required.
        query = begin(client)
        client.cookies.delete(github.FLOW_COOKIE, domain='testserver.local', path='/auth/github')
        assert '校验失败' in callback(client, query).text
        query = begin(client)
        with oauth.db(write=True) as db:
            db.execute('UPDATE oauth_flows SET expires_at=?', (time.time() - 1,))
        assert '已过期' in callback(client, query).text
        assert not calls
        with oauth.db() as db:
            assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0


def test_callback_single_use_and_cancel_is_recoverable(oauth, monkeypatch):
    fake_profile(monkeypatch)
    with TestClient(gateway.app) as client:
        query = begin(client)
        result = client.get('/auth/github/callback', params={'state': query['state'][0], 'error': 'access_denied', 'error_description': 'never-log-this-secret'})
        assert '授权已取消' in result.text and 'never-log-this-secret' not in result.text
        assert 'GitHub 登录成功' in callback(client, begin(client)).text
        # Replaying the consumed state with its original cookie cannot redeem again.
        query = begin(client)
        browser = client.cookies[github.FLOW_COOKIE]
        assert 'GitHub 登录成功' in callback(client, query).text
        client.cookies.set(github.FLOW_COOKIE, browser, path='/auth/github')
        assert '已过期或校验失败' in callback(client, query).text
        with oauth.db() as db:
            assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1


def test_binding_requires_local_auth_and_preserves_existing_workspace(oauth, monkeypatch):
    local_id = oauth.register('local-user', 'Local', PASSWORD)
    token = oauth.login('local-user', PASSWORD)
    workspace = oauth.spaces(token)[0]['id']
    fake_profile(monkeypatch)
    with TestClient(gateway.app) as client:
        assert client.get('/auth/account', follow_redirects=False).status_code == 303
        client.cookies.set(COOKIE, token)
        account = client.get('/auth/account')
        assert '绑定 GitHub' in account.text
        result = client.post('/auth/github/link', data={'csrf': 'bad'})
        assert '页面已过期' in result.text
        csrf = client.cookies['tracecase_csrf']
        result = client.post('/auth/github/link', data={'csrf': csrf}, follow_redirects=False)
        query = parse_qs(urlsplit(result.headers['location']).query)
        # Strict main cookie may be absent on the GitHub -> callback navigation.
        client.cookies.delete(COOKIE)
        assert 'GitHub 登录成功' in callback(client, query).text
        assert oauth.me(client.cookies[COOKIE])['id'] == local_id
        assert oauth.spaces(client.cookies[COOKIE])[0]['id'] == workspace
        assert oauth.me(client.cookies[COOKIE])['password_login'] == 1
        assert '已绑定 GitHub' in client.get('/auth/account').text


def test_same_display_name_does_not_merge_local_account(oauth, monkeypatch):
    local_id = oauth.register('cat-user', 'Summer', PASSWORD)
    fake_profile(monkeypatch)
    with TestClient(gateway.app) as client:
        callback(client, begin(client))
        assert oauth.me(client.cookies[COOKIE])['id'] != local_id
        with oauth.db() as db:
            assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 2


def test_revoked_binding_session_and_already_bound_identity_cannot_be_stolen(oauth):
    config = github.Config.load()
    alice = oauth.register('alice', 'Alice', PASSWORD)
    a = oauth.login('alice', PASSWORD)
    oauth.register('bob', 'Bob', PASSWORD)
    b = oauth.login('bob', PASSWORD)
    url, browser = github.start(oauth, config, a, True)
    flow = github.consume(oauth, config, parse_qs(urlsplit(url).query)['state'][0], browser)
    identity = {'id': '100', 'login': 'cat', 'name': 'Cat'}
    token = github.finish(oauth, identity, flow)
    assert oauth.me(token)['id'] == alice
    url, browser = github.start(oauth, config, b, True)
    flow = github.consume(oauth, config, parse_qs(urlsplit(url).query)['state'][0], browser)
    with pytest.raises(AccessError, match='已绑定其他账号'):
        github.finish(oauth, identity, flow)
    url, browser = github.start(oauth, config, b, True)
    flow = github.consume(oauth, config, parse_qs(urlsplit(url).query)['state'][0], browser)
    oauth.logout(b)
    with pytest.raises(AccessError, match='登录已失效'):
        github.finish(oauth, {'id': '200', 'login': 'dog', 'name': 'Dog'}, flow)
    with oauth.db() as db:
        assert db.execute('SELECT COUNT(*) FROM external_identities').fetchone()[0] == 1


def test_disabled_account_is_not_reactivated_by_github(oauth, monkeypatch):
    fake_profile(monkeypatch)
    with TestClient(gateway.app) as client:
        callback(client, begin(client))
        user = oauth.me(client.cookies[COOKIE])
        with oauth.db(write=True) as db:
            db.execute('UPDATE users SET enabled=0 WHERE id=?', (user['id'],))
        assert '账号已停用' in callback(client, begin(client)).text
        with pytest.raises(AccessError):
            oauth.me(client.cookies[COOKIE])


def test_missing_configuration_does_not_send_users_to_github(oauth, monkeypatch):
    monkeypatch.delenv('GITHUB_CLIENT_SECRET')
    with TestClient(gateway.app) as client:
        assert 'GitHub 登录暂不可用' in client.get('/auth/login').text
        result = client.get('/auth/github/start', follow_redirects=False)
        assert result.status_code == 200 and 'GitHub 登录尚未启用' in result.text
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'test')
    monkeypatch.setenv('GITHUB_REDIRECT_URI', 'http://evil.example/auth/github/callback')
    with pytest.raises(AccessError):
        github.Config.load()


def test_profile_http_transport_uses_pkce_and_never_requests_repositories(oauth, monkeypatch):
    actual_client = httpx.AsyncClient
    calls = []
    def transport(request):
        calls.append(request)
        if request.url.host == 'github.com':
            body = parse_qs(request.content.decode())
            assert body['code_verifier'] == ['test-verifier']
            assert body['client_secret'] == ['never-log-this-secret']
            return httpx.Response(200, json={'access_token': 'upstream-token-never-store', 'token_type': 'bearer'})
        assert str(request.url) == 'https://api.github.com/user'
        assert request.headers['Authorization'] == 'Bearer upstream-token-never-store'
        return httpx.Response(200, json={'id': 100, 'login': 'cat', 'name': None, 'email': 'ignored@example.invalid'})
    monkeypatch.setattr(github.httpx, 'AsyncClient', lambda **kwargs: actual_client(transport=httpx.MockTransport(transport), **kwargs))
    result = asyncio.run(github.profile(github.Config.load(), 'test-code', 'test-verifier'))
    assert result == {'id': '100', 'login': 'cat', 'name': 'cat'} and len(calls) == 2
    assert 'email' not in result and 'upstream-token' not in str(result)


def test_upstream_failure_does_not_expose_tokens(oauth, monkeypatch):
    actual_client = httpx.AsyncClient
    def transport(request):
        return httpx.Response(401, json={'error': 'never-log-this-secret'})
    monkeypatch.setattr(github.httpx, 'AsyncClient', lambda **kwargs: actual_client(transport=httpx.MockTransport(transport), **kwargs))
    with pytest.raises(AccessError) as error:
        asyncio.run(github.profile(github.Config.load(), 'test-code', 'verifier'))
    assert 'never-log-this-secret' not in str(error.value)


def test_account_migration_preserves_existing_password_users_and_history(oauth):
    oauth.register('old-user', 'Old', PASSWORD)
    with oauth.db(write=True) as db:
        db.execute('UPDATE account_meta SET version=3')
        db.execute('ALTER TABLE users DROP COLUMN password_login')
        db.execute('DROP TABLE external_identities')
        db.execute('DROP TABLE oauth_flows')
    Accounts(oauth.path)
    assert oauth.me(oauth.login('old-user', PASSWORD))['password_login'] == 1
    with oauth.db() as db:
        assert db.execute('SELECT version FROM account_meta').fetchone()[0] == 4
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    # Bound identity keys are database-enforced, even under concurrent first logins.
    with pytest.raises(sqlite3.IntegrityError):
        with oauth.db(write=True) as db:
            db.execute("INSERT INTO external_identities VALUES ('github','42','missing','cat',0)")

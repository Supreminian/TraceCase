"""Logout must be a single same-tab POST, invalidate the session and protect newer logins."""
import pytest
from starlette.testclient import TestClient

from sdlc.accounts import Accounts, AccessError, COOKIE, logout_proof
from sdlc import github_auth
import sdlc.gateway as gateway


@pytest.fixture
def login(tmp_path, monkeypatch):
    store = Accounts(tmp_path / 'logout.sqlite3')
    store.register('alice', 'Alice', 'disposable-password')
    token = store.login('alice', 'disposable-password')
    monkeypatch.setattr(gateway, 'DB_PATH', store.path)
    return store, token


def test_single_post_revokes_session_clears_cookies_and_returns_login(login):
    store, token = login
    config = github_auth.Config('test', 'secret', 'http://localhost/auth/github/callback')
    github_auth.start(store, config, token, True)
    with TestClient(gateway.app) as client:
        client.cookies.set(COOKIE, token, domain='testserver.local')
        client.cookies.set(github_auth.FLOW_COOKIE, 'temporary-browser-binding', domain='testserver.local', path='/auth/github')
        response = client.post('/auth/logout', data={'logout_proof': logout_proof(token)}, follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/auth/login'
        assert response.headers['cache-control'] == 'no-store'
        assert COOKIE not in client.cookies and github_auth.FLOW_COOKIE not in client.cookies
        # This form does not depend on a CSRF cookie that can expire before the login session.
        assert client.get('/auth/session').status_code == 401
        result = client.get('/auth/login')
        assert '已退出登录。' in result.text and "postMessage({type: 'logout'})" in result.text
        assert 'tracecase_signed_out' not in client.cookies
        assert '已退出登录。' not in client.get('/auth/login').text
    with pytest.raises(AccessError):
        store.me(token)
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM oauth_flows').fetchone()[0] == 0


def test_get_does_not_log_out_and_post_requires_action_proof(login):
    store, token = login
    with TestClient(gateway.app) as client:
        client.cookies.set(COOKIE, token, domain='testserver.local')
        assert client.get('/auth/login', follow_redirects=False).headers['location'] == '/'
        result = client.get('/auth/logout')
        assert '退出当前登录' in result.text
        assert store.me(token)
        response = client.post('/auth/logout', data={'logout_proof': 'wrong'})
        assert '登录状态已变化' in response.text and store.me(token)
        response = client.post('/auth/logout', data={'logout_proof': logout_proof(token)}, headers={'Origin': 'https://evil.invalid'})
        assert '请求来源无效' in response.text and store.me(token)
        # A cross-site request without Strict cookies cannot clear the victim's browser session.
        client.cookies.clear()
        response = client.post('/auth/logout', data={'logout_proof': 'wrong'}, headers={'Origin': 'https://evil.invalid'})
        assert not any(COOKIE + '=' in value for value in response.headers.get_list('set-cookie'))


def test_stale_tab_cannot_log_out_a_new_session(login):
    store, old_token = login
    token = store.login('alice', 'disposable-password', previous_token=old_token)
    with TestClient(gateway.app) as client:
        client.cookies.set(COOKIE, token, domain='testserver.local')
        response = client.post('/auth/logout', data={'logout_proof': logout_proof(old_token)})
        assert '登录状态已变化' in response.text
        assert store.me(token)
        status = client.get('/auth/session')
        assert status.json()['user_id'] == store.me(token)['id']
        assert status.json()['session']
        assert logout_proof(token) not in status.text
        assert status.headers['cache-control'] == 'no-store'
        assert token not in status.text


def test_expired_session_and_already_signed_out_are_idempotent(login):
    store, token = login
    store.logout(token)
    with TestClient(gateway.app) as client:
        client.cookies.set(COOKIE, token, domain='testserver.local')
        response = client.post('/auth/logout', data={'logout_proof': logout_proof(token)}, follow_redirects=False)
        assert response.status_code == 303
        response = client.post('/auth/logout', data={'logout_proof': logout_proof(token)},
                               headers={'Origin': 'http://testserver'}, follow_redirects=False)
        assert response.status_code == 303
        assert client.get('/auth/logout', follow_redirects=False).headers['location'] == '/auth/login'


def test_ui_uses_native_same_tab_post_without_bearer_token(login):
    from streamlit.testing.v1 import AppTest
    _, token = login
    script = '''from sdlc.account_ui import logout_control
logout_control(TOKEN)
'''.replace('TOKEN', repr(token))
    app = AppTest.from_string(script).run()
    assert not app.exception
    html = app.get('html')[0].proto.body
    assert 'method="post"' in html and 'target="_self"' in html and 'action="/auth/logout"' in html
    assert token not in html and logout_proof(token) in html
    assert not app.get('link_button')


def test_password_error_stays_on_settings_without_echoing_credentials(login):
    store, token = login
    with TestClient(gateway.app) as client:
        client.cookies.set(COOKIE, token, domain='testserver.local')
        client.get('/auth/password')
        response = client.post('/auth/password', data={
            'csrf': client.cookies['tracecase_csrf'], 'old_password': 'incorrect-old-password',
            'new_password': 'private-new-password'})
        assert '<title>TraceCase · 修改密码</title>' in response.text
        assert 'action="/auth/password"' in response.text
        assert 'role="status"' in response.text
        assert 'incorrect-old-password' not in response.text
        assert 'private-new-password' not in response.text
        assert store.me(token)

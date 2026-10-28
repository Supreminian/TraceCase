"""Opaque-origin embedded forms need both same-origin metadata and CSRF proof."""
import pytest
from starlette.testclient import TestClient

from sdlc.accounts import Accounts, COOKIE, logout_proof
from sdlc import gateway


@pytest.fixture
def login_store(tmp_path, monkeypatch):
    store = Accounts(tmp_path / 'forms.sqlite3')
    store.register('alice', 'Alice', 'disposable-password')
    monkeypatch.setattr(gateway, 'DB_PATH', str(store.path))
    return store


@pytest.mark.parametrize('headers,valid_csrf,accepted', [
    ({'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'}, True, True),
    ({'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'}, True, True),
    ({}, True, True),  # Older clients still require the cookie-bound CSRF token.
    ({'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'}, False, False),
    ({'Origin': 'null'}, True, False),
    ({'Origin': 'null', 'Sec-Fetch-Site': 'same-site'}, True, False),
    ({'Origin': 'null', 'Sec-Fetch-Site': 'cross-site'}, True, False),
    ({'Origin': 'https://evil.invalid', 'Sec-Fetch-Site': 'same-origin'}, True, False),
    ({'Sec-Fetch-Site': 'cross-site'}, True, False),
])
def test_login_source_requires_metadata_and_token(login_store, headers, valid_csrf, accepted):
    with TestClient(gateway.app) as client:
        client.get('/auth/login')
        response = client.post('/auth/login', data={
            'username': 'alice', 'password': 'disposable-password',
            'csrf': client.cookies['tracecase_csrf'] if valid_csrf else 'invalid'},
            headers=headers, follow_redirects=False)
        if accepted:
            assert response.status_code == 303 and response.headers['location'] == '/'
            assert login_store.me(client.cookies[COOKIE])
        else:
            assert response.status_code == 200
            assert '请求来源无效' in response.text or '页面已过期' in response.text
            assert COOKIE not in client.cookies
            with login_store.db() as db:
                assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


@pytest.mark.parametrize('valid_proof', [True, False])
def test_embedded_logout_still_requires_session_proof(login_store, valid_proof):
    token = login_store.login('alice', 'disposable-password')
    with TestClient(gateway.app) as client:
        client.cookies.set(COOKIE, token, domain='testserver.local')
        response = client.post('/auth/logout', data={
            'logout_proof': logout_proof(token) if valid_proof else 'invalid'},
            headers={'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'}, follow_redirects=False)
        if valid_proof:
            assert response.status_code == 303 and COOKIE not in client.cookies
            assert client.get('/auth/session').status_code == 401
        else:
            assert '登录状态已变化' in response.text
            assert login_store.me(token)

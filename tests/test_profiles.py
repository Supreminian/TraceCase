"""Profile ownership, avatar decoding, atomic changes and migration coverage."""
from io import BytesIO

from PIL import Image, PngImagePlugin
import pytest
from starlette.testclient import TestClient

from sdlc.accounts import Accounts, AccessError, COOKIE
from sdlc import gateway, profiles


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Accounts(tmp_path / 'profiles.sqlite3')
    store.register('alice', 'Alice', 'disposable-password')
    store.register('bob', 'Bob', 'disposable-password')
    alice = store.login('alice', 'disposable-password')
    bob = store.login('bob', 'disposable-password')
    monkeypatch.setattr(gateway, 'DB_PATH', str(store.path))
    return store, alice, bob


def image_data(color='red', size=(320, 160)):
    data = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('private', 'discard-image-metadata')
    Image.new('RGB', size, color).save(data, format='PNG', pnginfo=metadata)
    return data.getvalue()


def sign_in(client, token):
    client.cookies.set(COOKIE, token, domain='testserver.local')
    return client.get('/auth/profile')


def fields(client, revision=0, **changes):
    return {'csrf': client.cookies['tracecase_csrf'], 'revision': str(revision),
            'display_name': '新的显示名称', 'bio': '负责需求与测试审核。', **changes}


def test_profile_requires_login_and_avatar_is_not_public(context):
    with TestClient(gateway.app) as client:
        assert client.get('/auth/profile', follow_redirects=False).headers['location'] == '/auth/login'
        assert client.post('/auth/profile', data={}, follow_redirects=False).status_code == 303
        assert client.get('/auth/avatar').status_code == 401


def test_profile_save_crop_metadata_removal_and_restore_default(context):
    store, alice, _ = context
    with TestClient(gateway.app) as client:
        page = sign_in(client, alice)
        assert '个人资料' in page.text and 'multipart/form-data' in page.text
        assert 'aria-current="page">个人资料' in page.text
        response = client.post('/auth/profile', data=fields(client),
                               files={'avatar': ('photo.png', image_data(), 'image/png')},
                               headers={'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'}, follow_redirects=False)
        assert response.status_code == 303
        assert store.me(alice)['display_name'] == '新的显示名称'
        profile = profiles.get_profile(store, alice)
        assert profile['bio'] == '负责需求与测试审核。' and profile['has_avatar']
        avatar = client.get('/auth/avatar')
        assert avatar.headers['content-type'] == 'image/png'
        assert avatar.headers['cache-control'] == 'private, no-store'
        assert avatar.headers['x-content-type-options'] == 'nosniff'
        with Image.open(BytesIO(avatar.content)) as image:
            assert image.size == (256, 256) and image.getpixel((100, 100)) == (255, 0, 0)
            assert not image.info
        response = client.post('/auth/profile', data=fields(client, 1, remove_avatar='1'), follow_redirects=False)
        assert response.status_code == 303 and not profiles.get_profile(store, alice)['has_avatar']
        with Image.open(BytesIO(client.get('/auth/avatar').content)) as image:
            assert image.size == (256, 256)
        with store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM account_events WHERE action='profile_updated'").fetchone()[0] == 2


def test_identity_is_session_scoped_and_old_audit_names_are_preserved(context):
    store, alice, bob = context
    original = profiles.avatar_png(store, bob)
    profiles.save_profile(store, alice, 'Alice Updated', '', 0, avatar=image_data(), filename='avatar.png')
    with TestClient(gateway.app) as client:
        sign_in(client, bob)
        assert client.get('/auth/avatar?user_id=' + store.me(alice)['id']).content == original
        client.post('/auth/profile', data=fields(client, user_id=store.me(alice)['id']))
    assert store.me(alice)['display_name'] == 'Alice Updated'
    assert store.me(bob)['display_name'] == '新的显示名称'
    with store.db() as db:
        names = [r[0] for r in db.execute("SELECT display_name FROM account_events WHERE user_id=? AND action='login'", (store.me(alice)['id'],))]
        assert names == ['Alice']


@pytest.mark.parametrize('filename,data', [
    ('bad.svg', b'<svg onload="alert(1)"></svg>'),
    ('fake.png', b'not-an-image'),
    ('huge.png', b'x' * (profiles.MAX_AVATAR_BYTES + 1)),
    ('wide.png', image_data(size=(4097, 1))),
])
def test_invalid_avatar_does_not_partially_update_profile(context, filename, data):
    store, alice, _ = context
    with TestClient(gateway.app) as client:
        sign_in(client, alice)
        response = client.post('/auth/profile', data=fields(client), files={'avatar': (filename, data, 'image/png')})
        assert response.status_code == 200 and 'tc-note error' in response.text
    assert store.me(alice)['display_name'] == 'Alice'
    assert profiles.get_profile(store, alice)['revision'] == 0


def test_csrf_cross_origin_validation_and_optimistic_conflict(context):
    store, alice, _ = context
    with TestClient(gateway.app) as client:
        sign_in(client, alice)
        for headers, changes in [({}, {'csrf': 'bad'}), ({'Origin': 'https://evil.invalid'}, {})]:
            response = client.post('/auth/profile', data=fields(client, **changes), headers=headers)
            assert 'tc-note error' in response.text
            assert profiles.get_profile(store, alice)['revision'] == 0
        client.post('/auth/profile', data=fields(client, display_name='First'))
        response = client.post('/auth/profile', data=fields(client, display_name='Stale'))
        assert '资料已在其他页面更新' in response.text
        assert store.me(alice)['display_name'] == 'First'
        response = client.post('/auth/profile', data=fields(client, 1, display_name=''))
        assert '1–40' in response.text
        response = client.post('/auth/profile', data=fields(client, 1, bio='x' * 501))
        assert '最多 500' in response.text
        assert store.me(alice)['display_name'] == 'First'


def test_profile_html_escapes_user_input_and_keeps_avatar_without_new_upload(context):
    store, alice, _ = context
    profiles.save_profile(store, alice, '<img onerror=alert(1)>', '<script>bad()</script>', 0,
                          avatar=image_data(), filename='avatar.png')
    before = profiles.avatar_png(store, alice)
    with TestClient(gateway.app) as client:
        response = sign_in(client, alice)
        assert '&lt;script&gt;bad()&lt;/script&gt;' in response.text
        assert '<script>bad()' not in response.text and '<img onerror=' not in response.text
        client.post('/auth/profile', data=fields(client, 1))
    assert profiles.avatar_png(store, alice) == before


def test_account_v4_migrates_profiles_without_invalidating_sessions(context):
    store, alice, _ = context
    with store.db(write=True) as db:
        db.execute('DROP TABLE user_profiles')
        db.execute('UPDATE account_meta SET version=4')
    upgraded = Accounts(store.path)
    assert upgraded.me(alice)['display_name'] == 'Alice'
    assert profiles.get_profile(upgraded, alice)['bio'] == ''
    with upgraded.db() as db:
        assert db.execute('SELECT version FROM account_meta').fetchone()[0] == 5
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    with pytest.raises(AccessError):
        profiles.save_profile(upgraded, 'invalid-session', 'Bad', '', 0)


def test_profile_hub_sections_share_local_menu_and_logout_requires_confirmation(context):
    from sdlc.accounts import logout_proof
    store, alice, _ = context
    with TestClient(gateway.app) as client:
        sign_in(client, alice)
        for section, label, expected in [
            ('account', '登录账号与绑定', 'GitHub 账号绑定'),
            ('password', '修改密码', 'action="/auth/password"'),
            ('logout', '退出登录', '确认退出登录'),
        ]:
            response = client.get('/auth/profile', params={'section': section})
            assert response.status_code == 200
            assert 'aria-label="个人资料功能"' in response.text
            assert f'aria-current="page">{label}</a>' in response.text
            assert expected in response.text
            assert store.me(alice)
        response = client.get('/auth/profile?section=logout')
        assert logout_proof(alice) in response.text and alice not in response.text
        result = client.post('/auth/logout', data={'logout_proof': logout_proof(alice)},
                             headers={'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'}, follow_redirects=False)
        assert result.status_code == 303
        assert client.get('/auth/profile', follow_redirects=False).headers['location'] == '/auth/login'

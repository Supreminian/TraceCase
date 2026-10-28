"""Local same-origin login gateway. HttpOnly session cookies never enter URLs or localStorage."""
import asyncio
from html import escape
import os
from pathlib import Path
import secrets

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocketDisconnect
from starlette.responses import RedirectResponse, Response, JSONResponse
from starlette.routing import Route, WebSocketRoute
import websockets

from sdlc.accounts import Accounts, AccessError, COOKIE, SESSION_SECONDS, logout_proof, session_marker
from sdlc import github_auth, web_ui, profiles

DB_PATH = os.environ.get('SDLC_DB_PATH', str(Path(__file__).resolve().parents[1] / 'data/sdlc.sqlite3'))
UPSTREAM = 'http://127.0.0.1:' + os.environ.get('SDLC_PRIVATE_PORT', '8502')
SECURE = os.environ.get('SDLC_COOKIE_SECURE', '0') == '1'


def accounts():
    return Accounts(DB_PATH)


AUTH_BROADCAST = """<script>
if ('BroadcastChannel' in window) {
  const channel = new BroadcastChannel('tracecase-auth');
  channel.postMessage({type: 'logout'});
  channel.close();
}
</script>"""


def watch_session(user_id, token):
    import json
    return """<script>
(() => {
  const expectedUser = USER_ID;
  const expectedSession = SESSION_MARKER;
  let leaving = false;
  const leave = (url) => { if (!leaving) { leaving = true; window.location.replace(url); } };
  if ('BroadcastChannel' in window) {
    const channel = new BroadcastChannel('tracecase-auth');
    channel.onmessage = (event) => { if (event.data?.type === 'logout') leave('/auth/login'); };
  }
  const check = async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await fetch('/auth/session', {credentials: 'same-origin', cache: 'no-store'});
      if (result.status === 401) return leave('/auth/login');
      if (result.ok) {
        const status = await result.json();
        if (status.user_id !== expectedUser || status.session !== expectedSession) return leave('/');
        document.documentElement.style.visibility = '';
      }
    } catch (_) { /* A temporary network failure must not sign out a valid session. */ }
  };
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) document.documentElement.style.visibility = 'hidden';
    check();
  });
  document.addEventListener('visibilitychange', check);
  window.addEventListener('focus', check);
  setInterval(check, 15000);
})();
</script>""".replace('USER_ID', json.dumps(user_id)).replace('SESSION_MARKER', json.dumps(session_marker(token)))


async def session_status(request):
    try:
        user = accounts().me(request.cookies.get(COOKIE))
        return JSONResponse({'user_id': user['id'], 'session': session_marker(request.cookies.get(COOKIE))}, headers={'Cache-Control': 'no-store'})
    except AccessError:
        return Response(status_code=401, headers={'Cache-Control': 'no-store'})


def page(message='', csrf=None, signed_out=False):
    csrf = csrf or secrets.token_urlsafe(32)
    hidden = web_ui.hidden_csrf(csrf)
    github = ('<a class="tc-button github" href="/auth/github/start">使用 GitHub 登录</a>'
              if github_auth.configured() else '<button class="tc-button github" disabled>GitHub 登录暂不可用</button>')
    login = ('<section class="tc-card"><h2 class="tc-section">登录</h2>'
             + web_ui.notice(message, 'success' if signed_out or message.startswith(('注册成功', '密码已修改')) else 'error')
             + github + '<div class="tc-divider">或使用账号密码</div><form method="post" action="/auth/login">' + hidden
             + web_ui.field('账号', 'username', autocomplete='username', maximum=40)
             + web_ui.field('密码', 'password', password=True, autocomplete='current-password')
             + '<button class="tc-button primary tc-wide">登录</button></form>'
             + '<details><summary>还没有账号？注册</summary><form method="post" action="/auth/register">' + hidden
             + web_ui.field('账号', 'username', input_id='register_username', autocomplete='username', minimum=3, maximum=40,
                            hint='3–40 个字符，支持英文字母、数字、_、- 或 .')
             + web_ui.field('显示名称', 'display_name', autocomplete='nickname', maximum=40)
             + web_ui.field('密码（10–128 个字符）', 'password', input_id='register_password', password=True, autocomplete='new-password', minimum=10)
             + web_ui.field('确认密码', 'confirmation', password=True, autocomplete='new-password')
             + '<button class="tc-button primary tc-wide">创建账号</button></form></details></section>')
    aside = '<aside class="tc-card"><h2 class="tc-section">从需求到测试用例</h2><ol class="tc-list"><li>输入 PRD，生成测试用例与待澄清问题。</li><li>在个人或团队空间逐条审核，退回后继续迭代。</li><li>全部通过后导出，保留需求引用与审核记录。</li></ol></aside>'
    response = web_ui.render('欢迎回来', '登录后，在个人空间或团队空间中生成、审核和导出测试用例。',
                             '<div class="tc-grid">' + login + aside + '</div>', accent='#2563eb',
                             scripts=AUTH_BROADCAST if signed_out else '')
    response.set_cookie('tracecase_csrf', csrf, httponly=True, secure=SECURE, samesite='strict', max_age=3600)
    return response


def check_form_source(request, message='请求来源无效，请重新打开登录页。'):
    """Check browser provenance; opaque origins also need same-origin Fetch Metadata.

    Some embedded browsers sandbox native form submissions, serializing Origin as
    null. That value is never trusted alone. Callers must additionally verify a
    cookie-bound CSRF token or session-derived action proof before any mutation.
    """
    origin = request.headers.get('origin')
    site = request.headers.get('sec-fetch-site')
    expected = str(request.base_url).rstrip('/')
    if site == 'cross-site':
        raise AccessError(message)
    if origin == 'null':
        if site != 'same-origin':
            raise AccessError(message)
        return True
    if origin and origin != expected:
        raise AccessError(message)
    return origin == expected


def csrf_ok(request, form):
    check_form_source(request)
    cookie, field = request.cookies.get('tracecase_csrf', ''), form.get('csrf', '')
    if not cookie or not isinstance(field, str) or not secrets.compare_digest(cookie, field):
        raise AccessError('页面已过期，请重新提交。')


async def auth(request: Request):
    if request.method == 'GET':
        if request.url.path == '/auth/logout':
            token = request.cookies.get(COOKIE)
            try:
                user = accounts().me(token)
            except AccessError:
                return RedirectResponse('/auth/login', 303)
            # GET never changes login state; only old bookmarks use this confirmation page.
            body = '<section class="tc-card"><h2 class="tc-section">确认退出</h2><p>退出后需要重新登录才能继续访问项目。</p><form method="post" action="/auth/logout"><input type="hidden" name="logout_proof" value="' + logout_proof(token) + '"><div class="tc-actions"><button class="tc-button primary">退出登录</button>' + web_ui.button('返回工作台', '/') + '</div></form></section>'
            return web_ui.render('退出当前登录', '确认结束当前登录会话。', body, user=user, token=token,
                                 scripts=watch_session(user['id'], token))
        if request.url.path == '/auth/login':
            try:
                accounts().me(request.cookies.get(COOKIE))
            except AccessError:
                pass
            else:
                response = RedirectResponse('/', 303)
                response.headers['Cache-Control'] = 'no-store'
                response.delete_cookie('tracecase_signed_out')
                return response
        signed_out = request.cookies.get('tracecase_signed_out') == '1'
        response = page('已退出登录。' if signed_out else '', csrf=request.cookies.get('tracecase_csrf'), signed_out=signed_out)
        if signed_out:
            response.delete_cookie('tracecase_signed_out')
        return response
    try:
        body_size, chunks = 0, []
        async for chunk in request.stream():
            body_size += len(chunk)
            chunks.append(chunk)
            if body_size > 8192:
                raise AccessError('请求过长。')
        # Starlette caches body only through body(); retain the bounded bytes for form parsing.
        request._body = b''.join(chunks)
        form = await request.form()
        if request.url.path == '/auth/logout' and 'logout_proof' in form:
            token = request.cookies.get(COOKIE)
            proof = form.get('logout_proof')
            verified_source = check_form_source(request, '请求来源无效，请刷新工作台后重试。')
            if token:
                expected = logout_proof(token)
                if not expected or not isinstance(proof, str) or not secrets.compare_digest(proof, expected):
                    raise AccessError('登录状态已变化，请刷新工作台后再退出。')
            elif not verified_source:
                raise AccessError('登录状态已失效，请重新打开登录页。')
        else:
            csrf_ok(request, form)
        store = accounts()
        token = request.cookies.get(COOKIE)
        source = request.client.host
        if request.url.path == '/auth/register':
            if form.get('password') != form.get('confirmation'):
                raise AccessError('两次密码输入不一致。')
            await asyncio.to_thread(store.register, form.get('username', ''), form.get('display_name', ''), form.get('password', ''), source)
            return page('注册成功，请登录。', csrf=request.cookies.get('tracecase_csrf'))
        if request.url.path == '/auth/logout':
            store.logout(token)
            result = RedirectResponse('/auth/login', 303)
            result.delete_cookie(COOKIE)
            result.delete_cookie('tracecase_csrf')
            result.delete_cookie(github_auth.FLOW_COOKIE, path='/auth/github')
            result.set_cookie('tracecase_signed_out', '1', httponly=True, secure=SECURE, samesite='strict', max_age=30)
            result.headers['Cache-Control'] = 'no-store'
            return result
        if request.url.path == '/auth/password':
            await asyncio.to_thread(store.change_password, token, form.get('old_password', ''), form.get('new_password', ''))
            response = page('密码已修改，所有会话已退出，请重新登录。')
            response.delete_cookie(COOKIE)
            return response
        token = await asyncio.to_thread(store.login, form.get('username', ''), form.get('password', ''), source, token)
        response = RedirectResponse('/', 303)
        response.set_cookie(COOKIE, token, httponly=True, secure=SECURE, samesite='strict', max_age=SESSION_SECONDS)
        response.headers['Cache-Control'] = 'no-store'
        return response
    except (AccessError, ValueError):
        # Known safe domain errors only; never include submitted passwords or transport traces.
        import sys
        error = sys.exception()
        if request.url.path == '/auth/password':
            try:
                user = accounts().me(request.cookies.get(COOKIE))
            except AccessError:
                pass
            else:
                return password_response(request, user, str(error))
        return page(str(error), csrf=request.cookies.get('tracecase_csrf'))


def password_response(request, user, message=''):
    token = request.cookies.get(COOKIE)
    csrf = request.cookies.get('tracecase_csrf') or secrets.token_urlsafe(32)
    if user['password_login']:
        body = ('<div class="tc-grid"><section class="tc-card"><h2 class="tc-section">设置新密码</h2>'
                + web_ui.notice(message) + '<form method="post" action="/auth/password">' + web_ui.hidden_csrf(csrf)
                + web_ui.field('当前密码', 'old_password', password=True, autocomplete='current-password')
                + web_ui.field('新密码', 'new_password', password=True, autocomplete='new-password', minimum=10,
                               hint='请输入 10–128 个字符。')
                + '<div class="tc-actions"><button class="tc-button primary">修改密码</button>'
                + web_ui.button('返回工作台', '/') + '</div></form></section>'
                + '<aside class="tc-card"><h2 class="tc-section">修改后会发生什么</h2><p class="tc-muted">修改成功后，所有设备须重新登录。请使用新密码登录后继续工作。</p>'
                + web_ui.button('查看登录账号与绑定', '/auth/account') + '</aside></div>')
    else:
        body = '<section class="tc-card"><h2 class="tc-section">GitHub 登录</h2><p>此账号使用 GitHub 登录，没有本地密码。</p>' + web_ui.button('查看登录账号', '/auth/account') + '</section>'
    response = web_ui.render('修改密码', '管理账号密码，保障工作空间的访问安全。', body,
                             user=user, token=token, active='password', accent='#2563eb',
                             scripts=watch_session(user['id'], token))
    response.set_cookie('tracecase_csrf', csrf, httponly=True, secure=SECURE, samesite='strict', max_age=3600)
    return response


async def password_page(request):
    try:
        user = accounts().me(request.cookies.get(COOKIE))
    except AccessError:
        return RedirectResponse('/auth/login', 303)
    return password_response(request, user)


async def account_page(request):
    try:
        store = accounts()
        token = request.cookies.get(COOKIE)
        user = store.me(token)
        with store.db() as db:
            linked = db.execute("SELECT login FROM external_identities WHERE user_id=? AND provider='github'", (user['id'],)).fetchone()
    except AccessError:
        return RedirectResponse('/auth/login', 303)
    csrf = request.cookies.get('tracecase_csrf') or secrets.token_urlsafe(32)
    profile = ('<section class="tc-card"><h2 class="tc-section">登录账号</h2><dl class="tc-facts"><div><dt>显示名称</dt><dd>'
               + escape(user['display_name']) + '</dd></div><div><dt>账号</dt><dd>' + escape(user['username']) + '</dd></div></dl></section>')
    github = '<section class="tc-card"><h2 class="tc-section">GitHub 账号绑定</h2>'
    if linked:
        github += '<span class="tc-badge success">已绑定</span><p>已绑定 GitHub：' + escape(linked['login']) + '</p><p class="tc-muted">可使用此 GitHub 账号登录当前工作空间。</p>'
    elif github_auth.configured():
        github += '<span class="tc-badge">未绑定</span><p class="tc-muted">绑定后可使用 GitHub 登录当前账号，保留原有项目和团队身份。</p><form method="post" action="/auth/github/link">' + web_ui.hidden_csrf(csrf) + '<button class="tc-button primary">绑定 GitHub</button></form>'
    else:
        github += '<span class="tc-badge">暂不可用</span><p class="tc-muted">GitHub 登录暂不可用，请稍后重试。</p><button class="tc-button secondary" disabled>绑定 GitHub</button>'
    security = '<aside class="tc-card"><h2 class="tc-section">密码与安全</h2>'
    if user['password_login']:
        security += '<span class="tc-badge success">已启用密码登录</span><p class="tc-muted">使用账号密码登录，也可以绑定 GitHub 以便下次快速进入工作台。</p>' + web_ui.button('修改密码', '/auth/password')
    else:
        security += '<span class="tc-badge">GitHub 登录</span><p class="tc-muted">此账号使用 GitHub 登录，没有本地密码。请在 GitHub 管理密码与登录安全。</p>'
    body = '<div class="tc-grid"><div>' + profile + github + '</section></div>' + security + '</aside></div>'
    response = web_ui.render('登录账号与绑定', '查看账号信息，管理登录方式。', body, user=user, token=token,
                             active='account', accent='#4f46e5', scripts=watch_session(user['id'], token))
    response.set_cookie('tracecase_csrf', csrf, httponly=True, secure=SECURE, samesite='strict', max_age=3600)
    return response


def profile_response(request, user, message='', submitted=None):
    store = accounts()
    token = request.cookies.get(COOKIE)
    try:
        profile = profiles.get_profile(store, token)
    except AccessError:
        return RedirectResponse('/auth/login', 303)
    csrf = request.cookies.get('tracecase_csrf') or secrets.token_urlsafe(32)
    values = submitted or profile
    display_name = values.get('display_name', profile['display_name'])
    bio = values.get('bio', profile['bio'])
    if not isinstance(display_name, str) or not isinstance(bio, str):
        display_name, bio = profile['display_name'], profile['bio']
    notice = web_ui.notice(message) if message else web_ui.notice('个人资料已保存。', 'success') if request.query_params.get('saved') == '1' else ''
    body = ('<div class="tc-grid"><section class="tc-card"><h2 class="tc-section">编辑个人资料</h2>' + notice
            + '<form method="post" action="/auth/profile" enctype="multipart/form-data">' + web_ui.hidden_csrf(csrf)
            + '<input type="hidden" name="revision" value="' + str(profile['revision']) + '">'
            + '<div class="tc-field"><label class="tc-label" for="display_name">显示名称</label><input id="display_name" name="display_name" autocomplete="nickname" required maxlength="40" value="'
            + escape(display_name, quote=True) + '"><small>用于导航栏与新产生的操作记录，最多 40 个字符。</small></div>'
            + '<div class="tc-field"><label class="tc-label" for="bio">个人简介</label><textarea id="bio" name="bio" maxlength="500" placeholder="介绍你的工作方向或协作习惯…">'
            + escape(bio) + '</textarea><small>选填，最多 500 个字符。</small></div>'
            + '<div class="tc-field"><label class="tc-label" for="avatar">上传头像</label><input id="avatar" name="avatar" type="file" accept="image/png,image/jpeg,image/webp">'
            + '<small>PNG / JPEG / WebP，最多 2 MiB。保存后自动居中裁切为正方形；不选择文件则保留现有头像。</small></div>')
    if profile['has_avatar']:
        body += '<label><input type="checkbox" name="remove_avatar" value="1" style="width:auto;min-height:auto"> 恢复默认头像</label>'
    body += ('<div class="tc-actions"><button class="tc-button primary">保存个人资料</button>' + web_ui.button('返回工作台', '/')
             + '</div></form></section><aside class="tc-card"><h2 class="tc-section">当前资料</h2>'
             + '<img class="tc-avatar large" src="/auth/avatar" alt="当前头像"><h3>' + escape(profile['display_name']) + '</h3>'
             + '<p class="tc-muted">' + escape(profile['bio'] or '还没有填写个人简介。') + '</p>'
             + '<dl class="tc-facts"><div><dt>登录账号</dt><dd>' + escape(profile['username']) + '</dd></div></dl>'
             + '<div class="tc-actions">' + web_ui.button('登录账号与绑定', '/auth/account') + '</div></aside></div>')
    response = web_ui.render('个人资料', '设置头像和个人介绍，让工作空间里的身份更清晰。', body,
                             user=profile, token=token, active='profile', accent='#4f46e5',
                             scripts=watch_session(user['id'], token))
    response.set_cookie('tracecase_csrf', csrf, httponly=True, secure=SECURE, samesite='strict', max_age=3600)
    return response


async def profile_page(request):
    store = accounts()
    token = request.cookies.get(COOKIE)
    try:
        user = store.me(token)
    except AccessError:
        return RedirectResponse('/auth/login', 303)
    if request.method == 'GET':
        return profile_response(request, user)
    form = None
    try:
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > profiles.MAX_AVATAR_BYTES + 16384:
                raise AccessError('头像上传内容过大，请选择 2 MiB 以内的图片。')
            chunks.append(chunk)
        request._body = b''.join(chunks)
        form = await request.form(max_files=1, max_fields=8, max_part_size=2048)
        csrf_ok(request, form)
        store.throttle('profile:' + user['id'], 30)
        upload = form.get('avatar')
        data = None
        filename = ''
        from starlette.datastructures import UploadFile
        if isinstance(upload, UploadFile) and upload.filename:
            filename = upload.filename
            data = await upload.read(profiles.MAX_AVATAR_BYTES + 1)
        try:
            revision = int(form.get('revision', ''))
        except (TypeError, ValueError):
            raise AccessError('资料版本无效，请刷新后重试。') from None
        await asyncio.to_thread(profiles.save_profile, store, token, form.get('display_name', ''),
                                form.get('bio', ''), revision, avatar=data, filename=filename,
                                remove_avatar=form.get('remove_avatar') == '1')
        return RedirectResponse('/auth/profile?saved=1', 303, headers={'Cache-Control': 'no-store'})
    except HTTPException as exc:
        if exc.status_code != 400:
            raise
        return profile_response(request, user, '上传表单无效，请重新选择头像后提交。')
    except profiles.ProfileConflict as exc:
        return profile_response(request, user, str(exc))
    except AccessError as exc:
        return profile_response(request, user, str(exc), form)
    finally:
        if form is not None:
            await form.close()


async def avatar_image(request):
    try:
        content = await asyncio.to_thread(profiles.avatar_png, accounts(), request.cookies.get(COOKIE))
    except AccessError:
        return Response(status_code=401, headers={'Cache-Control': 'no-store'})
    return Response(content, media_type='image/png', headers={
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Disposition': 'inline; filename="avatar.png"'})


async def github_start(request):
    try:
        config = github_auth.Config.load()
        store = accounts()
        store.throttle('github-start:' + request.client.host, 30)
        link = request.url.path == '/auth/github/link'
        if link:
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8192:
                    raise AccessError('请求过长。')
                chunks.append(chunk)
            request._body = b''.join(chunks)
            form = await request.form()
            csrf_ok(request, form)
        url, browser = github_auth.start(store, config, request.cookies.get(COOKIE), link)
        response = RedirectResponse(url, 303)
        # Lax is required for this short-lived browser binding on the external redirect back.
        response.set_cookie(github_auth.FLOW_COOKIE, browser, httponly=True, secure=SECURE,
                            samesite='lax', max_age=github_auth.FLOW_TTL, path='/auth/github')
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        return response
    except AccessError as exc:
        return page(str(exc), csrf=request.cookies.get('tracecase_csrf'))


async def github_callback(request):
    try:
        config = github_auth.Config.load()
        store = accounts()
        store.throttle('github-callback:' + request.client.host, 30)
        if any(len(request.query_params.getlist(key)) > 1 for key in ('state', 'code', 'error')):
            raise AccessError('GitHub 登录参数无效，请重新发起。')
        flow = github_auth.consume(store, config, request.query_params.get('state'),
                                   request.cookies.get(github_auth.FLOW_COOKIE))
        if request.query_params.get('error'):
            raise AccessError('GitHub 授权已取消或未完成，可重新尝试或使用账号密码登录。')
        identity = await github_auth.profile(config, request.query_params.get('code'), flow['verifier'])
        token = await asyncio.to_thread(github_auth.finish, store, identity, flow)
        # Commit a same-origin document before navigating, preserving Strict session-cookie semantics.
        response = web_ui.render('GitHub 登录成功', '正在进入工作台…', '<section class="tc-card"><p>登录已完成，可以继续访问项目。</p>' + web_ui.button('进入工作台', '/', primary=True) + '</section>', scripts='<script>window.location.replace("/");</script>')
        response.set_cookie(COOKIE, token, httponly=True, secure=SECURE, samesite='strict', max_age=SESSION_SECONDS)
    except AccessError as exc:
        response = page(str(exc), csrf=request.cookies.get('tracecase_csrf'))
    response.delete_cookie(github_auth.FLOW_COOKIE, path='/auth/github')
    response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})
    return response


async def download(request):
    from sdlc.secure_service import Service
    try:
        service = Service(DB_PATH, token=request.cookies.get(COOKIE),
                          workspace_id=request.query_params.get('workspace'), project_id=request.query_params.get('project'))
        exported = service.get_export(request.path_params['export_id'])
        return Response(exported['content'], media_type=exported['mime'], headers={
            'Content-Disposition': 'attachment; filename="' + exported['filename'] + '"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    except ValueError:
        return web_ui.render('暂时无法下载', '无权下载此快照，请重新登录或核对权限。', '<section class="tc-card">' + web_ui.button('返回工作台', '/') + '</section>', status_code=403)


async def proxy(request):
    try:
        user = accounts().me(request.cookies.get(COOKIE))
    except AccessError:
        if request.url.path != '/':
            return Response('登录已失效，请重新登录。', status_code=401)
        return RedirectResponse('/auth/login', 303)
    # Streamlit's byte/media cache is not an authorization boundary. All downloads use /auth/download.
    if request.url.path.startswith('/media/'):
        return Response(status_code=403)
    content, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 2 * 1024 * 1024:
            return Response('上传内容过大。', status_code=413)
        content.append(chunk)
    url = UPSTREAM + request.url.path
    if request.url.query:
        url += '?' + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {'host', 'connection', 'content-length'}}
    try:
        async with httpx.AsyncClient(timeout=310) as client:
            upstream = await client.request(request.method, url, headers=headers, content=b''.join(content))
        response_headers = {k: v for k, v in upstream.headers.items()
                            if k.lower() not in {'transfer-encoding', 'content-encoding', 'content-length', 'connection'}}
        response_headers['Cache-Control'] = 'no-store'
        content = upstream.content
        if upstream.status_code == 200 and 'text/html' in upstream.headers.get('content-type', ''):
            content += watch_session(user['id'], request.cookies.get(COOKIE)).encode()
        return Response(content, upstream.status_code, response_headers)
    except httpx.HTTPError:
        return web_ui.render('工作台正在启动', '工作台正在启动，请稍后刷新。', '<section class="tc-card">' + web_ui.button('重新打开工作台', '/') + '</section>', user=user, token=request.cookies.get(COOKIE), scripts=watch_session(user['id'], request.cookies.get(COOKIE)), status_code=503)


async def socket_proxy(websocket):
    try:
        accounts().me(websocket.cookies.get(COOKIE))
        if websocket.headers.get('origin') not in {f'http://{websocket.headers.get("host")}', f'https://{websocket.headers.get("host")}'}:
            raise AccessError('来源无效')
    except AccessError:
        await websocket.close(code=1008)
        return
    protocols = websocket.scope.get('subprotocols', [])
    await websocket.accept(subprotocol=protocols[0] if protocols else None)
    try:
        async with websockets.connect(UPSTREAM.replace('http://', 'ws://') + websocket.url.path,
                additional_headers={'Cookie': websocket.headers.get('cookie', '')},
                subprotocols=protocols, max_size=4 * 1024 * 1024) as upstream:
            async def to_upstream():
                while True:
                    event = await websocket.receive()
                    if event['type'] == 'websocket.disconnect':
                        return
                    await upstream.send(event.get('bytes') if event.get('bytes') is not None else event['text'])

            async def to_browser():
                async for data in upstream:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
            tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, websockets.WebSocketException, RuntimeError, WebSocketDisconnect):
        pass


app = Starlette(routes=[
    Route('/auth/session', session_status),
    Route('/auth/account', account_page),
    Route('/auth/profile', profile_page, methods=['GET', 'POST']),
    Route('/auth/avatar', avatar_image),
    Route('/auth/github/start', github_start),
    Route('/auth/github/link', github_start, methods=['POST']),
    Route('/auth/github/callback', github_callback),
    Route('/auth/password', password_page, methods=['GET']),
    Route('/auth/download/{export_id}', download),
    *[Route('/auth/' + name, auth, methods=['GET', 'POST']) for name in ('login', 'register', 'logout', 'password')],
    WebSocketRoute('/{path:path}', socket_proxy),
    Route('/{path:path}', proxy, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']),
])

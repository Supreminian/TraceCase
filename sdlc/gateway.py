"""Local same-origin login gateway. HttpOnly session cookies never enter URLs or localStorage."""
import asyncio
from html import escape
import os
from pathlib import Path
import secrets

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect
from starlette.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from starlette.routing import Route, WebSocketRoute
import websockets

from sdlc.accounts import Accounts, AccessError, COOKIE, SESSION_SECONDS, logout_proof, session_marker
from sdlc import github_auth

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
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TraceCase · 登录</title><style>body{background:#f4f6fa;color:#18283e;font:16px system-ui;margin:0}main{max-width:440px;margin:7vh auto;padding:32px;background:white;border:1px solid #dde3ec;border-radius:18px}h1{color:#2563eb}input{box-sizing:border-box;width:100%;padding:12px;margin:6px 0 16px;border:1px solid #cbd5e1;border-radius:8px}button{background:#2563eb;color:white;border:0;border-radius:8px;padding:12px 24px;cursor:pointer}.github{display:block;background:#18283e;color:white;padding:12px;text-align:center;text-decoration:none;border-radius:8px}details{margin-top:26px;border-top:1px solid #dde3ec;padding-top:20px}p{line-height:1.7}.message{color:#b45309}</style>
<main><h1>TraceCase</h1><p>登录后，在个人空间或团队空间中生成、审核和导出测试用例。</p>'''
    html += '<p class="message">' + escape(message) + '</p>'
    if github_auth.configured():
        html += '<p><a class="github" href="/auth/github/start">使用 GitHub 登录</a></p>'
    else:
        html += '<p><button disabled style="background:#94a3b8;cursor:default">GitHub 登录暂不可用</button></p>'
    hidden = '<input type="hidden" name="csrf" value="' + escape(csrf) + '">'
    html += '<h2>登录</h2><form method="post" action="/auth/login">' + hidden
    html += '<label>账号<input name="username" autocomplete="username" required maxlength="40"></label><label>密码<input name="password" type="password" autocomplete="current-password" required maxlength="128"></label><button>登录</button></form>'
    html += '<details><summary>还没有账号？注册</summary><form method="post" action="/auth/register">' + hidden
    html += '<label>账号<input name="username" required minlength="3" maxlength="40" autocomplete="username" placeholder="英文字母、数字、_、- 或 ."></label><label>显示名称<input name="display_name" required maxlength="40"></label><label>密码（10–128 个字符）<input name="password" type="password" required minlength="10" maxlength="128" autocomplete="new-password"></label><label>确认密码<input name="confirmation" type="password" required maxlength="128" autocomplete="new-password"></label><button>创建账号</button></form></details></main></html>'
    if signed_out:
        html += AUTH_BROADCAST
    response = HTMLResponse(html)
    response.set_cookie('tracecase_csrf', csrf, httponly=True, secure=SECURE, samesite='strict', max_age=3600)
    response.headers['Cache-Control'] = 'no-store'
    return response


def csrf_ok(request, form):
    # Native forms share the gateway's origin. Ignore untrusted forwarded host headers.
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise AccessError('请求来源无效，请重新打开登录页。')
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
            html = '<h2>退出当前登录</h2><p>' + escape(user['display_name']) + '</p><form method="post" action="/auth/logout"><input type="hidden" name="logout_proof" value="' + logout_proof(token) + '"><button>退出登录</button></form><p><a href="/">返回工作台</a></p>'
            return HTMLResponse(html + watch_session(user['id'], request.cookies.get(COOKIE)), headers={'Cache-Control': 'no-store'})
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
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                raise AccessError('请求来源无效，请刷新工作台后重试。')
            if token:
                expected = logout_proof(token)
                if not expected or not isinstance(proof, str) or not secrets.compare_digest(proof, expected):
                    raise AccessError('登录状态已变化，请刷新工作台后再退出。')
            elif origin != str(request.base_url).rstrip('/'):
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
        return page(str(error), csrf=request.cookies.get('tracecase_csrf'))


async def password_page(request):
    try:
        user = accounts().me(request.cookies.get(COOKIE))
    except AccessError:
        return RedirectResponse('/auth/login', 303)
    if not user['password_login']:
        return HTMLResponse('<h2>登录方式</h2><p>此账号使用 GitHub 登录，没有本地密码。</p><a href="/auth/account">查看登录账号</a> · <a href="/">返回工作台</a>', headers={'Cache-Control': 'no-store'})
    csrf = request.cookies.get('tracecase_csrf') or secrets.token_urlsafe(32)
    response = HTMLResponse('<h2>修改密码</h2><p>修改成功后，所有设备须重新登录。</p><form method="post" action="/auth/password"><input type="hidden" name="csrf" value="' + escape(csrf) + '"><label>当前密码<input name="old_password" type="password" autocomplete="current-password" required maxlength="128"></label><label>新密码<input name="new_password" type="password" autocomplete="new-password" required minlength="10" maxlength="128"></label><button>修改密码</button></form><a href="/">返回工作台</a>' + watch_session(user['id'], request.cookies.get(COOKIE)))
    response.set_cookie('tracecase_csrf', csrf, httponly=True, secure=SECURE, samesite='strict')
    response.headers['Cache-Control'] = 'no-store'
    return response


async def account_page(request):
    try:
        store = accounts()
        user = store.me(request.cookies.get(COOKIE))
        with store.db() as db:
            linked = db.execute("SELECT login FROM external_identities WHERE user_id=? AND provider='github'", (user['id'],)).fetchone()
    except AccessError:
        return RedirectResponse('/auth/login', 303)
    csrf = request.cookies.get('tracecase_csrf') or secrets.token_urlsafe(32)
    html = '<h2>登录账号</h2><p>' + escape(user['display_name']) + '</p>'
    if linked:
        html += '<p>已绑定 GitHub：' + escape(linked['login']) + '</p>'
    elif github_auth.configured():
        html += '<p>绑定后可使用 GitHub 登录当前账号，保留原有项目和团队身份。</p><form method="post" action="/auth/github/link"><input type="hidden" name="csrf" value="' + escape(csrf) + '"><button>绑定 GitHub</button></form>'
    else:
        html += '<p>GitHub 登录暂不可用，请稍后重试。</p>'
    if user['password_login']:
        html += '<p><a href="/auth/password">修改密码</a></p>'
    html += '<p><a href="/">返回工作台</a></p>'
    response = HTMLResponse(html + watch_session(user['id'], request.cookies.get(COOKIE)), headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
    response.set_cookie('tracecase_csrf', csrf, httponly=True, secure=SECURE, samesite='strict', max_age=3600)
    return response


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
        response = HTMLResponse('<!doctype html><meta charset="utf-8"><title>登录成功</title><p>GitHub 登录成功，正在进入工作台…</p><a href="/">进入工作台</a><script>window.location.replace("/");</script>')
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
        return Response('无权下载此快照，请重新登录或核对权限。', status_code=403)


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
        return HTMLResponse('工作台正在启动，请稍后刷新。', status_code=503)


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
    Route('/auth/github/start', github_start),
    Route('/auth/github/link', github_start, methods=['POST']),
    Route('/auth/github/callback', github_callback),
    Route('/auth/password', password_page, methods=['GET']),
    Route('/auth/download/{export_id}', download),
    *[Route('/auth/' + name, auth, methods=['GET', 'POST']) for name in ('login', 'register', 'logout', 'password')],
    WebSocketRoute('/{path:path}', socket_proxy),
    Route('/{path:path}', proxy, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']),
])

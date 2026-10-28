"""Shared HTML shell for all gateway pages. Bodies/scripts must be trusted server templates."""
from html import escape

from starlette.responses import HTMLResponse

from sdlc.theme import TOKENS, secondary_buttons

CSS = TOKENS + secondary_buttons('.tc-button.secondary') + '''
* { box-sizing:border-box; }
body { margin:0; background:var(--tc-bg); color:var(--tc-ink); font:14px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
a { color:var(--tc-primary); text-underline-offset:3px; }
h1,h2,h3,p { margin-top:0; }
h1 { font-size:32px; line-height:1.3; font-weight:740; letter-spacing:-.035em; margin-bottom:10px; }
h2 { font-size:20px; line-height:1.4; margin-bottom:10px; }
h3 { font-size:16px; margin-bottom:8px; }
p { margin-bottom:16px; }
.tc-layout { display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }
.tc-sidebar { position:sticky; top:0; align-self:start; height:100vh; overflow-y:auto; display:flex; flex-direction:column; background:var(--tc-sidebar); border-right:1px solid var(--tc-line); padding:32px 22px; }
.tc-brand { display:flex; gap:12px; align-items:center; color:#1e3455; text-decoration:none; margin-bottom:32px; }
.tc-logo { display:grid; place-items:center; width:44px; height:44px; border-radius:11px; background:#1e3455; color:white; font-weight:800; }
.tc-brand strong { font-size:19px; } .tc-brand small { display:block; font-size:11px; color:var(--tc-muted); }
.tc-nav { display:grid; gap:6px; margin:24px 0; }
.tc-nav a { display:block; padding:11px 12px; border-radius:10px; text-decoration:none; color:var(--tc-ink); font-weight:600; }
.tc-nav a:hover { background:#e1e8f2; } .tc-nav a[aria-current="page"] { background:#1e3455; color:white; }
.tc-user { padding-top:24px; margin-top:auto; overflow-wrap:anywhere; }
.tc-avatar { width:52px; height:52px; border-radius:50%; object-fit:cover; border:2px solid white; background:#dbe7fc; }
.tc-avatar.large { width:112px; height:112px; margin-bottom:16px; }
.tc-profile-entry { display:flex; align-items:center; gap:12px; padding:12px; min-height:64px;
 border:1px solid #d8e1ee; border-radius:12px; text-decoration:none; color:var(--tc-ink); background:white;
 box-shadow:0 3px 12px rgba(30,52,85,.05); }
.tc-profile-entry:hover { background:#f8fbff; border-color:#a8bce2; }
.tc-profile-entry:focus-visible { outline:2px solid #2563eb; outline-offset:2px; }
.tc-profile-entry .tc-avatar { width:38px; height:38px; flex-shrink:0; border:0; }
.tc-profile-entry strong { display:block; font-size:14px; font-weight:700; overflow-wrap:anywhere; }
.tc-profile-entry small { display:block; color:var(--tc-muted); font-size:11px; }
.tc-profile-arrow { margin-left:auto; color:var(--tc-muted); font-size:22px; }
.tc-settings-nav { display:flex; flex-wrap:wrap; gap:6px; padding:6px; margin-bottom:24px;
 border:1px solid var(--tc-line); border-radius:12px; background:white; }
.tc-settings-nav a { padding:10px 16px; border-radius:8px; color:var(--tc-ink); text-decoration:none; font-weight:600; }
.tc-settings-nav a:hover { background:#edf1f7; }
.tc-settings-nav a[aria-current="page"] { background:#1e3455; color:white; }
.tc-user small { display:block; color:var(--tc-muted); margin-bottom:6px; }
.tc-user form { margin-top:18px; }
.tc-main { width:100%; max-width:1160px; padding:48px 40px 64px; margin:0 auto; }
.tc-main.guest { max-width:1120px; padding-top:40px; }
.tc-guest-brand { margin-bottom:28px; }
.tc-hero { padding:24px 28px; background:white; border:1px solid var(--tc-line); border-left:5px solid var(--tc-accent,#334155); border-radius:14px; margin-bottom:24px; }
.tc-hero p { margin-bottom:0; color:var(--tc-muted); }
.tc-eyebrow { text-transform:uppercase; font-size:11px; font-weight:750; letter-spacing:.12em; color:var(--tc-accent,#334155); margin-bottom:8px; }
.tc-grid { display:grid; grid-template-columns:minmax(0,1.6fr) minmax(220px,1fr); gap:24px; align-items:start; }
.tc-card { border:1px solid var(--tc-line); background:white; border-radius:14px; padding:24px; min-width:0; }
.tc-card + .tc-card { margin-top:20px; }
.tc-grid > .tc-card { margin-top:0; }
.tc-section { display:flex; align-items:center; gap:8px; margin-bottom:18px; font-size:16px; font-weight:720; }
.tc-section::before { content:""; width:8px; height:8px; border-radius:50%; background:var(--tc-accent,#2563eb); flex-shrink:0; }
.tc-muted { color:var(--tc-muted); }
.tc-label { display:block; margin-bottom:6px; font-weight:600; }
.tc-field { margin-bottom:20px; }
.tc-field small { display:block; margin-top:6px; color:var(--tc-muted); }
textarea { resize:vertical; min-height:140px; }
input:not([type=hidden]),textarea { width:100%; min-height:44px; padding:10px 12px; color:var(--tc-ink); background:#fbfcfe; border:1px solid #cbd5e1; border-radius:9px; font:inherit; }
input:focus-visible,textarea:focus-visible { outline:none; border-color:#2563eb; box-shadow:0 0 0 3px rgba(37,99,235,.15); }
.tc-button { display:inline-flex; align-items:center; justify-content:center; min-height:40px; padding:8px 14px; border:1px solid transparent; border-radius:9px; font:600 14px/1.6 system-ui,sans-serif; cursor:pointer; text-decoration:none; }
.tc-button.primary { color:white; background:var(--tc-primary); border-color:var(--tc-primary); }
.tc-button.primary:hover { background:#1d4ed8; border-color:#1d4ed8; }
.tc-button.github { color:white; background:#18283e; border-color:#18283e; width:100%; }
.tc-button.github:hover:not(:disabled) { background:#334155; }
.tc-button:focus-visible { outline:3px solid rgba(37,99,235,.3); outline-offset:2px; }
.tc-button:disabled { cursor:not-allowed; opacity:.45; }
.tc-wide { width:100%; }
.tc-actions { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-top:24px; }
.tc-note { background:#eff6ff; color:#244f9b; border:1px solid #c7d9fa; border-radius:10px; padding:14px 16px; margin-bottom:20px; }
.tc-note.error { background:#fff1f2; border-color:#fecdd3; color:#9f1239; }
.tc-note.success { background:#ecfdf5; border-color:#a7f3d0; color:#065f46; }
.tc-badge { display:inline-block; padding:4px 10px; border-radius:99px; font-size:12px; font-weight:600; color:#334155; background:#edf1f7; margin-bottom:14px; }
.tc-badge.success { color:#065f46; background:#ecfdf5; }
.tc-facts { margin:0; } .tc-facts div { padding:12px 0; border-bottom:1px solid #edf1f6; }
.tc-facts dt { color:var(--tc-muted); font-size:12px; } .tc-facts dd { margin:4px 0 0; font-weight:600; overflow-wrap:anywhere; }
.tc-divider { display:flex; align-items:center; gap:14px; color:var(--tc-muted); font-size:12px; margin:22px 0; }
.tc-divider::before,.tc-divider::after { content:""; flex:1; height:1px; background:var(--tc-line); }
details { border-top:1px solid var(--tc-line); margin-top:24px; padding-top:20px; }
summary { cursor:pointer; color:#244f9b; font-weight:600; } details form { margin-top:20px; }
.tc-list { padding-left:20px; color:var(--tc-muted); } .tc-list li { margin-bottom:12px; }
@media(max-width:760px) {
 .tc-layout { grid-template-columns:1fr; }
 .tc-sidebar { position:static; height:auto; align-self:auto; overflow:visible; padding:20px; border-right:0; border-bottom:1px solid var(--tc-line); }
 .tc-sidebar .tc-brand { margin-bottom:12px; } .tc-nav { display:flex; flex-wrap:wrap; margin:12px 0; }
 .tc-user { display:flex; flex-wrap:wrap; align-items:center; gap:12px; padding-top:14px; margin-top:14px; }
 .tc-user small { margin:0; } .tc-user form { margin:0 0 0 auto; } .tc-main,.tc-main.guest { padding:24px 16px 40px; }
 .tc-grid { grid-template-columns:1fr; } .tc-hero,.tc-card { padding:20px; } h1 { font-size:26px; }
}
'''


def button(label, href, primary=False):
    href = {'/auth/account': '/auth/profile?section=account', '/auth/password': '/auth/profile?section=password'}.get(href, href)
    return f'<a class="tc-button {"primary" if primary else "secondary"}" href="{escape(href, quote=True)}">{escape(label)}</a>'


def field(label, name, *, password=False, autocomplete='', minimum=None, maximum=128, hint='', placeholder='', input_id=None):
    input_id = input_id or name
    kind = 'password' if password else 'text'
    bounds = f' minlength="{minimum}"' if minimum is not None else ''
    help_text = f'<small id="help-{input_id}">{escape(hint)}</small>' if hint else ''
    described = f' aria-describedby="help-{input_id}"' if hint else ''
    return (f'<div class="tc-field"><label class="tc-label" for="{input_id}">{escape(label)}</label>'
            f'<input id="{input_id}" name="{name}" type="{kind}" autocomplete="{autocomplete}" required '
            f'maxlength="{maximum}"{bounds}{described} placeholder="{escape(placeholder, quote=True)}">{help_text}</div>')


def hidden_csrf(csrf):
    return '<input type="hidden" name="csrf" value="' + escape(csrf, quote=True) + '">'


def notice(message, kind='error'):
    return f'<div class="tc-note {kind}" role="status">{escape(message)}</div>' if message else ''


def render(title, description, body, *, user=None, token=None, active='', accent='#334155', scripts='', status_code=200):
    brand = '<a class="tc-brand" href="/"><span class="tc-logo">TC</span><span><strong>TraceCase</strong><small>需求与测试工作台</small></span></a>'
    hero = f'<header class="tc-hero"><div class="tc-eyebrow">{"账号设置" if user else "需求与测试复核"}</div><h1>{escape(title)}</h1><p>{escape(description)}</p></header>'
    if user:
        settings = active in {'profile', 'account', 'password', 'logout'}
        nav = '<a href="/">返回工作台</a><a href="/auth/profile"' + (' aria-current="page"' if settings else '') + '>个人资料</a>'
        sidebar = '<aside class="tc-sidebar">' + brand + '<nav class="tc-nav" aria-label="工作台导航">' + nav + '</nav><div class="tc-user">' + user_identity(user) + '</div></aside>'
        if settings:
            menu = ''
            for key, label, href in [('profile', '个人资料', '/auth/profile'),
                                      ('account', '登录账号与绑定', '/auth/profile?section=account'),
                                      ('password', '修改密码', '/auth/profile?section=password'),
                                      ('logout', '退出登录', '/auth/profile?section=logout')]:
                if key == 'password' and not user.get('password_login', True):
                    continue
                menu += '<a href="' + href + '"' + (' aria-current="page"' if active == key else '') + '>' + label + '</a>'
            body = '<nav class="tc-settings-nav" aria-label="个人资料功能">' + menu + '</nav>' + body
        content = '<div class="tc-layout">' + sidebar + '<main class="tc-main">' + hero + body + '</main></div>'
    else:
        content = '<main class="tc-main guest"><div class="tc-guest-brand">' + brand + '</div>' + hero + body + '</main>'
    html = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>TraceCase · ' + escape(title) + '</title><style>' + CSS + '</style></head>'
            '<body style="--tc-accent:' + accent + '">' + content + scripts + '</body></html>')
    return HTMLResponse(html, status_code=status_code, headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})


def user_identity(user):
    return ('<a class="tc-profile-entry" href="/auth/profile" target="_self" aria-label="查看个人资料" title="个人资料">'
            '<img class="tc-avatar" src="/auth/avatar" alt="我的头像">'
            '<span><strong>' + escape(user['display_name']) + '</strong><small>个人资料与账号设置</small></span><span class="tc-profile-arrow" aria-hidden="true">›</span></a>')

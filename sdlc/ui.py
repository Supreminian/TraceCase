"""Shared presentation: accessible surfaces, workflow cues and module identities."""
from html import escape
from datetime import datetime
import time
from zoneinfo import ZoneInfo

import streamlit as st

from sdlc.theme import TOKENS, secondary_buttons


TONES = {'input': '#2563eb', 'review': '#0f766e', 'clarify': '#b77910',
         'export': '#7c3aed', 'workspace': '#334155', 'analysis': '#4f46e5'}


def apply_style():
    st.markdown('<style>' + TOKENS + secondary_buttons(
        '.stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"], .tc-logout-button)') + '''
    .stApp { background:#f4f6fa; }
    .stMainBlockContainer { max-width:1380px; padding-top:4.5rem; padding-bottom:4rem; }
    [data-testid="stAppDeployButton"] { display:none; }
    h1,h2,h3 { color:var(--tc-ink); letter-spacing:-.035em; }
    h1 { font-size:2rem!important; font-weight:740!important; }
    h2 { font-size:1.35rem!important; } h3 { font-size:1.1rem!important; }
    [data-testid="stCaptionContainer"] { color:var(--tc-muted); line-height:1.65; }
    [data-testid="stSidebar"] { background:#edf1f7; border-right:1px solid var(--tc-line); }
    /* The sidebar menu scrolls independently of its persistent footer. */
    [data-testid="stSidebarContent"] { display:flex; flex-direction:column; height:100%; overflow:hidden; }
    [data-testid="stSidebarHeader"] { flex-shrink:0; }
    [data-testid="stSidebarUserContent"] { flex:1; min-height:0; overflow-y:auto; padding-bottom:120px; }
    [data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.tc-sidebar-footer) {
      position:absolute; bottom:0; left:0; right:0; z-index:3; width:100%; min-height:100px;
      background:#edf1f7; border-top:1px solid var(--tc-line); padding:12px 16px; margin:0; }
    .tc-sidebar-footer { color:var(--tc-muted); font-size:12px; line-height:1.65; }
    /* Navigation keeps keyboard/radio semantics, without form-control circles. */
    .st-key-page [data-testid="stRadioOption"] > div > div:first-child:not([data-testid]) { display:none; }
    .st-key-page [data-testid="stRadioOption"] > div { gap:0; }
    [data-testid="stSidebar"] [role="radiogroup"] { gap:5px; }
    [data-testid="stSidebar"] [role="radiogroup"] > div { width:100%; }
    [data-testid="stSidebar"] [role="radiogroup"] label {
      padding:10px 12px; border-radius:10px; width:100%; transition:background .15s; }
    [data-testid="stSidebar"] [role="radiogroup"] label:hover { background:#e1e8f2; }
    [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
      background:#1e3455; color:#fff; }
    [data-testid="stSidebar"] [role="radiogroup"] label:has(input:focus-visible) { outline:2px solid #2563eb; }
    .st-key-page [data-testid="stRadioOption"] p { font-size:14px; font-weight:600; }
    /* Account/team actions and project choices use the same navigation treatment. */
    .st-key-workspace_navigation { gap:5px; }
    .stApp [data-testid="stSidebar"] .st-key-workspace_navigation :is(button,a) > div {
      justify-content:flex-start; }
    .stApp [data-testid="stSidebar"] .st-key-workspace_navigation :is(button,a,.tc-logout-button) {
      display:flex; justify-content:flex-start; width:100%; min-height:44px;
      padding:10px 12px; border:0; border-radius:10px; background:transparent;
      color:var(--tc-ink); box-shadow:none; font-size:14px; font-weight:600; text-align:left; text-decoration:none; }
    .stApp [data-testid="stSidebar"] .st-key-workspace_navigation :is(button,a,.tc-logout-button):hover {
      background:#e1e8f2; color:var(--tc-ink); border:0; }
    .stApp [data-testid="stSidebar"] .st-key-workspace_navigation [data-testid="stBaseButton-primary"],
    .stApp [data-testid="stSidebar"] .st-key-workspace_navigation [data-testid="stBaseButton-primary"]:hover {
      background:#1e3455; color:white; }
    .stApp [data-testid="stSidebar"] .st-key-workspace_navigation :is(button,a,.tc-logout-button):focus-visible {
      outline:2px solid #2563eb; outline-offset:2px; }
    .stApp [data-testid="stSidebar"] .st-key-workspace_navigation p { font-size:14px; font-weight:600; }
    .tc-generation { background:linear-gradient(120deg,#fff,#eff5ff); border:1px solid #c7d9fa;
      border-left:4px solid #2563eb; border-radius:14px; padding:22px 24px; margin:12px 0 22px; }
    .tc-generation-top { display:flex; align-items:center; gap:14px; margin-bottom:18px; }
    .tc-generation-spinner { width:28px; height:28px; flex-shrink:0; border:3px solid #dbe7fc;
      border-top-color:#2563eb; border-radius:50%; animation:tc-spin 1s linear infinite; }
    .tc-generation h3 { font-size:18px!important; margin:0 0 5px; color:#18283e; }
    .tc-generation p { margin:0; color:#63748b; font-size:13px; line-height:1.7; }
    .tc-generation-stages { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; }
    .tc-generation-stage { padding:8px 12px; border:1px solid #e1e7ef; border-radius:8px;
      font-size:12px; color:#63748b; background:#fff; }
    .tc-generation-stage.done { color:#0f766e; background:#ecfdf5; border-color:#b9ddd3; }
    .tc-generation-stage.active { color:#1d4ed8; background:#e8effd; border-color:#b9cdf8; font-weight:700; }
    .tc-generation-time { font-size:12px; color:#1d4ed8; margin-bottom:7px; }
    .tc-generation-track { height:3px; background:#dbe7fc; overflow:hidden; border-radius:99px; margin-top:18px; }
    .tc-generation-track::after { content:""; display:block; width:35%; height:100%; background:#2563eb;
      animation:tc-travel 2s ease-in-out infinite; border-radius:99px; }
    @keyframes tc-spin { to { transform:rotate(360deg); } }
    @keyframes tc-travel { from { transform:translateX(-110%); } to { transform:translateX(390%); } }
    @media(prefers-reduced-motion:reduce) {
      .tc-generation-spinner,.tc-generation-track::after { animation:none; }
    }
    .tc-mode { --mode-accent:#0f766e; --mode-bg:#e8f5f1; --mode-line:#b9ddd3; }
    .tc-mode.personal { --mode-accent:#1d4ed8; --mode-bg:#eff6ff; --mode-line:#c7d9fa; }
    .tc-mode-banner { display:flex; align-items:center; flex-wrap:wrap; gap:10px 18px;
      border:1px solid var(--mode-line); border-left:4px solid var(--mode-accent);
      background:var(--mode-bg); padding:12px 18px; margin-bottom:18px; border-radius:10px; }
    .tc-mode-banner strong { color:var(--mode-accent); font-size:14px; font-weight:750; }
    .tc-mode-banner span { color:var(--tc-muted); font-size:12px; }
    .tc-space-name { color:var(--tc-ink); font-weight:700; overflow-wrap:anywhere; margin-bottom:8px; }
    [data-testid="stVerticalBlockBorderWrapper"] { border-color:var(--tc-line)!important; border-radius:14px; }
    [data-testid="stForm"] { background:white; border:1px solid var(--tc-line); border-radius:14px; padding:20px; }
    [data-testid="stMetric"] { background:#fff; border:1px solid var(--tc-line); border-radius:12px; padding:16px 18px; }
    [data-testid="stMetricLabel"] { color:var(--tc-muted); }
    [data-testid="stMetricValue"] { color:var(--tc-ink); font-weight:700; font-size:1.8rem; }
    .stTabs [data-baseweb="tab-list"] { gap:5px; background:#fff; border:1px solid var(--tc-line); padding:6px; border-radius:12px; }
    .stTabs [data-baseweb="tab"] { padding:10px 16px; height:auto; border-radius:8px; font-weight:600; }
    .stTabs [aria-selected="true"] { background:#e8effd!important; color:#244f9b!important; }
    .stTabs [data-baseweb="tab-highlight"],.stTabs [data-baseweb="tab-border"] { display:none; }
    [data-testid="stExpander"] { border-color:var(--tc-line); background:#fff; border-radius:10px; }
    button { border-radius:9px!important; font-weight:600!important; }
    .stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"]) p {
      margin:0; font-size:inherit; font-weight:inherit; line-height:inherit; }
    [data-testid="stTextArea"] textarea { background:#fbfcfe; border-radius:10px; line-height:1.7; }
    [data-testid="stAlert"] { border-radius:10px; }
    [class*="st-key-header_"] { padding:22px 26px; background:white; border:1px solid var(--tc-line); border-radius:14px; margin-bottom:6px; }
    .st-key-header_input { border-left:5px solid #2563eb; }
    .st-key-header_review { border-left:5px solid #0f766e; }
    .st-key-header_clarify { border-left:5px solid #b77910; }
    .st-key-header_workspace { border-left:5px solid #334155; }
    .st-key-header_export { border-left:5px solid #7c3aed; }
    .st-key-header_analysis { border-left:5px solid #4f46e5; }
    .st-key-workspace_personal,.st-key-workspace_team_empty,
    [class*="st-key-workspace_team_card_"],.st-key-project_empty,.st-key-project_create_action,.st-key-prd_input,.st-key-generation_summary,[class*="st-key-case_body_"],
    [class*="st-key-case_source_panel_"],[class*="st-key-review_actions_"],
    [class*="st-key-extraction_"],.st-key-export_workspace,.st-key-clarification_workspace,.st-key-review_result_workspace { background:#fff; padding:22px; border-radius:14px; }
    .st-key-workspace_personal,.st-key-workspace_team_empty,[class*="st-key-workspace_team_card_"] {
      padding:18px; max-width:360px; }
    .st-key-workspace_team_actions { max-width:360px; }
    [class*="st-key-workspace_team_card_"] [data-testid="stCaptionContainer"] p {
      display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }
    [class*="st-key-workspace_team_card_"] .tc-section { overflow-wrap:anywhere; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }
    @media(max-width:1050px) {
      .st-key-workspace_team_list [data-testid="stHorizontalBlock"]:has([class*="st-key-workspace_team_card_"]) { flex-wrap:wrap; }
      .st-key-workspace_team_list [data-testid="stHorizontalBlock"]:has([class*="st-key-workspace_team_card_"]) > [data-testid="stColumn"] {
        flex:1 1 100%!important; width:100%!important; }
    }
    .st-key-prd_input { border-top:3px solid #2563eb!important; }
    .st-key-generation_summary { border-top:3px solid #4f46e5!important; }
    [class*="st-key-case_body_"] { border-top:3px solid #0f766e!important; }
    [class*="st-key-case_source_panel_"] { border-top:3px solid #94a3b8!important; }
    [class*="st-key-review_actions_"] { border-top:3px solid #0f766e!important; }
    .st-key-extraction_features { border-top:3px solid #2563eb!important; }
    .st-key-extraction_roles { border-top:3px solid #7c3aed!important; }
    .st-key-extraction_business_rules { border-top:3px solid #0f766e!important; }
    .st-key-export_workspace { border-top:3px solid #7c3aed!important; }
    .st-key-clarification_workspace { border-top:3px solid #b77910!important; }
    .st-key-review_result_workspace { border-top:3px solid #0f766e!important; }
    .tc-eyebrow { font-size:11px; font-weight:750; letter-spacing:.12em; margin-bottom:4px; }
    .stApp [data-testid="stSidebar"] .tc-profile-entry {
      display:flex; align-items:center; gap:12px; padding:12px; margin:0;
      text-decoration:none; background:#fff; border:1px solid #d8e1ee; border-radius:12px;
      box-shadow:0 3px 12px rgba(30,52,85,.05); color:var(--tc-ink); }
    .stApp [data-testid="stSidebar"] .tc-profile-entry:hover {
      background:#f8fbff; border-color:#a8bce2; text-decoration:none; }
    .tc-profile-entry:focus-visible { outline:2px solid #2563eb; outline-offset:2px; }
    .tc-profile-entry .tc-avatar { width:38px; height:38px; border-radius:50%; object-fit:cover; flex-shrink:0; }
    .tc-profile-entry strong { display:block; color:var(--tc-ink); font-size:14px; font-weight:700; overflow-wrap:anywhere; }
    .tc-profile-entry small { display:block; color:var(--tc-muted); font-size:11px; }
    .tc-profile-entry .tc-profile-arrow { margin-left:auto; color:#63748b; font-size:22px; }
    .tc-brand { display:flex; align-items:center; gap:12px; margin-bottom:22px; }
    .tc-logo { background:#1e3455; color:#fff; padding:10px; border-radius:11px; font-weight:800; letter-spacing:-1px; }
    .tc-brand strong { font-size:19px; color:#1e3455; } .tc-brand small { color:#63748b; display:block; font-size:11px; }
    .tc-stepper { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:8px 0 22px; }
    .tc-step { padding:11px 12px; border:1px solid var(--tc-line); border-radius:10px; color:#64748b; background:#fff; font-size:12px; }
    .tc-step.active { background:#e8effd; color:#244f9b; border-color:#b9cdf8; font-weight:700; }
    .tc-step b { margin-right:7px; opacity:.65; }
    .tc-section { font-size:16px; font-weight:720; color:var(--tc-ink); margin-bottom:4px; }
    .tc-section span { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px; }
    .tc-output { border-bottom:1px solid #edf1f6; padding:14px 0; }
    .tc-output strong { color:#24354b; font-size:14px; } .tc-output p { color:#63748b; font-size:12px; margin:5px 0; }
    @media(max-width:700px) {
      .stMainBlockContainer { padding:4.5rem .8rem 3rem; }
      [class*="st-key-header_"] { padding:16px; }
      h1 { font-size:1.65rem!important; }
      .tc-step { padding:9px 7px; font-size:11px; } .tc-step b { display:block; }
      .stTabs [data-baseweb="tab"] { padding:8px 10px; }
      [data-testid="stHorizontalBlock"]:has(.st-key-workspace_personal),
      [data-testid="stHorizontalBlock"]:has(.st-key-project_create_action),
      [data-testid="stHorizontalBlock"]:has(.st-key-prd_input),
      [data-testid="stHorizontalBlock"]:has([class*="st-key-case_body_"]),
      [data-testid="stHorizontalBlock"]:has(.st-key-extraction_features) { flex-wrap:wrap; }
      [data-testid="stHorizontalBlock"]:has(.st-key-workspace_personal) > [data-testid="stColumn"],
      [data-testid="stHorizontalBlock"]:has(.st-key-project_create_action) > [data-testid="stColumn"],
      [data-testid="stHorizontalBlock"]:has(.st-key-prd_input) > [data-testid="stColumn"],
      [data-testid="stHorizontalBlock"]:has([class*="st-key-case_body_"]) > [data-testid="stColumn"],
      [data-testid="stHorizontalBlock"]:has(.st-key-extraction_features) > [data-testid="stColumn"] {
        flex:1 1 100%!important; width:100%!important; }
    }
    </style>''', unsafe_allow_html=True)


def page_header(title, description, tone='workspace', eyebrow='项目工作台'):
    with st.container(key='header_' + tone):
        st.markdown(f'<div class="tc-eyebrow" style="color:{TONES[tone]}">{escape(eyebrow)}</div>', unsafe_allow_html=True)
        st.title(title)
        st.caption(description)


def section_title(title, description='', tone='input'):
    st.markdown(f'<div class="tc-section" title="{escape(title, quote=True)}"><span style="background:{TONES[tone]}"></span>{escape(title)}</div>', unsafe_allow_html=True)
    if description:
        st.caption(description)


def workflow(active):
    steps = ('输入需求', '分析与生成', '人工审核', '导出用例')
    html = ''.join(f'<div class="tc-step {"active" if i == active else ""}"><b>0{i + 1}</b>{s}</div>'
                   for i, s in enumerate(steps))
    st.markdown('<div class="tc-stepper">' + html + '</div>', unsafe_allow_html=True)


def format_time(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(
            ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, AttributeError):
        return '时间未记录'


def workspace_mode(space, *, sidebar=False):
    team = space['kind'] == 'team'
    mode = '团队协作' if team else '个人工作'
    kind = 'team' if team else 'personal'
    name = escape(space['name'] if team else '我的个人空间')
    if sidebar:
        html = f'<div class="tc-space-name">{name}</div>'
    else:
        detail = '共享项目与用例，按团队角色协作审核。' if team else '当前项目、用例和审核记录保存在你的个人空间。'
        html = f'<div class="tc-mode tc-mode-banner {kind}" role="status"><strong>{mode} · {name}</strong><span>{detail}</span></div>'
    st.markdown(html, unsafe_allow_html=True)


def generation_activity(job, *, auto_open=False):
    queued = job['status'] == 'queued'
    title = '任务已提交，等待生成' if queued else 'AI 正在处理需求'
    description = ('正在等待可用的生成资源，请勿重复提交。' if queued else
                   '正在分析需求、生成测试用例并进行结果校验。需求不明确的部分会整理为澄清问题。')
    created = job.get('created_at')
    elapsed = max(0, int(time.time() - created)) if isinstance(created, (int, float)) else None
    elapsed_text = (f'任务已用时 {elapsed // 60} 分 {elapsed % 60} 秒' if elapsed is not None else '任务正在后台运行')
    stages = [('done', '✓ 需求已接收'), ('active', '等待开始' if queued else '分析、生成与校验'), ('', '下一步：人工审核')]
    stage_html = ''.join(f'<span class="tc-generation-stage {state}">{label}</span>' for state, label in stages)
    next_step = '完成后将自动打开本次结果。' if auto_open else '完成后可在当前项目中打开结果。'
    st.markdown(f'<section class="tc-generation"><div role="status" aria-live="polite" aria-busy="true">'
                f'<div class="tc-generation-top"><span class="tc-generation-spinner" aria-hidden="true"></span>'
                f'<div><h3>{title}</h3><p>{description}</p></div></div></div>'
                f'<div class="tc-generation-stages">{stage_html}</div><div class="tc-generation-time">{elapsed_text}</div>'
                f'<p>复杂需求可能需要更长时间。你可以继续浏览项目，任务会在后台继续。{next_step}</p>'
                '<div class="tc-generation-track" aria-hidden="true"></div></section>', unsafe_allow_html=True)

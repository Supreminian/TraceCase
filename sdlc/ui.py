"""Shared presentation: accessible surfaces, workflow cues and module identities."""
from html import escape
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st


TONES = {'input': '#2563eb', 'review': '#0f766e', 'clarify': '#b77910',
         'export': '#7c3aed', 'workspace': '#334155', 'analysis': '#4f46e5'}


def apply_style():
    st.markdown('''<style>
    :root { --tc-ink:#18283e; --tc-muted:#63748b; --tc-line:#e1e7ef; }
    .stApp { background:#f4f6fa; }
    .stMainBlockContainer { max-width:1380px; padding-top:4.5rem; padding-bottom:4rem; }
    [data-testid="stAppDeployButton"] { display:none; }
    h1,h2,h3 { color:var(--tc-ink); letter-spacing:-.035em; }
    h1 { font-size:2rem!important; font-weight:740!important; }
    h2 { font-size:1.35rem!important; } h3 { font-size:1.1rem!important; }
    [data-testid="stCaptionContainer"] { color:var(--tc-muted); line-height:1.65; }
    [data-testid="stSidebar"] { background:#edf1f7; border-right:1px solid var(--tc-line); }
    [data-testid="stSidebar"] [role="radiogroup"] { gap:5px; }
    [data-testid="stSidebar"] [role="radiogroup"] > div { width:100%; }
    [data-testid="stSidebar"] [role="radiogroup"] label {
      padding:10px 12px; border-radius:10px; width:100%; transition:background .15s; }
    [data-testid="stSidebar"] [role="radiogroup"] label:hover { background:#e1e8f2; }
    [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
      background:#1e3455; color:#fff; }
    [data-testid="stSidebar"] [role="radiogroup"] label:has(input:focus-visible) { outline:2px solid #2563eb; }
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
    /* One secondary-button style for framework controls and native POST actions. */
    .stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"], .tc-logout-button) {
      display:inline-flex; align-items:center; justify-content:center; box-sizing:border-box;
      min-height:2.5rem; padding:.25rem .75rem; margin:0; font-family:inherit;
      font-size:.875rem; font-weight:600; line-height:1.6; text-decoration:none;
      border:1px solid #cbd5e1; border-radius:9px; background:#fff; color:var(--tc-ink);
      cursor:pointer; user-select:none; transition:background-color .15s, border-color .15s, box-shadow .15s; }
    .stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"]) p {
      margin:0; font-size:inherit; font-weight:inherit; line-height:inherit; }
    .stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"], .tc-logout-button):hover:not(:disabled):not([disabled]) {
      background:#edf1f7; border-color:#94a3b8; }
    .stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"], .tc-logout-button):active:not(:disabled):not([disabled]) {
      background:#e1e7ef; }
    .stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"], .tc-logout-button):focus-visible {
      outline:none; box-shadow:0 0 0 3px rgba(37,99,235,.2); border-color:#2563eb; }
    .stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"], .tc-logout-button):is(:disabled,[disabled]) {
      opacity:.45; cursor:not-allowed; }
    [data-testid="stTextArea"] textarea { background:#fbfcfe; border-radius:10px; line-height:1.7; }
    [data-testid="stAlert"] { border-radius:10px; }
    [class*="st-key-header_"] { padding:22px 26px; background:white; border:1px solid var(--tc-line); border-radius:14px; margin-bottom:6px; }
    .st-key-header_input { border-left:5px solid #2563eb; }
    .st-key-header_review { border-left:5px solid #0f766e; }
    .st-key-header_clarify { border-left:5px solid #b77910; }
    .st-key-header_workspace { border-left:5px solid #334155; }
    .st-key-header_export { border-left:5px solid #7c3aed; }
    .st-key-header_analysis { border-left:5px solid #4f46e5; }
    .st-key-prd_input,.st-key-generation_summary,[class*="st-key-case_body_"],
    [class*="st-key-case_source_panel_"],[class*="st-key-review_actions_"],
    [class*="st-key-extraction_"],.st-key-export_workspace,.st-key-clarification_workspace,.st-key-review_result_workspace { background:#fff; padding:22px; border-radius:14px; }
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
      [data-testid="stHorizontalBlock"]:has(.st-key-prd_input),
      [data-testid="stHorizontalBlock"]:has([class*="st-key-case_body_"]),
      [data-testid="stHorizontalBlock"]:has(.st-key-extraction_features) { flex-wrap:wrap; }
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
    st.markdown(f'<div class="tc-section"><span style="background:{TONES[tone]}"></span>{escape(title)}</div>', unsafe_allow_html=True)
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

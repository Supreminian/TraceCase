"""Visual tokens shared by Streamlit and server-rendered account pages."""
TOKENS = '''
:root {
  --tc-ink:#18283e; --tc-muted:#63748b; --tc-line:#e1e7ef;
  --tc-primary:#2563eb; --tc-bg:#f4f6fa; --tc-panel:#fff; --tc-sidebar:#edf1f7;
}
'''


def secondary_buttons(selector):
    return f'''
    {selector} {{
      display:inline-flex; align-items:center; justify-content:center; box-sizing:border-box;
      min-height:2.5rem; padding:.25rem .75rem; margin:0; font-family:inherit;
      font-size:.875rem; font-weight:600; line-height:1.6; text-decoration:none;
      border:1px solid #cbd5e1; border-radius:9px; background:#fff; color:var(--tc-ink);
      cursor:pointer; user-select:none; transition:background-color .15s, border-color .15s, box-shadow .15s;
    }}
    {selector}:hover:not(:disabled):not([disabled]) {{ background:#edf1f7; border-color:#94a3b8; }}
    {selector}:active:not(:disabled):not([disabled]) {{ background:#e1e7ef; }}
    {selector}:focus-visible {{ outline:none; box-shadow:0 0 0 3px rgba(37,99,235,.2); border-color:#2563eb; }}
    {selector}:is(:disabled,[disabled]) {{ opacity:.45; cursor:not-allowed; }}
    '''

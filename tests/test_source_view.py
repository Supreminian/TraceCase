"""Evidence navigation must never fall back to unrelated PRD paragraphs."""
from streamlit.testing.v1 import AppTest


DOCUMENT = {'paragraphs': [
    {'id': 'P001', 'text': '申请人提交报销单。'},
    {'id': 'P002', 'text': '金额大于 5000 元需要二级审批。'},
    {'id': 'P003', 'text': '驳回需填写原因。'},
]}


def view(references):
    return AppTest.from_string(f'''
import streamlit as st
from app import source_view
st.session_state.setdefault('references', {references!r})
source_view({DOCUMENT!r}, 'evidence', st.session_state['references'])
''').run()


def test_case_evidence_includes_direct_and_linked_rule_sources_only():
    app = AppTest.from_string(f'''
from app import source_view, linked_source_ids
run = {{'output': {{'business_rules': [{{'id': 'R001', 'paragraph_ids': ['P002']}}]}}}}
content = {{'paragraph_ids': ['P001'], 'requirement_ids': ['R001']}}
source_view({DOCUMENT!r}, 'evidence', linked_source_ids(run, content))
''').run()
    assert not app.exception
    assert app.selectbox(key='evidence').options == ['P001', 'P002']
    assert not any('驳回需填写原因' in text.value for text in app.text)
    app.selectbox(key='evidence').select('P002').run()
    assert any('金额大于 5000 元' in text.value for text in app.text)


def test_empty_or_missing_sources_do_not_expose_all_paragraphs():
    for references in ([], ['missing']):
        app = view(references)
        assert not app.exception and not app.selectbox
        assert any('没有可定位的关联段落' in info.value for info in app.info)
        assert not app.text


def test_full_prd_view_still_supports_all_paragraphs():
    app = view(None)
    assert app.selectbox(key='evidence').options == ['P001', 'P002', 'P003']


def test_changed_references_clear_stale_selection_and_deduplicate():
    app = view(['P001', 'P002'])
    app.selectbox(key='evidence').select('P001').run()
    app.session_state['references'] = ['P002', 'P002', 'missing']
    app.run()
    assert not app.exception
    assert app.selectbox(key='evidence').options == ['P002']
    assert app.selectbox(key='evidence').value == 'P002'

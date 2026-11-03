import json

import pytest
from streamlit.testing.v1 import AppTest

from sdlc.service import ServiceError
from sdlc.testing_agent.case_browser import get_case, list_cases, publish_collection
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.tools import build_tools
from tests.test_agent_ui import setup
from tests.test_testing_agent import ready


@pytest.fixture
def cases(tmp_path):
    service = setup(tmp_path / 'cards.sqlite3')
    task = ready(service)
    run = service.finalize_design(task)
    first = service.legacy.get_run(run)['cases'][0]
    service.legacy.review(run, first['case_id'], first['version'], first['revision'],
                          'approve', '', '与需求一致', 'approve-first')
    return service, task, run


def test_query_only_current_heads_and_real_approval_state(cases):
    service, task, run = cases
    approved = list_cases(service, status='approved')
    assert approved['total'] == 1
    case = approved['items'][0]
    assert case['case_id'] == 'TC1'
    assert list_cases(service, status='returned')['total'] == 0
    assert list_cases(service, task_id=task, page_size=1, page=2)['items'][0]['case_id'] == 'TC2'
    assert list_cases(service, keyword='二级')['total'] == 1
    assert list_cases(service, keyword="' OR 1=1 --")['total'] == 0
    content = {**case['content'], 'title': '人工修改标题'}
    service.legacy.edit(run, 'TC1', case['version'], case['revision'], content, '', '修正标题', 'edit-approved')
    assert list_cases(service, status='approved')['total'] == 0
    assert get_case(service, run, 'TC1')['case']['version'] == 2
    assert list_cases(service)['total'] == 2


def test_scope_validation_and_cross_project_denial(cases):
    service, task, run = cases
    other_project = service.legacy.create_project('其他项目', '')
    from sdlc.testing_agent.service import TestingService
    other = TestingService(service.db_path, service.token, service.workspace_id, other_project)
    assert list_cases(other)['total'] == 0
    with pytest.raises(ServiceError):
        list_cases(other, task_id=task)
    with pytest.raises(ServiceError):
        get_case(other, run, 'TC1')
    for kwargs in ({'page': 0}, {'page_size': 999}, {'status': 'invented'}, {'page': True}):
        with pytest.raises(ServiceError):
            list_cases(service, **kwargs)


def test_tool_publishes_live_reference_without_model_roundtrip(cases):
    service, task, run = cases
    tools = {t.name: t for t in build_tools(service, task, None, None)}
    result = json.loads(tools['list_test_cases'].execute({'status': 'approved'}))
    assert result['displayed_in_chat']
    assert result['total'] == 1
    assert 'content' not in result['cases'][0]
    message = next(m for m in ChatService(service).list_messages(task) if m['id'] == result['message_id'])
    assert message['kind'] == 'case_collection'
    payload = json.loads(message['content'])
    assert payload['query']['status'] == 'approved'
    assert payload['shown_references'][0]['case_id'] == 'TC1'
    assert list_cases(service, **payload['query'])['items'][0]['status'] == 'approved'
    detail = json.loads(tools['get_test_case'].execute({'run_id': run, 'case_id': 'TC1'}))
    assert detail['paragraphs']
    assert all(p['id'] in detail['case']['content']['paragraph_ids'] for p in detail['paragraphs'])


def show_cards(service, task):
    app = AppTest.from_string(f'''
import streamlit as st
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.case_cards import render_collection
s = TestingService({service.db_path!r}, {service.token!r}, {service.workspace_id!r}, {service.project_id!r})
@st.fragment
def render():
    for message in ChatService(s).list_messages({task!r}):
        if message['kind'] == 'case_collection':
            render_collection(s, message)
render()
''').run()
    assert not app.exception
    return app


def test_cards_paginate_without_model_and_show_current_status(cases):
    service, task, run = cases
    publish_collection(service, task, query={'status': 'all', 'page_size': 1})
    app = show_cards(service, task)
    assert any('提交申请' in m.value for m in app.markdown)
    next(b for b in app.button if b.label == '下一页').click().run()
    assert not app.exception
    assert any('触发二级审批' in m.value for m in app.markdown)
    assert any('第 2 / 2 页' in c.value for c in app.caption)


def test_card_approval_calls_existing_service(cases):
    service, task, run = cases
    publish_collection(service, task, query={'status': 'pending_review'})
    app = show_cards(service, task)
    next(b for b in app.button if b.label == '提交审核').click().run()
    assert not app.exception
    assert list_cases(service, status='approved')['total'] == 2
    assert any('没有符合条件' in i.value for i in app.info)


def test_chat_card_return_saves_feedback_without_model_call(cases):
    service, task, run = cases
    publish_collection(service, task, query={'status': 'pending_review'})
    app = show_cards(service, task)
    next(s for s in app.selectbox if s.label == '审核决定').select('退回')
    next(t for t in app.text_input if t.label == '说明（退回时必填）').set_value('步骤需要更明确')
    next(b for b in app.button if b.label == '提交审核').click().run()
    assert not app.exception
    assert list_cases(service, status='returned')['total'] == 1
    assert any(p['content'].get('reason') == '步骤需要更明确' for p in service.get_task(task)['proposals'])


def test_stale_card_does_not_approve_unseen_new_version(cases):
    service, task, run = cases
    publish_collection(service, task, query={'status': 'pending_review'})
    app = show_cards(service, task)
    original = get_case(service, run, 'TC2')['case']
    service.legacy.edit(run, 'TC2', original['version'], original['revision'],
                        {**original['content'], 'title': '其他协作者更新后的版本'}, '', '修正标题', 'concurrent-edit')
    next(b for b in app.button if b.label == '提交审核').click().run()
    assert not app.exception
    current = get_case(service, run, 'TC2')['case']
    assert current['version'] == 2
    assert current['status'] == 'pending_review'
    assert any('已有更新' in c.value for c in app.caption)

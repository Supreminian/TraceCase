from pathlib import Path

from streamlit.testing.v1 import AppTest

from sdlc.service import Service

ROOT = Path(__file__).resolve().parents[1]


def app_for(path, monkeypatch):
    from tests.fixture_provider import FixtureProvider
    monkeypatch.setattr('sdlc.providers.GeminiProvider', FixtureProvider)
    monkeypatch.setenv('SDLC_DB_PATH', str(path))
    app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=20).run()
    assert not app.exception
    app.text_input(key='operator').input('project-tester').run()
    return app


def test_first_project_creation_and_empty_workspace(tmp_path, monkeypatch):
    path = tmp_path / 'ui.sqlite3'
    app = app_for(path, monkeypatch)
    assert app.title[0].value == '项目列表'
    assert not any(b.key == 'generate' for b in app.button)
    app.text_input(key='new_project_name').input('报销审批项目')
    app.text_area(key='new_project_description').input('仅虚构数据')
    next(b for b in app.button if b.label == '新建项目').click().run()
    assert not app.exception
    project_id = app.selectbox(key='current_project').value
    assert Service(path).get_project(project_id)['name'] == '报销审批项目'
    assert app.radio(key='page').value == '项目概览'
    assert app.button(key='project_generate')
    app.button(key='project_generate').click().run()
    assert app.radio(key='page').value == '新建运行'


def test_switching_projects_keeps_drafts_and_clears_review_download_context(tmp_path, monkeypatch):
    path = tmp_path / 'ui.sqlite3'
    admin = Service(path)
    a = admin.create_project('A', '', 'tester')
    b = admin.create_project('B', '', 'tester')
    app = app_for(path, monkeypatch)
    app.selectbox(key='current_project').select(a).run()
    app.radio(key='page').set_value('新建运行').run()
    app.radio(key='input_source').set_value('粘贴 Markdown').run()
    app.text_area(key='prd_text').input('# A 项目的专有草稿').run()
    app.session_state['ready_export'] = {'run_id': 'A-only', 'export': {'content': b'private'}}
    app.session_state['_detail_target'] = {'run_id': 'A-only', 'tab': '导出'}
    app.selectbox(key='current_project').select(b).run()
    assert not app.exception
    assert app.session_state.get('ready_export') is None
    assert app.session_state.get('_detail_target') is None
    app.radio(key='page').set_value('新建运行').run()
    app.radio(key='input_source').set_value('粘贴 Markdown').run()
    assert app.text_area(key='prd_text').value == ''
    app.text_area(key='prd_text').input('# B 项目的草稿').run()
    app.selectbox(key='current_project').select(a).run()
    app.radio(key='page').set_value('新建运行').run()
    assert app.text_area(key='prd_text').value == '# A 项目的专有草稿'
    app.button(key='generate').click().run()  # Fixture mismatch is still recorded in A only.
    assert not app.exception
    assert len(Service(path, project_id=a).list_runs()) == 1
    assert not Service(path, project_id=b).list_runs()
    app.selectbox(key='current_project').select(b).run()
    app.radio(key='page').set_value('运行历史').run()
    assert not app.exception
    assert any('没有匹配' in i.value for i in app.info)


def test_project_case_navigation_export_and_archive(tmp_path, monkeypatch):
    from tests.fixture_provider import list_samples
    path = tmp_path / 'ui.sqlite3'
    admin = Service(path)
    pid = admin.create_project('用例项目', '', 'tester')
    service = Service(path, project_id=pid)
    run_id = service.generate(text=list_samples()[0]['text'], operator='tester')
    app = app_for(path, monkeypatch)
    app.selectbox(key='current_project').select(pid).run()
    app.radio(key='page').set_value('项目用例').run()
    assert not app.exception
    case_id = service.get_run(run_id)['cases'][0]['case_id']
    app.button(key=f'open_case_{run_id}_{case_id}').click().run()
    assert not app.exception
    assert app.selectbox(key='selected_run').value == run_id
    assert app.selectbox(key=f'case_selector_{run_id}').value == case_id
    for c in service.get_run(run_id)['cases']:
        service.review(run_id, c['case_id'], c['version'], c['revision'], 'approve', 'tester', '通过', c['case_id'])
    saved = service.export(run_id, 'csv', 'tester', service.get_run(run_id)['revision'], 'export-test')
    app.radio(key='page').set_value('导出记录').run()
    assert not app.exception
    app.button(key=f"project_export_{saved['id']}").click().run()
    assert not app.exception
    assert app.get('download_button')
    app.radio(key='page').set_value('项目列表').run()
    app.button(key='archive_project').click().run()
    assert not app.exception
    assert service.get_project()['status'] == 'archived'
    app.radio(key='page').set_value('运行详情与审核').run()
    assert not app.exception
    submit = next(b for b in app.button if b.label == '提交审核')
    assert submit.disabled
    assert app.button(key='export_approved').disabled
    app.radio(key='page').set_value('项目列表').run()
    app.button(key='archive_project').click().run()
    assert service.get_project()['status'] == 'active'

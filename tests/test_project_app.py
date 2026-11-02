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
    return app


def test_first_project_creation_and_empty_workspace(tmp_path, monkeypatch):
    path = tmp_path / 'ui.sqlite3'
    app = app_for(path, monkeypatch)
    assert app.title[0].value == '项目列表'
    assert not any(widget.key == 'page' for widget in app.radio)
    assert not any(b.key == 'generate' for b in app.button)
    app.text_input(key='new_project_name').input('报销审批项目')
    app.text_area(key='new_project_description').input('仅虚构数据')
    next(b for b in app.button if b.label == '新建项目').click().run()
    assert not app.exception
    project_id = app.selectbox(key='current_project').value
    assert Service(path).get_project(project_id)['name'] == '报销审批项目'
    assert app.radio(key='page').value == '测试任务'
    assert next(b for b in app.button if b.label == '开始测试准备')
    app.radio(key='page').set_value('新建运行').run()
    assert app.radio(key='page').value == '新建运行'


def test_existing_projects_show_cards_and_create_on_demand(tmp_path, monkeypatch):
    path = tmp_path / 'existing-projects.sqlite3'
    admin = Service(path)
    existing = admin.create_project('已有项目', '', 'tester')
    app = app_for(path, monkeypatch)
    assert app.button(key=f'enter_project_{existing}')
    assert not any(field.key == 'new_project_name' for field in app.text_input)
    app.button(key='open_create_project').click().run()
    assert not app.exception
    app.text_input(key='new_project_name').input('第二个项目')
    next(button for button in app.button if button.label == '新建项目').click().run()
    assert not app.exception
    assert app.radio(key='page').value == '测试任务'
    assert admin.get_project(app.selectbox(key='current_project').value)['name'] == '第二个项目'
    assert len(admin.list_projects()) == 2


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
    assert app.get('link_button')
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


def test_project_pagination_appears_only_over_page_limit_and_switches_pages(tmp_path, monkeypatch):
    path = tmp_path / 'pagination.sqlite3'
    admin = Service(path)
    for index in range(25):
        admin.create_project(f'项目 {index + 1}', '', 'tester')
    app = app_for(path, monkeypatch)
    assert not any(widget.key == 'projects_page' for widget in app.selectbox)
    assert not any(widget.label == '项目列表页码' for widget in app.number_input)
    assert len([b for b in app.button if (b.key or '').startswith('enter_project_')]) == 25
    admin.create_project('项目 26', '', 'tester')
    app.run()
    assert not app.exception
    assert app.selectbox(key='projects_page').options == ['第 1 页 / 共 2 页', '第 2 页 / 共 2 页']
    assert app.button(key='projects_previous').disabled
    app.button(key='projects_next').click().run()
    assert not app.exception
    assert app.selectbox(key='projects_page').value == 2
    assert app.button(key='projects_next').disabled
    assert len([b for b in app.button if (b.key or '').startswith('enter_project_')]) == 1
    app.selectbox(key='projects_page').select(1).run()
    assert not app.exception
    assert len([b for b in app.button if (b.key or '').startswith('enter_project_')]) == 25


def test_clearing_project_hides_project_navigation(tmp_path, monkeypatch):
    path = tmp_path / 'clear-project.sqlite3'
    project = Service(path).create_project('可切换项目', '', 'tester')
    app = app_for(path, monkeypatch)
    assert not any(widget.key == 'page' for widget in app.radio)
    app.selectbox(key='current_project').select(project).run()
    assert app.radio(key='page').value == '测试任务'
    app.selectbox(key='current_project').select(None).run()
    assert not app.exception
    assert not any(widget.key == 'page' for widget in app.radio)
    assert app.title[0].value == '项目列表'


def test_pending_generation_shows_activity_prevents_duplicates_and_recovers(tmp_path, monkeypatch):
    from sdlc import jobs
    from tests.fixture_provider import list_samples
    path = tmp_path / 'pending-generation.sqlite3'
    project = Service(path).create_project('生成状态项目', '', 'tester')
    app = app_for(path, monkeypatch)
    app.selectbox(key='current_project').select(project).run()
    app.radio(key='page').set_value('新建运行').run()
    text = list_samples()[0]['text']
    app.text_area(key='prd_text').input(text).run()
    monkeypatch.setattr(jobs, 'submit_initial', lambda service, job: None)
    app.button(key='generate').click().run()
    assert not app.exception
    assert not any(button.key == 'generate' for button in app.button)
    assert not any(field.key == 'prd_text' for field in app.text_area)
    assert any('任务已提交，等待生成' in item.value for item in app.markdown)
    import sqlite3
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM generation_jobs WHERE kind='initial'").fetchone()[0] == 1
        db.execute("UPDATE generation_jobs SET status='running' WHERE kind='initial'")
    app.run()
    assert any('AI 正在处理需求' in item.value for item in app.markdown)
    assert not any(button.key == 'generate' for button in app.button)
    assert not any(field.key == 'prd_text' for field in app.text_area)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE generation_jobs SET status='failed', error='测试生成服务超时' WHERE kind='initial'")
    app.run()
    assert not app.exception
    assert not app.button(key='generate').disabled
    assert any('测试生成服务超时' in item.value for item in app.error)
    assert app.text_area(key='prd_text').value == text
    assert not any('class="tc-generation"' in item.value for item in app.markdown)

    from tests.fixture_provider import FixtureProvider
    monkeypatch.setattr(jobs, 'submit_initial', lambda service, job: jobs.execute_initial(service, job, FixtureProvider()))
    app.button(key='generate').click().run()
    assert not app.exception
    assert app.radio(key='page').value == '运行详情与审核'
    app.radio(key='page').set_value('运行历史').run()
    assert not app.exception
    assert not any(button.label == '生成完成，打开结果' for button in app.button)

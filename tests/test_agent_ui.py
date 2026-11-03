from streamlit.testing.v1 import AppTest
import csv
import io

from sdlc.accounts import Accounts
from sdlc.secure_service import Service
from sdlc.testing_agent.service import TestingService
from tests.test_testing_agent import TEXT, ready


def setup(path):
    accounts = Accounts(path)
    accounts.register('ui-agent', 'Agent tester', 'disposable-password')
    token = accounts.login('ui-agent', 'disposable-password')
    workspace = accounts.spaces(token)[0]['id']
    service = Service(path, token=token, workspace_id=workspace)
    project = service.create_project('任务界面', '')
    return TestingService(path, token, workspace, project)


def show(service):
    app = AppTest.from_string(f'''
from sdlc.secure_service import Service
from sdlc.testing_agent.ui import workspace
from sdlc.ui import apply_style
apply_style()
s = Service({service.db_path!r}, token={service.token!r}, workspace_id={service.workspace_id!r}, project_id={service.project_id!r})
workspace(s)
''', default_timeout=15).run()
    assert not app.exception
    return app


def test_budget_stop_keeps_old_questions_as_history_without_repeating_answer_forms(tmp_path):
    from sdlc.testing_agent.chat import ChatService
    from tests.test_testing_agent import analysis
    service = setup(tmp_path / 'budget-ui.sqlite3')
    task_id = service.create_task(TEXT)
    service.save_analysis(task_id, analysis(ambiguous=True))
    service.submit_answer(task_id, 'Q1', '保留这条尚未确认的原始答复。', expected_revision=1)
    before = service.get_task(task_id)['questions']
    ChatService(service).request_confirmation(task_id, kind='budget_exhausted')
    app = show(service)
    assert any(button.label == '创建新一轮继续' for button in app.button)
    assert any(expander.label == '上一轮未解决的问题与答复' for expander in app.expander)
    assert not any(button.label == '确认本轮答复并继续' for button in app.button)
    assert service.get_task(task_id)['questions'] == before


def test_empty_task_board_creates_durable_work_without_network(tmp_path):
    service = setup(tmp_path / 'ui.sqlite3')
    app = show(service)
    assert any('测试协作对话' in m.value for m in app.markdown)
    app.chat_input(key='agent_new_chat_' + service.project_id).set_value(TEXT).run()
    assert not app.exception
    tasks = service.list_tasks()
    assert len(tasks) == 1
    with service.accounts.db() as db:
        assert db.execute("SELECT status FROM agent_work_items WHERE task_id=?", (tasks[0]['id'],)).fetchone()[0] == 'queued'
    assert any('正在理解需求' in m.value for m in app.markdown)


def test_task_review_and_delivery_remain_separate_from_execution(tmp_path):
    service = setup(tmp_path / 'ui.sqlite3')
    task_id = ready(service)
    run_id = service.finalize_design(task_id)
    app = show(service)
    assert any('测试未执行' in c.value for c in app.caption)
    next(b for b in app.button if b.label == '提交并查看下一条').click().run()
    assert not app.exception
    assert sum(c['status'] == 'approved' for c in service.legacy.get_run(run_id)['cases']) == 1
    next(b for b in app.button if b.label == '提交并查看下一条').click().run()
    assert not app.exception
    next(b for b in app.button if b.label == '建立批准用例基线').click().run()
    assert not app.exception
    assert service.get_task(task_id)['status'] == 'completed'
    assert service.get_task(task_id)['baselines'][0]['snapshot']['execution_status'] == 'not_executed'
    next(b for b in app.button if b.label == '生成批准导出文件').click().run()
    assert not app.exception
    assert len(service.legacy.list_exports()) == 1
    exported = service.legacy.get_export(service.legacy.list_exports()[0]['id'])
    row = next(csv.DictReader(io.StringIO(exported['content'].decode('utf-8-sig'))))
    assert row['role'] == '申请人' and row['test_data'] == '虚构发票'
    assert row['task_id'] == task_id and row['execution_status'] == 'not_executed'
    app.selectbox(key='agent_fmt_' + task_id).set_value('md').run()
    next(b for b in app.button if b.label == '生成批准导出文件').click().run()
    assert not app.exception
    assert {e['format'] for e in service.legacy.list_exports()} == {'csv', 'md'}
    markdown = next(e for e in service.legacy.list_exports() if e['format'] == 'md')
    assert '测试数据准备' in service.legacy.get_export(markdown['id'])['content'].decode()


def test_return_moves_to_next_case_then_keeps_revision_work_visible(tmp_path):
    service = setup(tmp_path / 'ui.sqlite3')
    task_id = ready(service)
    run_id = service.finalize_design(task_id)
    app = show(service)
    app.selectbox(key='agent_review_action_' + task_id + 'TC1').set_value('退回修改').run()
    next(t for t in app.text_area if t.label == '退回意见（必填）').input('请把预期改成可观察的状态变化。')
    next(b for b in app.button if b.label == '提交并查看下一条').click().run()
    assert not app.exception
    assert app.selectbox(key='agent_case_' + task_id).value == 'TC2'
    assert app.session_state['agent_tabs_' + task_id] == '用例评审'
    next(b for b in app.button if b.label == '提交并查看下一条').click().run()
    assert not app.exception
    assert app.selectbox(key='agent_case_' + task_id).value == 'TC1'
    assert app.session_state['agent_tabs_' + task_id] == '用例评审'
    assert any('存在退回用例' in w.value for w in app.warning)
    assert {c['status'] for c in service.legacy.get_run(run_id)['cases']} == {'approved', 'returned'}


def test_public_stream_updates_existing_chat_bubble_and_waits_for_failure_choice(tmp_path):
    from sdlc.testing_agent.chat import ChatService
    service = setup(tmp_path / 'ui.sqlite3')
    task_id = service.create_task(TEXT)
    chat = ChatService(service)
    chat.post_user(task_id, '请先检查需求中的边界。')
    chat.handle_event(task_id, {'type': 'assistant_start', 'message_id': 'public-message'})
    chat.handle_event(task_id, {'type': 'assistant_delta', 'message_id': 'public-message', 'text': '我会先核对'})
    app = show(service)
    assert any('我会先核对 ▍' in m.value for m in app.markdown)
    assert not any(s.label == '发送内容作为' for s in app.selectbox)
    assert not any(e.label == '对话活动记录' for e in app.expander)
    chat.handle_event(task_id, {'type': 'assistant_delta', 'message_id': 'public-message', 'text': '需求边界。'})
    chat.handle_event(task_id, {'type': 'assistant_message', 'message_id': 'public-message', 'content': '我会先核对需求边界。'})
    pending = chat.request_confirmation(task_id, reason='timeout')
    app.run()
    assert not app.exception
    assert any('我会先核对需求边界。' == m.value for m in app.markdown)
    assert next(b for b in app.button if b.label == '重试未完成部分')
    next(b for b in app.button if b.label == '暂不继续').click().run()
    assert not app.exception and chat.get_pending(task_id) is None
    assert next(c for c in chat.list_confirmations(task_id) if c['id'] == pending['id'])['choice'] == 'stop'
    with service.connection() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_work_items'").fetchone() or db.execute('SELECT COUNT(*) FROM agent_work_items').fetchone()[0] == 0

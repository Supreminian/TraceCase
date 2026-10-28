"""Exercise the actual Streamlit forms against an isolated SQLite database."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.fixture_provider import list_samples
from tests.helpers import scoped_service as Service


ROOT = Path(__file__).resolve().parents[1]


def button(app, label):
    return next(item for item in app.button if item.label == label)


def assert_clean(app):
    assert not app.exception, [item.message for item in app.exception]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    path = tmp_path / "app.sqlite3"
    monkeypatch.setenv("SDLC_DB_PATH", str(path))
    service = Service(path)
    from tests.fixture_provider import FixtureProvider
    monkeypatch.setattr("sdlc.providers.GeminiProvider", FixtureProvider)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert_clean(app)
    app.selectbox(key="current_project").select(service.project_id).run()
    app.radio(key="page").set_value("新建运行").run()
    app.text_input(key="operator").input("demo-reviewer").run()
    return app, service


def generate(app, service, sample_id=None):
    sample = next(x for x in list_samples() if x['id'] == (sample_id or 'normal'))
    app.text_area(key="prd_text").input(sample['text']).run()
    app.button(key="generate").click().run()
    assert_clean(app)
    runs = service.list_runs()
    assert len(runs) == 1
    return service.get_run(runs[0]["id"])


def test_ui_review_export_then_edit_revokes_current_approval(workspace):
    app, service = workspace
    run = generate(app, service)
    assert run["status"] == "valid"
    assert service.gate(run["id"])
    assert app.button(key="export_approved").disabled
    first_case_id = run["cases"][0]["case_id"]
    for index, case in enumerate(run["cases"]):
        app.selectbox(key=f"case_selector_{run['id']}").select(case["case_id"]).run()
        current = next(c for c in service.get_run(run["id"])["cases"]
                       if c["case_id"] == case["case_id"])
        token = f"{run['id']}_{case['case_id']}_{current['version']}_{current['revision']}"
        button(app, "提交审核").click().run()
        assert_clean(app)
        assert not app.error
        expected_id = run["cases"][index + 1]["case_id"] if index + 1 < len(run["cases"]) else case["case_id"]
        assert app.selectbox(key=f"case_selector_{run['id']}").value == expected_id
    assert any("本轮用例已全部处理" in message.value for message in app.success)
    assert service.gate(run["id"]) == []
    assert app.session_state[f"detail_tabs_{run['id']}"] == "导出"
    assert not app.button(key="export_approved").disabled
    app.button(key="export_approved").click().run()
    assert_clean(app)
    exported_run = service.get_run(run["id"])
    assert len(exported_run["exports"]) == 1
    export_id = exported_run["exports"][0]["id"]
    original_snapshot = service.get_export(export_id)["content"]

    app.selectbox(key=f"case_selector_{run['id']}").select(first_case_id).run()
    current = next(c for c in service.get_run(run["id"])["cases"] if c["case_id"] == first_case_id)
    token = f"{run['id']}_{first_case_id}_{current['version']}_{current['revision']}"
    app.text_input(key=f"edit_title_{token}").input(current["content"]["title"] + "（措辞修订）")
    app.text_area(key=f"edit_reason_{token}").input("明确测试标题，不更改业务规则")
    button(app, "保存修订（生成新版本）").click().run()
    assert_clean(app)
    assert not app.error
    revised_run = service.get_run(run["id"])
    revised = next(c for c in revised_run["cases"] if c["case_id"] == first_case_id)
    assert revised["version"] == current["version"] + 1
    assert revised["status"] == "pending_review"
    assert service.gate(run["id"])
    assert app.button(key="export_approved").disabled
    assert service.get_export(export_id)["content"] == original_snapshot
    assert len([v for v in revised_run["case_versions"] if v["case_id"] == first_case_id]) == 2

    app.radio(key="page").set_value("运行历史").run()
    assert_clean(app)
    button(app, "查看记录与继续审核").click().run()
    assert_clean(app)
    assert app.selectbox(key="selected_run").value == run["id"]


def test_ui_ambiguous_case_cannot_be_approved_or_exported(workspace):
    app, service = workspace
    ambiguous = next(sample for sample in list_samples() if "ambiguous" in sample["id"])
    run = generate(app, service, ambiguous["id"])
    assert run["status"] == "valid"
    assert run["clarifications"]
    blocked = next(case for case in run["cases"] if case["content"]["readiness"] == "needs_clarification")
    app.selectbox(key=f"case_selector_{run['id']}").select(blocked["case_id"]).run()
    button(app, "提交审核").click().run()
    assert_clean(app)
    assert app.error
    assert app.selectbox(key=f"case_selector_{run['id']}").value == blocked["case_id"]
    refreshed = service.get_run(run["id"])
    assert all(case["status"] != "approved" for case in refreshed["cases"])
    assert app.button(key="export_approved").disabled
    assert refreshed["exports"] == []


def test_ui_requires_operator_and_retains_failed_generation(workspace):
    app, service = workspace
    app.text_input(key="operator").input("").run()
    assert app.button(key="generate").disabled
    app.text_input(key="operator").input("demo-reviewer").run()
    app.radio(key="input_source").set_value("粘贴 Markdown").run()
    app.text_area(key="prd_text").input("# 自定义虚构输入\n\n原型尚未接入实际模型。")
    app.button(key="generate").click().run()
    assert_clean(app)
    runs = service.list_runs()
    assert len(runs) == 1
    run = service.get_run(runs[0]["id"])
    assert run["status"] == "failed"
    assert app.button(key="export_approved").disabled
    assert run["raw_output"] is None


@pytest.mark.parametrize("action", ["通过", "退回"])
@pytest.mark.parametrize("wrap", [False, True])
def test_review_moves_to_next_pending_and_wraps(workspace, action, wrap):
    app, service = workspace
    run = generate(app, service)
    cases = run["cases"]
    # A previously handled case must not be selected by automatic navigation.
    skipped = cases[1]
    service.review(run["id"], skipped["case_id"], skipped["version"], skipped["revision"],
                   "approve", "demo-reviewer", "已核对", "skip-case")
    current = cases[-1] if wrap else cases[0]
    key = f"case_selector_{run['id']}"
    app.selectbox(key=key).select(current["case_id"]).run()
    token = f"{run['id']}_{current['case_id']}_{current['version']}_{current['revision']}"
    app.radio(key=f"action_{token}").set_value(action).run()
    if action == "退回":
        app.text_area(key=f"review_reason_{token}").input("核对完成或退回修订")
    button(app, "提交审核").click().run()
    assert_clean(app)
    assert not app.error
    assert app.selectbox(key=key).value == (cases[0]["case_id"] if wrap else cases[2]["case_id"])
    reviewed = next(c for c in service.get_run(run["id"])["cases"] if c["case_id"] == current["case_id"])
    assert reviewed["status"] == ("approved" if action == "通过" else "returned")


@pytest.mark.parametrize("all_returned", [False, True])
def test_completed_returned_review_shows_next_actions_and_manual_revision(workspace, all_returned):
    app, service = workspace
    run = generate(app, service)
    for index, c in enumerate(run["cases"]):
        service.review(run["id"], c["case_id"], c["version"], c["revision"],
                       "return" if all_returned or index == 0 else "approve",
                       "tester", "明确步骤角色", f"review-{index}")
    app.run()
    assert_clean(app)
    assert app.session_state[f"detail_tabs_{run['id']}"] == "审核结果"
    assert button(app, "人工修订此用例")
    assert app.button(key="export_approved").disabled
    button(app, "人工修订此用例").click().run()
    assert_clean(app)
    assert app.session_state[f"detail_tabs_{run['id']}"] == "用例审核"
    current = service.get_run(run["id"])["cases"][0]
    token = f"{run['id']}_{current['case_id']}_{current['version']}_{current['revision']}"
    app.text_input(key=f"edit_title_{token}").input(current["content"]["title"] + "（人工修订）")
    app.text_area(key=f"edit_reason_{token}").input("根据原文明确步骤角色")
    button(app, "保存修订（生成新版本）").click().run()
    assert_clean(app)
    revised = service.get_run(run["id"])["cases"][0]
    assert revised["version"] == 2 and revised["status"] == "pending_review"
    assert app.session_state[f"detail_tabs_{run['id']}"] == "用例审核"


def test_model_revision_ui_starts_linked_review_without_inheriting_approval(workspace, monkeypatch):
    from tests.test_review_iteration import RevisionProvider
    app, service = workspace
    run = generate(app, service)
    monkeypatch.setattr("sdlc.providers.GeminiProvider", RevisionProvider)
    for index, c in enumerate(run["cases"]):
        service.review(run["id"], c["case_id"], c["version"], c["revision"],
                       "return" if index == 0 else "approve", "tester", "明确步骤角色", f"review-{index}")
    app.run()
    app.checkbox(key=f"revision_confirm_{run['id']}").check().run()
    app.button(key="model_revision").click().run()
    assert_clean(app)
    assert not app.error
    child_id = app.selectbox(key="selected_run").value
    assert child_id != run["id"]
    child = service.get_run(child_id)
    assert child["parent_run_id"] == run["id"] and child["status"] == "valid"
    assert all(c["status"] == "pending_review" for c in child["cases"])


def test_quick_approval_records_selected_note_without_text(workspace):
    app, service = workspace
    run = generate(app, service)
    c = run["cases"][0]
    token = f"{run['id']}_{c['case_id']}_{c['version']}_{c['revision']}"
    app.selectbox(key=f"approval_note_{token}").select("与需求一致").run()
    assert not any(x.key == f"review_reason_{token}" for x in app.text_area)
    button(app, "提交审核").click().run()
    assert_clean(app)
    event = next(e for e in service.get_run(run["id"])["events"] if e["action"] == "approve")
    assert event["reason"] == "与需求一致"


@pytest.mark.parametrize("action", ["通过", "退回"])
def test_custom_approval_and_return_still_require_text(workspace, action):
    app, service = workspace
    run = generate(app, service)
    c = run["cases"][0]
    token = f"{run['id']}_{c['case_id']}_{c['version']}_{c['revision']}"
    if action == "通过":
        app.selectbox(key=f"approval_note_{token}").select("其他（填写说明）").run()
    else:
        app.radio(key=f"action_{token}").set_value(action).run()
    button(app, "提交审核").click().run()
    assert_clean(app)
    assert app.error
    assert service.get_run(run["id"])["cases"][0]["status"] == "pending_review"
    app.text_area(key=f"review_reason_{token}").input("具体审核说明")
    button(app, "提交审核").click().run()
    assert_clean(app)
    event = next(e for e in service.get_run(run["id"])["events"] if e["action"] == ("approve" if action == "通过" else "return"))
    assert event["reason"] == "具体审核说明"

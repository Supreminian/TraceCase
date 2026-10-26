"""Exercise the actual Streamlit forms against an isolated SQLite database."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sdlc.providers import list_samples
from sdlc.service import Service


ROOT = Path(__file__).resolve().parents[1]


def button(app, label):
    return next(item for item in app.button if item.label == label)


def assert_clean(app):
    assert not app.exception, [item.message for item in app.exception]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    path = tmp_path / "app.sqlite3"
    monkeypatch.setenv("SDLC_DB_PATH", str(path))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert_clean(app)
    app.text_input(key="operator").input("demo-reviewer").run()
    return app, Service(path)


def generate(app, service, sample_id=None):
    if sample_id:
        app.selectbox(key="sample").select(sample_id).run()
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
    for case in run["cases"]:
        app.selectbox(key=f"case_selector_{run['id']}").select(case["case_id"]).run()
        current = next(c for c in service.get_run(run["id"])["cases"]
                       if c["case_id"] == case["case_id"])
        token = f"{run['id']}_{case['case_id']}_{current['version']}_{current['revision']}"
        app.text_area(key=f"review_reason_{token}").input("已逐条核对原文、边界、步骤与预期")
        button(app, "提交审核").click().run()
        assert_clean(app)
        assert not app.error
    assert service.gate(run["id"]) == []
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
    token = f"{run['id']}_{blocked['case_id']}_{blocked['version']}_{blocked['revision']}"
    app.text_area(key=f"review_reason_{token}").input("尝试通过待澄清用例以检查门禁")
    button(app, "提交审核").click().run()
    assert_clean(app)
    assert app.error
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

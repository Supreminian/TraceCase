import copy
import csv
import io
import sqlite3
import threading
from uuid import uuid4

import pytest

from tests.fixture_provider import FixtureProvider, list_samples
from sdlc.service import ServiceError
from tests.helpers import scoped_service as Service


def rid():
    return uuid4().hex


def sample(name="normal"):
    return next(s["text"] for s in list_samples() if s["id"] == name)


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path / "test.sqlite3")


def generate(service, name="normal"):
    return service.generate(text=sample(name), operator="reviewer", request_id=rid())


def approve(service, run_id, case):
    return service.review(run_id, case["case_id"], case["version"], case["revision"],
                          "approve", "reviewer", "逐项核对原文", rid())


def approve_all(service, run_id):
    for case in service.get_run(run_id)["cases"]:
        approve(service, run_id, case)


def export(service, run_id, fmt="csv", request_id=None):
    return service.export(run_id, fmt, "reviewer", service.get_run(run_id)["revision"], request_id or rid())


def test_generation_records_original_and_restart(service):
    run_id = generate(service)
    run = Service(service.db_path).get_run(run_id)
    assert run["status"] == "valid"
    assert run["raw_output"]
    assert run["input"]["normalized_text"] == sample()
    assert run["config"]["provider"] == "fixture"
    assert run["prompt"]["template_hash"]
    assert run["usage"] is None
    assert all(c["version"] == 1 and c["status"] == "pending_review" for c in run["cases"])
    assert service.gate(run_id)


def test_invalid_input_never_calls_provider(service):
    class NeverCalled:
        def generate(self, request):
            pytest.fail("provider must not be called")
    with pytest.raises(ValueError):
        service.generate(text=" " * 10, operator="reviewer", provider=NeverCalled())
    assert service.list_runs() == []


@pytest.mark.parametrize("name,status", [("invalid-json", "invalid"), ("invalid-schema", "invalid")])
def test_invalid_generation_preserves_response_blocks_review(service, name, status):
    run_id = generate(service, name)
    run = service.get_run(run_id)
    assert run["status"] == status and run["raw_output"]
    assert run["validation"]["errors"]
    with pytest.raises(ServiceError):
        export(service, run_id)


def test_unmatched_fixture_fails_honestly(service):
    run_id = service.generate(text="# unknown\n\nnot a sample", operator="reviewer")
    run = service.get_run(run_id)
    assert run["status"] == "failed"
    assert "未匹配" in run["error"]
    assert run["raw_output"] is None


def test_failed_provider_does_not_persist_secret(service):
    class Broken:
        config = {"provider": "test", "model": "mock", "api_key": "SECRET"}
        def generate(self, request):
            raise TimeoutError("Authorization: SECRET")
    run_id = service.generate(text=sample(), operator="reviewer", provider=Broken())
    run = service.get_run(run_id)
    assert "SECRET" not in str(run)
    assert run["status"] == "failed" and run["latency_ms"] >= 0


def test_all_current_versions_required_and_snapshot_is_immutable(service):
    run_id = generate(service)
    with pytest.raises(ServiceError):
        export(service, run_id)
    approve_all(service, run_id)
    assert not service.gate(run_id)
    saved = export(service, run_id)
    rows = list(csv.DictReader(io.StringIO(saved["content"].decode("utf-8-sig"))))
    assert len(rows) == len(service.get_run(run_id)["cases"])
    assert rows[0]["approved_by"] == "reviewer"
    assert all(r["approval_event_id"] for r in rows)
    case = service.get_run(run_id)["cases"][0]
    content = copy.deepcopy(case["content"])
    content["title"] += "（修订）"
    service.edit(run_id, case["case_id"], case["version"], case["revision"], content,
                 "editor", "标题更清楚", rid())
    assert service.gate(run_id)
    with pytest.raises(ServiceError):
        export(service, run_id)
    assert Service(service.db_path).get_export(saved["id"])["content"] == saved["content"]
    versions = service.get_run(run_id)["case_versions"]
    assert next(c for c in versions if c["case_id"] == case["case_id"] and c["version"] == 1)["status"] == "approved"


def test_return_requires_new_revision_and_old_page_cannot_approve(service):
    run_id = generate(service)
    old = service.get_run(run_id)["cases"][0]
    service.review(run_id, old["case_id"], 1, old["revision"], "return", "reviewer", "步骤需复核", rid())
    returned = service.get_run(run_id)["cases"][0]
    with pytest.raises(ServiceError):
        approve(service, run_id, returned)
    service.edit(run_id, returned["case_id"], 1, returned["revision"], returned["content"],
                 "editor", "核对原文，保留此版本内容提交复审", rid())
    with pytest.raises(ServiceError, match="变化"):
        approve(service, run_id, old)
    current = service.get_run(run_id)["cases"][0]
    assert current["version"] == 2
    approve(service, run_id, current)
    events = service.get_run(run_id)["events"]
    assert any(e["action"] == "return" and e["version"] == 1 for e in events)
    assert any(e["action"] == "approve" and e["version"] == 2 for e in events)


def test_request_idempotency_and_payload_mismatch(service):
    run_id = generate(service)
    c = service.get_run(run_id)["cases"][0]
    request_id = rid()
    args = [run_id, c["case_id"], 1, c["revision"], "approve", "reviewer", "符合原文", request_id]
    event = service.review(*args)
    assert service.review(*args) == event
    assert len([e for e in service.get_run(run_id)["events"] if e["action"] == "approve"]) == 1
    args[-2] = "更换理由"
    with pytest.raises(ServiceError, match="不同操作"):
        service.review(*args)


def test_edit_transaction_rolls_back_on_event_write_failure(service, monkeypatch):
    run_id = generate(service)
    case = service.get_run(run_id)["cases"][0]
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated disk failure")
    monkeypatch.setattr(service, "_event", fail)
    with pytest.raises(sqlite3.OperationalError):
        service.edit(run_id, case["case_id"], 1, case["revision"], case["content"], "editor", "改动", rid())
    assert service.get_run(run_id)["cases"][0]["version"] == 1
    assert len(service.get_run(run_id)["case_versions"]) == len(service.get_run(run_id)["cases"])


@pytest.mark.parametrize("name", ["ambiguous", "missing-role", "conflicting", "original-incomplete"])
def test_uncertainty_blocks_export_and_affected_case_approval(service, name):
    run_id = generate(service, name)
    run = service.get_run(run_id)
    assert run["status"] == "valid"
    blocked = next(c for c in run["cases"] if c["content"]["readiness"] == "needs_clarification")
    with pytest.raises(ServiceError, match="阻断"):
        approve(service, run_id, blocked)
    assert service.gate(run_id)


def question(run):
    rule = run["output"]["business_rules"][0]
    return {"id": "CL-H001", "question": "此规则是否已清楚？", "paragraph_ids": rule["paragraph_ids"],
            "requirement_ids": [rule["id"]], "affected_case_ids": [], "scope": "run", "blocking": True}


def test_new_clarification_revisions_and_evidenced_resolution(service):
    run_id = generate(service)
    approve_all(service, run_id)
    run = service.get_run(run_id)
    q = question(run)
    service.add_clarification(run_id, q, run["revision"], "reviewer", "复核问题", rid())
    run = service.get_run(run_id)
    assert all(c["version"] == 2 and c["status"] == "pending_review" for c in run["cases"])
    assert service.gate(run_id)
    with pytest.raises(ServiceError, match="依据"):
        service.resolve_clarification(run_id, q["id"], run["revision"], "false_positive", [],
                                      "reviewer", "误报", rid())
    service.resolve_clarification(run_id, q["id"], run["revision"], "false_positive", q["paragraph_ids"],
                                  "reviewer", "原文明确，该问题为误报", rid())
    run = service.get_run(run_id)
    assert all(c["version"] == 3 and c["content"]["readiness"] == "ready" for c in run["cases"])
    assert run["clarifications"][0]["status"] == "false_positive"
    assert service.gate(run_id)  # Still requires fresh decisions.
    approve_all(service, run_id)
    assert not service.gate(run_id)


def test_correcting_references_preserves_review_and_exports_coverage_notice(service):
    from sdlc.domain import coverage_report
    run_id = generate(service)
    run = service.get_run(run_id)
    rule, other = run['output']['business_rules'][:2]
    referencing = [c for c in run['cases'] if rule['id'] in c['content']['requirement_ids']]
    for c in referencing:
        changed = copy.deepcopy(c['content'])
        changed['requirement_ids'] = [other['id']]
        changed['paragraph_ids'] = list(dict.fromkeys(changed['paragraph_ids'] + other['paragraph_ids']))
        service.edit(run_id, c['case_id'], c['version'], c['revision'], changed, 'editor', '纠正引用并记录覆盖缺口', rid())
    updated = service.get_run(run_id)
    coverage = coverage_report({**updated['output'], 'test_cases': [c['content'] for c in updated['cases']]})
    assert rule['id'] in {r['id'] for r in coverage['uncovered_rules']}
    approve_all(service, run_id)
    csv_export = export(service, run_id, 'csv')
    md_export = export(service, run_id, 'md')
    assert 'coverage_note' in csv_export['content'].decode('utf-8-sig')
    assert '存在未关联用例的需求' in csv_export['content'].decode('utf-8-sig')
    assert '尚未关联用例的需求' in md_export['content'].decode()



def test_child_run_does_not_inherit_approval(service):
    parent = generate(service)
    approve_all(service, parent)
    child = service.generate(text=sample(), operator="reviewer", parent_run_id=parent)
    run = service.get_run(child)
    assert run["parent_run_id"] == parent
    assert all(c["version"] == 1 and c["status"] == "pending_review" for c in run["cases"])


def test_export_formula_html_and_markdown_escaping(service):
    run_id = generate(service)
    c = service.get_run(run_id)["cases"][0]
    content = copy.deepcopy(c["content"])
    content["title"] = '=HYPERLINK("https://example.invalid","unsafe"),中文\n|<script>alert(1)</script>![x](https://example.invalid/x.png)'
    service.edit(run_id, c["case_id"], 1, c["revision"], content, "editor", "转义测试", rid())
    approve_all(service, run_id)
    csv_file = export(service, run_id)
    rows = list(csv.DictReader(io.StringIO(csv_file["content"].decode("utf-8-sig"))))
    assert rows[0]["title"] == "'" + content["title"]
    markdown = export(service, run_id, "md")["content"].decode()
    assert "<script>" not in markdown and "![x](" not in markdown
    assert "&lt;script&gt;" in markdown and "\\|" in markdown


def test_history_sqlite_guard(service):
    run_id = generate(service)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with service.connection(write=True) as db:
            db.execute("DELETE FROM case_versions WHERE run_id=?", (run_id,))


def test_export_failure_rolls_back_snapshot_and_event(service, monkeypatch):
    run_id = generate(service)
    approve_all(service, run_id)
    before = service.get_run(run_id)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated event write failure")
    monkeypatch.setattr(service, "_event", fail)
    with pytest.raises(sqlite3.OperationalError):
        export(service, run_id)
    after = service.get_run(run_id)
    assert after["exports"] == []
    assert after["revision"] == before["revision"]
    assert not service.gate(run_id)


def test_export_duplicate_request_is_same_snapshot(service):
    run_id = generate(service)
    approve_all(service, run_id)
    revision, request_id = service.get_run(run_id)["revision"], rid()
    first = service.export(run_id, "md", "reviewer", revision, request_id)
    second = service.export(run_id, "md", "reviewer", revision, request_id)
    assert first == second
    assert len(service.get_run(run_id)["exports"]) == 1


def test_nonblocking_resolution_requires_fresh_review(service):
    run_id = generate(service)
    run = service.get_run(run_id)
    q = question(run)
    service.add_clarification(run_id, q, run["revision"], "reviewer", "复核文字", rid())
    run = service.get_run(run_id)
    service.resolve_clarification(run_id, q["id"], run["revision"], "nonblocking", q["paragraph_ids"],
                                  "reviewer", "仅措辞建议，已有完整执行条件与预期", rid())
    assert all(c["status"] == "pending_review" for c in service.get_run(run_id)["cases"])
    approve_all(service, run_id)
    assert not service.gate(run_id)


def test_interrupted_call_is_not_resurrected(service):
    entered, release = threading.Event(), threading.Event()
    class Slow:
        config = {"provider": "test", "model": "slow"}
        def generate(self, request):
            entered.set()
            assert release.wait(5)
            return FixtureProvider().generate(request)
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(service.generate(text=sample(), operator="reviewer", provider=Slow())))
    thread.start()
    assert entered.wait(5)
    run_id = service.list_runs()[0]["id"]
    service.mark_interrupted(run_id, "reviewer", "确认中断")
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome == [run_id]
    assert service.get_run(run_id)["status"] == "failed"

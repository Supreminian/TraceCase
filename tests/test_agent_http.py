from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from sdlc.accounts import Accounts, COOKIE
from sdlc.agent import AssistantMessage, Session
from sdlc.agent.adapters.session_store import JsonlSessionStore
from sdlc.agent.http import agent_routes, MAX_BODY
from sdlc.secure_service import Service
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent import worker

PASSWORD = "disposable-http-password"
PRD = "# 报销审批\n\n申请人提交报销单；审批角色尚未定义。"


@pytest.fixture
def context(tmp_path, monkeypatch):
    path = tmp_path / "agent-http.sqlite3"
    monkeypatch.setenv("SDLC_AGENT_SESSION_DIR", str(tmp_path / "sessions"))
    accounts = Accounts(path)
    accounts.register("alice-http", "Alice", PASSWORD)
    accounts.register("bob-http", "Bob", PASSWORD)
    alice, bob = accounts.login("alice-http", PASSWORD), accounts.login("bob-http", PASSWORD)
    wa, wb = accounts.spaces(alice)[0]["id"], accounts.spaces(bob)[0]["id"]
    project = Service(path, token=alice, workspace_id=wa).create_project("HTTP project", "")
    client = TestClient(Starlette(routes=agent_routes(path)))
    client.cookies.set(COOKIE, alice)
    return path, accounts, alice, bob, wa, wb, project, client


def url(path, workspace, project):
    return path + "?" + urlencode({"workspace": workspace, "project": project})


def headers(client, workspace, project):
    response = client.get(url("/api/agent/context", workspace, project))
    assert response.status_code == 200, response.text
    return {"Origin": "http://testserver", "X-TraceCase-CSRF": response.json()["csrf"]}


def create(context):
    _, _, _, _, workspace, _, project, client = context
    response = client.post(url("/api/agent/tasks", workspace, project),
                           json={"text": PRD, "goal": "检查角色权限"},
                           headers=headers(client, workspace, project))
    assert response.status_code == 202, response.text
    return response.json()["task_id"], response.json()["work_id"]


def test_context_requires_cookie_and_explicit_scope_not_bearer(context):
    _, accounts, alice, _, workspace, _, project, client = context
    response = client.get(url("/api/agent/context", workspace, project))
    assert response.json()["user"]["id"] == accounts.me(alice)["id"]
    assert "generate" in response.json()["permissions"]
    assert "no-store" in response.headers["cache-control"]
    assert "model" not in response.text
    assert client.get("/api/agent/context").status_code == 400
    assert client.get(url("/api/agent/context", workspace, project) + "&project=duplicate").status_code == 400
    client.cookies.clear()
    assert client.get(url("/api/agent/context", workspace, project),
                      headers={"Authorization": "Bearer " + alice}).status_code == 401


def test_create_is_queued_not_model_execution_and_repeat_resume_keeps_one_job(context, monkeypatch):
    def no_model(*args, **kwargs):
        pytest.fail("HTTP request must not execute the model")

    monkeypatch.setattr("sdlc.agent.composition.build_runtime", no_model)
    task_id, work_id = create(context)
    _, _, _, _, workspace, _, project, client = context
    proof = headers(client, workspace, project)
    response = client.post(url(f"/api/agent/tasks/{task_id}/resume", workspace, project), json={}, headers=proof)
    assert response.status_code == 202 and response.json()["work_id"] == work_id
    detail = client.get(url(f"/api/agent/tasks/{task_id}", workspace, project)).json()
    assert detail["task"]["goal"] == "检查角色权限"
    assert detail["summary"]["execution_status"] == "not_executed"
    assert len(detail["work"]) == 1 and detail["work"][0]["status"] == "queued"
    assert len(client.get(url("/api/agent/tasks", workspace, project)).json()["tasks"]) == 1
    blocked = client.post(url(f"/api/agent/tasks/{task_id}/message", workspace, project),
                          json={"content": "Another task"}, headers=proof)
    assert blocked.status_code == 400


def test_mutations_need_session_bound_proof_and_same_origin(context):
    _, accounts, alice, _, workspace, _, project, client = context
    endpoint = url("/api/agent/tasks", workspace, project)
    proof = headers(client, workspace, project)
    assert client.post(endpoint, json={"text": PRD}, headers={"Origin": "http://testserver"}).status_code == 403
    assert client.post(endpoint, json={"text": PRD}, headers={**proof, "Origin": "https://attacker.invalid"}).status_code == 403
    assert client.post(endpoint, json={"text": PRD}, headers={**proof, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(endpoint, json={"text": PRD}, headers={"X-TraceCase-CSRF": proof["X-TraceCase-CSRF"]}).status_code == 403
    assert client.post(endpoint, json={"text": PRD}, headers={**proof, "Origin": "null"}).status_code == 403
    allowed = client.post(endpoint, json={"text": PRD},
                          headers={**proof, "Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert allowed.status_code == 202
    # Rotation invalidates a proof even when the new login belongs to the same user.
    rotated = accounts.login("alice-http", PASSWORD, previous_token=alice)
    client.cookies.set(COOKIE, rotated)
    assert client.post(endpoint, json={"text": PRD}, headers=proof).status_code == 403
    assert headers(client, workspace, project)["X-TraceCase-CSRF"] != proof["X-TraceCase-CSRF"]


def test_json_is_strict_and_bounded(context):
    _, _, _, _, workspace, _, project, client = context
    endpoint = url("/api/agent/tasks", workspace, project)
    proof = {**headers(client, workspace, project), "Content-Type": "application/json"}
    for body in ('{"text":"a","text":"b"}', '{"text":"a","goal":NaN}',
                 '{"text":"a","created_by":"spoof"}', '[]', '{"text":1}'):
        assert client.post(endpoint, content=body, headers=proof).status_code == 400
    response = client.post(endpoint, content="x" * (MAX_BODY + 1), headers=proof)
    assert response.status_code == 413
    assert client.post(endpoint, content="text", headers=headers(client, workspace, project)).status_code == 415


def test_tasks_are_project_and_workspace_isolated(context):
    task_id, _ = create(context)
    path, accounts, _, bob, wa, wb, project, client = context
    other = Service(path, token=bob, workspace_id=wb).create_project("Other project", "")
    client.cookies.set(COOKIE, bob)
    assert client.get(url(f"/api/agent/tasks/{task_id}", wa, project)).status_code == 403
    assert client.get(url(f"/api/agent/tasks/{task_id}", wb, other)).status_code != 200
    assert client.get(url("/api/agent/tasks", wb, other)).json()["tasks"] == []
    assert client.get(url("/api/agent/tasks", wb, project)).status_code != 200
    assert accounts.me(bob)["username"] == "bob-http"


def test_shared_resources_http_and_fixed_task_binding(context):
    _, _, _, _, workspace, _, project, client = context
    endpoint = url('/api/agent/resources', workspace, project)
    proof = headers(client, workspace, project)
    response = client.post(endpoint, json={'kind': 'steps', 'title': '公共提交步骤', 'body': '使用虚构数据填写表单并提交。'}, headers=proof)
    assert response.status_code == 201, response.text
    resource = response.json()['resource']
    assert client.get(endpoint).json()['resources'][0]['revision'] == 1
    update = url('/api/agent/resources/' + resource['id'] + '/update', workspace, project)
    assert client.post(update, json={'expected_revision': 1, 'title': '新提交步骤', 'body': '填写并检查后提交。'}, headers=proof).status_code == 200
    assert client.post(update, json={'expected_revision': 1, 'title': '旧请求', 'body': '旧版本不能覆盖。'}, headers=proof).status_code == 409
    created = client.post(url('/api/agent/tasks', workspace, project), json={'text': PRD, 'resource_ids': [resource['id']]}, headers=proof)
    assert created.status_code == 202, created.text
    task_id = created.json()['task_id']
    archive = url('/api/agent/resources/' + resource['id'] + '/archive', workspace, project)
    assert client.post(archive, json={'expected_revision': 2}, headers=proof).status_code == 200
    assert not client.get(endpoint).json()['resources']
    detail = client.get(url('/api/agent/tasks/' + task_id, workspace, project)).json()
    fixed = next(a['content'] for a in detail['task']['artifacts'] if a['kind'] == 'project_resources')
    assert fixed['resources'][0]['revision'] == 2 and fixed['resources'][0]['business_fact_confirmed'] is False


def seed_question(service, task_id):
    service.save_analysis(task_id, {
        "features": [{"id": "F1", "description": "提交报销单", "paragraph_ids": ["P002"]}],
        "roles": [],
        "business_rules": [{"id": "R1", "statement": "审批角色尚未定义", "status": "incomplete", "paragraph_ids": ["P002"]}],
        "clarifications": [{"id": "Q1", "question": "由谁审批？", "paragraph_ids": ["P002"],
            "requirement_ids": ["R1"], "affected_case_ids": [], "scope": "run", "blocking": True}],
        "risks": [],
    })


def test_team_answer_and_confirmation_permissions_and_revision(context):
    path, accounts, alice, bob, _, _, _, client = context
    team = accounts.create_team(alice, "Collaborative HTTP")
    invite = accounts.create_invite(alice, team)
    request = accounts.request_join(bob, invite, confirm=True)
    accounts.decide_join(alice, team, request["request_id"], True, "observer")
    project = Service(path, token=alice, workspace_id=team).create_project("Team project", "")
    service = TestingService(path, alice, team, project)
    task_id = service.create_task(PRD)
    seed_question(service, task_id)
    client.cookies.set(COOKIE, bob)
    proof = headers(client, team, project)
    answer_url = url(f"/api/agent/tasks/{task_id}/answer", team, project)
    answer = {"question_id": "Q1", "answer": "直属主管", "expected_revision": 1}
    assert client.post(answer_url, json=answer, headers=proof).status_code != 200
    accounts.set_member(alice, team, accounts.me(bob)["id"], "editor")
    assert client.post(answer_url, json=answer, headers=proof).status_code == 200
    assert client.post(answer_url, json=answer, headers=proof).status_code == 409
    confirm_url = url(f"/api/agent/tasks/{task_id}/confirm", team, project)
    confirmation = {"question_id": "Q1", "reason": "产品负责人确认", "expected_revision": 2}
    assert client.post(confirm_url, json=confirmation, headers=proof).status_code != 200
    client.cookies.set(COOKIE, alice)
    result = client.post(confirm_url, json=confirmation, headers=headers(client, team, project))
    assert result.status_code == 202, result.text
    child = service.get_task(result.json()["task_id"])
    assert child["parent_task_id"] == task_id and child["document_version"] == 2
    assert "直属主管" in child["input"]["normalized_text"]
    work = client.get(url(f"/api/agent/tasks/{child['id']}", team, project)).json()["work"]
    assert len(work) == 1 and work[0]["status"] == "queued"
    repeated = client.post(confirm_url, json=confirmation, headers=headers(client, team, project))
    assert repeated.status_code == 202 and repeated.json()["work_id"] == result.json()["work_id"]
    accounts.set_member(alice, team, accounts.me(bob)["id"], "observer", remove=True)
    client.cookies.set(COOKIE, bob)
    assert client.get(url(f"/api/agent/tasks/{task_id}", team, project)).status_code == 403


def test_session_reads_only_bound_authorized_project_and_redacts_provider_state(context, monkeypatch):
    path, _, alice, _, workspace, _, project, client = context
    service = TestingService(path, alice, workspace, project)
    task_id = service.create_task(PRD)
    root = Path(path).parent / "sessions"
    store = JsonlSessionStore(root, scope=workspace + ":" + project)
    session = Session(id="safe-session", metadata={"workspace_id": workspace, "project_id": project,
        "task_id": task_id, "model_settings": {"model_id": "hidden"}}, messages=[
        AssistantMessage(content="已完成需求分析", metadata={"provider_state": "opaque-private-token"})])
    store.save(session)
    service.bind_session(task_id, session.id)
    store.save(Session(id="foreign-session", metadata={"workspace_id": "foreign", "project_id": project, "task_id": task_id}))

    def no_model(*args, **kwargs):
        pytest.fail("Session reads must not build any runtime")

    monkeypatch.setattr("sdlc.agent.composition.build_runtime", no_model)
    listing = client.get(url("/api/agent/sessions", workspace, project))
    assert [item["id"] for item in listing.json()["sessions"]] == ["safe-session"]
    detail = client.get(url("/api/agent/sessions/safe-session", workspace, project))
    assert detail.status_code == 200
    assert detail.json()["session"]["messages"][0]["content"] == "已完成需求分析"
    assert "opaque-private-token" not in detail.text and "model_settings" not in detail.text
    assert client.get(url("/api/agent/sessions/foreign-session", workspace, project)).status_code == 404


def test_cancel_and_action_allowlist(context):
    task_id, _ = create(context)
    _, _, _, _, workspace, _, project, client = context
    proof = headers(client, workspace, project)
    assert client.post(url(f"/api/agent/tasks/{task_id}/execute_sql", workspace, project), json={}, headers=proof).status_code == 404
    assert client.post(url(f"/api/agent/tasks/{task_id}/cancel", workspace, project), json={}, headers=proof).status_code == 200
    detail = client.get(url(f"/api/agent/tasks/{task_id}", workspace, project)).json()
    assert detail["task"]["status"] == "cancelled"
    assert detail["work"][0]["status"] == "cancelled"
    assert client.post(url(f"/api/agent/tasks/{task_id}/resume", workspace, project), json={}, headers=proof).status_code == 409


def test_internal_exceptions_do_not_expose_credentials(context, monkeypatch):
    task_id, _ = create(context)
    _, _, _, _, workspace, _, project, client = context

    def fail(*args, **kwargs):
        raise RuntimeError("api_key=DO-NOT-EXPOSE")

    monkeypatch.setattr(TestingService, "task_summary", fail)
    response = client.get(url(f"/api/agent/tasks/{task_id}", workspace, project))
    assert response.status_code == 503 and "DO-NOT-EXPOSE" not in response.text


def prepared_design(service, count=1):
    task_id = service.create_task("# 提交\n\n申请人可提交报销单。")
    service.save_analysis(task_id, {
        "features": [{"id": "F1", "description": "提交报销单", "paragraph_ids": ["P002"]}],
        "roles": [{"id": "ROLE1", "name": "申请人", "responsibilities": ["提交报销单"], "paragraph_ids": ["P002"]}],
        "business_rules": [{"id": "R1", "statement": "申请人可提交报销单", "status": "clear", "paragraph_ids": ["P002"]}],
        "clarifications": [], "risks": [],
    })
    service.save_plan(task_id, {"conditions": [{"id": f"C{index}", "module": "提交", "description": f"正常提交场景 {index}",
        "requirement_ids": ["R1"], "paragraph_ids": ["P002"], "method": "scenario",
        "priority": "P1", "status": "planned", "rationale": "验证明确规定的提交行为"} for index in range(1, count + 1)]})
    service.save_batch(task_id, "submit", {"cases": [{"case": {
        "case_id": f"TC{index}", "title": "申请人正常提交报销单", "requirement_ids": ["R1"],
        "paragraph_ids": ["P002"], "preconditions": ["申请人准备一份虚构报销单"],
        "steps": [{"number": 1, "action": "申请人提交报销单"}], "expected_results": ["报销单提交成功"],
        "priority": "P1", "category": "normal", "readiness": "ready", "clarification_ids": []},
        "condition_ids": [f"C{index}"], "role": "申请人", "test_data": ["虚构发票FAKE-001"],
        "cleanup": [], "method": "scenario"} for index in range(1, count + 1)]})
    service.finalize_design(task_id)
    return task_id


def test_human_approval_baseline_and_download_are_a_complete_gated_path(context):
    path, _, alice, _, workspace, _, project, client = context
    service = TestingService(path, alice, workspace, project)
    task_id = prepared_design(service)
    proof = headers(client, workspace, project)
    prefix = f"/api/agent/tasks/{task_id}"
    detail = client.get(url(prefix, workspace, project)).json()
    case = detail["review"]["cases"][0]
    export_data = {"format": "md", "expected_run_revision": detail["review"]["revision"], "request_key": "export-1"}
    assert client.post(url(prefix + "/export", workspace, project), json=export_data, headers=proof).status_code == 409
    assert client.post(url(prefix + "/baseline", workspace, project), json={}, headers=proof).status_code == 409
    approval = {"case_id": case["case_id"], "version": case["version"], "revision": case["revision"], "request_key": "approve-1"}
    stale = {**approval, "revision": 999}
    assert client.post(url(prefix + "/approve", workspace, project), json=stale, headers=proof).status_code == 409
    approved = client.post(url(prefix + "/approve", workspace, project), json=approval, headers=proof)
    assert approved.status_code == 200, approved.text
    repeated = client.post(url(prefix + "/approve", workspace, project), json=approval, headers=proof)
    assert repeated.status_code == 200 and repeated.json() == approved.json()
    assert client.post(url(prefix + "/export", workspace, project), json=export_data, headers=proof).status_code == 409
    baseline = client.post(url(prefix + "/baseline", workspace, project), json={}, headers=proof)
    assert baseline.status_code == 200, baseline.text
    detail = client.get(url(prefix, workspace, project)).json()
    assert detail["task"]["status"] == "completed"
    export_data["expected_run_revision"] = detail["review"]["revision"]
    exported = client.post(url(prefix + "/export", workspace, project), json=export_data, headers=proof)
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-disposition"].startswith("attachment;")
    assert "申请人正常提交报销单" in exported.content.decode("utf-8-sig")
    again = client.post(url(prefix + "/export", workspace, project), json=export_data, headers=proof)
    assert again.headers["x-tracecase-export-id"] == exported.headers["x-tracecase-export-id"]
    # Approval of a later edited version must not silently replace the approved baseline.
    run_id = detail["review"]["run_id"]
    current = service.legacy.get_run(run_id)["cases"][0]
    changed = {**current["content"], "title": "人工修改后的提交用例"}
    service.legacy.edit(run_id, "TC1", current["version"], current["revision"], changed, "", "修改标题", "edit-after-baseline")
    current = service.legacy.get_run(run_id)["cases"][0]
    service.legacy.review(run_id, "TC1", current["version"], current["revision"], "approve", "", "通过", "reapprove")
    export_data["expected_run_revision"] = service.legacy.get_run(run_id)["revision"]
    export_data["request_key"] = "export-drift"
    drifted = client.post(url(prefix + "/export", workspace, project), json=export_data, headers=proof)
    assert drifted.status_code == 409 and "偏离" in drifted.text


def test_approval_and_export_enforce_current_team_role_permissions(context):
    path, accounts, alice, bob, _, _, _, client = context
    team = accounts.create_team(alice, "Approval team")
    invite = accounts.create_invite(alice, team)
    application = accounts.request_join(bob, invite, confirm=True)
    accounts.decide_join(alice, team, application["request_id"], True, "editor")
    project = Service(path, token=alice, workspace_id=team).create_project("Review permissions", "")
    service = TestingService(path, alice, team, project)
    task_id = prepared_design(service)
    client.cookies.set(COOKIE, bob)
    proof = headers(client, team, project)
    prefix = f"/api/agent/tasks/{task_id}"
    case = service.legacy.get_run(service.get_task(task_id)["run_id"])["cases"][0]
    approval = {"case_id": "TC1", "version": case["version"], "revision": case["revision"], "request_key": "bob-approval"}
    assert client.post(url(prefix + "/approve", team, project), json=approval, headers=proof).status_code != 200
    accounts.set_member(alice, team, accounts.me(bob)["id"], "reviewer")
    assert client.post(url(prefix + "/approve", team, project), json=approval, headers=proof).status_code == 200
    client.cookies.set(COOKIE, alice)
    assert client.post(url(prefix + "/baseline", team, project), json={}, headers=headers(client, team, project)).status_code == 200
    accounts.set_member(alice, team, accounts.me(bob)["id"], "reviewer", {"export": False})
    client.cookies.set(COOKIE, bob)
    revision = service.legacy.get_run(service.get_task(task_id)["run_id"])["revision"]
    response = client.post(url(prefix + "/export", team, project),
        json={"format": "csv", "expected_run_revision": revision, "request_key": "not-allowed-export"},
        headers=headers(client, team, project))
    assert response.status_code != 200


def test_human_scope_and_merge_actions_preserve_originals_and_create_reviewable_children(context):
    path, _, alice, _, workspace, _, project, client = context
    service = TestingService(path, alice, workspace, project)
    source = prepared_design(service, count=2)
    proof = headers(client, workspace, project)
    scoped = client.post(url(f"/api/agent/tasks/{source}/exclude_condition", workspace, project),
        json={"condition_ids": ["C1"], "reason": "产品确认本轮仅覆盖第二项条件"}, headers=proof)
    assert scoped.status_code == 200, scoped.text
    child = service.get_task(scoped.json()["task_id"])
    assert child["parent_task_id"] == source
    assert [case["case_id"] for case in service.legacy.get_run(child["run_id"])["cases"]] == ["TC2"]
    assert len(service.legacy.get_run(service.get_task(source)["run_id"])["cases"]) == 2

    feedback = client.post(url(f"/api/agent/tasks/{source}/review", workspace, project), json={
        "case_id": "TC1", "category": "duplicate", "reason": "可以合并至相同提交场景", "expected_version": 1, "expected_revision": 1}, headers=proof)
    assert feedback.status_code == 200, feedback.text
    merged = client.post(url(f"/api/agent/tasks/{source}/resolve_proposal", workspace, project), json={
        "proposal_id": feedback.json()["proposal_id"], "decision": "merge", "reason": "确认两项条件由目标用例一并覆盖", "target_case_id": "TC2"}, headers=proof)
    assert merged.status_code == 200, merged.text
    merged_task = service.get_task(merged.json()["task_id"])
    merged_cases = service.legacy.get_run(merged_task["run_id"])["cases"]
    assert len(merged_cases) == 1 and merged_cases[0]["status"] == "pending_review"
    assert service.case_drafts(merged_task["id"])[0]["condition_ids"] == ["C1", "C2"]
    original = service.legacy.get_run(service.get_task(source)["run_id"])["cases"]
    assert next(case for case in original if case["case_id"] == "TC1")["status"] == "returned"


def test_reviewer_can_queue_targeted_feedback_and_message_without_generate(context, monkeypatch):
    path, accounts, alice, bob, _, _, _, client = context
    team = accounts.create_team(alice, "Reviewer API team")
    invite = accounts.create_invite(alice, team)
    application = accounts.request_join(bob, invite, confirm=True)
    accounts.decide_join(alice, team, application["request_id"], True, "reviewer")
    project = Service(path, token=alice, workspace_id=team).create_project("Targeted revision", "")
    owner = TestingService(path, alice, team, project)
    task_id = prepared_design(owner)
    assert "generate" not in accounts.access(bob, team)["permissions"]
    client.cookies.set(COOKIE, bob)
    proof = headers(client, team, project)
    feedback = client.post(url(f"/api/agent/tasks/{task_id}/review", team, project), json={
        "case_id": "TC1", "category": "unclear_steps", "reason": "明确步骤中的操作主体", "expected_version": 1, "expected_revision": 1}, headers=proof)
    assert feedback.status_code == 202, feedback.text
    assert feedback.json()["work_id"]
    with accounts.db() as db:
        payload = db.execute("SELECT payload FROM agent_work_items WHERE id=?", (feedback.json()["work_id"],)).fetchone()[0]
        assert '"capability": "regenerate"' in payload
    worker.cancel_work(owner, task_id)

    def forbidden_resume(*args, **kwargs):
        pytest.fail("A revision-only message must not call generate-protected resume_task")

    monkeypatch.setattr(TestingService, "resume_task", forbidden_resume)
    message = client.post(url(f"/api/agent/tasks/{task_id}/message", team, project), json={
        "content": "请继续处理该条质量反馈", "request_key": "reviewer-message"}, headers=proof)
    assert message.status_code == 202, message.text
    assert client.post(url(f"/api/agent/tasks/{task_id}/resume", team, project), json={}, headers=proof).status_code == 403
    assert client.post(url("/api/agent/tasks", team, project), json={"text": PRD}, headers=proof).status_code != 202


@pytest.mark.parametrize("category", ["duplicate", "requirement_changed"])
def test_non_revision_feedback_does_not_automatically_queue_generation(context, category):
    path, _, alice, _, workspace, _, project, client = context
    service = TestingService(path, alice, workspace, project)
    task_id = prepared_design(service)
    result = client.post(url(f"/api/agent/tasks/{task_id}/review", workspace, project), json={
        "case_id": "TC1", "category": category, "reason": "需要人工确认后处理", "expected_version": 1, "expected_revision": 1},
        headers=headers(client, workspace, project))
    assert result.status_code == 200 and "work_id" not in result.json()
    assert worker.list_work(service, task_id) == []

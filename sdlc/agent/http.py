"""Authenticated HTTP adapter. Model execution only happens in the leased Worker."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from sdlc.accounts import Accounts, AccessError, COOKIE
from sdlc.domain import TestCase
from sdlc.service import ServiceError
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent import worker
from sdlc.testing_agent.domain import FEEDBACK_ROUTES

MAX_BODY = 256 * 1024
NO_STORE = {"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"}


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateTask(Payload):
    text: str = Field(min_length=1, max_length=20_000)
    goal: str = Field(default="", max_length=2000)
    scope: str = Field(default="", max_length=4000)
    parent_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    owner_id: str | None = Field(default=None, min_length=1, max_length=128)
    confirmer_id: str | None = Field(default=None, min_length=1, max_length=128)
    due_at: str | None = Field(default=None, max_length=10)
    resource_ids: list[str] | None = Field(default=None, max_length=50)


class CreateResource(Payload):
    kind: Literal['precondition', 'steps', 'test_data']
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=8000)


class UpdateResource(Payload):
    expected_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=8000)


class ArchiveResource(Payload):
    expected_revision: int = Field(ge=1)
    archived: bool = True


class Empty(Payload):
    pass


class Enqueue(Payload):
    request_key: str | None = Field(default=None, min_length=1, max_length=128)


class MessagePayload(Enqueue):
    content: str = Field(min_length=1, max_length=20_000)


class AnswerPayload(Payload):
    question_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(default="", max_length=4000)
    disposition: Literal["answer", "unknown", "needs_confirmation", "false_positive"] = "answer"
    assignee_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class ConfirmPayload(Payload):
    question_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=1)


class ReviewPayload(Payload):
    case_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class ApplyPayload(Payload):
    proposal_id: str = Field(min_length=1, max_length=128)


class ImportPayload(Payload):
    data: str = Field(min_length=1, max_length=MAX_BODY)
    field_mapping: dict[str, str] | None = None


class OldTaskPayload(Payload):
    old_task_id: str = Field(min_length=1, max_length=128)


class RegressionPayload(Payload):
    old_task_id: str | None = Field(default=None, min_length=1, max_length=128)


class CarryPayload(OldTaskPayload):
    case_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


class ApprovePayload(Payload):
    case_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    revision: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=2000)


class ExportPayload(Payload):
    format: Literal["csv", "md"]
    expected_run_revision: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=128)


class ResolveQuestionPayload(Payload):
    question_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=500)


class ExcludePayload(Payload):
    condition_ids: list[str] = Field(default_factory=list, max_length=500)
    requirement_ids: list[str] = Field(default_factory=list, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)


class ResolveProposalPayload(Payload):
    proposal_id: str = Field(min_length=1, max_length=128)
    decision: Literal["dismiss", "merge", "deactivate"]
    reason: str = Field(min_length=1, max_length=2000)
    target_case_id: str | None = Field(default=None, min_length=1, max_length=128)
    merged_content: TestCase | None = None


ACTION_SCHEMAS = {
    "resume": Enqueue, "message": MessagePayload, "cancel": Empty,
    "answer": AnswerPayload, "confirm": ConfirmPayload, "review": ReviewPayload,
    "apply_revision": ApplyPayload, "baseline": Empty, "import": ImportPayload,
    "regression": RegressionPayload, "compare": OldTaskPayload,
    "carry_approval": CarryPayload,
    "approve": ApprovePayload, "export": ExportPayload,
    "resolve_question": ResolveQuestionPayload, "exclude_condition": ExcludePayload,
    "resolve_proposal": ResolveProposalPayload,
}


def csrf_proof(token: str) -> str:
    """Purpose-specific proof bound to the current HttpOnly login credential."""
    return hmac.new(token.encode(), b"tracecase:agent-api:csrf", hashlib.sha256).hexdigest()


def _single_query(request, name, required=False, default=None):
    values = request.query_params.getlist(name)
    if len(values) > 1 or required and not values:
        raise APIError("请提供唯一的工作空间和项目标识。")
    value = values[0] if values else default
    if required and (not value or len(value) > 128):
        raise APIError("工作空间或项目标识无效。")
    return value


def _identity(request, path):
    token = request.cookies.get(COOKIE)
    accounts = Accounts(path)
    try:
        user = accounts.me(token)
    except AccessError:
        raise APIError("请先登录，或重新登录后继续。", 401) from None
    workspace = _single_query(request, "workspace", required=True)
    project = _single_query(request, "project", required=True)
    access = accounts.access(token, workspace)
    service = TestingService(path, token, workspace, project)
    return token, user, access, service


def _mutation(request, token):
    # Lazy import avoids a route-registration cycle with the gateway composition root.
    from sdlc.gateway import check_form_source
    same_origin = check_form_source(request)
    if not same_origin and request.headers.get("sec-fetch-site") != "same-origin":
        raise APIError("请求来源无效，请重新打开当前项目。", 403)
    proof = request.headers.get("x-tracecase-csrf", "")
    if len(proof) != 64 or not proof.isascii() or not hmac.compare_digest(proof, csrf_proof(token)):
        raise APIError("页面已过期或缺少请求校验，请刷新后重试。", 403)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


async def _payload(request, schema):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise APIError("请求必须使用 JSON 格式。", 415)
    length = request.headers.get("content-length")
    try:
        if length is not None and (int(length) < 0 or int(length) > MAX_BODY):
            raise APIError("请求内容超过 256 KiB。", 413)
    except ValueError:
        raise APIError("请求长度无效。") from None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise APIError("请求内容超过 256 KiB。", 413)
    try:
        value = json.loads(body, object_pairs_hook=_unique_json,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        return schema.model_validate(value)
    except (ValueError, UnicodeError, ValidationError):
        raise APIError("请求字段无效、重复或不完整，请刷新后重试。") from None


def _public_task(task):
    result = {key: value for key, value in task.items()
              if not key.endswith("_json") and key not in {"budget"}}
    for name in ("artifacts", "events", "proposals", "baselines"):
        if name in result:
            result[name] = [{key: value for key, value in item.items() if not key.endswith("_json")}
                            for item in result[name]]
    result['artifacts'] = [item for item in result.get('artifacts', []) if item.get('kind') != 'model_attempt']
    for event in result.get("events", []):
        if event.get("action") in {"model_call", "model_usage"}:
            event["details"] = {key: value for key, value in event.get("details", {}).items()
                                if key in {"type", "role", "call_number", "latency_ms"}}
    return result


def _store(service):
    from sdlc.agent.composition import build_session_store
    return build_session_store(service.db_path, service.workspace_id, service.project_id)


def _session_allowed(session, service):
    if (session.metadata.get("workspace_id") != service.workspace_id
            or session.metadata.get("project_id") != service.project_id):
        return False
    task_id = session.metadata.get("task_id")
    if not isinstance(task_id, str):
        return False
    try:
        task = service.get_task(task_id)
    except ServiceError:
        return False
    return task.get("session_id") == session.id


def _public_session(session, detail=False):
    result = {"id": session.id, "revision": session.revision,
              "metadata": {key: value for key, value in session.metadata.items()
                           if key in {"workspace_id", "project_id", "task_id", "agent_turn"}}}
    if detail:
        result["messages"] = [{key: value for key, value in message.model_dump(mode="json").items()
                               if key != "metadata"} for message in session.messages]
    return result


def _review_state(service, task):
    if not task["run_id"]:
        return None
    run = service.legacy.get_run(task["run_id"])
    return {"run_id": run["id"], "revision": run["revision"], "cases": run["cases"],
            "gate": service.legacy.gate(run["id"])}


def _baseline_export(service, task_id, values):
    task = service.get_task(task_id)
    if task["status"] != "completed" or not task["baselines"] or not task["run_id"]:
        raise APIError("请先完成全部评审并创建批准基线，再导出。", 409)
    run = service.legacy.get_run(task["run_id"])
    current = {(c["case_id"], c["version"], c["revision"]) for c in run["cases"]}
    snapshots = [b["snapshot"] for b in task["baselines"] if b["run_id"] == task["run_id"]]
    if not any(current == {(c["case_id"], c["version"], c["revision"]) for c in b["cases"]}
               for b in snapshots):
        raise APIError("用例已偏离批准基线，请创建新任务重新评审后导出。", 409)
    return service.legacy.export(task["run_id"], values["format"], "",
                                 values["expected_run_revision"], values["request_key"])


def agent_routes(db_path):
    async def endpoint(request: Request):
        try:
            token, user, access, service = _identity(request, db_path)
            action = request.path_params.get("action")
            task_id = request.path_params.get("task_id")
            path = request.url.path.rstrip("/")
            status = 200
            if request.method == "POST":
                _mutation(request, token)
                if path == '/api/agent/resources' or request.path_params.get('resource_id'):
                    from sdlc.testing_agent.resources import ResourceLibrary
                    library = ResourceLibrary(db_path, token, service.workspace_id, service.project_id)
                    if path == '/api/agent/resources':
                        data = await _payload(request, CreateResource)
                        result, status = {'resource': library.create(**data.model_dump())}, 201
                    elif action == 'update':
                        data = await _payload(request, UpdateResource)
                        result = {'resource': library.update(request.path_params['resource_id'], **data.model_dump())}
                    elif action == 'archive':
                        data = await _payload(request, ArchiveResource)
                        result = {'resource': library.archive(request.path_params['resource_id'], **data.model_dump())}
                    else:
                        raise APIError('未找到此操作。', 404)
                elif path == "/api/agent/tasks":
                    data = await _payload(request, CreateTask)
                    new_id = service.create_task(**data.model_dump())
                    job_id = worker.enqueue(service, new_id, "start")
                    result, status = {"task_id": new_id, "work_id": job_id}, 202
                else:
                    schema = ACTION_SCHEMAS.get(action)
                    if schema is None:
                        raise APIError("未找到此操作。", 404)
                    data = await _payload(request, schema)
                    service.get_task(task_id)
                    values = data.model_dump()
                    if action in {"resume", "message"}:
                        task = service.get_task(task_id)
                        if task["status"] in {"cancelled", "completed"}:
                            raise APIError("任务已取消或完成，请创建关联新任务。", 409)
                        job_id = worker.enqueue(service, task_id, action,
                            content=values.get("content", ""), request_key=values.get("request_key"))
                        result, status = {"task_id": task_id, "work_id": job_id}, 202
                    elif action == "cancel":
                        worker.cancel_work(service, task_id)
                        service.cancel_task(task_id)
                        result = {"task_id": task_id, "status": "cancelled"}
                    elif action == "answer":
                        result = {"answer": service.submit_answer(task_id, **values)}
                    elif action == "confirm":
                        child = service.confirm_answer(task_id, **values)
                        key = "confirm-" + hashlib.sha256((task_id + ":" + values["question_id"]).encode()).hexdigest()
                        job_id = worker.enqueue(service, child, "start", request_key=key)
                        result, status = {"task_id": child, "parent_task_id": task_id, "work_id": job_id}, 202
                    elif action == "review":
                        proposal_id = service.review_feedback(task_id, **values)
                        result = {"proposal_id": proposal_id}
                        if FEEDBACK_ROUTES[values["category"]] == "revision" and "regenerate" in access["permissions"]:
                            try:
                                work_id = worker.enqueue(service, task_id, "message",
                                    content="请仅处理已记录的用例质量反馈并提出修订，保留原用例等待人工采纳。"
                                            + "用例ID：" + values["case_id"] + "；反馈提案ID：" + proposal_id,
                                    request_key="review-" + proposal_id)
                                result["work_id"], status = work_id, 202
                            except (ValueError, AccessError):
                                # The human return is already committed. Keep it visible even
                                # when queue admission or a simultaneous ACL change rejects work.
                                result["next_action"] = "退回反馈已保存；修订尚未排队，请在当前任务结束或权限恢复后继续修订。"
                    elif action == "approve":
                        task = service.get_task(task_id)
                        if task["status"] in {"cancelled", "completed"} or not task["run_id"]:
                            raise APIError("当前任务没有待评审的用例。", 409)
                        result = {"event_id": service.legacy.review(task["run_id"], values["case_id"],
                            values["version"], values["revision"], "approve", "", values["reason"].strip() or "通过", values["request_key"])}
                    elif action == "apply_revision":
                        result = {"version": service.apply_revision(task_id, **values)}
                    elif action == "baseline":
                        result = {"baseline_id": service.create_baseline(task_id)}
                    elif action == "export":
                        exported = _baseline_export(service, task_id, values)
                        return Response(exported["content"], media_type=exported["mime"], headers={
                            **NO_STORE, "Content-Disposition": 'attachment; filename="' + exported["filename"] + '"',
                            "X-TraceCase-Export-ID": exported["id"],
                        })
                    elif action == "import":
                        result = service.import_cases_csv(task_id, **values)
                    elif action == "resolve_question":
                        service.resolve_question(task_id, **values)
                        result = {"task_id": task_id, "status": "resolved"}
                    elif action == "exclude_condition":
                        result = {"task_id": service.exclude_condition(task_id, **values), "parent_task_id": task_id}
                    elif action == "resolve_proposal":
                        result = {"task_id": service.resolve_proposal(task_id, **values), "parent_task_id": task_id}
                    elif action == "regression":
                        result = service.regression_plan(task_id, **values)
                    elif action == "compare":
                        result = service.compare_tasks(values["old_task_id"], task_id)
                    else:
                        result = {"event_id": service.carry_approval(task_id, **values)}
            elif path == '/api/agent/resources':
                from sdlc.testing_agent.resources import ResourceLibrary
                library = ResourceLibrary(db_path, token, service.workspace_id, service.project_id)
                result = {'resources': library.list()}
            elif path == "/api/agent/context":
                result = {"user": user, "permissions": sorted(access["permissions"]),
                          "role": access["role"], "kind": access["kind"],
                          "workspace_id": service.workspace_id, "project_id": service.project_id,
                          "csrf": csrf_proof(token)}
            elif path == "/api/agent/tasks":
                try:
                    limit = int(_single_query(request, "limit", default="100"))
                    offset = int(_single_query(request, "offset", default="0"))
                except ValueError:
                    raise APIError("分页参数无效。") from None
                result = {"tasks": [_public_task(task) for task in service.list_tasks(limit, offset)]}
            elif task_id:
                task = service.get_task(task_id)
                result = {"task": _public_task(task), "review": _review_state(service, task),
                          "summary": service.task_summary(task_id), "work": worker.list_work(service, task_id)}
            elif request.path_params.get("session_id"):
                session = _store(service).get(request.path_params["session_id"])
                if session is None or not _session_allowed(session, service):
                    raise APIError("对话不存在或无权访问。", 404)
                result = {"session": _public_session(session, True)}
            else:
                result = {"sessions": [_public_session(session) for session in _store(service).list_sessions()
                                        if _session_allowed(session, service)]}
            return JSONResponse(result, status_code=status, headers=NO_STORE)
        except APIError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status, headers=NO_STORE)
        except AccessError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403, headers=NO_STORE)
        except ServiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409, headers=NO_STORE)
        except ValueError:
            return JSONResponse({"error": "操作参数无效或当前任务无法执行，请刷新后重试。"}, status_code=400, headers=NO_STORE)
        except Exception:
            return JSONResponse({"error": "任务服务暂时不可用，已有成果已保留，请稍后重试。"}, status_code=503, headers=NO_STORE)

    return [
        Route("/api/agent/context", endpoint, methods=["GET"]),
        Route('/api/agent/resources', endpoint, methods=['GET', 'POST']),
        Route('/api/agent/resources/{resource_id}/{action}', endpoint, methods=['POST']),
        Route("/api/agent/tasks", endpoint, methods=["GET", "POST"]),
        Route("/api/agent/tasks/{task_id}", endpoint, methods=["GET"]),
        Route("/api/agent/tasks/{task_id}/{action}", endpoint, methods=["POST"]),
        Route("/api/agent/sessions", endpoint, methods=["GET"]),
        Route("/api/agent/sessions/{session_id}", endpoint, methods=["GET"]),
    ]

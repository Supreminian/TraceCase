"""SQLite application service: version-bound decisions and transaction-bound exports."""

import copy
import hashlib
import json
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sdlc import __version__
from sdlc.domain import Clarification, prepare_input, validate_current, validate_output
from sdlc.exports import render_export
from sdlc.prompts import build_prompt, build_review_prompt
from sdlc.providers import FixtureProvider, ProviderError
from sdlc.storage import initialize


class ServiceError(ValueError):
    """An actionable, safe-to-display workflow error."""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier():
    return uuid.uuid4().hex


def actor_reason(operator, reason=None):
    operator = operator.strip()
    if not 1 <= len(operator) <= 64:
        raise ServiceError("操作者标识必须为 1–64 个字符。")
    if reason is not None and not 1 <= len(reason.strip()) <= 2000:
        raise ServiceError("请填写 1–2000 个字符的操作理由。")
    return operator, reason.strip() if reason is not None else None


def safe_metadata(value):
    allowed = {"provider", "model", "model_revision", "temperature", "seed", "max_output_tokens",
               "timeout_seconds", "fixture_id", "mode", "real_model_call", "endpoint", "finish_reason"}
    result = {k: v for k, v in value.items() if k in allowed}
    if isinstance(value.get("parameters"), dict):
        result["parameters"] = {k: v for k, v in value["parameters"].items()
                                if k in {"temperature", "top_p", "seed", "max_tokens",
                                         "max_output_tokens", "timeout_seconds"}
                                and (v is None or type(v) in (str, bool, int, float))}
    return result


class Service:
    def __init__(self, db_path="data/sdlc.sqlite3", *, project_id=None):
        self.db_path = str(db_path)
        self.project_id = project_id
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection(initialize=True) as db:
            db.execute("PRAGMA journal_mode=WAL")
            initialize(db, self.db_path)

    @contextmanager
    def connection(self, write=False, initialize=False):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if initialize:
                pass
            elif write:
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _request(self, db, request_id, payload):
        if not request_id or len(request_id) > 128:
            raise ServiceError("缺少有效请求标识。")
        self._project(db, active=True)
        fingerprint = hashlib.sha256(dumps([self.project_id, payload]).encode()).hexdigest()
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise ServiceError("请求标识已用于不同操作，请刷新后重试。")
            return fingerprint, json.loads(row["result_json"])
        return fingerprint, None

    def _remember(self, db, request_id, fingerprint, result):
        db.execute("INSERT INTO requests VALUES (?,?,?)", (request_id, fingerprint, dumps(result)))

    def _event(self, db, run_id, action, operator, reason, case_id=None, version=None,
               before=None, after=None, details=None):
        event_id = identifier()
        db.execute("""INSERT INTO events
            (id,run_id,case_id,version,action,operator,created_at,reason,before_status,after_status,details_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (event_id, run_id, case_id, version, action,
            operator, now(), reason, before, after, dumps(details or {})))
        db.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (run_id,))
        return event_id

    def _read_project(self, db, project_id):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ServiceError("项目不存在，请重新选择项目。")
        return dict(row)

    def _project(self, db, active=False):
        if not self.project_id:
            raise ServiceError("请先选择项目。")
        project = self._read_project(db, self.project_id)
        if active and project["status"] != "active":
            raise ServiceError("项目已归档，仅可查看和下载已有快照；恢复后再操作。")
        return project

    def list_projects(self, limit=200, offset=0):
        if not 1 <= limit <= 500 or offset < 0:
            raise ServiceError("项目分页参数无效。")
        with self.connection() as db:
            return [dict(r) for r in db.execute("""SELECT p.*,
                (SELECT COUNT(*) FROM runs r WHERE r.project_id=p.id) AS run_count
                FROM projects p ORDER BY p.created_at DESC,p.id LIMIT ? OFFSET ?""", (limit, offset))]

    def get_project(self, project_id=None):
        with self.connection() as db:
            project = self._read_project(db, project_id) if project_id else self._project(db)
            project["events"] = [dict(r) for r in db.execute(
                "SELECT * FROM project_events WHERE project_id=? ORDER BY created_at,id", (project["id"],))]
            return project

    def _project_fields(self, name, description):
        name, description = name.strip(), description.strip()
        if not 1 <= len(name) <= 80 or len(description) > 500:
            raise ServiceError("项目名称须为 1–80 字符，简介最多 500 字符。")
        return name, description

    def _project_event(self, db, project_id, action, operator, details):
        db.execute("INSERT INTO project_events VALUES (?,?,?,?,?,?)",
                   (identifier(), project_id, action, operator, now(), dumps(details)))

    def create_project(self, name, description, operator):
        operator, _ = actor_reason(operator)
        name, description = self._project_fields(name, description)
        project_id = identifier()
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM projects WHERE name_key=?", (name.casefold(),)).fetchone():
                raise ServiceError("项目名称已存在，请使用其他名称。")
            stamp = now()
            db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                       (project_id, name, name.casefold(), description, "active", stamp, stamp, operator, 1))
            self._project_event(db, project_id, "create", operator, {"name": name, "description": description})
        return project_id

    def update_project(self, project_id, name, description, operator, expected_revision):
        operator, _ = actor_reason(operator)
        name, description = self._project_fields(name, description)
        with self.connection(write=True) as db:
            old = self._read_project(db, project_id)
            if old["revision"] != expected_revision:
                raise ServiceError("项目信息已有变化，请刷新后重试。")
            if old["status"] != "active":
                raise ServiceError("请先恢复归档项目，再编辑。")
            if db.execute("SELECT 1 FROM projects WHERE name_key=? AND id!=?", (name.casefold(), project_id)).fetchone():
                raise ServiceError("项目名称已存在，请使用其他名称。")
            db.execute("UPDATE projects SET name=?,name_key=?,description=?,operator=?,updated_at=?,revision=revision+1 WHERE id=?",
                       (name, name.casefold(), description, operator, now(), project_id))
            self._project_event(db, project_id, "update", operator,
                                {"before": {k: old[k] for k in ("name", "description")},
                                 "after": {"name": name, "description": description}})

    def set_project_archived(self, project_id, archived, operator, expected_revision):
        operator, _ = actor_reason(operator)
        with self.connection(write=True) as db:
            old = self._read_project(db, project_id)
            if old["revision"] != expected_revision:
                raise ServiceError("项目状态已有变化，请刷新后重试。")
            if archived and db.execute("SELECT 1 FROM runs WHERE project_id=? AND status='generating'", (project_id,)).fetchone():
                raise ServiceError("项目仍有生成中的批次，请先完成或记录中断。")
            status = "archived" if archived else "active"
            if old["status"] == status:
                return
            db.execute("UPDATE projects SET status=?,operator=?,updated_at=?,revision=revision+1 WHERE id=?",
                       (status, operator, now(), project_id))
            self._project_event(db, project_id, "archive" if archived else "restore", operator,
                                {"before": old["status"], "after": status})

    def project_stats(self):
        with self.connection() as db:
            self._project(db)
            stats = dict(db.execute("""SELECT COUNT(*) AS runs,
                COALESCE(SUM(status='generating'),0) AS generating,
                COALESCE(SUM(status IN ('failed','invalid')),0) AS failed
                FROM runs WHERE project_id=?""", (self.project_id,)).fetchone())
            stats.update(dict(db.execute("""SELECT COUNT(*) AS cases,
                COALESCE(SUM(h.status='pending_review'),0) AS pending,
                COALESCE(SUM(h.status='returned'),0) AS returned,
                COALESCE(SUM(h.status='approved'),0) AS approved
                FROM case_heads h JOIN runs r ON r.id=h.run_id WHERE r.project_id=?""", (self.project_id,)).fetchone()))
            return stats

    def generate(self, text=None, filename=None, data=None, *, operator, prompt_version="v2",
                 parent_run_id=None, provider=None, request_id=None, review_source_revision=None):
        operator, _ = actor_reason(operator)
        prepared = prepare_input(text=text, filename=filename, data=data)
        provider = provider or FixtureProvider()
        prompt = build_prompt(prompt_version, prepared["paragraphs"])
        config = safe_metadata(provider.config)
        config.update({"app_version": __version__, "schema_version": "1.0"})
        try:
            config["commit"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            config["commit"] = "uncommitted"
        request_id = request_id or identifier()
        payload = ["generate", prepared, operator, prompt_version, parent_run_id, config, review_source_revision]
        revision_snapshot = None
        returned_ids = set()
        run_id = identifier()
        with self.connection(write=True) as db:
            fingerprint, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            if parent_run_id:
                self._run(db, parent_run_id)
            if review_source_revision is not None:
                source = self._check_run_revision(db, parent_run_id, review_source_revision)
                if source["input"]["input_hash"] != prepared["input_hash"]:
                    raise ServiceError("需求已改变，请使用修订 PRD 的新运行流程。")
                cases = self._cases(db, parent_run_id)
                if not cases or any(c["status"] == "pending_review" for c in cases):
                    raise ServiceError("请先完成本轮所有用例审核。")
                returned_ids = {c["case_id"] for c in cases if c["status"] == "returned"}
                if not returned_ids:
                    raise ServiceError("本轮没有退回用例，无需模型修订。")
                if provider.config.get("provider") == "fixture":
                    raise ServiceError("固定样例不能修订退回意见，请配置实际模型。")
                revision_snapshot = self._effective(db, parent_run_id, cases)
                feedback = []
                for c in cases:
                    if c["case_id"] in returned_ids:
                        event = db.execute("""SELECT id,reason,operator,created_at FROM events
                            WHERE run_id=? AND case_id=? AND version=? AND action='return'
                            ORDER BY seq DESC LIMIT 1""", (parent_run_id, c["case_id"], c["version"])).fetchone()
                        if event is None:
                            raise ServiceError("退回事件缺失，不能开始修订。")
                        feedback.append({**dict(event), "case_id": c["case_id"], "version": c["version"]})
                prompt = build_review_prompt(prepared["paragraphs"], revision_snapshot, feedback,
                                             {"run_id": parent_run_id, "revision": review_source_revision})
            db.execute("""INSERT INTO runs
                (id,parent_run_id,project_id,created_at,operator,status,input_json,config_json,prompt_json)
                VALUES (?,?,?,?,?,?,?,?,?)""", (run_id, parent_run_id, self.project_id, now(), operator,
                "generating", dumps(prepared), dumps(config), dumps(prompt)))
            if revision_snapshot is not None:
                self._event(db, parent_run_id, "review_iteration_started", operator,
                            "将本轮退回意见送入模型修订", details={"child_run_id": run_id,
                            "source_revision": review_source_revision, "returned_case_ids": sorted(returned_ids)})
            self._event(db, run_id, "generation_started", operator, "开始生成；输入快照已固定")
            self._remember(db, request_id, fingerprint, run_id)
        start = time.perf_counter()
        try:
            response = provider.generate({**prepared, "prompt": prompt})
            latency = (time.perf_counter() - start) * 1000
        except Exception as exc:
            # Unknown provider exceptions may contain authorization headers; never persist them.
            message = str(exc) if isinstance(exc, ProviderError) or type(provider) is FixtureProvider else f"模型调用失败（{type(exc).__name__}）"
            with self.connection(write=True) as db:
                if self._run(db, run_id)["status"] != "generating":
                    return run_id
                elapsed = (time.perf_counter() - start) * 1000
                db.execute("UPDATE runs SET status='failed',error=?,latency_ms=?,total_latency_ms=? WHERE id=?",
                           (message, elapsed, elapsed, run_id))
                self._event(db, run_id, "generation_failed", operator, message)
            return run_id
        validation = validate_output(response.raw_output, prepared["paragraphs"])
        finish_reason = response.metadata.get("finish_reason")
        if finish_reason in {"length", "content_filter", "tool_calls"}:
            validation["valid"] = False
            if finish_reason == "length":
                validation["errors"].insert(0,
                    "模型输出达到长度上限，被截断；不能进入审核，原始内容已保留。"
                    "请检查模型配置中的 parameters.max_tokens，调高 SDLC_MODEL_MAX_TOKENS 后重启应用，"
                    "再基于本次输入创建关联新运行；若已达模型支持上限，请按功能拆分 PRD 分别生成。"
                    "不会自动重试或额外消耗模型额度。")
            elif finish_reason == "content_filter":
                validation["errors"].insert(0, "模型服务的内容过滤中断了输出；不能进入审核，原始内容已保留。")
            else:
                validation["errors"].insert(0, "模型返回工具调用而非完整结果；不能进入审核，原始内容已保留。")
        output = validation.get("output")
        if validation["valid"] and revision_snapshot is not None:
            for field in ("features", "roles", "business_rules", "clarifications"):
                if output[field] != revision_snapshot[field]:
                    validation["errors"].append(f"修订越界：{field} 必须保持不变；新增业务事实请修订 PRD。")
            original_cases = {c["case_id"]: c for c in revision_snapshot["test_cases"]}
            revised_cases = {c["case_id"]: c for c in output["test_cases"]}
            if set(original_cases) != set(revised_cases):
                validation["errors"].append("修订不能新增、删除或更换用例编号。")
            for cid, content in original_cases.items():
                if cid not in returned_ids and revised_cases.get(cid) != content:
                    validation["errors"].append(f"修订越界：已通过用例 {cid} 被改动。")
            unchanged = [cid for cid in sorted(returned_ids)
                         if revised_cases.get(cid) == original_cases[cid]]
            if unchanged:
                validation["warnings"] = ["退回用例未改动：" + ", ".join(unchanged)]
            validation["valid"] = not validation["errors"]
        with self.connection(write=True) as db:
            if self._run(db, run_id)["status"] != "generating":
                # A recovered/interrupted run must not be resurrected by a late response.
                return run_id
            config.update(safe_metadata(response.metadata))
            usage = response.usage
            if usage is not None:
                usage = {k: v for k, v in usage.items() if k in {
                    "input_tokens", "output_tokens", "cost", "currency", "price_version"}}
            db.execute("""UPDATE runs SET status=?,raw_output=?,validation_json=?,output_json=?,
                config_json=?,latency_ms=?,total_latency_ms=?,usage_json=? WHERE id=?""",
                ("valid" if validation["valid"] else "invalid", response.raw_output,
                 dumps(validation), dumps(output), dumps(config), latency,
                 (time.perf_counter()-start)*1000, dumps(usage), run_id))
            if validation["valid"]:
                for c in output["test_cases"]:
                    self._new_case_version(db, run_id, c, operator)
                for q in output["clarifications"]:
                    db.execute("INSERT INTO clarifications (run_id,id,content_json) VALUES (?,?,?)",
                               (run_id, q["id"], dumps(q)))
            self._event(db, run_id, "generation_completed", operator,
                        "结构与引用校验通过" if validation["valid"] else "校验失败；原始输出已保留")
        return run_id

    def revise_returned(self, run_id, expected_run_revision, operator, provider, request_id):
        source = self.get_run(run_id)
        return self.generate(text=source["input"]["normalized_text"], operator=operator,
                             prompt_version="V2", parent_run_id=run_id, provider=provider,
                             request_id=request_id, review_source_revision=expected_run_revision)

    def _run(self, db, run_id):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ServiceError("运行不存在。")
        project = self._project(db)
        if row["project_id"] != self.project_id:
            raise ServiceError("此批次不属于当前项目。")
        run = dict(row)
        run["project_name"] = project["name"]
        run["project_status"] = project["status"]
        for key in ("input", "config", "prompt", "validation", "output", "usage"):
            raw = run.pop(key + "_json")
            run[key] = json.loads(raw) if raw else None
        return run

    def list_runs(self, limit=100, offset=0):
        with self.connection() as db:
            self._project(db)
            if not 1 <= limit <= 500 or offset < 0:
                raise ServiceError("批次分页参数无效。")
            rows = db.execute("SELECT id FROM runs WHERE project_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                              (self.project_id, limit, offset)).fetchall()
            return [self._run(db, r["id"]) for r in rows]

    def list_run_summaries(self, limit=50, offset=0, valid_only=False, query=""):
        with self.connection() as db:
            self._project(db)
            if not 1 <= limit <= 500 or offset < 0:
                raise ServiceError("批次分页参数无效。")
            return [dict(r) for r in db.execute("""SELECT r.id,r.parent_run_id,r.project_id,r.created_at,
                r.operator,r.status,r.revision,
                (SELECT COUNT(*) FROM case_heads h WHERE h.run_id=r.id) AS case_count
                FROM runs r WHERE r.project_id=? AND (?=0 OR r.status='valid') AND instr(r.id,?)>0
                ORDER BY r.created_at DESC,r.id LIMIT ? OFFSET ?""",
                (self.project_id, int(valid_only), query, limit, offset))]

    def _cases(self, db, run_id):
        rows = db.execute("""SELECT v.*,h.status,h.revision FROM case_heads h JOIN case_versions v
            ON v.run_id=h.run_id AND v.case_id=h.case_id AND v.version=h.version
            WHERE h.run_id=? ORDER BY h.case_id""", (run_id,)).fetchall()
        return [{**dict(r), "content": json.loads(r["content_json"])} for r in rows]

    def _questions(self, db, run_id):
        rows = db.execute("SELECT * FROM clarifications WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [{**dict(r), "content": json.loads(r["content_json"]),
                 "resolution": json.loads(r["resolution_json"]) if r["resolution_json"] else None} for r in rows]

    def get_run(self, run_id):
        with self.connection() as db:
            run = self._run(db, run_id)
            run["cases"] = self._cases(db, run_id)
            run["clarifications"] = self._questions(db, run_id)
            run["events"] = [{**dict(r), "details": json.loads(r["details_json"])} for r in
                             db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
            run["exports"] = [dict(r) for r in db.execute("""SELECT id,format,filename,operator,
                created_at,content_hash FROM exports WHERE run_id=? ORDER BY created_at DESC""", (run_id,))]
            versions = []
            for row in db.execute("SELECT * FROM case_versions WHERE run_id=? ORDER BY case_id,version", (run_id,)):
                version = {**dict(row), "content": json.loads(row["content_json"]), "status": "pending_review"}
                for event in run["events"]:
                    if event["case_id"] == version["case_id"] and event["version"] == version["version"] and event["after_status"]:
                        version["status"] = event["after_status"]
                versions.append(version)
            run["case_versions"] = versions
            return run

    def _new_case_version(self, db, run_id, content, operator, previous=None):
        version = previous["version"] + 1 if previous else 1
        revision = previous["revision"] + 1 if previous else 1
        db.execute("INSERT INTO case_versions VALUES (?,?,?,?,?,?)",
                   (run_id, content["case_id"], version, dumps(content), now(), operator))
        db.execute("""INSERT INTO case_heads VALUES (?,?,?,?,?)
            ON CONFLICT(run_id,case_id) DO UPDATE SET version=excluded.version,
            revision=excluded.revision,status=excluded.status""",
            (run_id, content["case_id"], version, revision, "pending_review"))
        return version

    def _case(self, db, run_id, case_id, version, revision):
        if self._run(db, run_id)["status"] != "valid":
            raise ServiceError("此运行未通过生成校验，不能审核。")
        case = next((c for c in self._cases(db, run_id) if c["case_id"] == case_id), None)
        if not case:
            raise ServiceError("用例不存在。")
        if case["version"] != version or case["revision"] != revision:
            raise ServiceError("用例版本或状态已变化，请刷新后重试。")
        return case

    def _effective(self, db, run_id, cases=None):
        run = self._run(db, run_id)
        output = copy.deepcopy(run["output"])
        if not output:
            return None
        questions = []
        for q in self._questions(db, run_id):
            if q["status"] == "false_positive":
                continue
            content = copy.deepcopy(q["content"])
            if q["status"] == "nonblocking":
                content["blocking"] = False
                content["rationale"] = q["resolution"]["reason"]
            questions.append(content)
        output["clarifications"] = questions
        for rule in output["business_rules"]:
            if rule["status"] != "clear" and not any(
                q["blocking"] and rule["id"] in q["requirement_ids"] for q in questions
            ):
                rule["status"] = "clear"
        output["test_cases"] = [c["content"] for c in (cases if cases is not None else self._cases(db, run_id))]
        return output

    @staticmethod
    def _blocking(content, output):
        return [q["id"] for q in output["clarifications"] if q["blocking"] and (
            q["scope"] == "run" or content["case_id"] in q["affected_case_ids"] or
            bool(set(content["requirement_ids"]) & set(q["requirement_ids"])))]

    def review(self, run_id, case_id, version, expected_revision, action, operator, reason, request_id):
        operator, reason = actor_reason(operator, reason)
        if action not in {"approve", "return"}:
            raise ServiceError("不支持的审核动作。")
        payload = ["review", run_id, case_id, version, expected_revision, action, operator, reason]
        with self.connection(write=True) as db:
            fingerprint, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            case = self._case(db, run_id, case_id, version, expected_revision)
            if action == "approve":
                if case["status"] != "pending_review":
                    raise ServiceError("仅待审核版本可通过；退回的用例请先保存新修订。")
                output = self._effective(db, run_id)
                if case["content"]["readiness"] != "ready" or self._blocking(case["content"], output):
                    raise ServiceError("用例仍有阻断性澄清，不能通过。")
                errors = validate_current(output, self._run(db, run_id)["input"]["paragraphs"])
                if errors:
                    raise ServiceError("当前集合校验失败：" + "; ".join(errors))
            elif case["status"] == "returned":
                raise ServiceError("此版本已退回，请保存新修订。")
            state = "approved" if action == "approve" else "returned"
            event_id = self._event(db, run_id, action, operator, reason, case_id, version,
                                   case["status"], state)
            db.execute("UPDATE case_heads SET status=?,revision=revision+1 WHERE run_id=? AND case_id=?",
                       (state, run_id, case_id))
            self._remember(db, request_id, fingerprint, event_id)
            return event_id

    def edit(self, run_id, case_id, version, expected_revision, content, operator, reason, request_id):
        operator, reason = actor_reason(operator, reason)
        payload = ["edit", run_id, case_id, version, expected_revision, content, operator, reason]
        with self.connection(write=True) as db:
            fingerprint, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            case = self._case(db, run_id, case_id, version, expected_revision)
            if content.get("case_id") != case_id:
                raise ServiceError("用例编号不可修改。")
            output = self._effective(db, run_id)
            content = copy.deepcopy(content)
            blockers = self._blocking(content, output)
            content["clarification_ids"] = blockers
            content["readiness"] = "needs_clarification" if blockers else "ready"
            output["test_cases"] = [content if c["case_id"] == case_id else c for c in output["test_cases"]]
            errors = validate_current(output, self._run(db, run_id)["input"]["paragraphs"])
            if errors:
                raise ServiceError("修改未保存：" + "; ".join(errors))
            new_version = self._new_case_version(db, run_id, content, operator, case)
            self._event(db, run_id, "edit", operator, reason, case_id, new_version,
                        case["status"], "pending_review", {"previous_version": version})
            self._remember(db, request_id, fingerprint, new_version)
            return new_version

    def _check_run_revision(self, db, run_id, expected_run_revision):
        run = self._run(db, run_id)
        if run["status"] != "valid":
            raise ServiceError("此运行未通过生成校验。")
        if run["revision"] != expected_run_revision:
            raise ServiceError("运行已有变化，请刷新后重试。")
        return run

    def _reversion_questions(self, db, run_id, operator, reason, affected):
        output = self._effective(db, run_id)
        for case in self._cases(db, run_id):
            if case["case_id"] not in affected:
                continue
            content = copy.deepcopy(case["content"])
            blockers = self._blocking(content, output)
            content["clarification_ids"] = blockers
            content["readiness"] = "needs_clarification" if blockers else "ready"
            version = self._new_case_version(db, run_id, content, operator, case)
            self._event(db, run_id, "clarification_revision", operator, reason,
                        case["case_id"], version, case["status"], "pending_review",
                        {"previous_version": case["version"]})
        errors = validate_current(self._effective(db, run_id), self._run(db, run_id)["input"]["paragraphs"])
        if errors:
            raise ServiceError("问题处理未保存；请先补齐受影响用例内容：" + "; ".join(errors))

    def add_clarification(self, run_id, content, expected_run_revision, operator, reason, request_id):
        operator, reason = actor_reason(operator, reason)
        content = Clarification.model_validate(content).model_dump()
        payload = ["add_clarification", run_id, content, expected_run_revision, operator, reason]
        with self.connection(write=True) as db:
            fingerprint, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            self._check_run_revision(db, run_id, expected_run_revision)
            if any(q["id"] == content["id"] for q in self._questions(db, run_id)):
                raise ServiceError("澄清编号已存在。")
            db.execute("INSERT INTO clarifications (run_id,id,content_json) VALUES (?,?,?)",
                       (run_id, content["id"], dumps(content)))
            affected = {c["case_id"] for c in self._cases(db, run_id) if content["scope"] == "run"
                        or c["case_id"] in content["affected_case_ids"]
                        or set(c["content"]["requirement_ids"]) & set(content["requirement_ids"])}
            self._reversion_questions(db, run_id, operator, reason, affected)
            event_id = self._event(db, run_id, "clarification_added", operator, reason,
                                   details={"clarification_id": content["id"], "content": content})
            self._remember(db, request_id, fingerprint, event_id)
            return event_id

    def resolve_clarification(self, run_id, clarification_id, expected_run_revision, resolution,
                              evidence_ids, operator, reason, request_id):
        operator, reason = actor_reason(operator, reason)
        if resolution not in {"false_positive", "nonblocking"}:
            raise ServiceError("新增业务事实须修订 PRD 并创建新运行。")
        payload = ["resolve", run_id, clarification_id, expected_run_revision, resolution,
                   evidence_ids, operator, reason]
        with self.connection(write=True) as db:
            fingerprint, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            run = self._check_run_revision(db, run_id, expected_run_revision)
            if not evidence_ids or not set(evidence_ids) <= {p["id"] for p in run["input"]["paragraphs"]}:
                raise ServiceError("必须引用本次运行中存在的段落作为判断依据。")
            q = next((q for q in self._questions(db, run_id) if q["id"] == clarification_id), None)
            if q is None or q["status"] != "open":
                raise ServiceError("问题不存在或已经处理。")
            db.execute("UPDATE clarifications SET status=?,revision=revision+1,resolution_json=? WHERE run_id=? AND id=?",
                       (resolution, dumps({"evidence_ids": evidence_ids, "reason": reason}), run_id, clarification_id))
            c = q["content"]
            affected = {case["case_id"] for case in self._cases(db, run_id) if c["scope"] == "run"
                        or case["case_id"] in c["affected_case_ids"]
                        or set(case["content"]["requirement_ids"]) & set(c["requirement_ids"])}
            self._reversion_questions(db, run_id, operator, reason, affected)
            event_id = self._event(db, run_id, "clarification_resolved", operator, reason,
                                   details={"clarification_id": clarification_id, "resolution": resolution,
                                            "evidence_ids": evidence_ids})
            self._remember(db, request_id, fingerprint, event_id)
            return event_id

    def _gate(self, db, run_id):
        run = self._run(db, run_id)
        if run["status"] != "valid":
            return ["运行尚未通过生成校验。"]
        cases = self._cases(db, run_id)
        issues = []
        if not cases:
            issues.append("没有可导出的用例。")
        output = self._effective(db, run_id, cases)
        issues.extend(validate_current(output, run["input"]["paragraphs"]))
        for c in cases:
            if c["status"] != "approved":
                issues.append(f'{c["case_id"]} v{c["version"]} 尚未通过审核（{c["status"]}）。')
            if c["content"]["readiness"] != "ready":
                issues.append(f'{c["case_id"]} 尚不可执行。')
        for q in output["clarifications"]:
            if q["blocking"]:
                issues.append(f'{q["id"]} 为未解决的阻断性澄清。')
        return issues

    def gate(self, run_id):
        with self.connection() as db:
            return self._gate(db, run_id)

    def export(self, run_id, format, operator, expected_run_revision, request_id):
        operator, _ = actor_reason(operator)
        if format not in {"csv", "md"}:
            raise ServiceError("仅支持 CSV / Markdown。")
        payload = ["export", run_id, format, operator, expected_run_revision]
        with self.connection(write=True) as db:
            fingerprint, old = self._request(db, request_id, payload)
            if old is not None:
                return self._export(db, old)
            run = self._check_run_revision(db, run_id, expected_run_revision)
            errors = self._gate(db, run_id)
            if errors:
                raise ServiceError("不能导出批准版本：" + "; ".join(errors))
            cases = self._cases(db, run_id)
            approvals = {}
            for c in cases:
                event = db.execute("""SELECT * FROM events WHERE run_id=? AND case_id=?
                    AND version=? AND action='approve' ORDER BY seq DESC LIMIT 1""",
                    (run_id, c["case_id"], c["version"])).fetchone()
                if event is None:
                    raise ServiceError("缺少当前版本的批准事件。")
                approvals[c["case_id"]] = dict(event)
            export_id = identifier()
            content, mime = render_export(run, cases, approvals, export_id, format)
            filename = f"approved-{self.project_id[:8]}-{run_id[:8]}-{export_id[:8]}.{format}"
            snapshot = [{"case_id": c["case_id"], "version": c["version"],
                         "approval_event_id": approvals[c["case_id"]]["id"]} for c in cases]
            db.execute("INSERT INTO exports (id,run_id,format,filename,mime,operator,created_at,content,content_hash,snapshot_json,project_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (export_id, run_id, format, filename, mime, operator, now(), content,
                        hashlib.sha256(content).hexdigest(), dumps(snapshot), run["project_name"]))
            self._event(db, run_id, "export", operator, "导出批准版本", details={"export_id": export_id})
            self._remember(db, request_id, fingerprint, export_id)
            return self._export(db, export_id)

    def _export(self, db, export_id):
        row = db.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
        if not row:
            raise ServiceError("导出快照不存在。")
        self._run(db, row["run_id"])
        result = dict(row)
        result["project_id"] = self.project_id
        result["snapshot"] = json.loads(result.pop("snapshot_json"))
        return result

    def list_exports(self, limit=25, offset=0):
        with self.connection() as db:
            self._project(db)
            if not 1 <= limit <= 100 or offset < 0:
                raise ServiceError("导出分页参数无效。")
            return [dict(r) for r in db.execute("""SELECT e.id,e.run_id,e.filename,e.format,e.created_at,
                e.operator,e.content_hash,e.project_name FROM exports e JOIN runs r ON r.id=e.run_id
                WHERE r.project_id=? ORDER BY e.created_at DESC,e.id LIMIT ? OFFSET ?""",
                (self.project_id, limit, offset))]

    def get_export(self, export_id):
        with self.connection() as db:
            return self._export(db, export_id)

    def mark_interrupted(self, run_id, operator, reason):
        """Explicit recovery; never invalidate another live call on a page refresh."""
        operator, reason = actor_reason(operator, reason)
        with self.connection(write=True) as db:
            self._project(db, active=True)
            if self._run(db, run_id)["status"] != "generating":
                raise ServiceError("此运行不处于生成中。")
            db.execute("UPDATE runs SET status='failed',error=? WHERE id=?", ("生成中断：" + reason, run_id))
            self._event(db, run_id, "generation_interrupted", operator, reason)

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
from sdlc.prompts import build_prompt
from sdlc.providers import FixtureProvider, ProviderError


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
    def __init__(self, db_path="data/sdlc.sqlite3"):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection(initialize=True) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, parent_run_id TEXT REFERENCES runs(id),
                    created_at TEXT NOT NULL, operator TEXT NOT NULL, status TEXT NOT NULL,
                    input_json TEXT NOT NULL, config_json TEXT NOT NULL, prompt_json TEXT NOT NULL,
                    raw_output TEXT, validation_json TEXT, output_json TEXT,
                    error TEXT, latency_ms REAL, total_latency_ms REAL, usage_json TEXT,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS case_versions (
                    run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT NOT NULL,
                    version INTEGER NOT NULL, content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, operator TEXT NOT NULL,
                    PRIMARY KEY(run_id,case_id,version)
                );
                CREATE TABLE IF NOT EXISTS case_heads (
                    run_id TEXT NOT NULL, case_id TEXT NOT NULL, version INTEGER NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(run_id,case_id),
                    FOREIGN KEY(run_id,case_id,version) REFERENCES case_versions(run_id,case_id,version)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
                    content_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    revision INTEGER NOT NULL DEFAULT 1, resolution_json TEXT,
                    PRIMARY KEY(run_id,id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT, version INTEGER,
                    action TEXT NOT NULL, operator TEXT NOT NULL, created_at TEXT NOT NULL,
                    reason TEXT NOT NULL, before_status TEXT, after_status TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    format TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
                    operator TEXT NOT NULL, created_at TEXT NOT NULL, content BLOB NOT NULL,
                    content_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
                PRAGMA user_version=1;
            """)
            for table in ("case_versions", "events", "exports"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'immutable history'); END""")

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
        fingerprint = hashlib.sha256(dumps(payload).encode()).hexdigest()
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

    def generate(self, text=None, filename=None, data=None, *, operator, prompt_version="v2",
                 parent_run_id=None, provider=None, request_id=None):
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
        payload = ["generate", prepared, operator, prompt_version, parent_run_id, config]
        run_id = identifier()
        with self.connection(write=True) as db:
            fingerprint, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            if parent_run_id:
                self._run(db, parent_run_id)
            db.execute("""INSERT INTO runs
                (id,parent_run_id,created_at,operator,status,input_json,config_json,prompt_json)
                VALUES (?,?,?,?,?,?,?,?)""", (run_id, parent_run_id, now(), operator,
                "generating", dumps(prepared), dumps(config), dumps(prompt)))
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
        if response.metadata.get("finish_reason") in {"length", "content_filter", "tool_calls"}:
            validation["valid"] = False
            validation["errors"].append("模型响应未正常完成，不能进入审批；原始内容已保留。")
        output = validation.get("output")
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

    def _run(self, db, run_id):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ServiceError("运行不存在。")
        run = dict(row)
        for key in ("input", "config", "prompt", "validation", "output", "usage"):
            raw = run.pop(key + "_json")
            run[key] = json.loads(raw) if raw else None
        return run

    def list_runs(self):
        with self.connection() as db:
            rows = db.execute("SELECT id FROM runs ORDER BY created_at DESC").fetchall()
            return [self._run(db, r["id"]) for r in rows]

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
            filename = f"approved-{run_id[:8]}-{export_id[:8]}.{format}"
            snapshot = [{"case_id": c["case_id"], "version": c["version"],
                         "approval_event_id": approvals[c["case_id"]]["id"]} for c in cases]
            db.execute("INSERT INTO exports VALUES (?,?,?,?,?,?,?,?,?,?)", (export_id, run_id, format,
                       filename, mime, operator, now(), content, hashlib.sha256(content).hexdigest(), dumps(snapshot)))
            self._event(db, run_id, "export", operator, "导出批准版本", details={"export_id": export_id})
            self._remember(db, request_id, fingerprint, export_id)
            return self._export(db, export_id)

    def _export(self, db, export_id):
        row = db.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
        if not row:
            raise ServiceError("导出快照不存在。")
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json"))
        return result

    def get_export(self, export_id):
        with self.connection() as db:
            return self._export(db, export_id)

    def mark_interrupted(self, run_id, operator, reason):
        """Explicit recovery; never invalidate another live call on a page refresh."""
        operator, reason = actor_reason(operator, reason)
        with self.connection(write=True) as db:
            if self._run(db, run_id)["status"] != "generating":
                raise ServiceError("此运行不处于生成中。")
            db.execute("UPDATE runs SET status='failed',error=? WHERE id=?", ("生成中断：" + reason, run_id))
            self._event(db, run_id, "generation_interrupted", operator, reason)

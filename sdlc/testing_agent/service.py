"""Durable testing work, with authorization rechecked on every transaction.

The Agent can write drafts and proposals. Human review and export continue through
the existing authenticated service; no model tool is given approval authority.
"""
from __future__ import annotations

import copy
import csv
import difflib
import hashlib
import io
import json
from contextlib import contextmanager

from sdlc.accounts import Accounts, AccessError
from sdlc.domain import OutputModel, prepare_input, validate_current
from sdlc.providers import ModelResponse
from sdlc.secure_service import Service
from sdlc.service import ServiceError, dumps, identifier, now, safe_metadata
from sdlc.testing_agent.domain import (CaseBatch, CoveragePlan, FEEDBACK_ROUTES,
                                      RequirementAnalysis, TASK_STATUSES)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS testing_agent_tasks (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 project_id TEXT NOT NULL REFERENCES projects(id), parent_task_id TEXT,
 owner_id TEXT NOT NULL REFERENCES users(id), confirmer_id TEXT, goal TEXT NOT NULL,
 scope TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
 document_id TEXT NOT NULL, document_version INTEGER NOT NULL,
 input_json TEXT NOT NULL, input_hash TEXT NOT NULL, run_id TEXT,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 next_action TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', due_at TEXT, session_id TEXT,
 budget_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS testing_tasks_project ON testing_agent_tasks(workspace_id,project_id,created_at);
CREATE TABLE IF NOT EXISTS testing_agent_artifacts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id),
 kind TEXT NOT NULL, artifact_key TEXT NOT NULL, input_hash TEXT NOT NULL,
 content_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(task_id,kind,artifact_key,input_hash));
CREATE TABLE IF NOT EXISTS testing_agent_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id), action TEXT NOT NULL,
 actor_id TEXT NOT NULL, created_at TEXT NOT NULL, details_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS testing_agent_questions (
 task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id), id TEXT NOT NULL,
 content_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 answer_json TEXT, confirmed_json TEXT, revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(task_id,id));
CREATE TABLE IF NOT EXISTS testing_agent_proposals (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id),
 kind TEXT NOT NULL, case_id TEXT, source_version INTEGER, source_revision INTEGER,
 content_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 actor_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS testing_agent_baselines (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id),
 run_id TEXT NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS testing_agent_publications (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id),
 output_hash TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(task_id,output_hash));
CREATE TABLE IF NOT EXISTS testing_agent_requirements (
 task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id), local_id TEXT NOT NULL,
 stable_id TEXT NOT NULL, content_hash TEXT NOT NULL, previous_hash TEXT, identity_method TEXT NOT NULL,
 PRIMARY KEY(task_id,local_id));
CREATE TABLE IF NOT EXISTS testing_agent_case_identities (
 task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id), local_id TEXT NOT NULL,
 stable_id TEXT NOT NULL, content_hash TEXT NOT NULL, previous_hash TEXT,
 PRIMARY KEY(task_id,local_id));
'''


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def spreadsheet_text(value):
    """Keep spreadsheet formulas as text when importing/exporting user material."""
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value


class AggregatedProvider:
    """Persist validated real stage outputs without another network request."""
    def __init__(self, output, metadata=None):
        self.output = output
        self.config = {**safe_metadata(metadata or {}), 'provider': 'testing-agent-aggregate',
                       'mode': 'staged-agent', 'real_model_call': False}

    def generate(self, request):
        return ModelResponse(dumps(self.output), {**self.config, 'finish_reason': 'stop'})


class TestingService:
    __test__ = False

    def __init__(self, db_path='data/sdlc.sqlite3', token=None, workspace_id=None, project_id=None):
        self.db_path = str(db_path)
        self.token, self.workspace_id, self.project_id = token, workspace_id, project_id
        # Set only by the trusted composition root for a restricted revision run.
        # HTTP/UI construct the default service, so this does not grant generation.
        self.execution_capability = 'generate'
        self.legacy = Service(db_path, token=token, workspace_id=workspace_id, project_id=project_id)
        self.accounts = self.legacy.accounts
        if not project_id:
            raise ServiceError('请先选择项目。')
        self.legacy.get_project()
        with self.accounts.db(write=True) as db:
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
            if 'session_id' not in {r['name'] for r in db.execute('PRAGMA table_info(testing_agent_tasks)')}:
                db.execute('ALTER TABLE testing_agent_tasks ADD COLUMN session_id TEXT')
            if 'created_by' not in {r['name'] for r in db.execute('PRAGMA table_info(testing_agent_tasks)')}:
                db.execute('ALTER TABLE testing_agent_tasks ADD COLUMN created_by TEXT')
                db.execute("UPDATE testing_agent_tasks SET created_by=COALESCE((SELECT actor_id FROM testing_agent_events e WHERE e.task_id=testing_agent_tasks.id AND e.action='created' ORDER BY seq LIMIT 1),owner_id)")

    @contextmanager
    def connection(self, capability='view', write=False):
        if capability == 'generate' and self.execution_capability == 'regenerate':
            capability = 'regenerate'
        with self.accounts.db(write=write) as db:
            try:
                self.user = Accounts.authorize(db, self.token, self.workspace_id, capability)
            except AccessError as exc:
                raise ServiceError(str(exc)) from None
            row = db.execute('''SELECT p.status FROM projects p JOIN project_workspaces w
                ON w.project_id=p.id WHERE p.id=? AND w.workspace_id=?''',
                (self.project_id, self.workspace_id)).fetchone()
            if not row or (capability != 'view' and row['status'] != 'active'):
                raise ServiceError('项目不存在、已归档或无权访问。')
            yield db

    def _task(self, db, task_id):
        row = db.execute('''SELECT * FROM testing_agent_tasks
            WHERE id=? AND workspace_id=? AND project_id=?''',
            (task_id, self.workspace_id, self.project_id)).fetchone()
        if not row:
            raise ServiceError('测试任务不存在或无权访问。')
        result = dict(row)
        result['input'] = json.loads(result.pop('input_json'))
        result['budget'] = json.loads(result.pop('budget_json'))
        result['document'] = result['input']
        result['created_by'] = result.get('created_by') or result['owner_id']
        return result

    def _event(self, db, task_id, action, details):
        db.execute('INSERT INTO testing_agent_events(id,task_id,action,actor_id,created_at,details_json) VALUES(?,?,?,?,?,?)',
                   (identifier(), task_id, action, self.user['id'], now(), dumps(details)))
        Accounts._audit(db, self.user, 'testing_agent_' + action, self.workspace_id,
                        task_id=task_id, project_id=self.project_id)

    def _active(self, task):
        if task['status'] in {'cancelled', 'completed'}:
            raise ServiceError('任务已取消或完成；请创建关联新任务。')

    def _member(self, db, user_id):
        if not db.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?',
                          (self.workspace_id, user_id)).fetchone():
            raise ServiceError('负责人必须是当前工作空间的成员。')

    def create_task(self, text, goal='', scope='', parent_task_id=None, owner_id=None,
                    confirmer_id=None, due_at=None, budget=None, resource_ids=None):
        try:
            prepared = prepare_input(text=text)
        except ValueError as exc:
            raise ServiceError(str(exc)) from None
        return self._create_task(prepared, goal, scope, parent_task_id, owner_id,
                                 confirmer_id, due_at, budget, resource_ids)

    def create_conversation(self, content, instruction='', **options):
        """Persist the first message and its job atomically, without a fake PRD."""
        from sdlc.testing_agent.chat import ChatService, _text
        from sdlc.testing_agent.worker import initialize
        content = _text(content, 20_000)
        instruction = _text(instruction, 20_000)
        if not content.strip():
            raise ServiceError('请输入消息或上传需求文档。')
        initialize(self.db_path)
        ChatService(self)
        prepared = {'kind': 'conversation', 'original_text': '', 'normalized_text': '',
                    'input_hash': hashlib.sha256(b'').hexdigest(), 'paragraphs': [],
                    'summary': {'title': '测试协作对话', 'character_count': 0, 'preview': ''}}
        return self._create_task(prepared, initial_message=content,
                                 initial_instruction=instruction, **options)

    def _create_task(self, prepared, goal='', scope='', parent_task_id=None, owner_id=None,
                     confirmer_id=None, due_at=None, budget=None, resource_ids=None,
                     initial_message=None, initial_instruction=''):
        if len(goal) > 2000 or len(scope) > 4000:
            raise ServiceError('目标最多 2,000 字符，范围最多 4,000 字符。')
        if due_at:
            from datetime import date
            try:
                date.fromisoformat(due_at)
            except (TypeError, ValueError):
                raise ServiceError('截止日期应为 YYYY-MM-DD。') from None
        resources = None
        if resource_ids is not None:
            from sdlc.testing_agent.resources import ResourceLibrary
            resources = ResourceLibrary(self.db_path, self.token, self.workspace_id, self.project_id).snapshot(resource_ids)
        with self.connection('generate', True) as db:
            task_id = self._insert_task(db, prepared, goal, scope, parent_task_id, owner_id,
                                        confirmer_id, due_at, budget, resources)
            if initial_message is not None:
                from sdlc.testing_agent.chat import ChatService
                from sdlc.testing_agent.worker import _enqueue_in_db
                if initial_instruction.strip() and initial_instruction != initial_message:
                    chat = object.__new__(ChatService)
                    chat.service = self
                    chat._post_user_db(db, task_id, initial_instruction, 'initial-instruction')
                _enqueue_in_db(self, db, task_id, 'message', initial_message,
                               'initial-' + task_id, record_chat=True)
            return task_id

    def _insert_task(self, db, prepared, goal, scope, parent_task_id, owner_id=None,
                     confirmer_id=None, due_at=None, budget=None, resources=None):
        conversation = prepared.get('kind') == 'conversation'
        task_id, document_id, version = identifier(), identifier(), 0 if conversation else 1
        parent = self._task(db, parent_task_id) if parent_task_id else None
        if parent:
            document_id = parent['document_id']
            version = db.execute('SELECT COALESCE(MAX(document_version),0)+1 FROM testing_agent_tasks WHERE document_id=?',
                                 (document_id,)).fetchone()[0]
        owner_id = owner_id or self.user['id']
        self._member(db, owner_id)
        if confirmer_id:
            self._member(db, confirmer_id)
            role = db.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',
                              (self.workspace_id, confirmer_id)).fetchone()['role']
            if role not in {'owner', 'admin'}:
                raise ServiceError('需求确认人必须为当前空间所有者或管理员。')
        old_blocks = parent['input']['paragraphs'] if parent else []
        used = set()
        for block in prepared['paragraphs']:
            old = next((p for p in old_blocks if p['text'] == block['text'] and p.get('stable_id') not in used), None)
            block['stable_id'] = old['stable_id'] if old else identifier()
            block['content_hash'] = fingerprint(block['text'])
            used.add(block['stable_id'])
        db.execute('''INSERT INTO testing_agent_tasks
            (id,workspace_id,project_id,parent_task_id,owner_id,confirmer_id,goal,scope,status,stage,
             document_id,document_version,input_json,input_hash,created_at,updated_at,due_at,budget_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (task_id, self.workspace_id, self.project_id, parent_task_id, owner_id, confirmer_id,
             goal.strip() or ('测试协作对话' if conversation else '完成本次明确需求的测试准备'),
             scope.strip() or ('' if conversation else '本次 PRD 的全部明确需求'),
             'queued', 'conversation' if conversation else 'analysis', document_id, version, dumps(prepared), prepared['input_hash'],
             now(), now(), due_at, dumps(budget or {})))
        db.execute('UPDATE testing_agent_tasks SET created_by=? WHERE id=?', (self.user['id'], task_id))
        if resources is None and parent:
            previous = db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='project_resources' AND artifact_key='default'",
                                  (parent_task_id,)).fetchone()
            resources = json.loads(previous['content_json'])['resources'] if previous else None
        if resources is not None:
            db.execute('INSERT INTO testing_agent_artifacts VALUES(?,?,?,?,?,?,?)',
                (identifier(), task_id, 'project_resources', 'default', prepared['input_hash'],
                 dumps({'resources': resources, 'notice': '共享测试准备资料，不是业务事实；使用前仍须核对需求与评审。'}), now()))
        self._event(db, task_id, 'created', {'parent_task_id': parent_task_id, 'document_version': version})
        return task_id

    def _refine_task(self, db, task_id, content):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
            raise ServiceError('请用 1–2,000 字符说明本轮测试目标或范围。')
        parent = self._task(db, task_id)
        self._active(parent)
        prepared = prepare_input(text=parent['input']['normalized_text'])
        child = self._insert_task(db, prepared, content.strip(), content.strip(), task_id,
                                  parent['owner_id'], parent['confirmer_id'], parent['due_at'])
        self._event(db, task_id, 'chat_scope_refined', {'child_task_id': child,
                    'instruction': content.strip(), 'business_facts_changed': False})
        return child

    def refine_task(self, task_id, content):
        with self.connection('generate', True) as db:
            return self._refine_task(db, task_id, content)

    def reanalyze_task(self, task_id, reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ServiceError('请说明重新分析当前需求的原因。')
        from sdlc.testing_agent.chat import ChatService
        from sdlc.testing_agent.worker import initialize, _enqueue_in_db
        initialize(self.db_path)
        ChatService(self)
        with self.connection('generate', True) as db:
            task = self._task(db, task_id)
            self._active(task)
            prior = db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='handoff' AND artifact_key='reanalysis-r1'", (task_id,)).fetchone()
            if prior:
                return json.loads(prior['content_json'])['child_task_id']
            prepared = prepare_input(text=task['input']['normalized_text'])
            child = self._insert_task(db, prepared, task['goal'], task['scope'], task_id,
                task['owner_id'], task['confirmer_id'], task['due_at'])
            _enqueue_in_db(self, db, child, 'start', '', 'reanalysis-' + task_id)
            db.execute('INSERT INTO testing_agent_artifacts VALUES(?,?,?,?,?,?,?)',
                (identifier(), task_id, 'handoff', 'reanalysis-r1', task['input_hash'],
                 dumps({'child_task_id': child, 'intent': 'reanalyze', 'reason': reason.strip()}), now()))
            db.execute("UPDATE testing_agent_tasks SET status='partial',error='',next_action=?,updated_at=?,revision=revision+1 WHERE id=?",
                ('已转入关联需求版本重新分析；原始结果与审核历史保留。', now(), task_id))
            self._event(db, task_id, 'agent_reanalysis_requested', {'child_task_id': child, 'reason': reason.strip()})
            return child

    def list_tasks(self, limit=100, offset=0):
        if not 1 <= limit <= 500 or offset < 0:
            raise ServiceError('分页参数无效。')
        with self.connection() as db:
            return [self._task(db, r['id']) for r in db.execute('''SELECT id FROM testing_agent_tasks
                WHERE workspace_id=? AND project_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?''',
                (self.workspace_id, self.project_id, limit, offset))]

    def get_task(self, task_id):
        with self.connection() as db:
            task = self._task(db, task_id)
            task['artifacts'] = [{**dict(r), 'content': json.loads(r['content_json'])} for r in db.execute(
                'SELECT * FROM testing_agent_artifacts WHERE task_id=? ORDER BY created_at,id', (task_id,))]
            task['questions'] = [self._question(r) for r in db.execute(
                'SELECT * FROM testing_agent_questions WHERE task_id=? ORDER BY id', (task_id,))]
            task['events'] = [{**dict(r), 'details': json.loads(r['details_json'])} for r in db.execute(
                'SELECT * FROM testing_agent_events WHERE task_id=? ORDER BY seq', (task_id,))]
            task['proposals'] = [{**dict(r), 'content': json.loads(r['content_json'])} for r in db.execute(
                'SELECT * FROM testing_agent_proposals WHERE task_id=? ORDER BY created_at,id', (task_id,))]
            task['baselines'] = [{**dict(r), 'snapshot': json.loads(r['snapshot_json'])} for r in db.execute(
                'SELECT * FROM testing_agent_baselines WHERE task_id=? ORDER BY created_at,id', (task_id,))]
            task['requirement_identities'] = [dict(r) for r in db.execute('SELECT * FROM testing_agent_requirements WHERE task_id=? ORDER BY local_id', (task_id,))]
            task['case_identities'] = [dict(r) for r in db.execute('SELECT * FROM testing_agent_case_identities WHERE task_id=? ORDER BY local_id', (task_id,))]
        return task

    @staticmethod
    def _question(row):
        result = dict(row)
        for key in ('content', 'answer', 'confirmed'):
            result[key] = json.loads(result.pop(key + '_json')) if result[key + '_json'] else None
        return result

    def set_state(self, task_id, status, stage=None, next_action='', error='', expected_revision=None):
        if status not in TASK_STATUSES:
            raise ServiceError('无效任务状态。')
        with self.connection('generate', True) as db:
            task = self._task(db, task_id)
            self._active(task)
            if status == 'completed':
                raise ServiceError('完成状态必须由批准基线门禁产生。')
            if expected_revision is not None and task['revision'] != expected_revision:
                raise ServiceError('任务已更新，请刷新。')
            db.execute('''UPDATE testing_agent_tasks SET status=?,stage=?,next_action=?,error=?,
                updated_at=?,revision=revision+1 WHERE id=?''',
                (status, stage or task['stage'], next_action[:2000], error[:2000], now(), task_id))
            self._event(db, task_id, 'state_changed', {'status': status, 'stage': stage or task['stage']})

    def cancel_task(self, task_id):
        self.set_state(task_id, 'cancelled', next_action='任务已取消；已有成果与历史记录保留。')

    def set_status(self, task_id, status, stage=None, error=None):
        return self.set_state(task_id, status, stage=stage, error=error or '')

    def bind_session(self, task_id, session_id):
        with self.connection('generate', True) as db:
            task = self._task(db, task_id)
            self._active(task)
            db.execute('UPDATE testing_agent_tasks SET session_id=?,revision=revision+1,updated_at=? WHERE id=?',
                       (session_id, now(), task_id))
            self._event(db, task_id, 'session_bound', {'session_id': session_id})

    def record_event(self, task_id, event):
        with self.connection('generate', True) as db:
            self._task(db, task_id)
            self._event(db, task_id, str(event.get('type', 'agent_event'))[:80], event)

    def resume_task(self, task_id):
        with self.connection('generate', True) as db:
            task = self._task(db, task_id)
            if task['status'] == 'completed':
                raise ServiceError('完成的任务不能重跑，请创建变更任务。')
            db.execute("UPDATE testing_agent_tasks SET status='queued',error='',revision=revision+1,updated_at=? WHERE id=?", (now(), task_id))
            self._event(db, task_id, 'resumed', {'input_hash': task['input_hash']})

    def add_artifact(self, task_id, kind, content, key='default', input_hash=None):
        if not kind or len(kind) > 64 or not key or len(key) > 200:
            raise ServiceError('产物类型或标识无效。')
        with self.connection('generate', True) as db:
            task = self._task(db, task_id)
            if not (kind == 'model_attempt' and task['status'] == 'completed'):
                self._active(task)
            if input_hash is not None and input_hash != task['input_hash']:
                raise ServiceError('任务输入版本已改变，旧结果已拒绝写入。')
            old = db.execute('''SELECT * FROM testing_agent_artifacts WHERE task_id=? AND kind=?
                AND artifact_key=? AND input_hash=?''', (task_id, kind, key, task['input_hash'])).fetchone()
            if old:
                if json.loads(old['content_json']) != content:
                    raise ServiceError('该阶段已有不同成果；请使用新的尝试标识或创建新任务。')
                return old['id']
            artifact_id = identifier()
            db.execute('INSERT INTO testing_agent_artifacts VALUES(?,?,?,?,?,?,?)',
                       (artifact_id, task_id, kind, key, task['input_hash'], dumps(content), now()))
            if task['status'] != 'completed':
                db.execute('UPDATE testing_agent_tasks SET updated_at=?,revision=revision+1 WHERE id=?', (now(), task_id))
            self._event(db, task_id, 'artifact_saved', {'artifact_id': artifact_id, 'kind': kind, 'key': key})
            return artifact_id

    def artifact(self, task_id, kind, key='default'):
        with self.connection() as db:
            task = self._task(db, task_id)
            row = db.execute('SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind=? AND artifact_key=? AND input_hash=?',
                             (task_id, kind, key, task['input_hash'])).fetchone()
            return json.loads(row['content_json']) if row else None

    def save_analysis(self, task_id, content):
        analysis = RequirementAnalysis.model_validate(content).model_dump()
        task = self.get_task(task_id)
        # Validate all cross references, including non-clear rules and questions.
        snapshot = {**{k: analysis[k] for k in ('features', 'roles', 'business_rules', 'clarifications')},
                    'schema_version': '1.0', 'test_cases': []}
        for q in snapshot['clarifications']:
            q['scope'], q['affected_case_ids'] = 'run', []
        errors = validate_current(snapshot, task['input']['paragraphs'])
        if errors:
            raise ServiceError('需求分析校验失败：' + '; '.join(errors))
        analysis['clarifications'] = snapshot['clarifications']
        self.add_artifact(task_id, 'analysis', analysis)
        with self.connection('generate', True) as db:
            current = self._task(db, task_id)
            self._active(current)
            parent_rules = {}
            if current['parent_task_id']:
                parent_rules = {r['local_id']: dict(r) for r in db.execute(
                    'SELECT * FROM testing_agent_requirements WHERE task_id=?', (current['parent_task_id'],))}
            blocks = {p['id']: p['content_hash'] for p in current['input']['paragraphs']}
            for rule in analysis['business_rules']:
                content_hash = fingerprint({k: v for k, v in {**rule, 'paragraph_ids': [blocks[p] for p in rule['paragraph_ids']]}.items() if k != 'id'})
                exact = next((r for r in parent_rules.values() if r['content_hash'] == content_hash), None)
                previous = exact or parent_rules.get(rule['id'])
                db.execute('INSERT OR IGNORE INTO testing_agent_requirements VALUES(?,?,?,?,?,?)',
                    (task_id, rule['id'], previous['stable_id'] if previous else identifier(), content_hash,
                     previous['content_hash'] if previous else None,
                     'exact_evidence' if exact else ('continued_id_requires_impact_review' if previous else 'new')))
            for q in analysis['clarifications']:
                db.execute('INSERT OR IGNORE INTO testing_agent_questions(task_id,id,content_json) VALUES(?,?,?)',
                           (task_id, q['id'], dumps(q)))
        return analysis

    def save_plan(self, task_id, content):
        plan = CoveragePlan.model_validate(content).model_dump()
        task, analysis = self.get_task(task_id), self.artifact(task_id, 'analysis')
        if not analysis:
            raise ServiceError('请先完成需求分析。')
        rules = {r['id'] for r in analysis['business_rules']}
        paragraphs = {p['id'] for p in task['input']['paragraphs']}
        seen = set()
        for condition in plan['conditions']:
            if condition['id'] in seen or not set(condition['requirement_ids']) <= rules or not set(condition['paragraph_ids']) <= paragraphs:
                raise ServiceError('测试条件存在重复编号或无效需求引用。')
            if condition['status'] == 'out_of_scope':
                raise ServiceError('范围排除需要人工确认，Agent 不能自行排除需求。')
            seen.add(condition['id'])
        self.add_artifact(task_id, 'coverage_plan', plan)
        self.reuse_unchanged_drafts(task_id)
        return plan

    def reuse_unchanged_drafts(self, task_id):
        """Continue confirmed-supplement work without regenerating unaffected cases.

        This carries drafts only, never approvals. Reuse requires exact rule,
        condition and source content matches, with paragraph-ID remapping.
        """
        task = self.get_task(task_id)
        continuation = self.artifact(task_id, 'continuation')
        if not task['parent_task_id'] or not continuation:
            return []
        reused = [a['content'] for a in task['artifacts'] if a['kind'] == 'case_batch' and a['artifact_key'].startswith('reused-parent-')]
        if reused:
            return [d for batch in reused for d in batch['cases']]
        parent = self.get_task(task['parent_task_id'])
        old_analysis = self.artifact(parent['id'], 'analysis')
        analysis, plan = self.artifact(task_id, 'analysis'), self.artifact(task_id, 'coverage_plan')
        old_plan = self.artifact(parent['id'], 'coverage_plan')
        if not all((old_analysis, analysis, plan, old_plan)):
            return []
        old_blocks = {p['id']: p for p in parent['input']['paragraphs']}
        new_blocks = {p['stable_id']: p for p in task['input']['paragraphs']}
        remap = {pid: new_blocks[p['stable_id']]['id'] for pid, p in old_blocks.items() if p['stable_id'] in new_blocks}
        new_rules = {r['id']: r for r in analysis['business_rules']}
        old_rules = {r['id']: r for r in old_analysis['business_rules']}
        changed_rules = set(continuation['affected_requirement_ids'])
        changed_cases = set(continuation.get('affected_case_ids', []))
        valid_rules = set()
        for rid, rule in old_rules.items():
            proposed = copy.deepcopy(rule)
            if not set(rule['paragraph_ids']) <= set(remap):
                continue
            proposed['paragraph_ids'] = [remap[p] for p in rule['paragraph_ids']]
            if rid not in changed_rules and new_rules.get(rid) == proposed:
                valid_rules.add(rid)
        new_conditions = {c['id']: c for c in plan['conditions']}
        old_conditions = {c['id']: c for c in old_plan['conditions']}
        kept = []
        # Prefer latest manually edited versions over stale original model drafts.
        current = {c['case_id']: c['content'] for c in self.legacy.get_run(parent['run_id'])['cases']} if parent['run_id'] else {}
        for draft in self.case_drafts(parent['id']):
            candidate = copy.deepcopy(draft)
            candidate['case'] = copy.deepcopy(current.get(draft['case']['case_id'], draft['case']))
            c = candidate['case']
            if c['case_id'] in changed_cases:
                continue
            if not set(c['requirement_ids']) <= valid_rules or not set(c['paragraph_ids']) <= set(remap):
                continue
            compatible = True
            for cid in candidate['condition_ids']:
                old_condition = copy.deepcopy(old_conditions.get(cid))
                if not old_condition or not set(old_condition['paragraph_ids']) <= set(remap):
                    compatible = False
                    break
                old_condition['paragraph_ids'] = [remap[p] for p in old_condition['paragraph_ids']]
                if new_conditions.get(cid) != old_condition:
                    compatible = False
                    break
            if not compatible:
                continue
            c['paragraph_ids'] = [remap[p] for p in c['paragraph_ids']]
            c['clarification_ids'], c['readiness'] = [], 'ready'
            kept.append(candidate)
        if kept:
            for index in range(0, len(kept), 12):
                self.save_batch(task_id, f'reused-parent-{index // 12}', {'cases': kept[index:index + 12]})
            with self.connection('generate', True) as db:
                self._event(db, task_id, 'unchanged_drafts_reused', {'source_task_id': parent['id'],
                    'case_ids': [d['case']['case_id'] for d in kept], 'approvals_carried': False})
        return kept

    def save_batch(self, task_id, batch_key, content):
        batch = CaseBatch.model_validate(content).model_dump()
        task, analysis, plan = self.get_task(task_id), self.artifact(task_id, 'analysis'), self.artifact(task_id, 'coverage_plan')
        if not analysis or not plan:
            raise ServiceError('请先分析需求并制定覆盖计划。')
        conditions = {c['id']: c for c in plan['conditions']}
        existing = self.case_drafts(task_id)
        previous = self.artifact(task_id, 'case_batch', batch_key)
        prior_ids = {d['case']['case_id'] for d in previous['cases']} if previous else set()
        other_ids = {d['case']['case_id'] for d in existing} - prior_ids
        ids = [d['case']['case_id'] for d in batch['cases']]
        if len(set(ids)) != len(ids) or set(ids) & other_ids:
            raise ServiceError('用例编号与已有批次重复。')
        for draft in batch['cases']:
            if not set(draft['condition_ids']) <= set(conditions):
                raise ServiceError('用例关联了不存在的测试条件。')
            expected_rules = set().union(*(set(conditions[i]['requirement_ids']) for i in draft['condition_ids']))
            if not expected_rules <= set(draft['case']['requirement_ids']):
                raise ServiceError('用例未关联测试条件所需的需求。')
        aggregate = self._aggregate(task_id, analysis, [d for d in existing if d['case']['case_id'] not in prior_ids] + batch['cases'])
        errors = validate_current(aggregate, task['input']['paragraphs'])
        if errors:
            raise ServiceError('用例批次校验失败：' + '; '.join(errors))
        # Store computed blockers, which are deterministic and not a model decision.
        mapped = {c['case_id']: c for c in aggregate['test_cases']}
        for draft in batch['cases']:
            draft['case'] = mapped[draft['case']['case_id']]
        self.add_artifact(task_id, 'case_batch', batch, batch_key)
        with self.connection('generate', True) as db:
            current = self._task(db, task_id)
            self._active(current)
            for draft in batch['cases']:
                content = draft['case']
                old = db.execute('SELECT * FROM testing_agent_case_identities WHERE task_id=? AND local_id=?',
                                 (current['parent_task_id'], content['case_id'])).fetchone() if current['parent_task_id'] else None
                db.execute('INSERT OR IGNORE INTO testing_agent_case_identities VALUES(?,?,?,?,?)',
                    (task_id, content['case_id'], old['stable_id'] if old else identifier(), fingerprint(content),
                     old['content_hash'] if old else None))
        return batch

    def case_drafts(self, task_id):
        task = self.get_task(task_id)
        result = []
        for artifact in task['artifacts']:
            if artifact['kind'] == 'case_batch' and artifact['input_hash'] == task['input_hash']:
                result.extend(artifact['content']['cases'])
        return result

    def _aggregate(self, task_id, analysis=None, drafts=None):
        analysis = analysis or self.artifact(task_id, 'analysis')
        if not analysis:
            raise ServiceError('尚无需求分析。')
        drafts = self.case_drafts(task_id) if drafts is None else drafts
        task = self.get_task(task_id)
        current_cases = {c['case_id']: c['content'] for c in self.legacy.get_run(task['run_id'])['cases']} if task['run_id'] else {}
        output = {'schema_version': '1.0', **{k: copy.deepcopy(analysis[k]) for k in
                  ('features', 'roles', 'business_rules', 'clarifications')},
                  'test_cases': [copy.deepcopy(current_cases.get(d['case']['case_id'], d['case'])) for d in drafts]}
        questions = task['questions']
        if questions:
            output['clarifications'] = [copy.deepcopy(q['content']) for q in questions if q['status'] != 'false_positive']
            for rule in output['business_rules']:
                if rule['status'] != 'clear' and not any(rule['id'] in q['requirement_ids'] for q in output['clarifications']):
                    rule['status'] = 'clear'
        # Keep a partial reviewable projection: unanswered local requirements with
        # no generated cases stay in the task's question/coverage views, rather
        # than turning every unrelated case into a run-wide blocker.
        referenced_rules = {r for c in output['test_cases'] for r in c['requirement_ids']}
        case_ids = {c['case_id'] for c in output['test_cases']}
        output['clarifications'] = [q for q in output['clarifications']
            if (not q['requirement_ids'] or referenced_rules & set(q['requirement_ids']))
            and (q['scope'] != 'case' or not q['affected_case_ids'] or case_ids & set(q['affected_case_ids']))]
        output['business_rules'] = [r for r in output['business_rules']
            if r['status'] == 'clear' or r['id'] in referenced_rules]
        for q in output['clarifications']:
            affected = ([cid for cid in q['affected_case_ids'] if cid in case_ids]
                if q['scope'] == 'case' and q['affected_case_ids'] else
                [c['case_id'] for c in output['test_cases'] if set(c['requirement_ids']) & set(q['requirement_ids'])])
            q['affected_case_ids'] = affected
            q['scope'] = 'case' if affected else 'run'
        for c in output['test_cases']:
            blockers = [q['id'] for q in output['clarifications'] if q['blocking'] and
                        (c['case_id'] in q['affected_case_ids'] or (q['scope'] == 'run' and not q['requirement_ids'])
                         or set(c['requirement_ids']) & set(q['requirement_ids']))]
            c['clarification_ids'] = blockers
            c['readiness'] = 'needs_clarification' if blockers else 'ready'
        # Preserve validator's run-wide semantics when a local dependency has no case yet.
        for q in output['clarifications']:
            if q['scope'] == 'run' and q['blocking']:
                for c in output['test_cases']:
                    if q['id'] not in c['clarification_ids']:
                        c['clarification_ids'].append(q['id'])
                        c['readiness'] = 'needs_clarification'
        return OutputModel.model_validate(output).model_dump()

    def coverage(self, task_id):
        analysis, plan, drafts = self.artifact(task_id, 'analysis'), self.artifact(task_id, 'coverage_plan'), self.case_drafts(task_id)
        conditions = (plan or {}).get('conditions', [])
        covered = {i for d in drafts for i in d['condition_ids']}
        rule_ids = {r['id'] for r in (analysis or {}).get('business_rules', [])}
        linked = {r for d in drafts for r in d['case']['requirement_ids']}
        return {'label': '已识别规则的关联覆盖（非独立质量标准）', 'identified_rules': len(rule_ids),
                'linked_rules': len(rule_ids & linked), 'missing_rule_ids': sorted(rule_ids - linked),
                'conditions': [{**c, 'case_ids': [d['case']['case_id'] for d in drafts if c['id'] in d['condition_ids']],
                                'covered': c['id'] in covered} for c in conditions],
                'missing_condition_ids': [c['id'] for c in conditions if c['id'] not in covered]}

    def finalize_design(self, task_id, metadata=None):
        return self.publish_partial(task_id, metadata)

    def publish_partial(self, task_id, metadata=None):
        """Publish each validated batch for review, retaining same-document decisions.

        A publication is an immutable run. Only byte-identical effective case
        content and dependencies can inherit a previous human decision under the
        fixed same-task/same-document policy recorded in the publication event.
        """
        task = self.get_task(task_id)
        output = self._aggregate(task_id)
        if not output['test_cases']:
            raise ServiceError('尚无可评审用例，请继续生成明确需求。')
        errors = validate_current(output, task['input']['paragraphs'])
        if errors:
            raise ServiceError('汇总校验失败：' + '; '.join(errors))
        self._active(task)
        output_hash = fingerprint(output)
        source_run = self.legacy.get_run(task['run_id']) if task['run_id'] else None
        with self.connection() as db:
            if source_run and self.legacy._effective(db, source_run['id']) == output:
                # Human edits change run revisions, but publishing an unchanged
                # current collection must not manufacture another review cycle.
                return source_run['id']
            prior = db.execute('SELECT run_id FROM testing_agent_publications WHERE task_id=? AND output_hash=?',
                               (task_id, output_hash)).fetchone()
            if prior:
                return prior['run_id']
        parent = self.get_task(task['parent_task_id']) if task['parent_task_id'] else None
        coverage = self.coverage(task_id)
        complete = not coverage['missing_condition_ids'] and not coverage['missing_rule_ids']
        run_id = self.legacy.generate(text=task['input']['normalized_text'], operator='',
            provider=AggregatedProvider(output, metadata), request_id='testing-publish-' + task_id + '-' + output_hash[:24],
            parent_run_id=task['run_id'] or (parent['run_id'] if parent else None))
        if self.legacy.get_run(run_id)['status'] != 'valid':
            raise ServiceError('分批汇总未通过校验，请检查已保存的生成结果。')
        with self.connection('generate', True) as db:
            current = self._task(db, task_id)
            self._active(current)
            # A concurrent publisher or human editor must win over a stale snapshot.
            prior = db.execute('SELECT run_id FROM testing_agent_publications WHERE task_id=? AND output_hash=?',
                               (task_id, output_hash)).fetchone()
            if prior:
                return prior['run_id']
            if current['run_id'] != task['run_id'] or current['input_hash'] != task['input_hash']:
                raise ServiceError('任务已有较新发布，旧结果未覆盖，请重试发布当前成果。')
            if source_run:
                live = self.legacy._run(db, source_run['id'])
                if live['revision'] != source_run['revision']:
                    raise ServiceError('发布期间人工已更新用例，旧结果未覆盖；请重新发布。')
                old_rules = {r['id']: r for r in self.legacy._effective(db, source_run['id'])['business_rules']}
                new_rules = {r['id']: r for r in output['business_rules']}
                old_cases = {c['case_id']: c for c in self.legacy._cases(db, source_run['id'])}
                self.legacy.user = self.user
                for candidate in self.legacy._cases(db, run_id):
                    previous = old_cases.get(candidate['case_id'])
                    if not previous or previous['status'] not in {'approved', 'returned'} or previous['content'] != candidate['content']:
                        continue
                    if any(old_rules.get(rid) != new_rules.get(rid) for rid in candidate['content']['requirement_ids']):
                        continue
                    # New global blockers would change readiness/content above and prevent inheritance.
                    source_event = db.execute('''SELECT id,operator,action FROM events WHERE run_id=? AND case_id=? AND version=?
                        AND after_status=? ORDER BY seq DESC LIMIT 1''',
                        (source_run['id'], previous['case_id'], previous['version'], previous['status'])).fetchone()
                    if not source_event:
                        continue
                    db.execute('UPDATE case_heads SET status=?,revision=revision+1 WHERE run_id=? AND case_id=?',
                               (previous['status'], run_id, candidate['case_id']))
                    self.legacy._event(db, run_id, 'same_document_decision_inherited', self.user['id'],
                        '同任务同需求版本，内容与需求依赖完全一致，沿用已有人工设计评审决定。',
                        candidate['case_id'], candidate['version'], 'pending_review', previous['status'],
                        {'policy': 'same-task-same-document-exact-content-v1', 'source_run_id': source_run['id'],
                         'source_case_version': previous['version'], 'source_event_id': source_event['id'],
                         'source_actor_id': source_event['operator'], 'source_action': source_event['action']})
                    db.execute('''UPDATE testing_agent_proposals SET source_version=?,source_revision=?
                        WHERE task_id=? AND case_id=? AND status='pending' AND source_version=? AND source_revision=?''',
                        (candidate['version'], candidate['revision'] + 1, task_id, candidate['case_id'],
                         previous['version'], previous['revision']))
            db.execute('INSERT INTO testing_agent_publications VALUES(?,?,?,?,?)',
                       (run_id, task_id, output_hash, int(complete), now()))
            db.execute("UPDATE testing_agent_tasks SET run_id=?,status=?,stage='review',next_action=?,revision=revision+1,updated_at=? WHERE id=?",
                       (run_id, 'waiting_review' if complete else 'partial',
                        '评审当前用例；Agent 将继续补充剩余范围。' if not complete else '评审明确用例，并处理待澄清问题。', now(), task_id))
            self._event(db, task_id, 'design_ready', {'run_id': run_id, 'complete': complete,
                'policy': 'same-task-same-document-exact-content-v1', 'source_run_id': task['run_id']})
        return run_id

    def submit_answer(self, task_id, question_id, answer='', disposition='answer', assignee_id=None, expected_revision=None):
        if disposition not in {'answer', 'unknown', 'needs_confirmation', 'false_positive'}:
            raise ServiceError('无效问题处理方式。')
        if disposition in {'answer', 'false_positive'} and not 1 <= len(answer.strip()) <= 4000:
            raise ServiceError('请提供 1–4,000 字符的答复或原文依据。')
        with self.connection('generate', True) as db:
            task = self._task(db, task_id)
            self._active(task)
            q = db.execute('SELECT * FROM testing_agent_questions WHERE task_id=? AND id=?', (task_id, question_id)).fetchone()
            if not q or q['status'] == 'confirmed' or (expected_revision is not None and q['revision'] != expected_revision):
                raise ServiceError('问题已更新或不存在，请刷新。')
            if assignee_id:
                self._member(db, assignee_id)
            content = {'text': answer.strip(), 'disposition': disposition, 'actor_id': self.user['id'],
                       'created_at': now(), 'assignee_id': assignee_id}
            db.execute("UPDATE testing_agent_questions SET answer_json=?,status='answered',revision=revision+1 WHERE task_id=? AND id=?",
                       (dumps(content), task_id, question_id))
            self._event(db, task_id, 'answer_submitted', {'question_id': question_id, 'disposition': disposition})
            return content

    def confirm_answer(self, task_id, question_id, reason, expected_revision=None):
        if not reason.strip() or len(reason) > 2000:
            raise ServiceError('请填写需求确认说明。')
        # A new document/task is created, never mutating the input of an executing task.
        with self.connection('manage', True) as db:
            return self._confirm_answers(db, task_id, {question_id: expected_revision}, reason)

    def _confirm_answers(self, db, task_id, question_revisions, reason):
        task = self._task(db, task_id)
        self._active(task)
        if task['confirmer_id'] and self.user['id'] != task['confirmer_id']:
            raise ServiceError('仅本任务指定的需求确认人可以确认该答复。')
        questions = [self._question(row) for row in db.execute(
            'SELECT * FROM testing_agent_questions WHERE task_id=? ORDER BY id', (task_id,))]
        lookup = {q['id']: q for q in questions}
        selected = []
        for question_id, revision in question_revisions.items():
            q = lookup.get(question_id)
            if not q or q['status'] not in {'answered', 'confirmed'}:
                raise ServiceError('答复尚未提交，或问题已经更新；请查看当前问题后重试。')
            if q['status'] != 'confirmed' and revision is not None and q['revision'] != revision:
                raise ServiceError('这项答复已被更新，旧答复未被确认；请核对最新答复后重试。')
            if not q['answer'] or q['answer']['disposition'] != 'answer':
                raise ServiceError('未知、待确认或误报答复不能作为新业务事实确认。')
            selected.append(q)
        children = {q['confirmed']['child_task_id'] for q in selected if q['status'] == 'confirmed'}
        if all(q['status'] == 'confirmed' for q in selected) and len(children) == 1:
            return children.pop()
        # Include facts previously confirmed on this source task, so confirming a
        # later question never creates a sibling version that loses earlier facts.
        supplements = [q for q in questions if q['status'] == 'confirmed' or q['id'] in question_revisions]
        text = task['input']['normalized_text'] + '\n\n## 已确认需求补充\n\n' + '\n\n'.join(
            '### ' + q['id'] + '：' + q['content']['question'] + '\n\n' + q['answer']['text'] for q in supplements)
        try:
            prepared = prepare_input(text=text)
        except ValueError as exc:
            raise ServiceError('补充未保存：' + str(exc)) from None
        child = self._insert_task(db, prepared, task['goal'], task['scope'], task_id,
                                 task['owner_id'], task['confirmer_id'], task['due_at'])
        confirmed_answers = []
        for q in selected:
            confirmed = {'answer': q['answer'], 'reason': reason.strip(), 'actor_id': self.user['id'],
                         'created_at': now(), 'child_task_id': child}
            confirmed_answers.append({'question_id': q['id'], 'question': q['content']['question'], **confirmed})
            if q['status'] != 'confirmed':
                db.execute("UPDATE testing_agent_questions SET status='confirmed',confirmed_json=?,revision=revision+1 WHERE task_id=? AND id=?",
                           (dumps(confirmed), task_id, q['id']))
                self._event(db, task_id, 'requirement_confirmed', {'question_id': q['id'], **confirmed})
        # Keep outstanding questions and submitted-but-unconfirmed answers visible
        # in the new version. They are not appended to the PRD as business facts.
        for q in questions:
            if q['id'] in question_revisions or q['status'] == 'confirmed':
                continue
            db.execute('INSERT INTO testing_agent_questions(task_id,id,content_json,status,answer_json,confirmed_json,revision) VALUES(?,?,?,?,?,?,?)',
                (child, q['id'], dumps(q['content']), q['status'], dumps(q['answer']) if q['answer'] else None,
                 dumps(q['confirmed']) if q['confirmed'] else None, q['revision']))
        supplement = {'answers': confirmed_answers, 'question_ids': [q['id'] for q in selected]}
        if len(confirmed_answers) == 1:
            supplement.update(confirmed_answers[0])
        for kind, content in [('confirmed_supplement', supplement),
                              ('continuation', {'parent_task_id': task_id,
                                'affected_requirement_ids': sorted({r for q in supplements for r in q['content']['requirement_ids']}),
                                'affected_case_ids': sorted({c for q in supplements for c in q['content']['affected_case_ids']}),
                                'reuse_policy': '仅复用内容及依赖不变的草稿；批准沿用须人工确认。'})]:
            db.execute('INSERT INTO testing_agent_artifacts VALUES(?,?,?,?,?,?,?)',
                       (identifier(), child, kind, 'default', prepared['input_hash'], dumps(content), now()))
        return child

    def confirm_answers_and_continue(self, task_id, question_revisions, reason):
        """Commit confirmed facts, public handoff and one queued turn together."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ServiceError('请填写需求确认说明。')
        if not isinstance(question_revisions, dict) or not question_revisions or any(
                not isinstance(qid, str) or not qid or type(revision) is not int or revision < 1
                for qid, revision in question_revisions.items()):
            raise ServiceError('请选择已提交的答复，并保留其当前版本。')
        from sdlc.testing_agent.chat import ChatService
        from sdlc.testing_agent.worker import initialize, _enqueue_in_db
        initialize(self.db_path)
        chat = ChatService(self)
        # These questions become immutable once confirmed. Key by their identities
        # so a double click with either the old or refreshed revision reuses the
        # same continuation; revisions are checked before the first commit.
        key = 'requirements-' + fingerprint(sorted(question_revisions))[:40]
        with self.connection('manage', True) as db:
            task = self._task(db, task_id)
            prior = db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='handoff' AND artifact_key=?",
                               (task_id, key)).fetchone()
            if prior:
                return json.loads(prior['content_json'])
            active = db.execute("SELECT 1 FROM agent_work_items WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchone()
            if active:
                raise ServiceError('Agent 仍在处理当前消息；请等本轮回复后再确认需求补充。')
            selected_rows = [db.execute('SELECT status FROM testing_agent_questions WHERE task_id=? AND id=?',
                (task_id, qid)).fetchone() for qid in question_revisions]
            already_confirmed = all(q and q['status'] == 'confirmed' for q in selected_rows)
            child = self._confirm_answers(db, task_id, question_revisions, reason)
            child_task = self._task(db, child)
            pending = db.execute("SELECT id FROM testing_agent_chat_confirmations WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
            timestamp = now()
            if pending:
                db.execute("UPDATE testing_agent_chat_confirmations SET status='resolved',choice='stop',content=?,response_actor_id=?,response_key=?,followup_task_id=?,updated_at=?,revision=revision+1 WHERE id=?",
                    ('需求补充已确认，转入关联版本继续。', self.user['id'], key, child, timestamp, pending['id']))
                self._event(db, task_id, 'chat_confirmation_replaced', {'confirmation_id': pending['id'], 'child_task_id': child})
            chat._post_user_db(db, task_id, '我确认已提交的需求补充，请据此继续测试设计。', key)
            prior_job = db.execute('SELECT id FROM agent_work_items WHERE task_id=? ORDER BY created_at DESC LIMIT 1', (child,)).fetchone()
            # A replay of a legacy fact confirmation navigates to its existing
            # work. It must not reopen a delivered/cancelled task or retry a failed
            # generation without the separate retry confirmation.
            enqueue = not already_confirmed or (child_task['status'] == 'queued' and not prior_job)
            job_id = _enqueue_in_db(self, db, child, 'start', '', 'requirements-' + child, record_chat=False) if enqueue else prior_job['id'] if prior_job else None
            if pending:
                db.execute('UPDATE testing_agent_chat_confirmations SET job_id=? WHERE id=?', (job_id, pending['id']))
            supplement_row = db.execute("SELECT content_json FROM testing_agent_artifacts WHERE task_id=? AND kind='confirmed_supplement'", (child,)).fetchone()
            supplement = json.loads(supplement_row['content_json']) if supplement_row else {}
            answers = (supplement.get('answers') or [supplement]) if supplement else []
            if answers:
                chat._post_user_db(db, child, '已确认需求补充：\n\n' + '\n\n'.join(
                    (q.get('question') or q.get('question_id', '需求补充')) + '\n' + q['answer']['text'] for q in answers), key)
            result = {'task_id': child, 'child_task_id': child, 'job_id': job_id, 'parent_task_id': task_id,
                      'enqueued': enqueue}
            db.execute('INSERT INTO testing_agent_artifacts VALUES(?,?,?,?,?,?,?)',
                (identifier(), task_id, 'handoff', key, task['input_hash'], dumps(result), timestamp))
            db.execute("UPDATE testing_agent_tasks SET status='partial',error='',next_action=?,updated_at=?,revision=revision+1 WHERE id=?",
                       ('需求补充已确认，Agent 正在关联版本继续处理。', timestamp, task_id))
            self._event(db, task_id, 'requirements_continued', {**result, 'question_ids': sorted(question_revisions)})
            return result

    def review_feedback(self, task_id, case_id, category, reason, expected_version=None, expected_revision=None):
        if category not in FEEDBACK_ROUTES or not reason.strip() or len(reason) > 2000:
            raise ServiceError('请选择反馈分类并填写说明。')
        task = self.get_task(task_id)
        if not task['run_id']:
            raise ServiceError('请先汇总生成结果。')
        run = self.legacy.get_run(task['run_id'])
        case = next((c for c in run['cases'] if c['case_id'] == case_id), None)
        if not case:
            raise ServiceError('用例不存在。')
        version = case['version'] if expected_version is None else expected_version
        revision = case['revision'] if expected_revision is None else expected_revision
        # Review, feedback route and task cancellation share the same write lock.
        with self.connection('review', True) as db:
            self._active(self._task(db, task_id))
            case = self.legacy._case(db, task['run_id'], case_id, version, revision)
            if case['status'] == 'returned':
                raise ServiceError('此版本已退回，请处理现有反馈或先保存新修订。')
            self.legacy.user = self.user
            self.legacy._event(db, task['run_id'], 'return', self.user['id'], reason,
                               case_id, version, case['status'], 'returned')
            db.execute("UPDATE case_heads SET status='returned',revision=revision+1 WHERE run_id=? AND case_id=?",
                       (task['run_id'], case_id))
            proposal_id = identifier()
            db.execute('INSERT INTO testing_agent_proposals VALUES(?,?,?,?,?,?,?,?,?,?)',
                (proposal_id, task_id, FEEDBACK_ROUTES[category], case_id, version, revision + 1,
                 dumps({'category': category, 'reason': reason, 'business_fact_confirmed': False}),
                 'pending', self.user['id'], now()))
            self._event(db, task_id, 'feedback_routed', {'proposal_id': proposal_id, 'category': category, 'case_id': case_id})
            return proposal_id

    def resolve_question(self, task_id, question_id, reason, evidence_ids):
        """An existing-source false positive adds no business rule and needs no new PRD."""
        if not reason.strip() or not evidence_ids:
            raise ServiceError('误报处置需要说明及原文段落依据。')
        with self.connection('review', True) as db:
            task = self._task(db, task_id)
            self._active(task)
            if not set(evidence_ids) <= {p['id'] for p in task['input']['paragraphs']}:
                raise ServiceError('依据不属于当前需求版本。')
            question = db.execute('SELECT * FROM testing_agent_questions WHERE task_id=? AND id=?', (task_id, question_id)).fetchone()
            if not question or question['status'] in {'confirmed', 'false_positive'}:
                raise ServiceError('问题不存在或已经处理。')
            if task['run_id']:
                stored = db.execute('SELECT * FROM clarifications WHERE run_id=? AND id=?', (task['run_id'], question_id)).fetchone()
                if stored:
                    db.execute("UPDATE clarifications SET status='false_positive',revision=revision+1,resolution_json=? WHERE run_id=? AND id=?",
                        (dumps({'evidence_ids': evidence_ids, 'reason': reason}), task['run_id'], question_id))
                    self.legacy.user = self.user
                    content = json.loads(stored['content_json'])
                    affected = {c['case_id'] for c in self.legacy._cases(db, task['run_id']) if content['scope'] == 'run'
                        or c['case_id'] in content['affected_case_ids'] or set(c['content']['requirement_ids']) & set(content['requirement_ids'])}
                    self.legacy._reversion_questions(db, task['run_id'], self.user['id'], reason, affected)
                    self.legacy._event(db, task['run_id'], 'clarification_resolved', self.user['id'], reason,
                                       details={'clarification_id': question_id, 'resolution': 'false_positive', 'evidence_ids': evidence_ids})
            db.execute("UPDATE testing_agent_questions SET status='false_positive',confirmed_json=?,revision=revision+1 WHERE task_id=? AND id=?",
                       (dumps({'reason': reason, 'evidence_ids': evidence_ids, 'actor_id': self.user['id']}), task_id, question_id))
            self._event(db, task_id, 'question_false_positive', {'question_id': question_id, 'reason': reason, 'evidence_ids': evidence_ids})

    def add_proposal(self, task_id, kind, content, case_id=None, source_version=None, source_revision=None, capability='generate'):
        proposal_id = identifier()
        with self.connection(capability, True) as db:
            task = self._task(db, task_id)
            self._active(task)
            db.execute('INSERT INTO testing_agent_proposals VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (proposal_id, task_id, kind, case_id, source_version, source_revision,
                        dumps(content), 'pending', self.user['id'], now()))
            self._event(db, task_id, 'proposal_created', {'proposal_id': proposal_id, 'kind': kind, 'case_id': case_id})
        return proposal_id

    def exclude_condition(self, task_id, condition_ids, reason, requirement_ids=None):
        """An explicit human scope decision produces a new reviewable task.

        The original run, returned cases and approval history remain intact.
        """
        if not reason.strip() or len(reason) > 2000:
            raise ServiceError('范围排除必须说明原因与影响。')
        with self.connection('manage') as db:
            task = self._task(db, task_id)
            planned = self.artifact(task_id, 'coverage_plan')
            rules = {r['id'] for r in (self.artifact(task_id, 'analysis') or {}).get('business_rules', [])}
            excluded_rules = set(requirement_ids or [])
            if not planned or not (condition_ids or excluded_rules) or not set(condition_ids) <= {c['id'] for c in planned['conditions']} or not excluded_rules <= rules:
                raise ServiceError('请选择当前覆盖计划中的测试条件。')
            condition_ids = set(condition_ids) | {c['id'] for c in planned['conditions'] if set(c['requirement_ids']) & excluded_rules}
            actor_id = self.user['id']
        return self._derive_scope_task(task, condition_ids, set(), reason, actor_id, excluded_rules=excluded_rules)

    def _derive_scope_task(self, task, excluded_conditions, excluded_cases, reason, actor_id,
                           merge_target=None, merged_content=None, excluded_rules=None):
        task_id = task['id']
        task = self.get_task(task_id)
        analysis = copy.deepcopy(self.artifact(task_id, 'analysis'))
        old_plan = self.artifact(task_id, 'coverage_plan')
        if not analysis or not old_plan:
            raise ServiceError('请先完成分析和覆盖计划。')
        planned = {'conditions': [copy.deepcopy(c) for c in old_plan['conditions'] if c['id'] not in excluded_conditions]}
        if not planned['conditions']:
            raise ServiceError('不能将整个任务范围全部排除；请取消任务并保留原因。')
        old_rules = {r['id'] for r in analysis['business_rules']}
        retained_rules = {r for c in planned['conditions'] for r in c['requirement_ids']}
        candidates = {r for c in old_plan['conditions'] if c['id'] in excluded_conditions for r in c['requirement_ids']}
        removed_rules = (candidates - retained_rules) | set(excluded_rules or [])
        if not removed_rules <= old_rules:
            raise ServiceError('排除的需求不属于当前任务。')
        analysis['business_rules'] = [r for r in analysis['business_rules'] if r['id'] not in removed_rules]
        analysis['clarifications'] = [copy.deepcopy(q['content']) for q in task['questions'] if q['status'] != 'false_positive']
        analysis['clarifications'] = [q for q in analysis['clarifications'] if not q['requirement_ids'] or set(q['requirement_ids']) - removed_rules]
        for question in analysis['clarifications']:
            question['requirement_ids'] = [r for r in question['requirement_ids'] if r not in removed_rules]
            question['scope'], question['affected_case_ids'] = 'run', []
        drafts = copy.deepcopy(self.case_drafts(task_id))
        current = {c['case_id']: c['content'] for c in self.legacy.get_run(task['run_id'])['cases']} if task['run_id'] else {}
        merge_conditions = {i for d in drafts if d['case']['case_id'] in excluded_cases for i in d['condition_ids']}
        kept = []
        for draft in drafts:
            cid = draft['case']['case_id']
            if cid in excluded_cases:
                continue
            draft['case'] = copy.deepcopy(current.get(cid, draft['case']))
            draft['condition_ids'] = [i for i in draft['condition_ids'] if i not in excluded_conditions]
            if cid == merge_target:
                if merged_content:
                    from sdlc.domain import TestCase
                    draft['case'] = TestCase.model_validate(merged_content).model_dump()
                    if draft['case']['case_id'] != cid:
                        raise ServiceError('合并用例编号必须与目标一致。')
                draft['condition_ids'] = sorted(set(draft['condition_ids']) | merge_conditions)
            if not draft['condition_ids']:
                continue
            draft['case']['requirement_ids'] = [r for r in draft['case']['requirement_ids'] if r not in removed_rules]
            kept.append(draft)
        # Scope decisions are server records, not implicit prompt instructions.
        child = self.create_task(task['input']['normalized_text'], task['goal'], task['scope'], task_id,
                                 task['owner_id'], task['confirmer_id'])
        decision = {'source_task_id': task_id, 'excluded_condition_ids': sorted(excluded_conditions),
            'excluded_case_ids': sorted(excluded_cases), 'removed_requirement_ids': sorted(removed_rules),
            'merge_target': merge_target, 'reason': reason, 'confirmed_by': actor_id,
            'confirmed_at': now(), 'unresolved_items_remain_in_source_task': True}
        self.add_artifact(child, 'scope_decision', decision)
        for artifact in task['artifacts']:
            if artifact['kind'] in {'evidence_policy', 'evidence_review'}:
                self.add_artifact(child, artifact['kind'], artifact['content'], artifact['artifact_key'])
        self.save_analysis(child, analysis)
        self.save_plan(child, planned)
        for index in range(0, len(kept), 12):
            self.save_batch(child, f'scope-kept-{index // 12}', {'cases': kept[index:index + 12]})
        if kept:
            self.finalize_design(child)
        with self.connection('manage', True) as db:
            self._event(db, task_id, 'scope_adjusted', {'child_task_id': child, **decision})
        return child

    def resolve_proposal(self, task_id, proposal_id, decision, reason, target_case_id=None, merged_content=None):
        if not reason.strip():
            raise ServiceError('请填写处理理由。')
        with self.connection('manage', True) as db:
            task = self._task(db, task_id)
            proposal = db.execute('SELECT * FROM testing_agent_proposals WHERE task_id=? AND id=?', (task_id, proposal_id)).fetchone()
            if not proposal or proposal['status'] != 'pending':
                raise ServiceError('提案不存在或已经处理。')
            if decision == 'dismiss':
                db.execute("UPDATE testing_agent_proposals SET status='dismissed' WHERE id=?", (proposal_id,))
                self._event(db, task_id, 'proposal_dismissed', {'proposal_id': proposal_id, 'reason': reason})
                return task_id
            if proposal['kind'] != 'merge_proposal' or decision not in {'merge', 'deactivate'}:
                raise ServiceError('此提案不能直接确认；业务事实必须走需求答复确认。')
            actor_id = self.user['id']
            case_id = proposal['case_id']
        if target_case_id == case_id or (decision == 'merge' and not target_case_id):
            raise ServiceError('请选择其他用例作为合并目标。')
        if decision == 'merge' and target_case_id not in {d['case']['case_id'] for d in self.case_drafts(task_id)}:
            raise ServiceError('合并目标不属于当前任务。')
        child = self._derive_scope_task(task, set(), {case_id}, reason, actor_id,
                                        target_case_id if decision == 'merge' else None, merged_content)
        with self.connection('manage', True) as db:
            db.execute("UPDATE testing_agent_proposals SET status='accepted' WHERE id=? AND status='pending'", (proposal_id,))
            self._event(db, task_id, 'proposal_accepted', {'proposal_id': proposal_id, 'decision': decision,
                        'child_task_id': child, 'reason': reason, 'target_case_id': target_case_id})
        return child

    def apply_revision(self, task_id, proposal_id):
        conflict = False
        with self.connection('regenerate', True) as db:
            task = self._task(db, task_id)
            self._active(task)
            row = db.execute("SELECT * FROM testing_agent_proposals WHERE task_id=? AND id=? AND kind='case_revision'",
                             (task_id, proposal_id)).fetchone()
            if not row or row['status'] != 'pending' or not task['run_id']:
                raise ServiceError('修订提案不存在或已处理。')
            proposal = dict(row)
            case = next((c for c in self.legacy._cases(db, task['run_id']) if c['case_id'] == proposal['case_id']), None)
            if not case or case['version'] != proposal['source_version'] or case['revision'] != proposal['source_revision']:
                db.execute("UPDATE testing_agent_proposals SET status='conflict' WHERE id=?", (proposal_id,))
                self._event(db, task_id, 'revision_conflict', {'proposal_id': proposal_id})
                conflict = True
            else:
                content = json.loads(proposal['content_json'])
                if content.get('unresolved'):
                    raise ServiceError('修订仍有未解决反馈，请先处理澄清。')
                candidate = copy.deepcopy(content['case'])
                if candidate['case_id'] != case['case_id']:
                    raise ServiceError('不能更改用例编号。')
                output = self.legacy._effective(db, task['run_id'])
                blockers = self.legacy._blocking(candidate, output)
                candidate['clarification_ids'] = blockers
                candidate['readiness'] = 'needs_clarification' if blockers else 'ready'
                output['test_cases'] = [candidate if c['case_id'] == case['case_id'] else c for c in output['test_cases']]
                errors = validate_current(output, task['input']['paragraphs'])
                if errors:
                    raise ServiceError('修订未保存：' + '; '.join(errors))
                version = self.legacy._new_case_version(db, task['run_id'], candidate, self.user['id'], case)
                self.legacy.user = self.user
                self.legacy._event(db, task['run_id'], 'edit', self.user['id'], content['explanation'],
                    case['case_id'], version, case['status'], 'pending_review', {'previous_version': case['version'], 'proposal_id': proposal_id})
                db.execute("UPDATE testing_agent_proposals SET status='applied' WHERE id=?", (proposal_id,))
                self._event(db, task_id, 'revision_applied', {'proposal_id': proposal_id, 'version': version})
        if conflict:
            raise ServiceError('人工已更新用例，模型建议已标记为冲突，没有覆盖当前版本。')
        return version

    def compare_tasks(self, old_task_id, new_task_id):
        old, new = self.get_task(old_task_id), self.get_task(new_task_id)
        a, b = old['input']['normalized_text'], new['input']['normalized_text']
        old_blocks = {p['stable_id']: p for p in old['input']['paragraphs']}
        new_blocks = {p['stable_id']: p for p in new['input']['paragraphs']}
        changed = set(old_blocks) - set(new_blocks)
        analysis = self.artifact(old_task_id, 'analysis') or {}
        changed_paragraphs = {p['id'] for sid, p in old_blocks.items() if sid in changed}
        affected_rules = {r['id'] for r in analysis.get('business_rules', []) if set(r['paragraph_ids']) & changed_paragraphs}
        cases = self.legacy.get_run(old['run_id'])['cases'] if old['run_id'] else []
        impacts = []
        for c in cases:
            direct = bool(set(c['content']['requirement_ids']) & affected_rules or set(c['content']['paragraph_ids']) & changed_paragraphs)
            impacts.append({'case_id': c['case_id'], 'status': 'affected' if direct else 'needs_indirect_review',
                'reason': '关联需求或证据发生变化' if direct else '未发现直接变化；间接依赖仍需人工确认',
                'approval_reuse_eligible': False})
        return {'old_task_id': old_task_id, 'new_task_id': new_task_id, 'changed_requirement_ids': sorted(affected_rules),
                'new_block_ids': [p['id'] for sid, p in new_blocks.items() if sid not in old_blocks],
                'diff': '\n'.join(difflib.unified_diff(a.splitlines(), b.splitlines(), fromfile='旧版', tofile='新版', lineterm='')),
                'impacts': impacts}

    def reuse_suggestions(self, task_id):
        target = self.get_task(task_id)
        target_texts = {p['text'] for p in target['input']['paragraphs']}
        suggestions = []
        for old in self.list_tasks(limit=500):
            if old['id'] == task_id or not old['run_id']:
                continue
            paragraphs = {p['id']: p['text'] for p in old['input']['paragraphs']}
            for c in self.legacy.get_run(old['run_id'])['cases']:
                if c['status'] != 'approved':
                    continue
                evidence = [paragraphs[p] for p in c['content']['paragraph_ids'] if p in paragraphs]
                suggestions.append({'task_id': old['id'], 'run_id': old['run_id'], 'case_id': c['case_id'],
                    'version': c['version'], 'title': c['content']['title'],
                    'same_evidence': bool(evidence) and set(evidence) <= target_texts,
                    'requires_human_confirmation': True, 'reason': '本项目已批准用例，需检查当前需求依赖和适用范围'})
        return suggestions

    def read_approved_case(self, source_task_id, case_id):
        source = self.get_task(source_task_id)
        if not source['run_id']:
            raise ServiceError('来源任务尚无有效用例。')
        case = next((c for c in self.legacy.get_run(source['run_id'])['cases'] if c['case_id'] == case_id), None)
        if not case or case['status'] != 'approved':
            raise ServiceError('只允许读取本项目有效批准用例作为复用来源。')
        return {'source_task_id': source_task_id, 'source_run_id': source['run_id'], 'case': case,
                'analysis': self.artifact(source_task_id, 'analysis'),
                'paragraphs': [p for p in source['input']['paragraphs'] if p['id'] in case['content']['paragraph_ids']]}

    def reuse_approved_case(self, task_id, source_task_id, case_id, condition_ids, batch_key):
        task, source = self.get_task(task_id), self.get_task(source_task_id)
        approved = self.read_approved_case(source_task_id, case_id)
        analysis = self.artifact(task_id, 'analysis')
        if not analysis:
            raise ServiceError('请先分析当前需求。')
        old_blocks = {p['id']: p['text'] for p in source['input']['paragraphs']}
        new_blocks = {p['text']: p['id'] for p in task['input']['paragraphs']}
        def signature(rule, blocks):
            return rule['statement'], rule['status'], sorted(blocks[p] for p in rule['paragraph_ids'])
        new_texts = {p['id']: p['text'] for p in task['input']['paragraphs']}
        target_rules = {dumps(signature(r, new_texts)): r['id'] for r in analysis['business_rules']}
        old_rules = {r['id']: r for r in approved['analysis']['business_rules']}
        content = copy.deepcopy(approved['case']['content'])
        try:
            content['requirement_ids'] = [target_rules[dumps(signature(old_rules[rid], old_blocks))] for rid in content['requirement_ids']]
            content['paragraph_ids'] = [new_blocks[old_blocks[p]] for p in content['paragraph_ids']]
        except KeyError:
            raise ServiceError('来源用例的需求规则或证据已变化，不能直接复用；请定向重新设计。') from None
        content['clarification_ids'], content['readiness'] = [], 'ready'
        draft = next((copy.deepcopy(d) for d in self.case_drafts(source_task_id) if d['case']['case_id'] == case_id),
                     {'role': '', 'test_data': [], 'cleanup': [], 'method': 'scenario'})
        draft['case'], draft['condition_ids'] = content, condition_ids
        self.save_batch(task_id, batch_key, {'cases': [draft]})
        self.add_artifact(task_id, 'reuse_provenance', {'case_id': case_id, 'source_task_id': source_task_id,
            'source_run_id': approved['source_run_id'], 'source_version': approved['case']['version'],
            'approval_carried': False, 'requires_review': True}, batch_key)
        return self.publish_partial(task_id)

    def carry_approval(self, task_id, old_task_id, case_id, reason):
        if not reason.strip():
            raise ServiceError('沿用批准须说明依据并确认间接影响。')
        task, old = self.get_task(task_id), self.get_task(old_task_id)
        if not task['run_id'] or not old['run_id']:
            raise ServiceError('新旧任务必须已有用例。')
        a = next((c for c in self.legacy.get_run(old['run_id'])['cases'] if c['case_id'] == case_id), None)
        b = next((c for c in self.legacy.get_run(task['run_id'])['cases'] if c['case_id'] == case_id), None)
        if not a or not b or a['status'] != 'approved':
            raise ServiceError('原用例没有有效批准。')
        def semantic(case, document, analysis):
            content = copy.deepcopy(case['content'])
            blocks = {p['id']: p['content_hash'] for p in document['input']['paragraphs']}
            rules = {r['id']: (r['statement'], r['status'], sorted(blocks[p] for p in r['paragraph_ids'])) for r in analysis['business_rules']}
            dependencies = sorted(rules[r] for r in content.pop('requirement_ids'))
            evidence = sorted(blocks[p] for p in content.pop('paragraph_ids'))
            return content, dependencies, evidence
        if semantic(a, old, self.artifact(old_task_id, 'analysis')) != semantic(b, task, self.artifact(task_id, 'analysis')):
            raise ServiceError('用例内容或需求依赖发生变化，不能沿用批准。')
        event = self.legacy.review(task['run_id'], case_id, b['version'], b['revision'], 'approve', '', reason, identifier())
        with self.connection('review', True) as db:
            self._event(db, task_id, 'approval_carried', {'source_task_id': old_task_id, 'case_id': case_id, 'event_id': event, 'reason': reason})
        return event

    def import_cases_csv(self, task_id, data, field_mapping=None):
        self.get_task(task_id)
        if isinstance(data, bytes):
            if len(data) > 2 * 1024 * 1024:
                raise ServiceError('CSV 最大 2 MiB。')
            try:
                data = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise ServiceError('CSV 必须采用 UTF-8 编码。') from None
        if not isinstance(data, str) or len(data.encode()) > 2 * 1024 * 1024:
            raise ServiceError('CSV 内容无效或过大。')
        reader = csv.DictReader(io.StringIO(data))
        mapping = field_mapping or {'title': 'title', 'preconditions': 'preconditions', 'steps': 'steps',
                                    'expected_results': 'expected_results',
                                    **({'priority': 'priority'} if 'priority' in (reader.fieldnames or []) else {})}
        required = {'title', 'preconditions', 'steps', 'expected_results'}
        if not required <= set(mapping) or not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ServiceError('请映射标题、前置条件、步骤和预期字段；列名不能重复。')
        if not set(mapping.values()) <= set(reader.fieldnames):
            raise ServiceError('字段映射引用了不存在的 CSV 列。')
        items, errors = [], []
        for index, row in enumerate(reader, 2):
            if index > 1001:
                raise ServiceError('每次最多导入 1,000 条用例。')
            if None in row or any(value is None for value in row.values()):
                errors.append(f'第 {index} 行列数不正确')
                continue
            item = {k: spreadsheet_text(row[v].strip()) for k, v in mapping.items()}
            if any(not item[k] for k in required) or any(len(v) > 20000 for v in item.values()):
                errors.append(f'第 {index} 行存在缺失字段或过长内容')
                continue
            if item.get('priority', 'P1') not in {'P0', 'P1', 'P2', ''}:
                errors.append(f'第 {index} 行优先级应为 P0/P1/P2')
                continue
            items.append({**item, 'import_id': identifier(), 'status': 'unverified', 'source_row': index,
                          'execution_status': 'not_executed', 'requires_requirement_mapping': True})
        if errors or not items:
            raise ServiceError('导入未保存：' + '; '.join(errors[:30] or ['没有有效数据行']))
        key = fingerprint(items)
        self.add_artifact(task_id, 'imported_cases', {'cases': items, 'mapping': mapping}, key)
        return {'count': len(items), 'cases': items, 'notice': '已导入为未验证资料，尚未批准；请关联需求并评审。'}

    def regression_plan(self, task_id, old_task_id=None):
        task = self.get_task(task_id)
        cases = self.legacy.get_run(task['run_id'])['cases'] if task['run_id'] else []
        impact = self.compare_tasks(old_task_id, task_id) if old_task_id else None
        affected = {i['case_id'] for i in impact['impacts'] if i['status'] == 'affected'} if impact else set()
        return {'execution_status': 'not_executed', 'requires_confirmation': True,
                'cases': [{'case_id': c['case_id'], 'title': c['content']['title'],
                    'tier': 'required' if c['case_id'] in affected or c['content']['priority'] == 'P0' else 'extended',
                    'reason': '需求变更直接影响' if c['case_id'] in affected else ('最高优先级关键场景' if c['content']['priority'] == 'P0' else '扩展回归候选，需确认间接影响')}
                    for c in cases], 'unknowns': [] if not impact else [i for i in impact['impacts'] if i['status'] == 'needs_indirect_review']}

    def create_baseline(self, task_id):
        task = self.get_task(task_id)
        if not task['run_id']:
            raise ServiceError('尚无设计结果。')
        gate = self.legacy.gate(task['run_id'])
        if gate:
            raise ServiceError('无法形成批准基线：' + '; '.join(gate))
        with self.connection('export', True) as db:
            current = self._task(db, task_id)
            self._active(current)
            if current['revision'] != task['revision'] or current['run_id'] != task['run_id']:
                raise ServiceError('任务成果刚发生变化，请刷新后重新建立基线。')
            # Recheck all gates and read the snapshot while holding the write lock.
            issues = self.legacy._gate(db, current['run_id'])
            if issues:
                raise ServiceError('评审状态刚发生变化：' + '; '.join(issues))
            if db.execute("SELECT 1 FROM testing_agent_questions WHERE task_id=? AND status NOT IN ('confirmed','false_positive') AND json_extract(content_json,'$.blocking')=1", (task_id,)).fetchone():
                raise ServiceError('仍有未处置的阻断问题。')
            coverage = self.coverage(task_id)
            if coverage['missing_condition_ids'] or coverage['missing_rule_ids']:
                raise ServiceError('覆盖计划仍有缺口，请补充或明确确认范围调整。')
            cases = self.legacy._cases(db, current['run_id'])
            details = {d['case']['case_id']: {k: v for k, v in d.items() if k != 'case'} for d in self.case_drafts(task_id)}
            snapshot = {'task_id': task_id, 'document_id': current['document_id'], 'document_version': current['document_version'],
                        'input_hash': current['input_hash'], 'goal': current['goal'], 'scope': current['scope'],
                        'run_id': current['run_id'], 'cases': cases, 'review_status': 'approved',
                        'execution_status': 'not_executed', 'coverage': coverage,
                        'project_resources': self.artifact(task_id, 'project_resources'), 'case_details': details}
            baseline_id = identifier()
            db.execute('INSERT INTO testing_agent_baselines VALUES(?,?,?,?,?,?,?)',
                       (baseline_id, task_id, current['run_id'], self.user['id'], now(), dumps(snapshot), fingerprint(snapshot)))
            db.execute("UPDATE testing_agent_tasks SET status='completed',stage='delivery',next_action=?,revision=revision+1,updated_at=? WHERE id=?",
                       ('设计评审完成；测试执行尚未开展。', now(), task_id))
            self._event(db, task_id, 'baseline_created', {'baseline_id': baseline_id, 'execution_status': 'not_executed'})
        return baseline_id

    def task_summary(self, task_id):
        task = self.get_task(task_id)
        cases = self.legacy.get_run(task['run_id'])['cases'] if task['run_id'] else []
        return {'task_id': task_id, 'status': task['status'], 'stage': task['stage'],
                'next_action': task['next_action'], 'goal': task['goal'], 'scope': task['scope'],
                'document_version': task['document_version'], 'run_id': task['run_id'],
                'case_count': len(cases) if cases else len(self.case_drafts(task_id)),
                'pending_review': sum(c['status'] == 'pending_review' for c in cases),
                'approved': sum(c['status'] == 'approved' for c in cases),
                'returned': sum(c['status'] == 'returned' for c in cases),
                'open_questions': sum(q['status'] not in {'confirmed', 'false_positive'} for q in task['questions']),
                'execution_status': 'not_executed', 'coverage': self.coverage(task_id)}

"""Project-scoped public conversation and explicit generation confirmations.

Only public assistant output and fixed progress summaries enter this store. Model
reasoning, credentials, raw tool arguments and provider exceptions never do.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os

from sdlc.accounts import Accounts
from sdlc.domain import prepare_input
from sdlc.service import ServiceError, dumps, identifier, now


SCHEMA = '''
CREATE TABLE IF NOT EXISTS testing_agent_chat_messages(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id), actor_id TEXT NOT NULL,
 role TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'message', content TEXT NOT NULL,
 state TEXT NOT NULL, request_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(task_id,actor_id,request_key));
CREATE INDEX IF NOT EXISTS testing_agent_chat_task ON testing_agent_chat_messages(task_id,seq);
CREATE TABLE IF NOT EXISTS testing_agent_chat_confirmations(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES testing_agent_tasks(id),
 status TEXT NOT NULL DEFAULT 'pending', kind TEXT NOT NULL,
 reason TEXT NOT NULL, prompt TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 request_key TEXT, choice TEXT, content TEXT NOT NULL DEFAULT '', plan_hash TEXT, response_actor_id TEXT,
 response_key TEXT, job_id TEXT, followup_task_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS testing_agent_chat_pending ON testing_agent_chat_confirmations(task_id)
 WHERE status='pending';
CREATE UNIQUE INDEX IF NOT EXISTS testing_agent_chat_confirmation_request ON testing_agent_chat_confirmations(task_id,request_key)
 WHERE request_key IS NOT NULL;
'''

TOOLS = {
    'read_task': '正在读取当前任务和已保存成果',
    'read_project_context': '正在整理项目背景与共享资料',
    'analyze_requirements': '正在分析需求、角色和待澄清事项',
    'plan_coverage': '正在规划测试范围与覆盖方案',
    'generate_case_batch': '正在生成本批测试用例',
    'inspect_coverage': '正在检查需求覆盖与缺口',
    'check_case_evidence': '正在复核用例与需求依据',
    'propose_revision': '正在根据退回意见提出修订',
    'propose_clarification': '正在整理需要你确认的问题',
    'finalize_design': '正在整理可供人工审核的用例成果',
    'read_approved_cases': '正在查找可复用的已审核用例',
    'reuse_approved_cases': '正在建立可复用用例草稿',
    'compare_tasks': '正在分析需求版本变化',
    'suggest_regression': '正在整理回归测试建议',
}
FAILURE_REASON = '本轮生成暂时未完成，已有对话和阶段成果均已保留。'
FAILURE_PROMPT = '是否重试未完成的部分？你也可以补充说明后继续，或先暂停。未经确认，我不会继续调用模型。'
PLAN_REASON = '需求分析与测试覆盖方案已准备好，生成用例前需要你确认。'
PLAN_PROMPT = '请确认按当前目标与范围生成用例，或补充说明调整方案；也可以暂不继续。'
REQUIREMENTS_REASON = '我还需要一些需求资料，才能设计有依据的测试用例。'
REQUIREMENTS_PROMPT = '请补充 PRD 或说明需要测试的功能、角色与业务规则；也可以暂不继续。'
BUDGET_REASON = '本轮处理已达到调用、Token 或时长预算上限，重试原任务无法继续推进；已有成果均已保留。'
BUDGET_PROMPT = '可以确认创建关联的新一轮，基于当前需求继续处理；原任务的预算、生成结果和审核历史会保留。也可以先调整目标或暂停。'

def _failure_reason(reason=None, error_code=None):
    # Classification inspects input but emits application-owned wording only.
    text = (str(error_code or '') + ' ' + str(reason or '')[:4096]).lower()
    if any(word in text for word in ('429', 'ratelimit', 'rate_limit', 'rate limit', '限流')):
        return '模型服务暂时限流，本轮生成尚未完成；已有成果已保留。'
    if any(word in text for word in ('timeout', 'timed out', '超时')):
        return '模型响应超时，本轮生成尚未完成；已有成果已保留。'
    if any(word in text for word in ('json', 'schema', 'validation', '结构', '引用校验', '校验')):
        return '生成内容未通过结构或需求引用校验，已有对话和成果已保留。'
    if any(word in text for word in ('permission', 'authorization', 'forbidden', '权限')):
        return '当前权限限制了这一步操作，请确认任务范围或联系团队管理员。'
    return FAILURE_REASON


def _key(value):
    if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128):
        raise ServiceError('请求标识无效。')
    return value


def _text(value, maximum=100_000):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ServiceError('对话内容为空、过长或包含不允许的字符。')
    return ''.join(c for c in value if ord(c) >= 32 or c in '\n\t')


class ChatService:
    def __init__(self, service):
        self.service = service
        with service.connection('view', True) as db:
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
            columns = {r['name'] for r in db.execute('PRAGMA table_info(testing_agent_chat_confirmations)')}
            for column in ('plan_hash', 'followup_task_id'):
                if column not in columns:
                    db.execute('ALTER TABLE testing_agent_chat_confirmations ADD COLUMN ' + column + ' TEXT')

    @contextmanager
    def _write(self):
        with self.service.connection('view', True) as db:
            capability = 'generate' if 'generate' in self.service.user['permissions'] else 'regenerate'
            Accounts.authorize(db, self.service.token, self.service.workspace_id, capability)
            project = db.execute('SELECT status FROM projects WHERE id=?', (self.service.project_id,)).fetchone()
            if not project or project['status'] != 'active':
                raise ServiceError('项目已归档，不能修改对话；历史记录仍可查看。')
            yield db

    @staticmethod
    def _message(row):
        result = dict(row)
        return {k: result[k] for k in ('id', 'role', 'kind', 'content', 'state', 'created_at', 'updated_at')}

    @staticmethod
    def _confirmation(row):
        result = dict(row)
        result['choices'] = ['refine', 'discuss', 'stop'] if result['kind'] == 'requirements_input' else ['retry', 'refine', 'discuss', 'stop']
        return result

    def list_messages(self, task_id, limit=200):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ServiceError('对话分页参数无效。')
        with self.service.connection() as db:
            self.service._task(db, task_id)
            rows = db.execute('SELECT * FROM (SELECT * FROM testing_agent_chat_messages WHERE task_id=? ORDER BY seq DESC LIMIT ?) ORDER BY seq', (task_id, limit))
            return [self._message(row) for row in rows]

    def _post_user_db(self, db, task_id, content, request_key):
        self.service._task(db, task_id)
        content = _text(content, 20_000)
        if not content.strip():
            raise ServiceError('请输入你的需求或补充说明。')
        request_key = _key(request_key)
        actor = self.service.user['id']
        if request_key:
            old = db.execute('SELECT * FROM testing_agent_chat_messages WHERE task_id=? AND actor_id=? AND request_key=?', (task_id, actor, request_key)).fetchone()
            if old:
                if old['role'] != 'user' or old['content'] != content:
                    raise ServiceError('请求标识已用于不同的对话内容。')
                return self._message(old)
        message_id, timestamp = identifier(), now()
        db.execute('INSERT INTO testing_agent_chat_messages(id,task_id,actor_id,role,content,state,request_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   (message_id, task_id, actor, 'user', content, 'completed', request_key, timestamp, timestamp))
        return self._message(db.execute('SELECT * FROM testing_agent_chat_messages WHERE id=?', (message_id,)).fetchone())

    def post_user(self, task_id, content, request_key=None):
        with self._write() as db:
            self.service._active(self.service._task(db, task_id))
            return self._post_user_db(db, task_id, content, request_key)

    def list_confirmations(self, task_id):
        with self.service.connection() as db:
            self.service._task(db, task_id)
            return [self._confirmation(row) for row in db.execute('SELECT * FROM testing_agent_chat_confirmations WHERE task_id=? ORDER BY created_at,id', (task_id,))]

    def get_pending(self, task_id):
        with self.service.connection() as db:
            self.service._task(db, task_id)
            row = db.execute("SELECT * FROM testing_agent_chat_confirmations WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
            return self._confirmation(row) if row else None

    def recover_waiting_confirmation(self, task_id):
        """Repair a legacy interrupted task without retrying or spending tokens.

        Older workers saved a waiting/failed task state without a public reply.
        Only an idle, still-interrupted task may acquire this recovery question;
        a running message must never be interrupted by a page refresh.
        """
        with self.service.connection() as db:
            task = self.service._task(db, task_id)
            row = db.execute("SELECT * FROM testing_agent_chat_confirmations WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
            pending = self._confirmation(row) if row else None
            project = db.execute('SELECT status FROM projects WHERE id=?', (self.service.project_id,)).fetchone()
            if not project or project['status'] != 'active':
                return pending
            if task['status'] not in {'failed', 'waiting_confirmation'} or (pending and pending['kind'] not in {'retry_generation', 'generation_plan'}):
                return pending
            attempts = [json.loads(row['details_json']) for row in db.execute(
                "SELECT details_json FROM testing_agent_events WHERE task_id=? AND action='model_call'", (task_id,))]
        if not {'generate', 'regenerate'} & set(self.service.user['permissions']):
            return pending
        from sdlc.agent.composition import BudgetedClient
        exhausted = (len(attempts) >= int(os.environ.get('SDLC_AGENT_MAX_MODEL_CALLS', '64'))
            or sum(BudgetedClient.token_count(event.get('usage', {})) for event in attempts)
            >= int(os.environ.get('SDLC_AGENT_MAX_TOTAL_TOKENS', '200000')))
        if pending:
            if not exhausted:
                return pending
            # Older releases mislabelled a spent task budget as a retryable
            # provider failure. Keep that history, but replace the dead-end
            # action with an explicit, versioned new-round decision.
            with self._write() as db:
                row = db.execute("SELECT * FROM testing_agent_chat_confirmations WHERE id=? AND task_id=? AND status='pending'",
                    (pending['id'], task_id)).fetchone()
                if not row or row['kind'] not in {'retry_generation', 'generation_plan'}:
                    return self._confirmation(row) if row else None
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_work_items'").fetchone():
                    if db.execute("SELECT 1 FROM agent_work_items WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchone():
                        return self._confirmation(row)
                timestamp = now()
                db.execute("UPDATE testing_agent_chat_confirmations SET kind='budget_exhausted',reason=?,prompt=?,updated_at=?,revision=revision+1 WHERE id=?",
                    (BUDGET_REASON, BUDGET_PROMPT, timestamp, pending['id']))
                db.execute('INSERT INTO testing_agent_chat_messages(id,task_id,actor_id,role,kind,content,state,request_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (identifier(), task_id, 'agent', 'assistant', 'confirmation', BUDGET_REASON + '\n\n' + BUDGET_PROMPT,
                     'completed', 'budget-recovery:' + pending['id'], timestamp, timestamp))
                db.execute("UPDATE testing_agent_tasks SET next_action=?,error='',updated_at=?,revision=revision+1 WHERE id=?",
                    (BUDGET_PROMPT, timestamp, task_id))
                self.service._event(db, task_id, 'chat_budget_reclassified', {'confirmation_id': pending['id']})
                return self._confirmation(db.execute('SELECT * FROM testing_agent_chat_confirmations WHERE id=?', (pending['id'],)).fetchone())
        return self.request_confirmation(task_id, task.get('error'),
            request_key='recovery-' + task_id + '-' + str(task['revision']),
            kind='budget_exhausted' if exhausted else 'retry_generation', _recover_only=True)

    def request_confirmation(self, task_id, reason=None, request_key=None, *, kind='retry_generation', prompt=None, plan_hash=None, error_code=None, public_message_id=None, _recover_only=False):
        if kind not in {'retry_generation', 'generation_plan', 'requirements_input', 'budget_exhausted'}:
            kind = 'retry_generation'
        # Arbitrary exception text must never reach the public conversation.
        safe_reason = (BUDGET_REASON if kind == 'budget_exhausted' else PLAN_REASON if kind == 'generation_plan' else REQUIREMENTS_REASON
                       if kind == 'requirements_input' else _failure_reason(reason, error_code))
        normal_wait = kind in {'generation_plan', 'requirements_input', 'budget_exhausted'}
        safe_prompt = (_text(prompt, 4000).strip() if normal_wait and prompt else
                       BUDGET_PROMPT if kind == 'budget_exhausted' else PLAN_PROMPT if kind == 'generation_plan' else REQUIREMENTS_PROMPT
                       if kind == 'requirements_input' else FAILURE_PROMPT)
        request_key = _key(request_key)
        if plan_hash is not None and (not isinstance(plan_hash, str) or len(plan_hash) > 128):
            raise ServiceError('方案版本标识无效。')
        with self._write() as db:
            task = self.service._task(db, task_id)
            if task['status'] in {'completed', 'cancelled'}:
                return None
            row = db.execute("SELECT * FROM testing_agent_chat_confirmations WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
            if _recover_only and not row:
                if task['status'] not in {'failed', 'waiting_confirmation'}:
                    return None
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_work_items'").fetchone():
                    if db.execute("SELECT 1 FROM agent_work_items WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchone():
                        return None
            if not row and request_key:
                row = db.execute('SELECT * FROM testing_agent_chat_confirmations WHERE task_id=? AND request_key=?', (task_id, request_key)).fetchone()
            if row:
                return self._confirmation(row)
            confirmation_id, timestamp = identifier(), now()
            db.execute('INSERT INTO testing_agent_chat_confirmations(id,task_id,kind,reason,prompt,request_key,plan_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (confirmation_id, task_id, kind, safe_reason, safe_prompt, request_key, plan_hash, timestamp, timestamp))
            db.execute("UPDATE testing_agent_chat_messages SET state='interrupted',updated_at=? WHERE task_id=? AND state='streaming'", (timestamp, task_id))
            message = None
            if public_message_id:
                prefix = hashlib.sha256(str(request_key or 'public').encode()).hexdigest()[:24]
                event_key = prefix + ':assistant:' + hashlib.sha256(str(public_message_id).encode()).hexdigest()[:48]
                message = db.execute("SELECT id FROM testing_agent_chat_messages WHERE task_id=? AND actor_id='agent' AND request_key=?", (task_id, event_key)).fetchone()
            public_content = safe_reason + '\n\n' + safe_prompt
            public_state = 'completed' if normal_wait else 'error'
            if message:
                db.execute("UPDATE testing_agent_chat_messages SET kind='confirmation',content=?,state=?,updated_at=? WHERE id=?", (public_content, public_state, timestamp, message['id']))
            else:
                db.execute('INSERT INTO testing_agent_chat_messages(id,task_id,actor_id,role,kind,content,state,request_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (identifier(), task_id, 'agent', 'assistant', 'confirmation', public_content,
                            public_state, 'confirmation:' + confirmation_id, timestamp, timestamp))
            db.execute("UPDATE testing_agent_tasks SET status='waiting_confirmation',next_action=?,error=?,updated_at=?,revision=revision+1 WHERE id=?",
                       (safe_prompt, '' if normal_wait else safe_reason, timestamp, task_id))
            self.service._event(db, task_id, 'chat_confirmation_requested', {'confirmation_id': confirmation_id, 'kind': kind})
            return self._confirmation(db.execute('SELECT * FROM testing_agent_chat_confirmations WHERE id=?', (confirmation_id,)).fetchone())

    def _carry_budget_progress(self, db, parent, child_id):
        """Carry validated drafts, never approval state, into an explicit round.

        A context-less legacy analysis may describe the wrong target product.
        Preserve it and its unanswered questions as labelled source material,
        rather than turning it into the new task's authoritative stage cache.
        """
        from sdlc.testing_agent.document_context import _validated
        child = self.service._task(db, child_id)
        source = [dict(row) for row in db.execute(
            'SELECT * FROM testing_agent_artifacts WHERE task_id=? AND input_hash=? ORDER BY created_at,id',
            (parent['id'], parent['input_hash']))]
        lookup = {(row['kind'], row['artifact_key']): row for row in source}
        same_input = child['input_hash'] == parent['input_hash']
        context = lookup.get(('document_context', 'default'))
        trusted = False
        if same_input and context:
            try:
                _validated(json.loads(context['content_json']), {p['id'] for p in child['input']['paragraphs']})
                trusted = True
            except (ServiceError, ValueError, TypeError):
                pass
        timestamp = now()
        def save(kind, key, content):
            db.execute('INSERT INTO testing_agent_artifacts VALUES(?,?,?,?,?,?,?)',
                (identifier(), child_id, kind, key, child['input_hash'], dumps(content), timestamp))

        questions = [self.service._question(row) for row in db.execute(
            'SELECT * FROM testing_agent_questions WHERE task_id=? ORDER BY id', (parent['id'],))]
        metadata = {'source_task_id': parent['id'], 'source_input_hash': parent['input_hash'],
            'reused_artifacts': [], 'reference_artifacts': [], 'retained_question_ids': [],
            'reference_question_ids': [], 'retained_case_ids': [], 'completed_condition_ids': [],
            'requires_reanalysis': not trusted, 'approvals_carried': False}
        latest_cases = ({row['case_id']: row['content'] for row in self.service.legacy._cases(db, parent['run_id'])}
                        if parent['run_id'] else {})
        kinds = {'document_context', 'analysis', 'coverage_plan', 'case_batch', 'evidence_policy',
                 'confirmed_supplement', 'continuation', 'scope_decision', 'budget_reference'}
        dependencies = (trusted and ('analysis', 'default') in lookup and ('coverage_plan', 'default') in lookup)
        for row in source:
            kind, key = row['kind'], row['artifact_key']
            if kind not in kinds:
                continue
            content = json.loads(row['content_json'])
            if kind == 'budget_reference':
                inherited_key = hashlib.sha256((parent['id'] + ':' + key).encode()).hexdigest()
                save('budget_reference', inherited_key, content)
                metadata['reference_artifacts'].append({'kind': 'budget_reference', 'artifact_key': inherited_key,
                    'source_kind': content.get('source_kind'), 'source_artifact_key': content.get('source_key')})
                if content.get('source_kind') == 'questions':
                    metadata['reference_question_ids'].extend(q['id'] for q in content['content'])
                continue
            reusable = same_input and (kind == 'confirmed_supplement' or
                (trusted and kind in {'document_context', 'analysis', 'coverage_plan', 'evidence_policy', 'scope_decision'}) or
                (dependencies and kind == 'case_batch'))
            if reusable:
                if kind == 'case_batch':
                    content = copy.deepcopy(content)
                    for draft in content['cases']:
                        case_id = draft['case']['case_id']
                        draft['case'] = copy.deepcopy(latest_cases.get(case_id, draft['case']))
                        metadata['retained_case_ids'].append(case_id)
                        metadata['completed_condition_ids'].extend(draft['condition_ids'])
                save(kind, key, content)
                metadata['reused_artifacts'].append({'kind': kind, 'artifact_key': key})
            else:
                reference_key = hashlib.sha256((parent['id'] + ':' + kind + ':' + key).encode()).hexdigest()
                save('budget_reference', reference_key, {'source_task_id': parent['id'],
                    'source_input_hash': parent['input_hash'], 'source_kind': kind, 'source_key': key,
                    'content': content, 'notice': '历史材料，仅供核对；未被确认为本轮需求事实或有效测试成果。'})
                metadata['reference_artifacts'].append({'kind': 'budget_reference', 'artifact_key': reference_key,
                    'source_kind': kind, 'source_artifact_key': key})
        if trusted:
            for question in questions:
                db.execute('INSERT INTO testing_agent_questions(task_id,id,content_json,status,answer_json,confirmed_json,revision) VALUES(?,?,?,?,?,?,?)',
                    (child_id, question['id'], dumps(question['content']), question['status'],
                     dumps(question['answer']) if question['answer'] else None,
                     dumps(question['confirmed']) if question['confirmed'] else None, question['revision']))
                metadata['retained_question_ids'].append(question['id'])
            if ('analysis', 'default') in lookup:
                db.execute('INSERT INTO testing_agent_requirements SELECT ?,local_id,stable_id,content_hash,previous_hash,identity_method FROM testing_agent_requirements WHERE task_id=?',
                    (child_id, parent['id']))
            for case_id in metadata['retained_case_ids']:
                identity = db.execute('SELECT * FROM testing_agent_case_identities WHERE task_id=? AND local_id=?',
                    (parent['id'], case_id)).fetchone()
                if identity:
                    content_hash = (hashlib.sha256(dumps(latest_cases[case_id]).encode()).hexdigest()
                                    if case_id in latest_cases else identity['content_hash'])
                    db.execute('INSERT INTO testing_agent_case_identities VALUES(?,?,?,?,?)',
                        (child_id, case_id, identity['stable_id'], content_hash, identity['content_hash']))
        elif questions:
            save('budget_reference', 'questions', {'source_task_id': parent['id'],
                'source_input_hash': parent['input_hash'], 'source_kind': 'questions', 'source_key': 'all',
                'content': questions, 'notice': '原任务未解决问题及原始答复，仅供重新核对；答复未被确认为需求事实，不阻断本轮成果。'})
            metadata['reference_artifacts'].append({'kind': 'budget_reference', 'artifact_key': 'questions',
                'source_kind': 'questions', 'source_artifact_key': 'all'})
            metadata['reference_question_ids'].extend(q['id'] for q in questions)
        # The continuation refers to this exact source round. Any older
        # continuation has already been preserved above as a labelled reference.
        save('continuation', 'default', {'parent_task_id': parent['id'],
            'affected_requirement_ids': [], 'affected_case_ids': [],
            'reuse_policy': '预算续接：仅复用同一输入及依赖一致的已验证草稿，所有用例重新人工审核。'})
        metadata['retained_case_ids'] = sorted(set(metadata['retained_case_ids']))
        metadata['completed_condition_ids'] = sorted(set(metadata['completed_condition_ids']))
        metadata['reference_question_ids'] = sorted(set(metadata['reference_question_ids']))
        save('budget_continuation', 'default', metadata)
        self.service._event(db, child_id, 'budget_progress_carried', metadata)

    def respond(self, task_id, confirmation_id, choice, content='', expected_revision=1, request_key=None):
        from sdlc.testing_agent.worker import SCHEMA as WORK_SCHEMA, _enqueue_in_db
        if choice not in {'retry', 'refine', 'discuss', 'stop'}:
            raise ServiceError('请选择重试、补充说明或暂停。')
        content = _text(content, 20_000)
        request_key = _key(request_key) or identifier()
        if choice in {'refine', 'discuss'} and not content.strip():
            raise ServiceError('请填写需要调整的目标、范围或补充信息。')
        if type(expected_revision) is not int:
            raise ServiceError('确认版本无效。')
        with self._write() as db:
            task = self.service._task(db, task_id)
            for statement in WORK_SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
            row = db.execute('SELECT * FROM testing_agent_chat_confirmations WHERE id=? AND task_id=?', (confirmation_id, task_id)).fetchone()
            if not row:
                raise ServiceError('确认消息不存在或无权访问。')
            old = dict(row)
            if old['status'] != 'pending':
                if old['response_key'] == request_key and old['response_actor_id'] == self.service.user['id'] and old['choice'] == choice and old['content'] == content:
                    return {'confirmation': self._confirmation(row), 'job_id': old['job_id'], 'task_id': old['followup_task_id'] or task_id}
                raise ServiceError('该消息已处理，请查看最新对话。')
            self.service._active(task)
            if db.execute("SELECT 1 FROM agent_work_items WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchone():
                raise ServiceError('当前回复还在保存，请稍后继续；你的确认尚未提交。')
            if old['kind'] == 'requirements_input' and choice == 'retry':
                raise ServiceError('还缺少需求资料，请先补充需求后继续。')
            if old['revision'] != expected_revision:
                raise ServiceError('确认消息已更新，请刷新后重试。')
            timestamp = now()
            db.execute("UPDATE testing_agent_chat_confirmations SET status='resolved',choice=?,content=?,response_actor_id=?,response_key=?,updated_at=?,revision=revision+1 WHERE id=? AND status='pending' AND revision=?",
                       (choice, content, self.service.user['id'], request_key, timestamp, confirmation_id, expected_revision))
            labels = {'retry': '确认创建关联的新一轮继续处理' if old['kind'] == 'budget_exhausted' else '确认按当前方案生成用例' if old['kind'] == 'generation_plan' else '确认重试未完成部分', 'refine': '补充说明后继续', 'discuss': '', 'stop': '暂不继续'}
            reply = content if choice == 'discuss' else labels[choice] + ('\n\n' + content if content else '')
            self._post_user_db(db, task_id, reply, 'confirmation-' + hashlib.sha256(request_key.encode()).hexdigest())
            self.service._event(db, task_id, 'chat_confirmation_responded', {'confirmation_id': confirmation_id, 'kind': old['kind'], 'choice': choice})
            job_id, followup_task_id = None, task_id
            if choice != 'stop':
                if choice == 'retry' and old['kind'] == 'budget_exhausted':
                    Accounts.authorize(db, self.service.token, self.service.workspace_id, 'generate')
                    prepared = prepare_input(text=task['input'].get('original_text', task['input']['normalized_text']))
                    followup_task_id = self.service._insert_task(db, prepared, task['goal'], task['scope'], task_id,
                        task['owner_id'], task['confirmer_id'], task['due_at'], budget=task['budget'])
                    self._carry_budget_progress(db, task, followup_task_id)
                    job_id = _enqueue_in_db(self.service, db, followup_task_id, 'start', '',
                        'budget-round-' + confirmation_id, record_chat=True)
                    db.execute('INSERT INTO testing_agent_artifacts VALUES(?,?,?,?,?,?,?)',
                        (identifier(), task_id, 'handoff', 'budget-' + confirmation_id, task['input_hash'],
                         dumps({'child_task_id': followup_task_id, 'intent': 'budget_restart', 'confirmation_id': confirmation_id}), timestamp))
                    self.service._event(db, task_id, 'budget_round_confirmed',
                        {'confirmation_id': confirmation_id, 'child_task_id': followup_task_id, 'previous_budget_preserved': True})
                elif choice == 'refine':
                    Accounts.authorize(db, self.service.token, self.service.workspace_id, 'generate')
                    followup_task_id = self.service._refine_task(db, task_id, content)
                    job_id = _enqueue_in_db(self.service, db, followup_task_id, 'start', '',
                        'confirmation-' + confirmation_id, record_chat=True)
                    self._post_user_db(db, followup_task_id, content, 'refinement-' + confirmation_id)
                else:
                    operation = 'message'
                    if choice == 'discuss':
                        followup = content
                    elif 'generate' in self.service.user['permissions']:
                        followup = ('我确认按当前测试目标与覆盖方案生成用例。' if old['kind'] == 'generation_plan'
                                    else '我确认重试当前任务未完成的部分，保留已有成果。')
                        followup += '此确认仅允许继续生成，不代表需求事实确认、用例审核通过或批准导出。'
                    else:
                        followup = '我确认继续处理当前已退回用例的修订，不要创建其他测试范围。'
                    job_id = _enqueue_in_db(self.service, db, task_id, operation, followup,
                        'confirmation-' + confirmation_id, confirmation_id=confirmation_id, record_chat=False)
                db.execute('UPDATE testing_agent_chat_confirmations SET job_id=?,followup_task_id=? WHERE id=?', (job_id, followup_task_id, confirmation_id))
                db.execute("UPDATE testing_agent_tasks SET status='queued',error='',next_action='已收到确认，正在准备下一步。',updated_at=?,revision=revision+1 WHERE id=?", (timestamp, followup_task_id))
                if followup_task_id != task_id:
                    db.execute("UPDATE testing_agent_tasks SET status='partial',error='',next_action='已根据补充说明创建关联任务；当前记录保留。',updated_at=?,revision=revision+1 WHERE id=?", (timestamp, task_id))
            else:
                db.execute("UPDATE testing_agent_tasks SET status='partial',error='',next_action='已暂停；已有成果保留。可以在对话中提出新的需求。',updated_at=?,revision=revision+1 WHERE id=?", (timestamp, task_id))
            return {'confirmation': self._confirmation(db.execute('SELECT * FROM testing_agent_chat_confirmations WHERE id=?', (confirmation_id,)).fetchone()), 'job_id': job_id, 'task_id': followup_task_id}

    def handle_event(self, task_id, event, request_key=None):
        if not isinstance(event, dict):
            return None
        kind = event.get('type') or event.get('event')
        if kind == 'waiting_confirmation':
            return self.request_confirmation(task_id, reason=event.get('error') or event.get('reason'), request_key=request_key, kind=event.get('kind', 'retry_generation'), prompt=event.get('prompt'), plan_hash=event.get('plan_hash'), error_code=event.get('error_code'), public_message_id=event.get('message_id'))
        if kind not in {'assistant_start', 'assistant_delta', 'assistant_message', 'assistant_end', 'tool_started', 'tool_completed', 'completed', 'failed', 'interrupted'}:
            return None
        prefix = hashlib.sha256(str(request_key or 'public').encode()).hexdigest()[:24]
        with self._write() as db:
            self.service._task(db, task_id)
            timestamp = now()
            if kind in {'completed', 'failed', 'interrupted'}:
                state = 'completed' if kind == 'completed' else 'interrupted'
                db.execute("UPDATE testing_agent_chat_messages SET state=?,updated_at=? WHERE task_id=? AND state='streaming'", (state, timestamp, task_id))
                return None
            if kind in {'tool_started', 'tool_completed'}:
                tool = event.get('tool') or event.get('name')
                summary = TOOLS.get(tool, '正在处理当前测试任务')
                failed = kind == 'tool_completed' and event.get('is_error')
                if kind == 'tool_completed':
                    summary = '该步骤暂未完成，正在保存已有成果' if failed else summary.replace('正在', '已', 1)
                event_key = prefix + ':tool:' + hashlib.sha256((str(event.get('tool_call_id', tool)) + kind).encode()).hexdigest()[:40]
                row = db.execute("SELECT * FROM testing_agent_chat_messages WHERE task_id=? AND actor_id='agent' AND request_key=?", (task_id, event_key)).fetchone()
                if row:
                    return self._message(row)
                message_id = identifier()
                db.execute('INSERT INTO testing_agent_chat_messages(id,task_id,actor_id,role,kind,content,state,request_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (message_id, task_id, 'agent', 'assistant', 'progress', summary, 'error' if failed else 'completed', event_key, timestamp, timestamp))
                return self._message(db.execute('SELECT * FROM testing_agent_chat_messages WHERE id=?', (message_id,)).fetchone())
            external_id = event.get('message_id')
            if external_id:
                event_key = prefix + ':assistant:' + hashlib.sha256(str(external_id).encode()).hexdigest()[:48]
                row = db.execute("SELECT * FROM testing_agent_chat_messages WHERE task_id=? AND actor_id='agent' AND request_key=?", (task_id, event_key)).fetchone()
            else:
                row = db.execute("SELECT * FROM testing_agent_chat_messages WHERE task_id=? AND actor_id='agent' AND state='streaming' ORDER BY seq DESC LIMIT 1", (task_id,)).fetchone()
                event_key = prefix + ':assistant:' + identifier()
            if kind == 'assistant_start' and row:
                return self._message(row)
            value = event.get('text', event.get('content', ''))
            value = _text(value)
            if kind == 'assistant_message' and not value and not row:
                return None
            if row:
                value = row['content'] + value if kind == 'assistant_delta' else row['content'] if kind in {'assistant_end', 'assistant_start'} else value
                if len(value) > 100_000:
                    raise ServiceError('Agent 对话过长，已有内容已保留。')
                state = 'completed' if kind in {'assistant_message', 'assistant_end'} else 'streaming'
                db.execute('UPDATE testing_agent_chat_messages SET content=?,state=?,updated_at=? WHERE id=?', (value, state, timestamp, row['id']))
                return self._message(db.execute('SELECT * FROM testing_agent_chat_messages WHERE id=?', (row['id'],)).fetchone())
            message_id = identifier()
            state = 'completed' if kind in {'assistant_message', 'assistant_end'} else 'streaming'
            db.execute('INSERT INTO testing_agent_chat_messages(id,task_id,actor_id,role,content,state,request_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (message_id, task_id, 'agent', 'assistant', value, state, event_key, timestamp, timestamp))
            return self._message(db.execute('SELECT * FROM testing_agent_chat_messages WHERE id=?', (message_id,)).fetchone())

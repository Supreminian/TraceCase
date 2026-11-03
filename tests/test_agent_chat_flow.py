"""Chat regressions cross the real queue, authorization, runtime and database.

The only substituted boundary is the provider. Public chunks are inspected while
the worker is still running, so a completed-message-only implementation fails.
"""
from __future__ import annotations

import json
import os

import pytest

from sdlc.accounts import Accounts
from sdlc.agent import AssistantMessage, Decision, ToolCall
from sdlc.agent.composition import build_runtime
from sdlc.secure_service import Service
from sdlc.testing_agent import worker
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.document_context import DocumentContext
from sdlc.testing_agent.domain import CaseBatch, CoveragePlan, EvidenceReview, RequirementAnalysis
from sdlc.testing_agent.intake import MessageIntent
from sdlc.testing_agent.service import TestingService


PRD = ('# 便签管理\n\n用户可以创建便签，标题长度上限待确认。'
       '\n\n用户可以删除自己创建的便签，删除前是否二次确认待明确。')


class ConversationModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.contexts = []
        self.streams = []
        self.after_chunk = None
        self.last_usage = {}
        self.last_metadata = {}

    def generate(self, context, settings):
        self.contexts.append(context)
        assert self.outputs, 'unexpected model call after the scripted conversation'
        response = self.outputs.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, context.output_type)
        self.last_usage = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
        self.last_metadata = {'request_id': 'mock-' + str(len(self.contexts))}
        return response

    def generate_stream(self, context, settings, on_delta):
        assert context.output_type is AssistantMessage
        response = self.generate(context, settings)
        if response.content:
            chunks = (response.content[:2], response.content[2:])
            for chunk in chunks:
                if chunk:
                    on_delta(chunk)
                    self.streams.append(chunk)
                    if self.after_chunk:
                        self.after_chunk()
        return response


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith('SDLC_AGENT_'):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SDLC_AGENT_PROVIDER', 'google')
    monkeypatch.setenv('SDLC_AGENT_ASSISTANT_MODEL', 'scripted-assistant')
    monkeypatch.setenv('SDLC_AGENT_REVIEWER_MODEL', 'scripted-reviewer')
    monkeypatch.setenv('SDLC_AGENT_SESSION_DIR', str(tmp_path / 'sessions'))
    path = tmp_path / 'conversation.sqlite3'
    accounts = Accounts(path)
    accounts.register('flow-user', 'Flow user', 'isolated-flow-password')
    token = accounts.login('flow-user', 'isolated-flow-password')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('便签回归', '')
    service = TestingService(path, token, workspace, project)
    task_id = service.create_task(PRD, '验证便签创建与删除')
    return service, ChatService(service), task_id


def execute_next(service, client, *, task_id=None, observe=None):
    owner = 'conversation-worker'
    job = worker.claim(service.db_path, owner)
    assert job is not None, 'the user action must leave executable durable work'
    if task_id:
        assert job['task_id'] == task_id

    def factory(path, token, workspace, project, task, *, execution_guard,
                capability, public_event_sink):
        def sink(event):
            public_event_sink(event)
            if observe:
                observe(event)

        return build_runtime(path, token, workspace, project, task,
                             model_client=client, execution_guard=execution_guard,
                             capability=capability, public_event_sink=sink)

    worker.execute(service.db_path, job, owner, runtime_factory=factory)
    record = next(item for item in worker.list_work(service, job['task_id']) if item['id'] == job['id'])
    assert record['status'] == 'completed', record
    assert not client.outputs, 'the intended model response was never consumed'
    return job


def assistant_replies(chat, task_id):
    return [message for message in chat.list_messages(task_id)
            if message['role'] == 'assistant' and message['kind'] != 'progress']


def test_natural_continue_after_agent_question_calls_model_and_streams_before_completion(conversation):
    service, chat, task_id = conversation
    ask = ConversationModel(AssistantMessage(tool_calls=[ToolCall(
        id='ask-scope', name='request_user_confirmation',
        arguments={'question': '这轮先验证创建和删除，继续吗？'})]), Decision(should_allow=True))
    worker.enqueue(service, task_id, request_key='initial-request')
    execute_next(service, ask, task_id=task_id)
    pending = chat.get_pending(task_id)
    assert pending and pending['kind'] == 'generation_plan'

    original_reply = '  好的，继续。  '
    accepted = chat.respond(task_id, pending['id'], 'discuss', original_reply,
                            expected_revision=pending['revision'], request_key='natural-consent')
    assert chat.respond(task_id, pending['id'], 'discuss', original_reply,
                        expected_revision=pending['revision'], request_key='natural-consent') == accepted
    answer = '已收到你的确认，接下来处理便签创建和删除测试。'
    followup = ConversationModel(MessageIntent(intent='continue_generation', confidence=.99,
        explicit_confirmation=True, evidence_quote='继续'), AssistantMessage(content=answer))
    live = []

    def observe(event):
        if event['type'] == 'assistant_delta':
            replies = assistant_replies(ChatService(service), task_id)
            live.append((replies[-1]['content'], replies[-1]['state'],
                         worker.list_work(service, task_id)[0]['status']))

    job = execute_next(service, followup, task_id=task_id, observe=observe)
    assert job['id'] == accepted['job_id']
    assert live == [(answer[:2], 'streaming', 'running'), (answer, 'streaming', 'running')]
    assert assistant_replies(chat, task_id)[-1]['content'] == answer
    assert assistant_replies(chat, task_id)[-1]['state'] == 'completed'
    assert chat.get_pending(task_id) is None
    assert service.get_task(task_id)['status'] == 'partial'
    assert chat.list_confirmations(task_id)[0]['choice'] == 'retry'
    assert json.loads(followup.contexts[0].messages[0].content)['original_user_message'] == original_reply
    assert followup.contexts[-1].messages[-1].content == original_reply
    assert worker.claim(service.db_path, 'no-auto-repeat') is None


def test_provider_failure_has_actionable_confirmation_and_chat_retry_reaches_model(conversation):
    service, chat, task_id = conversation
    worker.enqueue(service, task_id, request_key='first-turn')
    execute_next(service, ConversationModel(TimeoutError('private-upstream-token')), task_id=task_id)
    pending = chat.get_pending(task_id)
    assert pending and pending['kind'] == 'retry_generation'
    assert pending['prompt'] and pending['choices']
    assert 'private-upstream-token' not in str(chat.list_messages(task_id))
    assert worker.claim(service.db_path, 'must-wait-for-user') is None

    content = '继续，重试刚才未完成的步骤。'
    response = chat.respond(task_id, pending['id'], 'discuss', content,
                            expected_revision=pending['revision'], request_key='user-retry')
    client = ConversationModel(MessageIntent(intent='continue_generation', confidence=.99,
        explicit_confirmation=True, evidence_quote='继续'),
        AssistantMessage(content='已继续处理，上一轮失败没有覆盖已有成果。'))
    execute_next(service, client, task_id=task_id)
    assert any(message['role'] == 'user' and message['content'] == content
               for message in chat.list_messages(task_id))
    assert assistant_replies(chat, task_id)[-1]['content'].startswith('已继续处理')
    assert chat.get_pending(task_id) is None
    assert service.get_task(task_id)['status'] not in {'queued', 'running', 'failed', 'waiting_confirmation'}
    assert [item['id'] for item in worker.list_work(service, task_id)].count(response['job_id']) == 1


def test_each_ordinary_chat_turn_is_classified_and_answered_without_fixed_continue_rule(conversation):
    service, chat, task_id = conversation
    worker.enqueue(service, task_id)
    execute_next(service, ConversationModel(AssistantMessage(content='已阅读便签需求。')), task_id=task_id)
    for index, (content, answer) in enumerate([
        ('继续', '我将结合当前便签需求继续推进。'),
        ('先解释删除操作的风险', '删除用例需要检查操作对象与当前用户的归属关系。'),
    ]):
        client = ConversationModel(MessageIntent(intent='conversation', confidence=.99),
                                   AssistantMessage(content=answer))
        worker.enqueue(service, task_id, 'message', content, request_key=f'ordinary-{index}')
        execute_next(service, client, task_id=task_id)
        assert json.loads(client.contexts[0].messages[0].content)['original_user_message'] == content
        assert client.contexts[-1].messages[-1].content == content
        assert assistant_replies(chat, task_id)[-1]['content'] == answer
        assert ''.join(client.streams) == answer
        assert chat.get_pending(task_id) is None
        assert service.get_task(task_id)['status'] == 'partial'


@pytest.mark.parametrize('old_state', ['failed', 'waiting_confirmation'])
def test_continue_recovers_old_waiting_task_without_invisible_confirmation(conversation, old_state):
    service, chat, task_id = conversation
    service.set_state(task_id, old_state, error='旧版本任务中断', next_action='等待确认下一步')
    assert chat.get_pending(task_id) is None
    content = '继续'
    worker.enqueue(service, task_id, 'message', content, request_key='recover-old-' + old_state)
    client = ConversationModel(MessageIntent(intent='continue_generation', confidence=.99,
        explicit_confirmation=True, evidence_quote=content),
        AssistantMessage(content='已恢复这轮便签测试准备，我会从已有成果继续。'))
    execute_next(service, client, task_id=task_id)
    assert assistant_replies(chat, task_id)[-1]['content'].startswith('已恢复')
    assert chat.get_pending(task_id) is None
    assert service.get_task(task_id)['status'] == 'partial'
    assert len(service.list_tasks()) == 1
    assert worker.claim(service.db_path, 'no-repeat') is None


def test_asking_about_failure_then_continue_still_reaches_a_fresh_model_turn(conversation):
    service, chat, task_id = conversation
    worker.enqueue(service, task_id)
    execute_next(service, ConversationModel(TimeoutError('private-body')), task_id=task_id)
    assert chat.get_pending(task_id)
    question = '为什么刚才失败了？先解释一下。'
    worker.enqueue(service, task_id, 'message', question, request_key='why-failed')
    explanation = ConversationModel(MessageIntent(intent='conversation', confidence=.99),
        AssistantMessage(content='上次模型请求未完成，已有成果保留。你可以继续，或补充新的范围。'))
    execute_next(service, explanation, task_id=task_id)
    assert chat.list_confirmations(task_id)[0]['choice'] == 'discuss'
    assert chat.get_pending(task_id) is None

    content = '好的，继续重试。'
    worker.enqueue(service, task_id, 'message', content, request_key='continue-after-explanation')
    client = ConversationModel(MessageIntent(intent='continue_generation', confidence=.99,
        explicit_confirmation=True, evidence_quote='继续重试'),
        AssistantMessage(content='收到，我会重试尚未完成的部分。'))
    execute_next(service, client, task_id=task_id)
    intake = json.loads(client.contexts[0].messages[0].content)
    assert intake['previous_confirmation']['kind'] == 'retry_generation'
    assert intake['original_user_message'] == content
    assert assistant_replies(chat, task_id)[-1]['content'].startswith('收到')
    assert service.get_task(task_id)['status'] == 'partial'
    assert chat.list_confirmations(task_id)[0]['choice'] == 'retry'


def test_confirming_all_requirements_enqueues_one_child_and_generates_reviewable_cases(conversation):
    service, chat, task_id = conversation
    source = service.get_task(task_id)['input']['paragraphs']
    p_create, p_delete = source[1]['id'], source[2]['id']
    initial_analysis = {
        'features': [{'id': 'F1', 'description': '便签创建与删除',
                      'paragraph_ids': [p_create, p_delete]}],
        'roles': [{'id': 'ROLE1', 'name': '用户', 'responsibilities': ['管理本人便签'],
                   'paragraph_ids': [p_create, p_delete]}],
        'business_rules': [
            {'id': 'R1', 'statement': '用户可以创建便签，标题上限待确认',
             'status': 'incomplete', 'paragraph_ids': [p_create]},
            {'id': 'R2', 'statement': '用户可以删除本人便签，确认操作待明确',
             'status': 'incomplete', 'paragraph_ids': [p_delete]}],
        'clarifications': [
            {'id': 'Q1', 'question': '便签标题的长度上限是多少？',
             'paragraph_ids': [p_create], 'requirement_ids': ['R1'],
             'affected_case_ids': [], 'scope': 'run', 'blocking': True, 'rationale': None},
            {'id': 'Q2', 'question': '删除便签前是否需要确认？',
             'paragraph_ids': [p_delete], 'requirement_ids': ['R2'],
             'affected_case_ids': [], 'scope': 'run', 'blocking': True, 'rationale': None}],
        'risks': []}
    service.save_analysis(task_id, initial_analysis)
    service.set_state(task_id, 'waiting_answer', next_action='请补充两项需求')
    pending = chat.request_confirmation(task_id, kind='generation_plan', prompt='补齐需求后继续。')
    supplements = {'Q1': '便签标题最多100个字符。',
                   'Q2': '删除本人便签必须二次确认，确认后删除。'}
    for question in service.get_task(task_id)['questions']:
        service.submit_answer(task_id, question['id'], supplements[question['id']],
                              expected_revision=question['revision'])
    revisions = {question['id']: question['revision']
                 for question in service.get_task(task_id)['questions']}

    accepted = service.confirm_answers_and_continue(task_id, revisions, '产品负责人确认两项补充')
    assert service.confirm_answers_and_continue(task_id, revisions, '重复提交同一批确认') == accepted
    child_id = accepted['task_id']
    assert child_id != task_id and len(service.list_tasks()) == 2
    child = service.get_task(child_id)
    assert all(text in child['input']['normalized_text'] for text in supplements.values())
    assert child['status'] == 'queued' and chat.get_pending(child_id) is None
    assert chat.get_pending(task_id) is None
    assert chat.list_confirmations(task_id)[0]['id'] == pending['id']
    assert chat.list_confirmations(task_id)[0]['followup_task_id'] == child_id
    assert service.get_task(task_id)['input']['original_text'] == PRD
    queued = worker.list_work(service, child_id)
    assert len(queued) == 1 and queued[0]['id'] == accepted['job_id']

    paragraphs = child['input']['paragraphs']
    rule_paragraphs = {
        rule_id: [base] + [p['id'] for p in paragraphs if supplement in p['text']]
        for rule_id, base, supplement in [('R1', p_create, supplements['Q1']),
                                          ('R2', p_delete, supplements['Q2'])]}
    resolved = {**initial_analysis,
        'business_rules': [{'id': rid, 'statement': supplements[qid], 'status': 'clear',
                            'paragraph_ids': rule_paragraphs[rid]}
                           for rid, qid in [('R1', 'Q1'), ('R2', 'Q2')]],
        'clarifications': []}
    conditions = [{'id': 'C' + str(index), 'module': '便签', 'description': description,
                   'requirement_ids': [rid], 'paragraph_ids': rule_paragraphs[rid],
                   'method': 'scenario', 'priority': 'P1', 'status': 'planned',
                   'rationale': '验证已确认的便签规则'}
                  for index, rid, description in [(1, 'R1', '创建100字符标题的便签'),
                                                   (2, 'R2', '确认后删除本人便签')]]
    cases = []
    for condition, precondition, step, expected in [
        (conditions[0], '准备100字符的虚构便签标题', '用户填写标题并提交创建便签', '便签保存成功'),
        (conditions[1], '用户已有一条自己创建的虚构便签', '用户删除该便签并确认删除', '该便签被删除')]:
        cases.append({'case': {
            'case_id': 'TC-' + condition['id'], 'title': condition['description'],
            'requirement_ids': condition['requirement_ids'], 'paragraph_ids': condition['paragraph_ids'],
            'preconditions': [precondition], 'steps': [{'number': 1, 'action': step}],
            'expected_results': [expected], 'priority': 'P1', 'category': '正常',
            'readiness': 'ready', 'clarification_ids': []},
            'condition_ids': [condition['id']], 'role': '用户',
            'test_data': ['虚构便签'], 'cleanup': [], 'method': 'scenario'})

    def call(number, name, arguments=None):
        return AssistantMessage(tool_calls=[ToolCall(id='call-' + str(number),
            name=name, arguments=arguments or {})])

    client = ConversationModel(
        call(1, 'analyze_requirements'), Decision(should_allow=True),
        DocumentContext(target_product='便签管理', document_purpose='定义便签管理行为'),
        RequirementAnalysis.model_validate(resolved),
        call(2, 'plan_coverage'), Decision(should_allow=True), CoveragePlan(conditions=conditions),
        call(3, 'generate_case_batch', {'module': '便签', 'condition_ids': ['C1', 'C2'], 'batch_key': 'notes'}),
        Decision(should_allow=True), CaseBatch(cases=cases),
        call(4, 'finalize_design'), Decision(should_allow=True), EvidenceReview(findings=[]),
        AssistantMessage(content='两项需求补充已纳入本轮，2条便签用例已生成，请审核。'))
    execute_next(service, client, task_id=child_id)
    final = service.get_task(child_id)
    assert final['status'] == 'waiting_review' and final['run_id']
    assert chat.get_pending(child_id) is None
    assert not [q for q in final['questions'] if q['status'] in {'open', 'answered'}]
    run = service.legacy.get_run(final['run_id'])
    assert len(run['cases']) == 2
    assert all(case['status'] == 'pending_review' for case in run['cases'])
    assert assistant_replies(chat, child_id)[-1]['content'].startswith('两项需求补充已纳入本轮')
    assert worker.claim(service.db_path, 'no-orphan-work') is None

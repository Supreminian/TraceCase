"""Large tool histories can shrink without losing source identity or protocol."""
import copy
import json
from types import SimpleNamespace

import pytest

from sdlc.accounts import Accounts
from sdlc.agent import AssistantMessage, Context, Decision, ModelSettings, ToolCall, ToolResult, UserMessage
from sdlc.agent.application import PublicToolError
from sdlc.agent.composition import BudgetedClient
from sdlc.secure_service import Service
from sdlc.testing_agent.context_projection import project_context
from sdlc.testing_agent.domain import RequirementAnalysis
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent.tools import build_tools


def large_context(output_type=AssistantMessage):
    source = '用户提供的需求原文。' * 1000
    analysis = {
        'business_rules': [{'id': f'R{index}', 'status': 'clear',
                            'statement': f'这是规则{index}的完整陈述：' + '业务要求。' * 200,
                            'paragraph_ids': [f'P{index:03d}']}
                           for index in range(1, 21)],
        'clarifications': [], 'features': [], 'roles': []}
    plan = {'conditions': [{'id': f'C{index}', 'module': '便签管理',
                            'description': '验证第' + str(index) + '条便签规则',
                            'requirement_ids': [f'R{index}'],
                            'paragraph_ids': [f'P{index:03d}'], 'status': 'planned'}
                           for index in range(1, 21)]}
    payload = {'id': 'task-current', 'input_hash': 'document-current',
               'input': {'original_text': source,
                         'paragraphs': [{'id': 'P001', 'text': source}]},
               'analysis': analysis, 'coverage_plan': plan}
    messages = [UserMessage(content=source)]
    for number in range(6):
        identifier = f'read-{number}'
        messages.extend([AssistantMessage(tool_calls=[ToolCall(id=identifier,
            name='read_task', arguments={})], metadata={'google': {'opaque': 'keep-this-token'}}),
            ToolResult(tool_call_id=identifier, content=json.dumps(payload, ensure_ascii=False))])
    messages.append(UserMessage(content='继续，先解释当前便签规则。'))
    return Context('trusted instruction', [], messages, output_type, 'memory', 'policy')


@pytest.mark.parametrize('output_type', [AssistantMessage, Decision])
def test_projection_reduces_repeated_context_and_keeps_user_input_protocol_and_exact_ids(output_type):
    original = large_context(output_type)
    saved = copy.deepcopy(original)
    projected = project_context(original)
    assert original == saved
    original_size = sum(len(message.model_dump_json()) for message in original.messages)
    projected_size = sum(len(message.model_dump_json()) for message in projected.messages)
    assert projected_size < original_size * .4
    assert len(projected.messages) == len(original.messages)
    for before, after in zip(original.messages, projected.messages, strict=True):
        assert type(after) is type(before)
        if not isinstance(before, ToolResult):
            assert before == after
        else:
            assert before.tool_call_id == after.tool_call_id and not after.is_error
            content = json.loads(after.content)
            assert content['_context_projection']['sha256']
            assert content['_context_projection']['tool_call_id'] == before.tool_call_id
            for index in range(1, 21):
                assert f'"R{index}"' in after.content
                assert f'"C{index}"' in after.content
                assert f'"P{index:03d}"' in after.content
            assert '便签管理' in after.content
    # Business prose is omitted as an entire, hash-addressable field, not clipped
    # into a shorter assertion that a model might mistake for the original rule.
    assert '_omitted_text' in projected.messages[-2].content
    assert 'read_saved_artifact' in projected.messages[-2].content
    assert 'read_requirement_paragraphs' in projected.messages[-2].content


def test_current_explicit_read_and_errors_remain_exact_and_typed_stages_keep_full_input():
    context = large_context()
    current = json.dumps({'items': [{'id': 'R1', 'statement': '必须完整保留的已读取业务规则。' * 500}]}, ensure_ascii=False)
    context.messages.extend([
        AssistantMessage(tool_calls=[ToolCall(id='exact-read', name='read_saved_artifact',
            arguments={'kind': 'analysis', 'section': 'business_rules', 'item_ids': ['R1']})]),
        ToolResult(tool_call_id='exact-read', content=current),
        ToolResult(tool_call_id='failed-call', content='可供用户处理的完整错误说明。' * 500, is_error=True)])
    projected = project_context(context)
    assert projected.messages[-2].content == current
    assert projected.messages[-1] == context.messages[-1]
    typed = Context('full stage', [], context.messages, RequirementAnalysis)
    assert project_context(typed) is typed


def test_unindexed_answers_and_confirmations_are_never_omitted():
    context = large_context()
    original = json.loads(context.messages[-2].content)
    answers = [{'id': 'Q1', 'answer': {'text': '完整的用户需求答复。' * 100}}]
    confirmed = [{'text': '完整的已确认补充。' * 100}]
    original['questions'], original['confirmed_supplements'] = answers, confirmed
    context.messages[-2].content = json.dumps(original, ensure_ascii=False)
    projected = project_context(context)
    content = json.loads(projected.messages[-2].content)['data']
    assert content['questions'] == answers
    assert content['confirmed_supplements'] == confirmed


def test_old_case_bodies_have_a_precise_saved_batch_locator_even_with_short_steps():
    cases = [{'case': {'case_id': f'TC{index}', 'title': '验证便签创建',
                       'paragraph_ids': ['P002'], 'requirement_ids': ['R1'],
                       'readiness': 'ready', 'category': '正常',
                       'steps': [{'number': number, 'action': '逐项操作便签创建界面并核对当前页面状态。' * 5}
                                 for number in range(1, 10)],
                       'expected_results': ['便签保存成功']},
              'condition_ids': ['C1']}
             for index in range(10)]
    context = Context('trusted', [], [AssistantMessage(tool_calls=[ToolCall(id='batch-call',
        name='generate_case_batch', arguments={'module': '便签', 'batch_key': 'note-create',
                                                'condition_ids': ['C1']})]),
        ToolResult(tool_call_id='batch-call', content=json.dumps({'batch': {'cases': cases}}, ensure_ascii=False))],
        AssistantMessage)
    projected = project_context(context)
    assert len(projected.messages[-1].content) < len(context.messages[-1].content) * .5
    compact = json.loads(projected.messages[-1].content)['data']['batch']['data']['cases']
    assert compact[0]['case']['case_id'] == 'TC0'
    assert compact[0]['condition_ids'] == ['C1']
    locator = compact[0]['case']['_saved_case_body']
    assert locator['read_from']['kind'] == 'case_batch'
    assert locator['read_from']['artifact_key'] == 'note-create'
    assert locator['section'] == 'cases' and locator['item_ids'] == ['TC0']


def test_budget_audit_records_the_context_actually_sent_to_the_provider():
    context = large_context()
    artifacts, wire = [], []

    class Provider:
        last_usage = {'input_tokens': 10, 'output_tokens': 1}
        last_metadata = {}

        def generate(self, actual, settings):
            wire.append(actual)
            return AssistantMessage(content='已收到')

    service = SimpleNamespace(get_task=lambda task: {'events': []},
        record_event=lambda *args: None,
        add_artifact=lambda task, kind, content, key: artifacts.append(content))
    client = BudgetedClient(Provider(), service, 'task', lambda: None)
    client.generate(context, ModelSettings(model_id='scripted'))
    audit = artifacts[-1]
    assert audit['messages'] == [message.model_dump(exclude={'metadata'}) for message in wire[0].messages]
    assert audit['messages'] != [message.model_dump(exclude={'metadata'}) for message in context.messages]
    assert context.messages[2].content != wire[0].messages[2].content


@pytest.fixture
def artifact_context(tmp_path):
    path = tmp_path / 'artifacts.sqlite3'
    accounts = Accounts(path)
    accounts.register('projection-user', 'Projection user', 'projection-test-password')
    token = accounts.login('projection-user', 'projection-test-password')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('Artifact test', '')
    service = TestingService(path, token, workspace, project)
    task_id = service.create_task('# 便签\n\n用户创建便签。')
    statement = '用户创建便签，其业务陈述必须按原样返回。' * 40
    service.save_analysis(task_id, {'features': [], 'roles': [], 'business_rules': [
        {'id': 'R1', 'statement': statement, 'paragraph_ids': ['P002'], 'status': 'clear'}],
        'clarifications': [], 'risks': []})
    tool = next(tool for tool in build_tools(service, task_id, None, ModelSettings(model_id='unused'))
                if tool.name == 'read_saved_artifact')
    return service, task_id, tool, statement


def test_saved_artifact_read_returns_only_exact_current_task_items(artifact_context):
    service, task_id, tool, statement = artifact_context
    result = json.loads(tool.execute({'kind': 'analysis', 'section': 'business_rules', 'item_ids': ['R1']}))
    assert result['task_id'] == task_id
    assert result['input_hash'] == service.get_task(task_id)['input_hash']
    assert result['items'][0]['statement'] == statement
    assert result['items'][0]['id'] == 'R1'
    for arguments in [
        {'kind': 'analysis', 'section': 'business_rules', 'item_ids': ['R-OTHER']},
        {'kind': 'analysis', 'section': 'business_rules', 'item_ids': ['R1', 'R1']},
        {'kind': 'analysis', 'section': 'cases', 'item_ids': ['R1']},
        {'kind': 'model_attempt', 'section': 'messages', 'item_ids': ['R1']},
    ]:
        with pytest.raises(PublicToolError):
            tool.execute(arguments)


def test_saved_artifact_read_rejects_a_mismatched_input_hash(artifact_context):
    service, task_id, tool, _ = artifact_context
    with service.connection('generate', True) as db:
        db.execute("UPDATE testing_agent_artifacts SET input_hash='stale-version' WHERE task_id=? AND kind='analysis'", (task_id,))
    with pytest.raises(PublicToolError, match='需求版本'):
        tool.execute({'kind': 'analysis', 'section': 'business_rules', 'item_ids': ['R1']})

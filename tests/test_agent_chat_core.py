"""Public chat streaming and human decision barriers preserve the execution ledger."""
from types import SimpleNamespace

import pytest

from sdlc.agent import Assistant, AssistantMessage, Decision, ModelSettings, Reviewer, ToolCall, ToolResult
from sdlc.agent.application import AgentService, ModelUnavailableError, PublicToolError, TaskHandoff, ToolDelivered, UserInputRequired
from sdlc.agent.composition import BudgetedClient
from tests.test_agent_core import MemoryStore, ReadTool, ScriptedClient, invocation, make_agent


class StreamingClient(ScriptedClient):
    def __init__(self, *outputs):
        super().__init__(*outputs)
        self.stream_calls = 0

    def generate_stream(self, context, settings, on_delta):
        assert context.output_type is AssistantMessage
        self.stream_calls += 1
        result = self.generate(context, settings)
        if result.content:
            on_delta(result.content[:2])
            on_delta(result.content[2:])
        return result


def streaming_agent(client, tool=None, **kwargs):
    return AgentService(client, MemoryStore(), Assistant('Prepare grounded tests', [tool or ReadTool()]),
                        Reviewer('Review safe calls'), ModelSettings(model_id='main'),
                        ModelSettings(model_id='review'), **kwargs)


def test_only_public_assistant_generations_stream_with_stable_message_ids():
    client = StreamingClient(invocation(), Decision(should_allow=True), AssistantMessage(content='已完成分析'))
    agent = streaming_agent(client)
    events = []
    result = agent.send_message(agent.new_session().id, '准备测试', events.append)
    assert result.metadata['agent_turn']['status'] == 'completed'
    assert client.stream_calls == 2
    assert client.contexts[1].output_type is Decision
    starts = [event for event in events if event['type'] == 'assistant_start']
    finals = [event for event in events if event['type'] == 'assistant_message']
    assert len(starts) == len(finals) == 2
    assert [e['message_id'] for e in starts] == [e['message_id'] for e in finals]
    deltas = [event for event in events if event['type'] == 'assistant_delta']
    assert ''.join(event['text'] for event in deltas) == '已完成分析'
    assert {event['message_id'] for event in deltas} == {finals[-1]['message_id']}
    assert finals[0]['tool_calls'] == [{'id': 'call-1', 'name': 'read_requirement'}]
    assert 'arguments' not in str(finals)


def test_nonstream_model_still_emits_one_complete_public_message():
    agent, _, _, _ = make_agent(AssistantMessage(content='请确认范围'))
    events = []
    agent.send_message(agent.new_session().id, '开始', events.append)
    assert not [event for event in events if event['type'] == 'assistant_delta']
    assert [event['content'] for event in events if event['type'] == 'assistant_message'] == ['请确认范围']


@pytest.mark.parametrize('failure', [PublicToolError('需要有效的需求段落'), ModelUnavailableError('PRIVATE key')])
def test_first_tool_failure_pauses_and_requires_new_human_message(failure):
    tool = ReadTool()
    tool.error = failure
    first = invocation()
    first.tool_calls.append(ToolCall(id='call-2', name=tool.name, arguments={'requirement_id': 'R2'}))
    agent, store, _, client = make_agent(first, Decision(should_allow=True), tool=tool,
                                        pause_on_tool_error=True)
    events = []
    paused = agent.send_message(agent.new_session().id, '生成测试', events.append, request_id='first')
    assert paused.metadata['agent_turn']['status'] == 'waiting_confirmation'
    assert len(tool.calls) == 1 and len(client.contexts) == 2
    assert not agent._pending(paused)
    assert len([message for message in paused.messages if isinstance(message, ToolResult)]) == 2
    assert 'PRIVATE' not in paused.model_dump_json() and 'PRIVATE' not in str(events)
    confirmation = next(event for event in events if event['type'] == 'waiting_confirmation')
    assert confirmation['kind'] == 'retry_generation' and confirmation['tool'] == tool.name
    assert confirmation['error'] and confirmation['suggested_next_action']
    assert agent.resume(paused.id) == paused
    assert agent.send_message(paused.id, '生成测试', request_id='first') == paused
    assert len(client.contexts) == 2
    followup, _, _, next_client = make_agent(AssistantMessage(content='将按补充需求重新规划。'),
                                            store=store, tool=tool, pause_on_tool_error=True)
    completed = followup.send_message(paused.id, '补充需求：只检查明确的R1', request_id='human-next')
    assert completed.metadata['agent_turn']['status'] == 'completed'
    assert len(next_client.contexts) == 1 and len(tool.calls) == 1
    assert [m.content for m in completed.messages if m.role == 'user'] == ['生成测试', '补充需求：只检查明确的R1']


def test_model_unavailable_during_review_is_recorded_without_running_tool():
    agent, _, tool, _ = make_agent(invocation(), ModelUnavailableError('PRIVATE provider response'),
                                   pause_on_tool_error=True)
    paused = agent.send_message(agent.new_session().id, '生成测试')
    assert paused.metadata['agent_turn']['status'] == 'waiting_confirmation'
    assert not tool.calls
    assert any(isinstance(message, ToolResult) and message.is_error for message in paused.messages)
    assert 'PRIVATE' not in paused.model_dump_json()


def test_normal_generation_plan_question_is_not_a_failure():
    tool = ReadTool()
    tool.error = UserInputRequired('已识别角色和规则，是否按此范围生成？', 'generation_plan',
                                   ['确认生成', '调整范围'], plan_hash='plan-digest')
    agent, _, _, client = make_agent(invocation(), Decision(should_allow=True), tool=tool,
                                     pause_on_tool_error=True)
    events = []
    paused = agent.send_message(agent.new_session().id, '准备测试', events.append)
    event = next(event for event in events if event['type'] == 'waiting_confirmation')
    assert event['error'] == '' and event['kind'] == 'generation_plan'
    assert event['plan_hash'] == 'plan-digest'
    assert event['choices'] == ['确认生成', '调整范围']
    assert paused.metadata['agent_turn']['error'] == ''
    assert len(client.contexts) == 2
    assert paused.metadata['tool_executions']['call-1']['status'] == 'waiting_confirmation'


def test_trusted_task_handoff_completes_turn_and_does_not_run_pending_calls_or_ask_retry():
    tool = ReadTool()
    tool.error = TaskHandoff('child-task', '已按当前需求重新建立关联分析任务，正在继续。')
    first = invocation()
    first.tool_calls.append(ToolCall(id='call-2', name=tool.name, arguments={'requirement_id': 'R2'}))
    agent, _, _, client = make_agent(first, Decision(should_allow=True), tool=tool, pause_on_tool_error=True)
    events = []
    result = agent.send_message(agent.new_session().id, '请根据当前PRD重新分析', events.append)
    assert result.metadata['agent_turn']['status'] == 'completed'
    assert result.metadata['agent_turn']['child_task_id'] == 'child-task'
    assert len(tool.calls) == 1 and len(client.contexts) == 2
    assert not agent._pending(result)
    results = [message for message in result.messages if isinstance(message, ToolResult)]
    assert len(results) == 2 and all(not message.is_error for message in results)
    assert any(event['type'] == 'task_handoff' and event['child_task_id'] == 'child-task' for event in events)
    assert any(event['type'] == 'assistant_message' and event['content'] == tool.error.content for event in events)
    assert not any(event['type'] in {'waiting_confirmation', 'failed'} for event in events)
    assert agent.resume(result.id) == result


def test_main_model_failure_asks_user_instead_of_automatic_retry():
    agent, _, _, client = make_agent(ModelUnavailableError('PRIVATE secret'), pause_on_tool_error=True)
    events = []
    paused = agent.send_message(agent.new_session().id, '开始', events.append)
    assert paused.metadata['agent_turn']['status'] == 'waiting_confirmation'
    assert paused.messages[-1].role == 'assistant'
    assert agent.resume(paused.id) == paused
    assert len(client.contexts) == 1 and 'PRIVATE' not in str(events)


def test_missing_new_tool_argument_can_be_corrected_by_model_then_delivered_directly():
    tool = ReadTool()
    tool.parameters = {'type': 'object', 'properties': {'display_only': {'type': 'boolean'}},
                       'required': ['display_only'], 'additionalProperties': False}
    tool.error = ToolDelivered('已展示真实用例卡片。')
    stale_call = AssistantMessage(tool_calls=[ToolCall(id='old-shape', name=tool.name, arguments={})])
    corrected = AssistantMessage(tool_calls=[ToolCall(id='corrected', name=tool.name,
                                                     arguments={'display_only': True})])
    agent, _, _, client = make_agent(stale_call, corrected, Decision(should_allow=True),
        tool=tool, pause_on_tool_error=True, max_argument_repairs=2)
    events = []
    result = agent.send_message(agent.new_session().id, '展示已批准用例', events.append)
    assert result.metadata['agent_turn']['status'] == 'completed'
    assert result.metadata['agent_turn']['argument_repairs'] == 1
    assert tool.calls == [{'display_only': True}]
    assert len(client.contexts) == 3  # Two decisions and one review, no rewrite after delivery.
    failure = next(m for m in result.messages if isinstance(m, ToolResult) and m.is_error)
    assert '缺少必填参数：display_only' in failure.content
    assert 'old-shape' not in result.metadata['tool_executions']
    assert any(event['type'] == 'tool_arguments_invalid' for event in events)
    assert not any(event['type'] == 'waiting_confirmation' for event in events)


@pytest.mark.parametrize('pause_on_tool_error', [True, False])
def test_argument_repairs_are_bounded_per_turn_and_never_execute_invalid_calls(pause_on_tool_error):
    invalid = [AssistantMessage(tool_calls=[ToolCall(id=f'invalid-{i}', name='read_requirement',
                                                   arguments={})]) for i in range(3)]
    agent, store, tool, client = make_agent(*invalid, pause_on_tool_error=pause_on_tool_error, max_argument_repairs=2)
    result = agent.send_message(agent.new_session().id, '读取需求')
    assert result.metadata['agent_turn']['status'] == ('waiting_confirmation' if pause_on_tool_error else 'limited')
    assert result.metadata['agent_turn']['argument_repairs'] == 2
    assert len(client.contexts) == 3 and not tool.calls
    assert not result.metadata.get('tool_executions')
    assert not agent._pending(result)
    recovered, _, _, next_client = make_agent(store=store, pause_on_tool_error=True, max_argument_repairs=2)
    assert recovered.resume(result.id) == result
    assert not next_client.contexts


@pytest.mark.parametrize('arguments,expected', [
    ({}, 'requirement_id'),
    ({'requirement_id': {'PRIVATE_VALUE': 'PRIVATE_VALUE'}}, 'string'),
    ({'requirement_id': 'R1', 'PRIVATE_FIELD': 'PRIVATE_VALUE'}, '未声明的参数'),
])
def test_argument_repair_feedback_does_not_echo_user_values_or_unknown_field_names(arguments, expected):
    invalid = AssistantMessage(tool_calls=[ToolCall(id='invalid', name='read_requirement', arguments=arguments)])
    agent, _, tool, _ = make_agent(invalid, AssistantMessage(content='请提供有效需求。'),
                                   pause_on_tool_error=True, max_argument_repairs=1)
    result = agent.send_message(agent.new_session().id, '读取需求')
    feedback = next(m.content for m in result.messages if isinstance(m, ToolResult))
    assert expected in feedback and 'PRIVATE' not in feedback
    assert not tool.calls


@pytest.mark.parametrize('failure', [PublicToolError('需确认需求'), ModelUnavailableError('PRIVATE')])
def test_enabling_argument_repair_does_not_retry_actual_tool_failures(failure):
    tool = ReadTool()
    tool.error = failure
    agent, _, _, client = make_agent(invocation(), Decision(should_allow=True), tool=tool,
                                    pause_on_tool_error=True, max_argument_repairs=2)
    result = agent.send_message(agent.new_session().id, '生成')
    assert result.metadata['agent_turn']['status'] == 'waiting_confirmation'
    assert len(client.contexts) == 2 and len(tool.calls) == 1
    assert not result.metadata['agent_turn'].get('argument_repairs')


def test_enabling_argument_repair_does_not_retry_reviewer_denial():
    agent, _, tool, client = make_agent(invocation(), Decision(should_allow=False, reason='范围不符'),
                                       pause_on_tool_error=True, max_argument_repairs=2)
    result = agent.send_message(agent.new_session().id, '读取需求')
    assert result.metadata['agent_turn']['status'] == 'waiting_confirmation'
    assert not tool.calls and len(client.contexts) == 2
    assert not result.metadata['agent_turn'].get('argument_repairs')


def test_crash_between_failed_result_and_prompt_cannot_bypass_confirmation():
    class CrashOnPrompt(MemoryStore):
        crash = True

        def save(self, session, expected_revision=None):
            if self.crash and session.messages and isinstance(session.messages[-1], AssistantMessage) and session.messages[-1].content:
                self.crash = False
                raise KeyboardInterrupt()
            super().save(session, expected_revision)

    store, tool = CrashOnPrompt(), ReadTool()
    tool.error = PublicToolError('当前范围需要确认')
    agent, _, _, _ = make_agent(invocation(), Decision(should_allow=True), store=store, tool=tool,
                                 pause_on_tool_error=True)
    session = agent.new_session()
    with pytest.raises(KeyboardInterrupt):
        agent.send_message(session.id, '生成')
    persisted = store.get(session.id)
    assert persisted.metadata['agent_turn']['status'] == 'waiting_confirmation'
    assert isinstance(persisted.messages[-1], ToolResult)
    recovered, _, _, client = make_agent(store=store, tool=tool, pause_on_tool_error=True)
    events = []
    result = recovered.resume(session.id, events.append)
    assert result.metadata['agent_turn']['status'] == 'waiting_confirmation'
    assert len(tool.calls) == 1 and client.contexts == []
    assert any(event['type'] == 'waiting_confirmation' for event in events)


class AuditService:
    def __init__(self):
        self.user = {'id': 'audit-test-user'}
        self.events = []
        self.artifacts = []

    def get_task(self, task_id):
        return {'events': self.events}

    def record_event(self, task_id, event):
        self.events.append({'action': event['type'], 'details': event})

    def add_artifact(self, *args):
        self.artifacts.append(args)


def test_token_reservation_reports_exact_reason_before_call_or_time_limit():
    from sdlc.agent.application import UserInputRequired
    service = AuditService()
    for index in range(15):
        service.record_event('task', {'type': 'model_call',
            'usage': {'total_tokens': 199381 if index == 14 else 0}})
    client = ScriptedClient()
    budget = BudgetedClient(client, service, 'task', lambda: None)
    context = SimpleNamespace(output_type=AssistantMessage, messages=[], instruction='', memory='', agents_md='')
    with pytest.raises(UserInputRequired) as stopped:
        budget.generate(context, ModelSettings(model_id='reviewer', max_tokens=1024))
    assert stopped.value.kind == 'budget_exhausted'
    assert all(value in str(stopped.value) for value in ('199,381', '1,024', '200,000'))
    assert '时间' not in str(stopped.value) and '次模型调用' not in str(stopped.value)
    assert not client.contexts


def test_stream_and_nonstream_share_budget_guard_and_model_attempt_audit():
    client = StreamingClient(AssistantMessage(content='公开答复'), Decision(should_allow=True))
    client.last_usage = {'total_tokens': 7}
    client.last_metadata = {'request_id': 'public-request-id'}
    client.last_public_output = {'text': '公开答复'}
    service, guarded = AuditService(), []
    budget = BudgetedClient(client, service, 't1', lambda: guarded.append(True), max_calls=2)
    context = SimpleNamespace(output_type=AssistantMessage, instruction='test', memory='', agents_md='', messages=[])
    chunks = []
    result = budget.generate_stream(context, ModelSettings(model_id='model'), chunks.append)
    assert result.content == ''.join(chunks) == '公开答复'
    context.output_type = Decision
    assert budget.generate_stream(context, ModelSettings(model_id='model'), chunks.append).should_allow
    assert client.stream_calls == 1 and len(service.artifacts) == 2
    assert budget.calls == 2 and budget.tokens == 14 and len(guarded) == 4
    assert len(service.events) == 2
    from sdlc.agent.application import UserInputRequired
    with pytest.raises(UserInputRequired, match='预算') as stopped:
        budget.generate(context, ModelSettings(model_id='model'))
    assert stopped.value.kind == 'budget_exhausted'
    assert len(client.contexts) == 2

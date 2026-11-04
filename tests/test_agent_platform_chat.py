"""The model selects platform tools; human decisions bind authenticated versions."""
import copy
import json

import pytest

from sdlc.agent import AssistantMessage, Decision, ToolCall
from sdlc.testing_agent import worker
from sdlc.testing_agent.domain import RevisionProposal
from sdlc.testing_agent.intake import MessageIntent
from sdlc.testing_agent.platform_actions import confirm_action, list_actions, propose_action
from sdlc.testing_agent.clarifications import submit_bundle
from tests.test_agent_chat_flow import ConversationModel, execute_next
from tests.test_agent_ui import setup, show
from tests.test_testing_agent import TEXT, analysis, ready


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('SDLC_AGENT_PROVIDER', 'google')
    monkeypatch.setenv('SDLC_AGENT_ASSISTANT_MODEL', 'test-assistant')
    monkeypatch.setenv('SDLC_AGENT_REVIEWER_MODEL', 'test-reviewer')
    monkeypatch.setenv('SDLC_AGENT_SESSION_DIR', str(tmp_path / 'sessions'))
    service = setup(tmp_path / 'platform-chat.sqlite3')
    task = service.create_task(TEXT)
    output = analysis(ambiguous=True)
    other = copy.deepcopy(output['clarifications'][0])
    other.update(id='Q2', question='重复发票退回是否需要保留记录？')
    output['clarifications'].append(other)
    service.save_analysis(task, output)
    return service, task


def message(service, task, content, *responses):
    worker.enqueue(service, task, 'message', content)
    client = ConversationModel(MessageIntent(intent='conversation', confidence=1), *responses)
    execute_next(service, client, task_id=task)
    return client


def test_agent_proposes_all_question_edits_in_one_card_then_human_confirms(context):
    service, task = context
    changes = [{'task_id': task, 'question_id': q, 'question_text': '请确认修订后的问题 ' + q,
                'blocking': True, 'reason': '用户调整问题表述'} for q in ('Q1', 'Q2')]
    message(service, task, '修改这两项阻断性澄清，让问题更清楚。', AssistantMessage(tool_calls=[ToolCall(
        id='edit-both', name='propose_platform_action', arguments={'action': 'clarification_edit_batch',
            'arguments': {'changes': changes}, 'summary': '修改两项澄清'})]))
    records = list_actions(service, task)
    assert len(records) == 1
    assert all(q['revision'] == 1 for q in service.get_task(task)['questions'])
    app = show(service)
    assert not app.exception
    assert len([button for button in app.button if button.label == '确认执行']) == 1
    next(button for button in app.button if button.label == '确认执行').click().run()
    assert not app.exception
    current = service.get_task(task)
    assert all(q['revision'] == 2 and q['content']['question'].startswith('请确认修订') for q in current['questions'])
    assert len([event for event in current['events'] if event['action'] == 'question_edited']) == 2
    assert not any(j['status'] in {'queued', 'running'} for j in worker.list_work(service, task))


def test_chat_answers_remain_draft_until_one_combined_confirmation(context):
    service, task = context
    message(service, task, 'Q1由财务审批，Q2需要保留记录。', AssistantMessage(tool_calls=[ToolCall(
        id='draft-both', name='prepare_clarification_answers', arguments={'target_task_id': task,
            'answers': [{'question_id': 'Q1', 'answer': '由财务审批', 'expected_revision': 1},
                        {'question_id': 'Q2', 'answer': '需要保留记录', 'expected_revision': 1}]})]))
    original = service.get_task(task)
    assert all(q['status'] == 'answered' and q['confirmed'] is None for q in original['questions'])
    assert len(service.list_tasks()) == 1
    app = show(service)
    assert not app.exception
    assert len([button for button in app.button if button.label == '确认全部答复并继续']) == 1
    answers = {q['id']: q['answer']['text'] for q in original['questions']}
    result = submit_bundle(service, task, answers, {'Q1': 2, 'Q2': 2}, '统一确认')
    assert len(worker.list_work(service, result['task_id'])) == 1
    assert all(q['status'] == 'confirmed' for q in service.get_task(task)['questions'])


def test_agent_queries_authenticated_approval_actor(context):
    service, task = context
    source = ready(service)
    run = service.finalize_design(source)
    service.legacy.review(run, 'TC1', 1, 1, 'approve', '', '人工确认', 'audit-chat')
    client = message(service, task, '是谁审批了这个用例？', AssistantMessage(tool_calls=[ToolCall(
        id='who', name='read_platform_record', arguments={'kind': 'case_approval', 'record_id': run, 'case_id': 'TC1'})]),
        AssistantMessage(content='审批记录已核验。'))
    result = json.loads(client.contexts[-1].messages[-1].content)
    assert result['events'][0]['actor_id'] == service.user['id']
    assert result['events'][0]['actor_verified'] is True
    assert not any(c.output_type is Decision for c in client.contexts)


def test_confirmed_regeneration_is_targeted_and_skips_new_prd_intake(context):
    service, task = context
    source = ready(service)
    run = service.finalize_design(source)
    before = copy.deepcopy(service.legacy.get_run(run)['cases'])
    proposal = propose_action(service, task, 'case_regenerate', {'run_id': run, 'case_id': 'TC1', 'reason': '请优化这条用例步骤'})
    result = confirm_action(service, task, proposal['id'])['result']
    content = copy.deepcopy(before[0]['content'])
    content['title'] = '更清楚的申请测试'
    client = ConversationModel(AssistantMessage(tool_calls=[ToolCall(id='revise-only-target', name='propose_revision',
        arguments={'case_id': 'TC1', 'feedback_proposal_id': result['feedback_proposal_id']})]), Decision(should_allow=True),
        RevisionProposal(case=content, explanation='细化目标用例'), AssistantMessage(content='修订建议已准备好，等待审核。'))
    execute_next(service, client, task_id=source)
    assert client.contexts[0].output_type is AssistantMessage
    assert len([p for p in service.get_task(source)['proposals'] if p['kind'] == 'case_revision']) == 1
    assert service.legacy.get_run(run)['cases'] == before


def test_confirmed_regeneration_cannot_be_redirected_to_another_case(context):
    from sdlc.agent.composition import build_runtime
    service, task = context
    source = ready(service)
    run = service.finalize_design(source)
    record = propose_action(service, task, 'case_regenerate', {'run_id': run, 'case_id': 'TC1', 'reason': '优化步骤'})
    result = confirm_action(service, task, record['id'])['result']
    job = worker.claim(service.db_path, 'redirect-attempt')
    client = ConversationModel(AssistantMessage(tool_calls=[ToolCall(id='wrong-case', name='propose_revision',
        arguments={'case_id': 'TC2', 'feedback_proposal_id': result['feedback_proposal_id']})]))
    def factory(path, token, workspace, project, task_id, **kwargs):
        return build_runtime(path, token, workspace, project, task_id, model_client=client, **kwargs)
    worker.execute(service.db_path, job, 'redirect-attempt', runtime_factory=factory)
    assert not any(p['kind'] == 'case_revision' for p in service.get_task(source)['proposals'])
    assert not any(context.output_type is RevisionProposal for context in client.contexts)


def test_editing_delivered_case_creates_a_new_review_round(context):
    service, task = context
    source = ready(service)
    run = service.finalize_design(source)
    for case in service.legacy.get_run(run)['cases']:
        service.legacy.review(run, case['case_id'], case['version'], case['revision'], 'approve', '', '人工确认', 'approve-' + case['case_id'])
    service.create_baseline(source)
    before = copy.deepcopy(service.legacy.get_run(run)['cases'])
    content = {**before[0]['content'], 'title': '新版用例标题'}
    record = propose_action(service, task, 'case_edit', {'run_id': run, 'case_id': 'TC1', 'content': content, 'reason': '调整标题'})
    result = confirm_action(service, task, record['id'])['result']
    assert result['target_task_id'] != source
    assert service.legacy.get_run(run)['cases'] == before
    child = service.get_task(result['target_task_id'])
    assert child['parent_task_id'] == source
    assert all(case['status'] == 'pending_review' for case in service.legacy.get_run(child['run_id'])['cases'])


def test_latest_question_versions_reach_model_after_human_edits(context):
    from sdlc.testing_agent.clarifications import edit_question
    service, task = context
    edit_question(service, task, 'Q1', '修订后的审批人问题', True, '人工修订', 1)
    client = message(service, task, 'Q1的答案是财务主管。', AssistantMessage(tool_calls=[ToolCall(
        id='fresh-answer', name='prepare_clarification_answers', arguments={'target_task_id': task,
            'answers': [{'question_id': 'Q1', 'answer': '财务主管', 'expected_revision': 2}]})]))
    memory = json.loads(client.contexts[-1].memory)
    current = next(q for q in memory['current_question_snapshot']['questions'] if q['id'] == 'Q1')
    assert current['revision'] == 2 and current['content']['question'] == '修订后的审批人问题'
    assert service.get_task(task)['questions'][0]['status'] == 'answered'
    assert len(service.list_tasks()) == 1

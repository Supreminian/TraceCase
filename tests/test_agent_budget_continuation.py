"""A renewed model budget must preserve progress without inheriting approval."""
import json

import pytest

from sdlc.accounts import Accounts
from sdlc.agent import AssistantMessage, Decision, ModelSettings, ToolCall, ToolResult
from sdlc.secure_service import Service
from sdlc.testing_agent import worker
from sdlc.testing_agent.chat import ChatService
from sdlc.testing_agent.domain import CaseBatch, EvidenceReview
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent.tools import build_tools
from tests.test_agent_chat_flow import ConversationModel, assistant_replies, execute_next
from tests.test_testing_agent import batch, ready


@pytest.fixture
def progress(tmp_path, monkeypatch):
    monkeypatch.setenv('SDLC_AGENT_PROVIDER', 'google')
    monkeypatch.setenv('SDLC_AGENT_ASSISTANT_MODEL', 'scripted-assistant')
    monkeypatch.setenv('SDLC_AGENT_REVIEWER_MODEL', 'scripted-reviewer')
    monkeypatch.setenv('SDLC_AGENT_SESSION_DIR', str(tmp_path / 'sessions'))
    path = tmp_path / 'budget-continuation.sqlite3'
    accounts = Accounts(path)
    accounts.register('budget-progress', 'Budget progress', 'disposable-budget-progress')
    token = accounts.login('budget-progress', 'disposable-budget-progress')
    workspace = accounts.spaces(token)[0]['id']
    project = Service(path, token=token, workspace_id=workspace).create_project('Budget progress', '')
    service = TestingService(path, token, workspace, project)
    task_id = ready(service, ambiguous=True, complete=False)
    service.submit_answer(task_id, 'Q1', '这是未确认的原始业务答复，不能自动成为新事实。', expected_revision=1)
    run_id = service.finalize_design(task_id)
    case = service.legacy.get_run(run_id)['cases'][0]
    service.legacy.review(run_id, case['case_id'], case['version'], case['revision'],
                          'approve', '', '通过', 'parent-approval')
    return service, ChatService(service), task_id, run_id


def renew(service, chat, task_id):
    pending = chat.request_confirmation(task_id, kind='budget_exhausted')
    result = chat.respond(task_id, pending['id'], 'retry',
                           expected_revision=pending['revision'], request_key='renew-budget')
    assert chat.respond(task_id, pending['id'], 'retry',
                        expected_revision=pending['revision'], request_key='renew-budget') == result
    assert len(worker.list_work(service, result['task_id'])) == 1
    return result['task_id']


def task_view(service, task_id):
    tools = build_tools(service, task_id, None, ModelSettings(model_id='unused'))
    return json.loads(next(tool for tool in tools if tool.name == 'read_task').execute({}))


def assert_reference_locators(service, task_id):
    references = service.artifact(task_id, 'budget_continuation')['reference_artifacts']
    assert references
    for locator in references:
        stored = service.artifact(task_id, locator['kind'], locator['artifact_key'])
        assert stored is not None
        assert stored['source_kind'] == locator['source_kind']
        assert stored['source_key'] == locator['source_artifact_key']


def test_trusted_budget_round_retains_batches_and_answers_but_republishes_for_review(progress):
    service, chat, task_id, original_run = progress
    service.add_artifact(task_id, 'document_context', {
        'target_product': '虚构费用报销', 'document_purpose': '定义用于隔离回归的虚构报销规则',
        'embedded_materials': []})
    before = service.get_task(task_id)
    original_drafts = service.case_drafts(task_id)
    child_id = renew(service, chat, task_id)
    child = service.get_task(child_id)
    assert child['input_hash'] == before['input_hash']
    assert child['input']['original_text'] == before['input']['original_text']
    assert child['run_id'] is None and not child['baselines']
    for kind in ('document_context', 'analysis', 'coverage_plan'):
        assert service.artifact(child_id, kind) == service.artifact(task_id, kind)
    assert service.case_drafts(child_id) == original_drafts
    copied = next(question for question in child['questions'] if question['id'] == 'Q1')
    original = next(question for question in before['questions'] if question['id'] == 'Q1')
    assert copied['status'] == 'answered' and copied['answer'] == original['answer']
    assert copied['confirmed'] is None
    assert original['answer']['text'] not in child['input']['normalized_text']
    continuation = service.artifact(child_id, 'budget_continuation')
    assert continuation['approvals_carried'] is False
    assert not continuation['requires_reanalysis']
    assert continuation['completed_condition_ids'] == ['C1']
    view = task_view(service, child_id)
    assert view['coverage_summary']['completed_condition_ids'] == ['C1']
    assert view['coverage_summary']['missing_condition_ids'] == ['C2']
    assert view['coverage_summary']['existing_case_ids'] == ['TC1']
    assert view['current_questions'][0]['answer'] == original['answer']
    assert view['inherited_reference_questions'] == []

    def call(number, name, arguments=None):
        return AssistantMessage(tool_calls=[ToolCall(id='resume-' + str(number),
            name=name, arguments=arguments or {})])

    client = ConversationModel(
        call(1, 'read_task'), Decision(should_allow=True),
        # Even a repeated old condition with a new batch key must avoid a new
        # typed generation call; existing progress is the source of truth.
        call(2, 'generate_case_batch', {'module': '申请', 'condition_ids': ['C1'], 'batch_key': 'do-not-regenerate'}),
        Decision(should_allow=True),
        call(3, 'generate_case_batch', {'module': '审批', 'condition_ids': ['C2'], 'batch_key': 'remaining-approval'}),
        Decision(should_allow=True), CaseBatch.model_validate(batch(2)),
        call(4, 'finalize_design'), Decision(should_allow=True), EvidenceReview(findings=[]),
        AssistantMessage(content='原有提交用例已保留，仅补充了剩余审批条件。请审核新一轮成果。'))
    execute_next(service, client, task_id=child_id)
    assert sum(context.output_type is CaseBatch for context in client.contexts) == 1
    assert not service.artifact(child_id, 'case_batch', 'do-not-regenerate')
    current = service.get_task(child_id)
    assert current['status'] == 'waiting_review' and current['run_id'] != original_run
    run = service.legacy.get_run(current['run_id'])
    assert {case['case_id'] for case in run['cases']} == {'TC1', 'TC2'}
    assert all(case['status'] == 'pending_review' for case in run['cases'])
    assert service.legacy.get_run(original_run)['cases'][0]['status'] == 'approved'
    assert assistant_replies(chat, child_id)[-1]['content'].startswith('原有提交用例已保留')
    assert chat.get_pending(child_id) is None
    assert worker.claim(service.db_path, 'no-duplicate-resume') is None
    # The next orchestration context can see the exact completed/remaining IDs.
    read_result = next(message for context in client.contexts for message in context.messages
                       if isinstance(message, ToolResult) and message.tool_call_id == 'resume-1')
    payload = json.loads(read_result.content)
    payload = payload.get('data', payload)
    assert payload['coverage_summary']['completed_condition_ids'] == ['C1']
    assert payload['coverage_summary']['missing_condition_ids'] == ['C2']


def test_legacy_budget_round_retains_reference_material_without_reusing_untrusted_analysis(progress):
    service, chat, task_id, original_run = progress
    before = service.get_task(task_id)
    assert service.artifact(task_id, 'document_context') is None
    child_id = renew(service, chat, task_id)
    child = service.get_task(child_id)
    for kind in ('document_context', 'analysis', 'coverage_plan', 'case_batch'):
        assert not [artifact for artifact in child['artifacts'] if artifact['kind'] == kind]
    assert service.case_drafts(child_id) == [] and child['run_id'] is None
    assert not child['questions'], 'legacy questions must not become new-analysis blockers'
    continuation = service.artifact(child_id, 'budget_continuation')
    assert continuation['requires_reanalysis'] and continuation['approvals_carried'] is False
    assert continuation['reference_question_ids'] == ['Q1']
    reference_text = json.dumps([artifact['content'] for artifact in child['artifacts']
                                if artifact['kind'] == 'budget_reference'], ensure_ascii=False)
    original_question = before['questions'][0]
    assert original_question['content']['question'] in reference_text
    assert original_question['answer']['text'] in reference_text
    assert service.legacy.get_run(original_run)['cases'][0]['status'] == 'approved'
    assert child['input']['original_text'] == before['input']['original_text']
    assert original_question['answer']['text'] not in child['input']['normalized_text']
    view = task_view(service, child_id)
    assert not view['analysis'] and not view['coverage_plan']
    assert view['coverage_summary']['existing_case_ids'] == []
    assert view['current_questions'] == []
    assert view['inherited_reference_questions'][0]['items'][0]['answer'] == original_question['answer']
    assert view['inherited_reference_questions'][0]['source_task_id'] == task_id
    assert_reference_locators(service, child_id)


def test_malformed_document_context_cannot_make_legacy_stages_authoritative(progress):
    service, chat, task_id, _ = progress
    service.add_artifact(task_id, 'document_context', {
        'target_product': '错误版本', 'document_purpose': '引用不存在段落的历史缓存',
        'embedded_materials': [{'paragraph_ids': ['P-NOT-CURRENT'],
                               'kind': 'supporting_example', 'reason': '无效段落引用'}]})
    child_id = renew(service, chat, task_id)
    assert service.artifact(child_id, 'budget_continuation')['requires_reanalysis']
    assert service.artifact(child_id, 'analysis') is None
    assert service.artifact(child_id, 'coverage_plan') is None
    assert service.case_drafts(child_id) == []
    assert task_view(service, child_id)['inherited_reference_questions'][0]['items'][0]['status'] == 'answered'


def test_multiple_budget_rounds_keep_original_question_references_readable(progress):
    service, chat, task_id, _ = progress
    original = service.get_task(task_id)['questions'][0]
    first = renew(service, chat, task_id)
    execute_next(service, ConversationModel(AssistantMessage(content='历史分析需要重新核对。')), task_id=first)
    second = renew(service, chat, first)
    view = task_view(service, second)
    assert view['current_questions'] == []
    references = view['inherited_reference_questions']
    assert len(references) == 1 and references[0]['source_task_id'] == task_id
    assert references[0]['items'][0]['id'] == original['id']
    assert references[0]['items'][0]['answer'] == original['answer']
    assert service.artifact(second, 'analysis') is None
    assert_reference_locators(service, second)


def test_new_budget_round_carries_latest_human_edit_without_approval(progress):
    service, chat, task_id, original_run = progress
    service.add_artifact(task_id, 'document_context', {
        'target_product': '虚构费用报销', 'document_purpose': '验证续接已保存的草稿',
        'embedded_materials': []})
    original_case = service.legacy.get_run(original_run)['cases'][0]
    changed = dict(original_case['content'])
    changed['title'] = '人工补充：申请人正常提交已准备的报销单'
    changed['steps'] = [{'number': 1, 'action': '申请人提交已经准备的虚构报销单'}]
    service.legacy.edit(original_run, original_case['case_id'], original_case['version'],
                        original_case['revision'], changed, '', '明确提交操作主体', 'manual-before-budget')
    edited = service.legacy.get_run(original_run)['cases'][0]
    service.legacy.review(original_run, edited['case_id'], edited['version'], edited['revision'],
                          'approve', '', '人工修改后通过', 'approve-edited-before-budget')
    child_id = renew(service, chat, task_id)
    carried = next(draft for draft in service.case_drafts(child_id) if draft['case']['case_id'] == edited['case_id'])
    assert carried['case'] == changed
    new_run = service.finalize_design(child_id)
    child_case = service.legacy.get_run(new_run)['cases'][0]
    assert child_case['content'] == changed and child_case['status'] == 'pending_review'
    assert child_case['version'] == 1
    parent_case = service.legacy.get_run(original_run)['cases'][0]
    assert parent_case['version'] == 2 and parent_case['status'] == 'approved'
    assert not service.get_task(child_id)['baselines']

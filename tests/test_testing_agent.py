"""Testing tasks preserve authority, evidence, versions and review semantics."""
import copy
import json

import pytest

from sdlc.accounts import Accounts
from sdlc.agent.domain import ModelSettings
from sdlc.secure_service import Service
from sdlc.service import ServiceError
from sdlc.testing_agent.service import TestingService
from sdlc.testing_agent.tools import build_tools


TEXT = '# 报销\n\n申请人提交报销单。\n\n金额大于5000元需要二级审批。'


@pytest.fixture
def ctx(tmp_path):
    path = tmp_path / 'agent.sqlite3'
    accounts = Accounts(path)
    accounts.register('alice', 'Alice', 'testing-password-123')
    token = accounts.login('alice', 'testing-password-123')
    workspace = accounts.spaces(token)[0]['id']
    legacy = Service(path, token=token, workspace_id=workspace)
    project = legacy.create_project('Agent测试', '')
    return TestingService(path, token, workspace, project), accounts


def analysis(ambiguous=False):
    return {'features': [{'id': 'F1', 'description': '提交报销', 'paragraph_ids': ['P002']}],
        'roles': [{'id': 'ROLE1', 'name': '申请人', 'responsibilities': ['提交报销'], 'paragraph_ids': ['P002']}],
        'business_rules': [{'id': 'R1', 'statement': '申请人提交报销单', 'status': 'clear', 'paragraph_ids': ['P002']},
                           {'id': 'R2', 'statement': '金额大于5000元需要二级审批', 'status': 'incomplete' if ambiguous else 'clear', 'paragraph_ids': ['P003']}],
        'clarifications': [{'id': 'Q1', 'question': '二级审批人是谁？', 'paragraph_ids': ['P003'],
                            'requirement_ids': ['R2'], 'affected_case_ids': [], 'scope': 'run', 'blocking': True,
                            'rationale': None}] if ambiguous else [], 'risks': []}


def plan():
    return {'conditions': [{'id': 'C1', 'module': '申请', 'description': '正常提交', 'requirement_ids': ['R1'],
        'paragraph_ids': ['P002'], 'method': 'scenario', 'priority': 'P1', 'status': 'planned', 'rationale': '验证提交'},
        {'id': 'C2', 'module': '审批', 'description': '超过阈值', 'requirement_ids': ['R2'],
        'paragraph_ids': ['P003'], 'method': 'boundary', 'priority': 'P0', 'status': 'planned', 'rationale': '验证大于阈值'}]}


def batch(number=1):
    return {'cases': [{'case': {'case_id': f'TC{number}', 'title': '提交申请' if number == 1 else '触发二级审批',
        'requirement_ids': [f'R{number}'], 'paragraph_ids': [f'P00{number + 1}'],
        'preconditions': ['准备虚构报销单'], 'steps': [{'number': 1, 'action': '提交申请'}],
        'expected_results': ['提交成功' if number == 1 else '进入二级审批'], 'priority': 'P1',
        'category': '正常', 'readiness': 'ready', 'clarification_ids': []},
        'condition_ids': [f'C{number}'], 'role': '申请人', 'test_data': ['虚构发票'], 'cleanup': [], 'method': 'scenario'}]}


def ready(service, ambiguous=False, complete=True):
    task = service.create_task(TEXT)
    service.save_analysis(task, analysis(ambiguous))
    service.save_plan(task, plan())
    service.save_batch(task, '申请', batch())
    if complete:
        service.save_batch(task, '审批', batch(2))
    return task


def test_persistent_task_authority_input_hash_cache_and_cancel(ctx):
    service, accounts = ctx
    task = service.create_task(TEXT, '边界测试', '报销审批')
    service.bind_session(task, 'session-1')
    assert service.get_task(task)['session_id'] == 'session-1'
    artifact = service.add_artifact(task, 'diagnostic', {'ok': True})
    assert artifact == service.add_artifact(task, 'diagnostic', {'ok': True})
    with pytest.raises(ServiceError, match='已有不同成果'):
        service.add_artifact(task, 'diagnostic', {'ok': False})
    with pytest.raises(ServiceError, match='版本已改变'):
        service.add_artifact(task, 'other', {}, input_hash='stale')
    service.cancel_task(task)
    with pytest.raises(ServiceError, match='取消或完成'):
        service.add_artifact(task, 'late', {})
    service.resume_task(task)
    assert service.get_task(task)['status'] == 'queued'
    other = service.legacy.create_project('other', '')
    isolated = TestingService(service.db_path, service.token, service.workspace_id, other)
    with pytest.raises(ServiceError, match='无权访问'):
        isolated.get_task(task)
    accounts.logout(service.token)
    with pytest.raises(ServiceError, match='登录已失效'):
        service.get_task(task)


def test_batch_validation_partial_results_and_review_execution_distinction(ctx):
    service, _ = ctx
    task = ready(service, ambiguous=True, complete=False)
    run_id = service.finalize_design(task)
    run = service.legacy.get_run(run_id)
    assert run['status'] == 'valid'
    assert len(run['cases']) == 1
    assert run['cases'][0]['content']['readiness'] == 'ready'
    assert service.coverage(task)['missing_condition_ids'] == ['C2']
    case = run['cases'][0]
    service.legacy.review(run_id, 'TC1', case['version'], case['revision'], 'approve', '', '通过', 'review-1')
    with pytest.raises(ServiceError, match='覆盖'):
        service.create_baseline(task)
    assert service.task_summary(task)['execution_status'] == 'not_executed'


def test_interrupted_publication_cannot_export_through_legacy_entry(ctx, monkeypatch):
    service, _ = ctx
    task = ready(service, complete=False)
    generate = service.legacy.generate
    def interrupt_after_run_saved(**kwargs):
        generate(**kwargs)
        raise RuntimeError('simulated crash before task publication')
    monkeypatch.setattr(service.legacy, 'generate', interrupt_after_run_saved)
    with pytest.raises(RuntimeError, match='simulated crash'):
        service.finalize_design(task)
    assert service.get_task(task)['run_id'] is None
    run_id = service.legacy.list_runs()[0]['id']
    case = service.legacy.get_run(run_id)['cases'][0]
    service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '人工通过', 'orphan-review')
    assert any('尚未完成任务关联' in issue for issue in service.legacy.gate(run_id))
    with pytest.raises(ServiceError, match='任务关联'):
        service.legacy.export(run_id, 'csv', '尝试导出', service.legacy.get_run(run_id)['revision'], 'orphan-export')
    monkeypatch.setattr(service.legacy, 'generate', generate)
    assert service.finalize_design(task) == run_id
    assert any('覆盖范围' in issue for issue in service.legacy.gate(run_id))


def test_invalid_references_and_scope_exclusion_cannot_be_silently_saved(ctx):
    service, _ = ctx
    task = service.create_task(TEXT)
    invalid = analysis()
    invalid['business_rules'][0]['paragraph_ids'] = ['P999']
    with pytest.raises(ServiceError, match='需求分析校验失败'):
        service.save_analysis(task, invalid)
    service.save_analysis(task, analysis())
    excluded = plan()
    excluded['conditions'][0]['status'] = 'out_of_scope'
    with pytest.raises(ServiceError, match='人工确认'):
        service.save_plan(task, excluded)
    service.save_plan(task, plan())
    invalid_batch = batch()
    invalid_batch['cases'][0]['case']['requirement_ids'] = ['R2']
    with pytest.raises(ServiceError, match='未关联测试条件'):
        service.save_batch(task, 'bad', invalid_batch)
    assert service.case_drafts(task) == []


def test_answer_confirmation_new_version_local_draft_reuse(ctx):
    service, _ = ctx
    task = ready(service, ambiguous=True, complete=False)
    original = service.get_task(task)
    service.submit_answer(task, 'Q1', '二级审批人是财务负责人。')
    child = service.confirm_answer(task, 'Q1', '产品确认补充角色')
    assert service.confirm_answer(task, 'Q1', '重复点击') == child
    current = service.get_task(child)
    assert current['document_version'] == 2
    assert current['document_id'] == original['document_id']
    assert service.get_task(task)['input_hash'] == original['input_hash']
    assert current['input']['paragraphs'][1]['stable_id'] == original['input']['paragraphs'][1]['stable_id']
    updated = analysis()
    service.save_analysis(child, updated)
    service.save_plan(child, plan())
    assert [d['case']['case_id'] for d in service.case_drafts(child)] == ['TC1']
    assert any(e['action'] == 'unchanged_drafts_reused' for e in service.get_task(child)['events'])


def test_selected_shared_resources_are_fixed_and_inherited_by_confirmed_child(ctx):
    from sdlc.testing_agent.resources import ResourceLibrary
    service, _ = ctx
    library = ResourceLibrary(service.db_path, service.token, service.workspace_id, service.project_id)
    resource = library.create('test_data', '虚构发票准备', '准备两张不同编号的虚构发票。')
    task = service.create_task(TEXT, resource_ids=[resource['id']])
    service.save_analysis(task, analysis(ambiguous=True))
    library.update(resource['id'], 1, '改为三张发票', '准备三张虚构发票。')
    fixed = service.artifact(task, 'project_resources')
    assert fixed['resources'][0]['revision'] == 1
    assert fixed['resources'][0]['business_fact_confirmed'] is False
    tools = {t.name: t for t in build_tools(service, task, object(), ModelSettings(model_id='test-only'))}
    context = json.loads(tools['read_project_context'].execute({}))
    assert context['project_resources'] == fixed
    service.submit_answer(task, 'Q1', '财务负责人审批。')
    child = service.confirm_answer(task, 'Q1', '确认审批人')
    assert service.artifact(child, 'project_resources') == fixed
    with pytest.raises(ServiceError, match='不存在或无权访问'):
        service.create_task(TEXT, resource_ids=['invalid-resource'])


def test_asking_why_a_generation_failed_does_not_retry_a_model_stage(ctx):
    from sdlc.agent.application import UserInputRequired
    from sdlc.testing_agent.chat import ChatService
    service, _ = ctx
    task = ready(service)
    service.add_artifact(task, 'interaction_policy', {'mode': 'chat'}, 'chat-v1')
    chat = ChatService(service)
    pending = chat.request_confirmation(task, reason='timeout')
    chat.respond(task, pending['id'], 'discuss', '为什么失败了？', expected_revision=1, request_key='question-about-failure')
    class Client:
        def generate(self, context, settings):
            pytest.fail('a question about failure cannot authorize another generation call')
    tools = {t.name: t for t in build_tools(service, task, Client(), ModelSettings(model_id='test-only'))}
    with pytest.raises(UserInputRequired) as error:
        tools['check_case_evidence'].execute({})
    assert error.value.kind == 'retry_generation'


def test_agent_can_reanalyze_current_source_without_overwriting_old_results(ctx):
    from sdlc.agent.application import TaskHandoff
    service, _ = ctx
    task = ready(service, ambiguous=True)
    run_id = service.finalize_design(task)
    original = service.legacy.get_run(run_id)
    tools = {t.name: t for t in build_tools(service, task, object(), ModelSettings(model_id='test-only'))}
    with pytest.raises(TaskHandoff) as handoff:
        tools['reanalyze_current_requirements'].execute({'reason': '需要重新判断当前文档的目标产品与嵌入材料。'})
    child = service.get_task(handoff.value.child_task_id)
    assert child['parent_task_id'] == task and child['input_hash'] == service.get_task(task)['input_hash']
    assert child['run_id'] is None and child['questions'] == []
    assert service.legacy.get_run(run_id)['cases'] == original['cases']
    assert service.reanalyze_task(task, '重复调用使用同一关联任务') == child['id']
    original_paragraph = service.get_task(task)['input']['paragraphs'][1]
    source = json.loads(tools['read_requirement_paragraphs'].execute({'paragraph_ids': [original_paragraph['id']]}))
    assert source['paragraphs'][0] == original_paragraph


def test_team_editor_cannot_confirm_business_facts(ctx):
    service, accounts = ctx
    accounts.register('bob', 'Bob', 'testing-password-123')
    bob = accounts.login('bob', 'testing-password-123')
    team = accounts.create_team(service.token, '团队')
    invite = accounts.create_invite(service.token, team)
    request = accounts.request_join(bob, invite, confirm=True)
    accounts.decide_join(service.token, team, request['request_id'], True, 'editor')
    project = Service(service.db_path, token=service.token, workspace_id=team).create_project('团队测试', '')
    owner = TestingService(service.db_path, service.token, team, project)
    editor = TestingService(service.db_path, bob, team, project)
    task = ready(owner, ambiguous=True, complete=False)
    editor.submit_answer(task, 'Q1', '财务负责人')
    with pytest.raises(ServiceError, match='无此操作权限'):
        editor.confirm_answer(task, 'Q1', '自行确认')
    assert owner.get_task(task)['questions'][0]['status'] == 'answered'


def test_feedback_routing_revision_conflict_and_immutable_old_versions(ctx):
    service, _ = ctx
    task = ready(service)
    run_id = service.finalize_design(task)
    feedback_id = service.review_feedback(task, 'TC1', 'unclear_expected', '需要更明确可观察的预期')
    feedback = service.get_task(task)['proposals'][0]
    assert feedback['id'] == feedback_id and feedback['kind'] == 'revision'
    run = service.legacy.get_run(run_id)
    case = next(c for c in run['cases'] if c['case_id'] == 'TC1')
    content = copy.deepcopy(case['content'])
    content['expected_results'] = ['页面显示提交成功']
    proposal = service.add_proposal(task, 'case_revision', {'case': content, 'explanation': '明确观察点'},
                                    'TC1', case['version'], case['revision'])
    service.legacy.edit(run_id, 'TC1', case['version'], case['revision'], content, '', '人工修改', 'manual')
    with pytest.raises(ServiceError, match='冲突'):
        service.apply_revision(task, proposal)
    assert service.legacy.get_run(run_id)['cases'][0]['version'] == 2
    business = service.review_feedback(task, 'TC2', 'requirement_changed', '阈值改了，需要产品确认')
    assert next(p for p in service.get_task(task)['proposals'] if p['id'] == business)['kind'] == 'requirement_confirmation'


def test_baseline_requires_approvals_preserves_execution_and_history(ctx):
    service, _ = ctx
    task = ready(service)
    run_id = service.finalize_design(task)
    with pytest.raises(ServiceError):
        service.create_baseline(task)
    for case in service.legacy.get_run(run_id)['cases']:
        service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '通过', 'approve-' + case['case_id'])
    with pytest.raises(ServiceError, match='批准基线'):
        service.legacy.export(run_id, 'csv', '', service.legacy.get_run(run_id)['revision'], 'before-baseline')
    baseline_id = service.create_baseline(task)
    baseline = service.get_task(task)['baselines'][0]
    assert baseline['id'] == baseline_id
    assert baseline['snapshot']['review_status'] == 'approved'
    assert baseline['snapshot']['execution_status'] == 'not_executed'
    assert service.get_task(task)['status'] == 'completed'
    with pytest.raises(ServiceError, match='完成'):
        service.add_artifact(task, 'late', {})


def test_unpublished_task_question_blocks_old_run_export(ctx):
    service, _ = ctx
    task = ready(service)
    run_id = service.finalize_design(task)
    for case in service.legacy.get_run(run_id)['cases']:
        service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '通过', 'unpublished-' + case['case_id'])
    assert not service.legacy.gate(run_id)
    tool = next(t for t in build_tools(service, task, object(), ModelSettings(model_id='test-only')) if t.name == 'propose_clarification')
    tool.execute({'content': {'id': 'Q-LATE', 'question': '还有一个待确认条件？',
        'paragraph_ids': ['P002'], 'requirement_ids': ['R1'], 'affected_case_ids': [],
        'scope': 'run', 'blocking': True, 'rationale': None}})
    assert any('未处置的阻断问题' in issue for issue in service.legacy.gate(run_id))
    with pytest.raises(ServiceError, match='阻断问题'):
        service.legacy.export(run_id, 'csv', '', service.legacy.get_run(run_id)['revision'], 'unpublished-export')


def test_baseline_rejects_a_publication_race_instead_of_mixing_run_and_cases(ctx, monkeypatch):
    service, _ = ctx
    task = ready(service)
    run_id = service.finalize_design(task)
    for case in service.legacy.get_run(run_id)['cases']:
        service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '通过', 'race-' + case['case_id'])
    gate = service.legacy.gate
    def publish_during_precheck(current_run):
        result = gate(current_run)
        extra = batch()
        extra['cases'][0]['case']['case_id'] = 'TC3'
        service.save_batch(task, 'late-batch', extra)
        new_run = service.publish_partial(task)
        case = next(c for c in service.legacy.get_run(new_run)['cases'] if c['case_id'] == 'TC3')
        service.legacy.review(new_run, case['case_id'], case['version'], case['revision'], 'approve', '', '通过', 'race-new')
        return result
    monkeypatch.setattr(service.legacy, 'gate', publish_during_precheck)
    with pytest.raises(ServiceError, match='成果刚发生变化'):
        service.create_baseline(task)
    assert not service.get_task(task)['baselines']
    monkeypatch.setattr(service.legacy, 'gate', gate)
    service.create_baseline(task)
    current = service.get_task(task)
    baseline = current['baselines'][0]
    assert baseline['run_id'] == baseline['snapshot']['run_id'] == current['run_id'] != run_id
    assert {c['case_id'] for c in baseline['snapshot']['cases']} == {'TC1', 'TC2', 'TC3'}


def test_changed_threshold_impact_and_strict_approval_reuse(ctx):
    service, _ = ctx
    old = ready(service)
    service.finalize_design(old)
    for case in service.legacy.get_run(service.get_task(old)['run_id'])['cases']:
        service.legacy.review(service.get_task(old)['run_id'], case['case_id'], case['version'], case['revision'], 'approve', '', '通过', 'ok-' + case['case_id'])
    new = service.create_task(TEXT.replace('大于5000', '大于等于5000'), parent_task_id=old)
    changed = analysis()
    changed['business_rules'][1]['statement'] = '金额大于等于5000元需要二级审批'
    service.save_analysis(new, changed)
    service.save_plan(new, plan())
    service.save_batch(new, 'a', batch())
    service.save_batch(new, 'b', batch(2))
    service.finalize_design(new)
    impacts = service.compare_tasks(old, new)
    assert impacts['changed_requirement_ids'] == ['R2']
    assert next(i for i in impacts['impacts'] if i['case_id'] == 'TC2')['status'] == 'affected'
    service.carry_approval(new, old, 'TC1', '确认内容与所有依赖均未变化，无间接影响')
    with pytest.raises(ServiceError, match='依赖发生变化'):
        service.carry_approval(new, old, 'TC2', '沿用')
    assert service.regression_plan(new, old)['execution_status'] == 'not_executed'


def test_csv_import_mapping_formula_protection_and_atomic_validation(ctx):
    service, _ = ctx
    task = service.create_task(TEXT)
    imported = service.import_cases_csv(task, '标题,前提,步骤,预期\n=CMD(),虚构资料,提交,成功\n',
        {'title': '标题', 'preconditions': '前提', 'steps': '步骤', 'expected_results': '预期'})
    assert imported['cases'][0]['title'] == "'=CMD()"
    assert imported['cases'][0]['status'] == 'unverified'
    count = len(service.get_task(task)['artifacts'])
    with pytest.raises(ServiceError, match='导入未保存'):
        service.import_cases_csv(task, 'title,preconditions,steps,expected_results\nx,,submit,ok\n')
    assert len(service.get_task(task)['artifacts']) == count


def test_model_tools_use_typed_client_stage_cache_and_no_approval(ctx):
    service, _ = ctx
    task = service.create_task(TEXT)
    class Client:
        calls = 0
        def generate(self, context, settings):
            self.calls += 1
            value = {'RequirementAnalysis': analysis(), 'CoveragePlan': plan(), 'CaseBatch': batch(), 'EvidenceReview': {'findings': []}}[context.output_type.__name__]
            assert context.messages[0].role == 'user'
            return context.output_type.model_validate(value)
    client = Client()
    tools = {t.name: t for t in build_tools(service, task, client, ModelSettings(model_id='test-model'))}
    assert not {'approve', 'export', 'confirm_answer'} & set(tools)
    tools['analyze_requirements'].execute({})
    tools['analyze_requirements'].execute({})
    tools['plan_coverage'].execute({})
    args = {'module': '申请', 'condition_ids': ['C1'], 'batch_key': 'a'}
    tools['generate_case_batch'].execute(args)
    tools['generate_case_batch'].execute(args)
    assert client.calls == 3
    result = json.loads(tools['finalize_design'].execute({}))
    assert result['run_id'] == service.get_task(task)['run_id']


def test_explicit_scope_adjustment_retains_old_returned_case_and_history(ctx):
    service, _ = ctx
    task = ready(service)
    run_id = service.finalize_design(task)
    service.review_feedback(task, 'TC2', 'unclear_expected', '本次迭代暂不处理审批范围')
    child = service.exclude_condition(task, ['C2'], '产品确认本次仅交付申请功能；审批保留到下次迭代')
    current = service.get_task(child)
    assert current['parent_task_id'] == task
    assert [c['case_id'] for c in service.legacy.get_run(current['run_id'])['cases']] == ['TC1']
    assert service.coverage(child)['missing_condition_ids'] == []
    assert next(c for c in service.legacy.get_run(run_id)['cases'] if c['case_id'] == 'TC2')['status'] == 'returned'
    scope = service.artifact(child, 'scope_decision')
    assert scope['unresolved_items_remain_in_source_task'] is True


def test_false_positive_needs_existing_evidence_and_reversions(ctx):
    service, _ = ctx
    task = ready(service, ambiguous=True)
    run_id = service.finalize_design(task)
    with pytest.raises(ServiceError, match='当前需求版本'):
        service.resolve_question(task, 'Q1', '误报', ['P999'])
    service.resolve_question(task, 'Q1', '本次只验证进入二级审批状态，不需要指定审批人', ['P003'])
    case = next(c for c in service.legacy.get_run(run_id)['cases'] if c['case_id'] == 'TC2')
    assert case['version'] == 2 and case['content']['readiness'] == 'ready'
    assert service.get_task(task)['questions'][0]['status'] == 'false_positive'


def test_agent_loop_typed_stages_human_review_and_export(ctx):
    from sdlc.agent.application import AgentService
    from sdlc.agent.domain import Assistant, AssistantMessage, Decision, Reviewer, ToolCall, ToolResult
    service, _ = ctx
    task = service.create_task(TEXT)

    class Store:
        def __init__(self):
            self.sessions = {}
        def save(self, session, expected_revision=None):
            if expected_revision is not None:
                assert self.sessions[session.id].revision == expected_revision
            self.sessions[session.id] = session.model_copy(deep=True)
        def get(self, key):
            return self.sessions[key].model_copy(deep=True)
        def list_sessions(self):
            return list(self.sessions.values())

    class Client:
        def generate(self, context, settings):
            if context.output_type is Decision:
                return Decision(should_allow=True, reason='项目内草稿工具')
            if context.output_type is AssistantMessage:
                if any(isinstance(m, ToolResult) for m in context.messages):
                    return AssistantMessage(content='草稿已生成，等待人工评审；尚未执行测试。')
                return AssistantMessage(tool_calls=[
                    ToolCall(id='a', name='analyze_requirements', arguments={}),
                    ToolCall(id='p', name='plan_coverage', arguments={}),
                    ToolCall(id='g1', name='generate_case_batch', arguments={'module': '申请', 'condition_ids': ['C1'], 'batch_key': 'a'}),
                    ToolCall(id='g2', name='generate_case_batch', arguments={'module': '审批', 'condition_ids': ['C2'], 'batch_key': 'b'}),
                    ToolCall(id='f', name='finalize_design', arguments={}),
                ])
            values = {'RequirementAnalysis': analysis(), 'CoveragePlan': plan(), 'EvidenceReview': {'findings': []}}
            if context.output_type.__name__ == 'CaseBatch':
                payload = json.loads(context.messages[0].content)
                value = batch(1 if payload['conditions'][0]['id'] == 'C1' else 2)
            else:
                value = values[context.output_type.__name__]
            return context.output_type.model_validate(value)

    client = Client()
    settings = ModelSettings(model_id='test-only')
    agent = AgentService(client, Store(), Assistant('测试准备', build_tools(service, task, client, settings)),
                         Reviewer('检查项目范围'), settings, settings)
    session = agent.new_session({'task_id': task})
    result = agent.send_message(session.id, '准备审批测试')
    assert result.metadata['agent_turn']['status'] == 'completed'
    assert not [m for m in result.messages if isinstance(m, ToolResult) and m.is_error]
    run_id = service.get_task(task)['run_id']
    assert service.task_summary(task)['approved'] == 0
    for case in service.legacy.get_run(run_id)['cases']:
        service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '人工通过', 'human-' + case['case_id'])
    service.create_baseline(task)
    run = service.legacy.get_run(run_id)
    export_id = service.legacy.export(run_id, 'csv', '', run['revision'], 'final-export')
    assert export_id
    assert service.get_task(task)['baselines'][0]['snapshot']['execution_status'] == 'not_executed'


def test_duplicate_merge_is_human_decision_and_preserves_original(ctx):
    service, _ = ctx
    task = ready(service)
    duplicate = batch()
    duplicate['cases'][0]['case']['case_id'] = 'TC3'
    service.save_batch(task, 'duplicate', duplicate)
    original_run = service.finalize_design(task)
    proposal = service.review_feedback(task, 'TC3', 'duplicate', '与TC1相同')
    child = service.resolve_proposal(task, proposal, 'merge', '确认两条用例具有相同测试意图，合并至TC1', 'TC1')
    child_run = service.get_task(child)['run_id']
    assert {c['case_id'] for c in service.legacy.get_run(child_run)['cases']} == {'TC1', 'TC2'}
    assert next(c for c in service.legacy.get_run(original_run)['cases'] if c['case_id'] == 'TC3')['status'] == 'returned'
    assert all(c['status'] == 'pending_review' for c in service.legacy.get_run(child_run)['cases'])


def test_cancelled_task_cannot_apply_late_revision(ctx):
    service, _ = ctx
    task = ready(service)
    run_id = service.finalize_design(task)
    service.review_feedback(task, 'TC1', 'style', '改写标题')
    case = next(c for c in service.legacy.get_run(run_id)['cases'] if c['case_id'] == 'TC1')
    content = copy.deepcopy(case['content'])
    content['title'] = '修订标题'
    proposal = service.add_proposal(task, 'case_revision', {'case': content, 'explanation': '改写'},
                                    'TC1', case['version'], case['revision'])
    service.cancel_task(task)
    with pytest.raises(ServiceError, match='取消或完成'):
        service.apply_revision(task, proposal)
    assert next(c for c in service.legacy.get_run(run_id)['cases'] if c['case_id'] == 'TC1')['version'] == 1


def test_batch_publication_keeps_review_and_manual_edits_without_exporting_partial(ctx):
    service, _ = ctx
    task = ready(service, complete=False)
    first = service.publish_partial(task)
    case = service.legacy.get_run(first)['cases'][0]
    updated = copy.deepcopy(case['content'])
    updated['title'] = '人工明确后的提交申请'
    service.legacy.edit(first, 'TC1', case['version'], case['revision'], updated, '', '人工改进', 'edit-first')
    case = service.legacy.get_run(first)['cases'][0]
    service.legacy.review(first, 'TC1', case['version'], case['revision'], 'approve', '', '人工批准此内容', 'approve-first')
    assert service.publish_partial(task) == first
    with pytest.raises(ServiceError, match='覆盖'):
        service.create_baseline(task)
    with pytest.raises(ServiceError, match='覆盖'):
        first_run = service.legacy.get_run(first)
        service.legacy.export(first, 'csv', '', first_run['revision'], 'partial-export')
    service.save_batch(task, 'new-module', batch(2))
    second = service.publish_partial(task)
    assert second != first
    cases = {c['case_id']: c for c in service.legacy.get_run(second)['cases']}
    assert cases['TC1']['status'] == 'approved'
    assert cases['TC1']['content']['title'] == '人工明确后的提交申请'
    assert cases['TC2']['status'] == 'pending_review'
    event = next(e for e in service.legacy.get_run(second)['events'] if e['action'] == 'same_document_decision_inherited')
    assert event['details']['source_run_id'] == first
    assert event['details']['source_event_id']
    with service.connection() as db:
        assert db.execute('SELECT complete FROM testing_agent_publications WHERE run_id=?', (first,)).fetchone()[0] == 0
        assert db.execute('SELECT complete FROM testing_agent_publications WHERE run_id=?', (second,)).fetchone()[0] == 1
    current = cases['TC2']
    service.legacy.review(second, 'TC2', current['version'], current['revision'], 'approve', '', '通过新用例', 'approve-new')
    assert service.create_baseline(task)


def test_approved_case_reuse_maps_same_project_evidence_without_approval(ctx):
    service, _ = ctx
    source = ready(service)
    run_id = service.finalize_design(source)
    case = service.legacy.get_run(run_id)['cases'][0]
    service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '批准', 'source-approve')
    target = service.create_task(TEXT)
    service.save_analysis(target, analysis())
    service.save_plan(target, plan())
    reused_run = service.reuse_approved_case(target, source, 'TC1', ['C1'], 'reuse-a')
    result = service.legacy.get_run(reused_run)['cases'][0]
    assert result['case_id'] == 'TC1' and result['status'] == 'pending_review'
    assert service.artifact(target, 'reuse_provenance', 'reuse-a')['approval_carried'] is False


def test_reviewer_can_apply_reviewed_model_revision_but_cannot_generate_or_manually_edit(ctx):
    service, accounts = ctx
    accounts.register('reviewer', 'Reviewer', 'testing-password-123')
    token = accounts.login('reviewer', 'testing-password-123')
    team = accounts.create_team(service.token, '评审协作')
    invite = accounts.create_invite(service.token, team)
    request = accounts.request_join(token, invite, confirm=True)
    accounts.decide_join(service.token, team, request['request_id'], True, 'reviewer')
    project = Service(service.db_path, token=service.token, workspace_id=team).create_project('协作用例', '')
    owner = TestingService(service.db_path, service.token, team, project)
    reviewer = TestingService(service.db_path, token, team, project)
    task = ready(owner)
    run_id = owner.finalize_design(task)
    reviewer.review_feedback(task, 'TC1', 'style', '调整表述')
    case = next(c for c in owner.legacy.get_run(run_id)['cases'] if c['case_id'] == 'TC1')
    content = copy.deepcopy(case['content'])
    content['title'] = '模型建议的明确标题'
    proposal = owner.add_proposal(task, 'case_revision', {'case': content, 'explanation': '调整标题', 'unresolved': []},
                                  'TC1', case['version'], case['revision'])
    with pytest.raises(ServiceError, match='无此操作权限'):
        reviewer.create_task(TEXT)
    with pytest.raises(ServiceError, match='无此操作权限'):
        reviewer.legacy.edit(run_id, 'TC1', case['version'], case['revision'], content, '', '任意人工编辑', 'forbidden')
    assert reviewer.apply_revision(task, proposal) == 2
    reviewer.execution_capability = 'regenerate'
    reviewer.record_event(task, {'type': 'restricted_revision_work'})
    assert any(e['action'] == 'restricted_revision_work' for e in owner.get_task(task)['events'])


def test_evidence_review_is_cached_and_current_content_is_required_for_delivery(ctx):
    service, _ = ctx
    task = ready(service)
    service.add_artifact(task, 'evidence_policy', {'required': True}, 'agent-v1')
    run_id = service.finalize_design(task)
    for case in service.legacy.get_run(run_id)['cases']:
        service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '人工审核', 'evidence-' + case['case_id'])
    assert any('依据复核' in issue for issue in service.legacy.gate(run_id))
    class Client:
        calls = 0
        def generate(self, context, settings):
            self.calls += 1
            return context.output_type.model_validate({'findings': []})
    client = Client()
    tools = {t.name: t for t in build_tools(service, task, client, ModelSettings(model_id='test-only'))}
    tools['check_case_evidence'].execute({})
    tools['check_case_evidence'].execute({})
    assert client.calls == 1 and not service.legacy.gate(run_id)
    case = service.legacy.get_run(run_id)['cases'][0]
    updated = {**case['content'], 'title': '人工修改标题后需要重新核对'}
    service.legacy.edit(run_id, case['case_id'], case['version'], case['revision'], updated, '', '更新', 'evidence-edit')
    case = service.legacy.get_run(run_id)['cases'][0]
    service.legacy.review(run_id, case['case_id'], case['version'], case['revision'], 'approve', '', '人工复核', 'evidence-reapprove')
    assert any('依据复核' in issue for issue in service.legacy.gate(run_id))


def test_unsupported_assertion_becomes_a_local_question_and_blocks_approval(ctx):
    service, _ = ctx
    task = ready(service)
    shared_rule = batch()
    shared_rule['cases'][0]['case']['case_id'] = 'TC3'
    service.save_batch(task, 'same-rule-other-case', shared_rule)
    class Client:
        def generate(self, context, settings):
            return context.output_type.model_validate({'findings': [{'case_id': 'TC1',
                'claim': '必须登录', 'verdict': 'unsupported', 'paragraph_ids': ['P002'],
                'rationale': '原文未规定登录条件'}]})
    tools = {t.name: t for t in build_tools(service, task, Client(), ModelSettings(model_id='test-only'))}
    tools['check_case_evidence'].execute({})
    run_id = service.finalize_design(task)
    run = service.legacy.get_run(run_id)
    c1, c2, c3 = run['cases']
    assert c1['content']['readiness'] == 'needs_clarification'
    assert c2['content']['readiness'] == 'ready'
    assert c3['content']['readiness'] == 'ready'
    assert len(service.get_task(task)['questions']) == 1
    with pytest.raises(ServiceError, match='澄清'):
        service.legacy.review(run_id, c1['case_id'], c1['version'], c1['revision'], 'approve', '', '通过', 'unsafe-approve')
    question = service.get_task(task)['questions'][0]
    service.submit_answer(task, question['id'], '原文补充：提交前必须登录。')
    child = service.confirm_answer(task, question['id'], '确认这条用例的登录条件')
    service.save_analysis(child, analysis())
    service.save_plan(child, plan())
    assert {d['case']['case_id'] for d in service.case_drafts(child)} == {'TC2', 'TC3'}


def test_scope_adjustment_cannot_silently_remove_an_unrelated_uncovered_rule(ctx):
    service, _ = ctx
    task = service.create_task(TEXT)
    content = analysis()
    content['business_rules'].append({'id': 'R3', 'statement': '待测试的第三条原文规则', 'status': 'clear', 'paragraph_ids': ['P003']})
    service.save_analysis(task, content)
    service.save_plan(task, plan())
    service.save_batch(task, 'a', batch())
    service.save_batch(task, 'b', batch(2))
    child = service.exclude_condition(task, ['C1'], '仅排除第一条件，其他缺口保留')
    assert service.coverage(child)['missing_rule_ids'] == ['R3']
    explicit = service.exclude_condition(task, [], '人工确认第三规则为本次范围外', ['R3'])
    assert not service.coverage(explicit)['missing_rule_ids']
    assert service.coverage(task)['missing_rule_ids'] == ['R3']


def test_deadline_is_validated_server_side(ctx):
    service, _ = ctx
    with pytest.raises(ServiceError, match='截止日期'):
        service.create_task(TEXT, due_at='tomorrow')
    task = service.create_task(TEXT, due_at='2026-11-10')
    assert service.get_task(task)['created_by'] == service.legacy.user['id']

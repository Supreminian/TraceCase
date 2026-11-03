"""Document purpose prevents embedded fixture facts becoming product requirements."""
from __future__ import annotations

import copy
import json

import pytest

from sdlc.agent.application import ModelUnavailableError
from sdlc.agent.domain import ModelSettings, UserMessage
from sdlc.domain import prepare_input
from sdlc.service import ServiceError
from sdlc.testing_agent.document_context import (
    DocumentContext, MAX_CONTEXT_REFERENCES, MAX_EMBEDDED_MATERIALS,
    build_document_context, section_context,
)


PLATFORM_PRD = '''# AI SDLC测试用例生成与人工复核平台

平台用户提交PRD，系统生成测试用例并支持人工逐条审核。

## 输入与人工复核

规则不足时提出澄清，不得编造业务规则。

## 演示PRD与歧义处理标准

以下报销片段只作为本平台的演示输入，不是本平台实现的业务功能。

申请人提交报销单，超过5000元需要二级审批。

| 输入情况 | 应有行为 | 禁止的推断 |
| --- | --- | --- |
| 未定义财务角色 | 提出澄清，明确内容先生成 | 不得补充财务审批权限 |
'''


class ServiceStub:
    def __init__(self, text):
        self.task = {'id': 'task', 'goal': '准备本产品明确需求的测试', 'scope': '本次PRD',
                     'input': prepare_input(text)}
        self.saved, self.writes = {}, []

    def get_task(self, task_id):
        assert task_id == 'task'
        return copy.deepcopy(self.task)

    def artifact(self, task_id, kind):
        assert task_id == 'task'
        return copy.deepcopy(self.saved.get(kind))

    def add_artifact(self, task_id, kind, content):
        assert task_id == 'task'
        self.saved[kind] = copy.deepcopy(content)
        self.writes.append((kind, copy.deepcopy(content)))


class ModelStub:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.contexts = output, error, []

    def generate(self, context, settings):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.output


def output(materials=None):
    return {'target_product': 'AI SDLC测试用例生成与人工复核平台',
            'document_purpose': '定义平台功能，并以外部业务PRD评测澄清与禁止推断能力',
            'embedded_materials': materials or []}


def settings():
    return ModelSettings(model_id='test-model')


def test_section_context_preserves_source_identity_and_nested_markdown_sections():
    prepared = prepare_input('前言\n\n# 产品\n\n正文\n\n### 子节\n\n```markdown\n# 代码里的标题\n```\n\n## 同级章节\n\n段落\n\nSetext heading\n---\n\n正文')
    paragraphs = copy.deepcopy(prepared['paragraphs'])
    enriched = section_context(paragraphs)
    assert paragraphs == prepared['paragraphs']
    assert [{k: v for k, v in item.items() if k != 'heading_hierarchy'} for item in enriched] == paragraphs
    assert enriched[0]['heading_hierarchy'] == []
    product = enriched[1]['heading_hierarchy'][0]
    assert product == {'paragraph_id': 'P002', 'level': 1, 'title': '产品'}
    code = next(item for item in enriched if item['type'] == 'code')
    assert [heading['title'] for heading in code['heading_hierarchy']] == ['产品', '子节']
    sibling = next(item for item in enriched if item['text'] == '## 同级章节')
    assert [heading['title'] for heading in sibling['heading_hierarchy']] == ['产品', '同级章节']
    assert enriched[-1]['heading_hierarchy'][-1]['title'] == 'Setext heading'
    enriched[2]['heading_hierarchy'][0]['title'] = 'changed copy'
    assert enriched[1]['heading_hierarchy'][0]['title'] == '产品'


def test_model_receives_complete_document_and_heading_context_as_untrusted_user_data():
    service = ServiceStub(PLATFORM_PRD)
    source = copy.deepcopy(service.task['input'])
    fixture_id = next(p['id'] for p in source['paragraphs'] if p['text'].startswith('申请人提交'))
    table_id = next(p['id'] for p in source['paragraphs'] if p['type'] == 'table')
    model = ModelStub(output([
        {'paragraph_ids': [fixture_id], 'kind': 'foreign_business_example',
         'reason': '外层章节明确将此另一业务规则作为平台输入材料'},
        {'paragraph_ids': [table_id], 'kind': 'supporting_example',
         'reason': '混合表格包含平台应有行为，必须保留，输入示例不是平台业务事实'},
    ]))
    result = build_document_context(service, 'task', model, settings())
    assert isinstance(result, DocumentContext)
    assert result.embedded_materials[0].paragraph_ids == [fixture_id]
    ctx = model.contexts[0]
    assert ctx.output_type is DocumentContext and ctx.tools == []
    assert isinstance(ctx.messages[0], UserMessage)
    payload = json.loads(ctx.messages[0].content)
    assert 'original_text' not in payload['document']
    assert 'normalized_text' not in payload['document']
    assert payload['document']['input_hash'] == source['input_hash']
    assert payload['document']['summary']['title'] == source['summary']['title']
    assert 'preview' not in payload['document']['summary']
    assert [p['text'] for p in payload['document']['paragraphs']] == [p['text'] for p in source['paragraphs']]
    assert payload['document']['paragraphs'][-1]['heading_hierarchy'][-1]['title'] == '演示PRD与歧义处理标准'
    assert len(payload['document']['paragraphs']) == len(source['paragraphs'])
    assert PLATFORM_PRD not in ctx.instruction
    assert '不要用' in ctx.instruction and '上下文' in ctx.instruction
    assert service.task['input'] == source
    assert service.saved['document_context'] == result.model_dump()


def test_reimbursement_product_supporting_examples_remain_without_keyword_filtering():
    text = '# 报销审批系统\n\n申请人提交报销单，金额大于5000元需要二级审批。\n\n## 正常业务示例\n\n例如提交5001元报销单，进入二级审批。'
    service = ServiceStub(text)
    sample_id = service.task['input']['paragraphs'][-1]['id']
    model = ModelStub({'target_product': '报销审批系统', 'document_purpose': '定义报销审批业务规则',
                      'embedded_materials': [{'paragraph_ids': [sample_id], 'kind': 'supporting_example',
                                              'reason': '阐释本系统真实金额规则的正常示例'}]})
    result = build_document_context(service, 'task', model, settings())
    assert result.target_product == '报销审批系统'
    assert result.embedded_materials[0].kind == 'supporting_example'
    assert any('5001元' in paragraph['text'] for paragraph in
               json.loads(model.contexts[0].messages[0].content)['document']['paragraphs'])
    assert all(p['id'] in {item['id'] for item in json.loads(model.contexts[0].messages[0].content)['document']['paragraphs']}
               for p in service.task['input']['paragraphs'])


def test_unclassified_paragraphs_are_preserved_and_validated_cache_avoids_model_call():
    service = ServiceStub(PLATFORM_PRD)
    model = ModelStub(output())
    first = build_document_context(service, 'task', model, settings())
    second = build_document_context(service, 'task', model, settings())
    assert first == second and first.embedded_materials == []
    assert len(model.contexts) == 1 and len(service.writes) == 1
    assert len(service.task['input']['paragraphs']) == len(section_context(service.task['input']['paragraphs']))


@pytest.mark.parametrize('materials', [
    [{'paragraph_ids': ['P999'], 'kind': 'foreign_business_example', 'reason': 'unknown paragraph'}],
    [{'paragraph_ids': ['P001', 'P001'], 'kind': 'superseded', 'reason': 'duplicate inside one group'}],
    [{'paragraph_ids': ['P001'], 'kind': 'foreign_business_example', 'reason': 'first classification'},
     {'paragraph_ids': ['P001'], 'kind': 'supporting_example', 'reason': 'conflicting classification'}],
    [{'paragraph_ids': [], 'kind': 'invalid_input_example', 'reason': 'missing evidence'}],
    [{'paragraph_ids': ['P001'], 'kind': 'unsupported_kind', 'reason': 'unrecognized kind'}],
])
def test_invalid_or_duplicate_context_references_never_enter_artifact_cache(materials):
    service = ServiceStub(PLATFORM_PRD)
    with pytest.raises(ServiceError, match='文档语境校验失败'):
        build_document_context(service, 'task', ModelStub(output(materials)), settings())
    assert service.saved == {} and service.writes == []


def test_invalid_cached_reference_is_not_silently_accepted_or_regenerated():
    service = ServiceStub(PLATFORM_PRD)
    service.saved['document_context'] = output([{
        'paragraph_ids': ['old-document-id'], 'kind': 'foreign_business_example', 'reason': 'stale reference'}])
    model = ModelStub(output())
    with pytest.raises(ServiceError, match='不存在'):
        build_document_context(service, 'task', model, settings())
    assert model.contexts == []


def test_context_limits_prevent_unbounded_or_ambiguous_classification():
    def material(index):
        return {'paragraph_ids': [f'P{index}'], 'kind': 'foreign_business_example', 'reason': 'fixture'}
    with pytest.raises(ValueError):
        DocumentContext.model_validate(output([material(index) for index in range(MAX_EMBEDDED_MATERIALS + 1)]))
    values = []
    for index in range(5):
        values.append({'paragraph_ids': [f'P{index}-{item}' for item in range(256)],
                       'kind': 'invalid_input_example', 'reason': 'fixture'})
    assert sum(len(item['paragraph_ids']) for item in values) > MAX_CONTEXT_REFERENCES
    with pytest.raises(ValueError):
        DocumentContext.model_validate(output(values))


def test_transport_failure_is_not_retried_and_never_cached():
    service = ServiceStub(PLATFORM_PRD)
    model = ModelStub(error=ModelUnavailableError('upstream unavailable'))
    with pytest.raises(ModelUnavailableError):
        build_document_context(service, 'task', model, settings())
    assert len(model.contexts) == 1 and service.saved == {}


def test_duplicate_input_ids_fail_before_model_call():
    service = ServiceStub(PLATFORM_PRD)
    service.task['input']['paragraphs'][1]['id'] = service.task['input']['paragraphs'][0]['id']
    model = ModelStub(output())
    with pytest.raises(ServiceError, match='ID重复'):
        build_document_context(service, 'task', model, settings())
    assert model.contexts == []



def test_model_payload_contains_one_complete_paragraph_copy_with_all_source_references():
    service = ServiceStub('# Unique product title\n\nA unique first requirement sentence.\n\n## Unique section\n\nA unique second requirement sentence.')
    model = ModelStub(output())
    build_document_context(service, 'task', model, settings())
    payload = json.loads(model.contexts[0].messages[0].content)
    document = payload['document']
    source = service.task['input']
    assert set(document) == {'summary', 'input_hash', 'paragraphs'}
    assert payload['task'] == {'goal': service.task['goal'], 'scope': service.task['scope']}
    assert [p['id'] for p in document['paragraphs']] == [p['id'] for p in source['paragraphs']]
    assert [(p['text'], p['start_line'], p['end_line']) for p in document['paragraphs']] == [
        (p['text'], p['start_line'], p['end_line']) for p in source['paragraphs']]
    encoded = json.dumps(payload, ensure_ascii=False)
    for paragraph in source['paragraphs']:
        assert encoded.count(json.dumps(paragraph['text'], ensure_ascii=False)) == 1
    assert document['paragraphs'][-1]['heading_hierarchy'][-1]['paragraph_id'] == source['paragraphs'][2]['id']

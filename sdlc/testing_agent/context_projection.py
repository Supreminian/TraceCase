"""A bounded view of stored tool data for conversational model calls.

Sessions keep their original messages. This pure projection is applied only at
the provider boundary; typed analysis/generation inputs retain the full source.
Omitted text is explicitly identified, never shortened into a new business fact.
"""
from __future__ import annotations

import hashlib
import json

from sdlc.agent.domain import AssistantMessage, Context, Decision, ToolResult


POLICY = 'stored-tool-context-v1'
MIN_TOOL_CHARACTERS = 4000
MAX_INLINE_TEXT = 400
FRESH_READ_TOOLS = {'read_requirement_paragraphs', 'read_saved_artifact'}
PROJECT_TOOLS = {
    'read_task', 'read_project_context', 'analyze_requirements', 'plan_coverage',
    'generate_case_batch', 'inspect_coverage', 'finalize_design',
    *FRESH_READ_TOOLS,
}
ARTIFACT_KEYS = {'analysis': 'analysis', 'coverage_plan': 'coverage_plan',
                 'plan': 'coverage_plan', 'batch': 'case_batch'}
IDENTIFIER_KEYS = {'id', 'module', 'artifact_key', 'input_hash', 'status', 'kind',
                   'priority', 'method', 'readiness', 'revision', 'version',
                   'document_version', 'tool_call_id'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier_key(key):
    return key in IDENTIFIER_KEYS or key.endswith('_id') or key.endswith('_ids')


def _index(value):
    """Keep every exact identifier and its dependency grouping, without prose."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if _identifier_key(key):
                result[key] = child
            elif isinstance(child, (dict, list)):
                nested = _index(child)
                if nested:
                    result[key] = nested
        return result
    if isinstance(value, list):
        return [entry for child in value if (entry := _index(child))]
    return None


def _shorten(value, locator, seen, *, key=''):
    if _identifier_key(key):
        return value
    if key in {'questions', 'confirmed_supplements', 'risks'}:
        # Answers, confirmations and free-form risks do not have an artifact
        # item lookup key. Keep them whole rather than hide unretrievable facts.
        return value
    if isinstance(value, str):
        # Whole field omission is explicit; a partial assertion could change its meaning.
        if len(value) > MAX_INLINE_TEXT and locator.get('kind') in {'analysis', 'coverage_plan', 'case_batch'}:
            return {'_omitted_text': {'characters': len(value), 'sha256': _digest(value),
                                      'read_from': locator}}
        if key in {'original_text', 'normalized_text'}:
            return {'_omitted_source': {'characters': len(value), 'sha256': _digest(value),
                'read_tool': 'read_requirement_paragraphs',
                'notice': '按同一需求输入内保留的段落ID读取原文。'}}
        return value
    if isinstance(value, list):
        return [_shorten(child, locator, seen) for child in value]
    if not isinstance(value, dict):
        return value
    if locator.get('kind') == 'case_batch' and key == 'case':
        # The orchestration model needs identity and coverage to choose its next
        # batch. Full executable bodies remain in the saved artifact and typed
        # evidence review, and can be reread explicitly by case_id.
        return {**_index(value),
                **{field: value[field] for field in ('title', 'category') if field in value},
                '_saved_case_body': {'sha256': _digest(value), 'read_from': locator,
                                     'section': 'cases', 'item_ids': [value.get('case_id')]}}
    result = {}
    for child_key, child in value.items():
        if child_key in ARTIFACT_KEYS and isinstance(child, dict):
            artifact = {'kind': ARTIFACT_KEYS[child_key],
                        'artifact_key': locator.get('artifact_key', 'default')
                        if child_key == 'batch' else 'default',
                        'read_tool': 'read_saved_artifact'}
            digest = _digest(child)
            if digest in seen:
                result[child_key] = {'_stored_artifact_reference': {**artifact,
                    'sha256': digest, 'tool_call_id': seen[digest]}, 'id_index': _index(child)}
            else:
                seen[digest] = locator['tool_call_id']
                result[child_key] = {'_stored_artifact': {**artifact, 'sha256': digest},
                    'data': _shorten(child, artifact | {'tool_call_id': locator['tool_call_id']}, seen)}
        elif child_key in {'paragraphs', 'headings'} and isinstance(child, list):
            result[child_key] = [{**_index(paragraph),
                **({'text': paragraph['text']} if child_key == 'headings' and len(paragraph.get('text', '')) <= MAX_INLINE_TEXT else {}),
                '_source': {'read_tool': 'read_requirement_paragraphs',
                            'paragraph_ids': [paragraph['id']]}}
                for paragraph in child if isinstance(paragraph, dict) and paragraph.get('id')]
        else:
            result[child_key] = _shorten(child, locator, seen, key=child_key)
    return result


def project_context(context: Context) -> Context:
    """Compact bulky business tool results without altering conversation protocol.

All user messages, assistant calls, tool-result IDs, errors and opaque provider
metadata remain intact. The latest explicit source/artifact read stays verbatim
so the model can retrieve exact omitted facts instead of guessing from an index.
"""
    if context.output_type not in {AssistantMessage, Decision}:
        return context
    calls = {call.id: call for message in context.messages if isinstance(message, AssistantMessage)
             for call in message.tool_calls}
    latest_read = next((message.tool_call_id for message in reversed(context.messages)
                        if isinstance(message, ToolResult)
                        and (call := calls.get(message.tool_call_id))
                        and call.name in FRESH_READ_TOOLS and not message.is_error), None)
    seen, replacements = {}, {}
    for index in range(len(context.messages) - 1, -1, -1):
        message = context.messages[index]
        if not isinstance(message, ToolResult) or message.is_error:
            continue
        call = calls.get(message.tool_call_id)
        if (not call or call.name not in PROJECT_TOOLS
                or len(message.content) < MIN_TOOL_CHARACTERS
                or message.tool_call_id == latest_read):
            continue
        try:
            content = json.loads(message.content)
        except (ValueError, TypeError):
            continue
        if not isinstance(content, (dict, list)):
            continue
        locator = {'read_tool': 'read_saved_artifact', 'tool_call_id': call.id,
                   'artifact_key': call.arguments.get('batch_key', 'default')}
        if call.name == 'read_saved_artifact' and isinstance(content, dict):
            locator.update({key: content[key] for key in ('kind', 'artifact_key', 'section')
                            if key in content})
        projected = {'_context_projection': {'policy': POLICY, 'source_tool': call.name,
            'tool_call_id': call.id, 'original_characters': len(message.content),
            'sha256': _digest(content),
            'notice': '这是已保存工具结果的紧凑视图。省略字段不是业务事实；需要精确内容时按ID重新读取。'},
            'data': _shorten(content, locator, seen)}
        rendered = _json(projected)
        # Short results and already compact indexes must never grow through wrapping.
        if len(rendered) < len(message.content):
            replacements[index] = message.model_copy(update={'content': rendered}, deep=True)
    if not replacements:
        return context
    return Context(context.instruction, list(context.tools),
                   [replacements.get(index, message.model_copy(deep=True))
                    for index, message in enumerate(context.messages)],
                   context.output_type, context.memory, context.agents_md)

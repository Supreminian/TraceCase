"""Strict input, JSON schema and reference checks; semantic review remains human."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

from markdown_it import MarkdownIt
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "1.0"
MAX_FILE_BYTES = 102_400
MAX_CHARACTERS = 20_000


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain non-whitespace text")
    return value


Text = Annotated[str, Field(min_length=1), AfterValidator(_not_blank)]
References = Annotated[list[Text], Field(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Feature(StrictModel):
    id: Text
    description: Text
    paragraph_ids: References


class Role(StrictModel):
    id: Text
    name: Text
    responsibilities: list[Text]
    paragraph_ids: References


class BusinessRule(StrictModel):
    id: Text
    statement: Text
    status: Literal["clear", "ambiguous", "conflicting", "incomplete"]
    paragraph_ids: References


class Clarification(StrictModel):
    id: Text
    question: Text
    paragraph_ids: References
    requirement_ids: list[Text]
    affected_case_ids: list[Text]
    scope: Literal["case", "run"]
    blocking: bool
    rationale: Text | None = None


class Step(StrictModel):
    number: Annotated[int, Field(ge=1)]
    action: Text


class TestCase(StrictModel):
    __test__ = False
    case_id: Text
    title: Text
    requirement_ids: References
    paragraph_ids: References
    preconditions: Annotated[list[Text], Field(min_length=1)]
    steps: Annotated[list[Step], Field(min_length=1)]
    expected_results: Annotated[list[Text], Field(min_length=1)]
    priority: Literal["P0", "P1", "P2"]
    category: Annotated[Text, Field(max_length=64)]
    readiness: Literal["ready", "needs_clarification"]
    clarification_ids: list[Text]


class OutputModel(StrictModel):
    schema_version: Literal["1.0"]
    features: list[Feature]
    roles: list[Role]
    business_rules: list[BusinessRule]
    clarifications: list[Clarification]
    test_cases: list[TestCase]


def normalize_text(text: str) -> str:
    """Only normalize the leading BOM and newline conventions."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _paragraphs(text: str) -> list[dict]:
    parser = MarkdownIt("commonmark").enable("table")
    lines = text.split("\n")
    blocks: list[tuple[int, int, str]] = []
    aliases = {
        "heading_open": "heading", "paragraph_open": "paragraph",
        "bullet_list_open": "list", "ordered_list_open": "list",
        "table_open": "table", "fence": "code", "code_block": "code",
        "blockquote_open": "blockquote", "html_block": "html",
        "hr": "thematic_break",
    }
    for token in parser.parse(text):
        if token.level == 0 and token.map and token.nesting != -1:
            start, end = token.map
            if token.nesting == 1:
                # Markdown list maps may include the separating blank line.
                # Keep content and line spans exact while excluding that separator.
                while end > start and not lines[end - 1].strip():
                    end -= 1
            blocks.append((start, end, aliases.get(token.type, token.type)))
    covered = {line for start, end, _ in blocks for line in range(start, end)}
    # Link-reference definitions can be consumed without a token; preserve them.
    missing_start = None
    for index in range(len(lines) + 1):
        missing = index < len(lines) and index not in covered and bool(lines[index].strip())
        if missing and missing_start is None:
            missing_start = index
        elif not missing and missing_start is not None:
            blocks.append((missing_start, index, "reference_or_text"))
            missing_start = None
    return [
        {"id": f"P{index:03d}", "text": "\n".join(lines[start:end]),
         "type": kind, "start_line": start + 1, "end_line": end}
        for index, (start, end, kind) in enumerate(sorted(blocks), start=1)
    ]


def prepare_input(text: str | None = None, filename: str | None = None,
                  data: bytes | None = None) -> dict:
    if text is not None and (data is not None or filename is not None):
        raise ValueError("粘贴和文件上传不能同时生效。")
    if data is not None:
        if not filename or Path(filename).suffix.lower() not in {".md", ".markdown"}:
            raise ValueError("仅支持 .md / .markdown 文件。")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("文件超过 100 KiB（102,400 字节）。")
        try:
            original = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("文件必须采用 UTF-8 编码。") from exc
    elif filename is not None:
        raise ValueError("未收到上传文件内容。")
    elif isinstance(text, str):
        original = text
    else:
        raise ValueError("请输入 Markdown 正文或上传文件。")
    normalized = normalize_text(original)
    if "\x00" in normalized:
        raise ValueError("输入包含不允许的 NUL 字符。")
    if not normalized.strip():
        raise ValueError("PRD 不能为空白。")
    if len(normalized) > MAX_CHARACTERS:
        raise ValueError("正文超过 20,000 个 Unicode 字符。")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("输入包含无效 Unicode 字符。") from exc
    paragraphs = _paragraphs(normalized)
    title_block = next((p["text"].split("\n")[0] for p in paragraphs if p["type"] == "heading"), "未命名 PRD")
    title = re.sub(r"^#{1,6}\s+|\s+#+\s*$", "", title_block)
    return {"original_text": original, "normalized_text": normalized,
            "input_hash": hashlib.sha256(encoded).hexdigest(),
            "summary": {"title": title, "character_count": len(normalized), "preview": normalized[:200]},
            "paragraphs": paragraphs}


def _schema_errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(part) for part in error['loc']) or '$'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)]


def validate_current(output: dict, paragraphs: list[dict]) -> list[str]:
    """Validate the whole current snapshot, including currently active clarifications.

    The service retains immutable originals and resolves historical questions before
    passing this projection. Existence checks cannot establish semantic support.
    """
    try:
        parsed = OutputModel.model_validate(output)
    except ValidationError as exc:
        return _schema_errors(exc)
    errors: list[str] = []
    paragraph_ids = {p["id"] for p in paragraphs}
    rules = {rule.id: rule for rule in parsed.business_rules}
    cases = {case.case_id: case for case in parsed.test_cases}
    questions = {question.id: question for question in parsed.clarifications}
    all_ids: list[str] = []
    for name, objects, id_field in (
        ("features", parsed.features, "id"), ("roles", parsed.roles, "id"),
        ("business_rules", parsed.business_rules, "id"),
        ("clarifications", parsed.clarifications, "id"), ("test_cases", parsed.test_cases, "case_id"),
    ):
        for index, item in enumerate(objects):
            item_id = getattr(item, id_field)
            if item_id in all_ids:
                errors.append(f"{name}.{index}.{id_field}: 重复 ID {item_id}")
            all_ids.append(item_id)
            for field, target in (("paragraph_ids", paragraph_ids), ("requirement_ids", set(rules)),
                                  ("affected_case_ids", set(cases)), ("clarification_ids", set(questions))):
                if not hasattr(item, field):
                    continue
                values = getattr(item, field)
                if len(values) != len(set(values)):
                    errors.append(f"{name}.{index}.{field}: 引用不能重复")
                for value in values:
                    if value not in target:
                        errors.append(f"{name}.{index}.{field}: 引用不存在 {value}")
    for index, question in enumerate(parsed.clarifications):
        if not question.blocking and question.rationale is None:
            errors.append(f"clarifications.{index}.rationale: 非阻断问题必须说明依据")
        if question.scope == "case" and not question.affected_case_ids:
            errors.append(f"clarifications.{index}.affected_case_ids: case 范围须列出影响用例；无局部用例时使用 run")
    for index, rule in enumerate(parsed.business_rules):
        if rule.status != "clear" and not any(rule.id in q.requirement_ids for q in parsed.clarifications):
            errors.append(f"business_rules.{index}: 非明确规则 {rule.id} 必须关联澄清问题")
    for index, case in enumerate(parsed.test_cases):
        if [step.number for step in case.steps] != list(range(1, len(case.steps) + 1)):
            errors.append(f"test_cases.{index}.steps: 步骤编号必须从 1 开始连续递增")
        blocking_ids = {
            q.id for q in parsed.clarifications if q.blocking and (
                q.scope == "run" or case.case_id in q.affected_case_ids
                or set(case.requirement_ids).intersection(q.requirement_ids)
            )
        }
        if set(case.clarification_ids) != blocking_ids:
            errors.append(f"test_cases.{index}.clarification_ids: 必须与当前关联阻断问题一致：{sorted(blocking_ids)}")
        expected_readiness = "needs_clarification" if blocking_ids else "ready"
        if case.readiness != expected_readiness:
            errors.append(f"test_cases.{index}.readiness: 应为 {expected_readiness}")
    return errors


def coverage_report(output: dict) -> dict:
    """Reference coverage is a quality signal, not proof of semantic completeness."""
    linked = {rid for case in output.get('test_cases', []) for rid in case['requirement_ids']}
    rules = output.get('business_rules', [])
    missing = [{'id': r['id'], 'statement': r['statement']} for r in rules if r['id'] not in linked]
    return {'rule_count': len(rules), 'linked_rule_count': len(rules) - len(missing),
            'uncovered_rules': missing}


def _reject_constant(value: str):
    raise ValueError(f"非标准 JSON 常量 {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象包含重复键：{key}")
        result[key] = value
    return result


def validate_output(raw: str, paragraphs: list[dict]) -> dict:
    try:
        output = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        if not isinstance(output, dict):
            raise ValueError("顶层必须为单一 JSON 对象。")
    except (ValueError, TypeError, RecursionError) as exc:
        return {"json_valid": False, "valid": False, "errors": [f"JSON: {exc}"], "output": None}
    warnings = []
    adjustments = []
    try:
        # Only compute presentation metadata after strict field/type validation.
        # Never infer business rules, rewrite unknown references or change source evidence.
        output = OutputModel.model_validate(output).model_dump()
        rules = {r['id']: r for r in output['business_rules']}
        questions = output['clarifications']
        question_ids = {q['id'] for q in questions}
        used_ids = {item.get('id', item.get('case_id')) for group in output.values()
                    if isinstance(group, list) for item in group}
        for rule in rules.values():
            if rule['status'] == 'clear' or any(rule['id'] in q['requirement_ids'] for q in questions):
                continue
            qid = 'CL-RULE-' + rule['id']
            while qid in used_ids:
                qid += '-1'
            used_ids.add(qid)
            question_ids.add(qid)
            affected = [c['case_id'] for c in output['test_cases'] if rule['id'] in c['requirement_ids']]
            questions.append({'id': qid, 'question': f"请确认规则中的未明确条件：{rule['statement']}",
                'paragraph_ids': rule['paragraph_ids'].copy(), 'requirement_ids': [rule['id']],
                'affected_case_ids': affected, 'scope': 'case' if affected else 'run',
                'blocking': True, 'rationale': None})
            warnings.append(f"规则 {rule['id']} 被标为未明确，已补充人工澄清入口，未补写业务规则。")
        for case in output['test_cases']:
            if set(case['clarification_ids']) - question_ids:
                continue  # Unknown references still require correction, not silent deletion.
            blocking = [q['id'] for q in questions if q['blocking'] and (
                q['scope'] == 'run' or case['case_id'] in q['affected_case_ids']
                or set(case['requirement_ids']).intersection(q['requirement_ids']))]
            created_question = None
            if not blocking and case['readiness'] == 'needs_clarification':
                # Preserve explicit uncertainty instead of silently declaring it ready.
                candidate = 'CL-AUTO-' + case['case_id']
                while candidate in used_ids:
                    candidate += '-1'
                created_question = candidate
                used_ids.add(candidate)
                question_ids.add(candidate)
                questions.append({'id': candidate,
                    'question': f"用例“{case['title']}”被模型标记待澄清但未关联阻断问题，请审核人确认缺失条件。",
                    'paragraph_ids': case['paragraph_ids'].copy(), 'requirement_ids': [],
                    'affected_case_ids': [case['case_id']], 'scope': 'case', 'blocking': True,
                    'rationale': None})
                blocking = [candidate]
            readiness = 'needs_clarification' if blocking else 'ready'
            if set(case['clarification_ids']) != set(blocking) or case['readiness'] != readiness:
                adjustments.append({'case_id': case['case_id'],
                    'created_clarification_id': created_question,
                    'before': {'clarification_ids': case['clarification_ids'], 'readiness': case['readiness']},
                    'after': {'clarification_ids': blocking, 'readiness': readiness}})
                case.update(clarification_ids=blocking, readiness=readiness)
        for group in ('clarifications', 'test_cases'):
            inherited = [item.get('case_id', item.get('id')) for item in output[group]
                         if {p for rid in item['requirement_ids'] if rid in rules
                             for p in rules[rid]['paragraph_ids']} - set(item['paragraph_ids'])]
            if inherited:
                warnings.append(f"{'澄清问题' if group == 'clarifications' else '用例'} {', '.join(inherited)} "
                                "的补充来源可通过关联规则查看，无需重复列出全部段落；请人工核对语义依据。")
        if adjustments:
            warnings.append(f"已按问题的实际关联关系整理 {len(adjustments)} 条用例的待澄清状态；未修改业务内容。")
    except ValidationError:
        pass  # Return the usual actionable schema errors below.
    errors = validate_current(output, paragraphs)
    coverage = None
    if not errors:
        coverage = coverage_report(output)
        if coverage['uncovered_rules']:
            warnings.append("以下需求尚未关联用例，请在审核时补充覆盖："
                            + '、'.join(r['id'] for r in coverage['uncovered_rules'])
                            + "。现有用例可继续审核；导出会保留覆盖缺口说明。")
    return {"json_valid": True, "valid": not errors, "errors": errors, "output": output,
            "warnings": warnings, "adjustments": adjustments, "coverage": coverage,
            "validation_policy": "review-friendly-2"}

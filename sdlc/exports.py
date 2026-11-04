"""Pure serializers. Authorization and immutable snapshots live in the service."""

import csv
import html
import io
import re

from sdlc.domain import coverage_report


def safe_cell(value):
    text = str(value)
    if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) or text.startswith(
        ("\t", "\r", "\n")
    ):
        return "'" + text
    return text


def md(value):
    text = html.escape(str(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", text).replace("\n", "<br>")


def render_export(run, cases, approvals, export_id, fmt):
    coverage = coverage_report({**(run.get('output') or {}), 'test_cases': [c['content'] for c in cases]})
    missing = coverage['uncovered_rules']
    coverage_note = ('存在未关联用例的需求：' + '、'.join(r['id'] for r in missing)) if missing else '所有已提取规则均有关联用例；语义覆盖仍以人工复核为准'
    fields = [
        "project_id", "project_name", "run_id", "export_id", "prompt_version", "input_hash", "case_id", "version",
        "title", "requirement_ids", "paragraph_ids", "preconditions", "steps",
        "expected_results", "priority", "category", "approved_by", "approved_at",
        "approval_event_id",
        "coverage_note",
        "execution_status",
    ]
    delivery = run.get('delivery_details') or {}
    extra_fields = ['task_id', 'baseline_id', 'document_version', 'scope', 'role', 'condition_ids', 'test_data', 'cleanup', 'method'] if delivery else []
    rows = []
    for case in cases:
        c = case["content"]
        approval = approvals[case["case_id"]]
        rows.append(dict(zip(fields, [
            run["project_id"], run["project_name"], run["id"], export_id, run["prompt"]["version"], run["input"]["input_hash"],
            c["case_id"], case["version"], c["title"], ", ".join(c["requirement_ids"]),
            ", ".join(c["paragraph_ids"]), "\n".join(c["preconditions"]),
            "\n".join(f'{s["number"]}. {s["action"]}' for s in c["steps"]),
            "\n".join(c["expected_results"]), c["priority"], c["category"],
            approval["operator"], approval["created_at"], approval["id"], coverage_note, 'not_executed',
        ], strict=True)))
        if delivery:
            details = (delivery.get('case_details') or {}).get(c['case_id'], {})
            rows[-1].update({key: delivery.get(key, '') for key in extra_fields[:4]})
            rows[-1].update({key: '\n'.join(details.get(key, [])) if key in {'condition_ids', 'test_data', 'cleanup'} else details.get(key, '') for key in extra_fields[4:]})
    fields += extra_fields
    if fmt == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: safe_cell(v) for k, v in row.items()} for row in rows)
        return stream.getvalue().encode("utf-8-sig"), "text/csv"
    lines = [
        "# 测试用例表 · 批准快照", "",
        f'项目：{md(run["project_name"])}  ', f'项目 ID：{md(run["project_id"])}  ',
        f'运行：{md(run["id"])}  ', f'导出：{md(export_id)}  ',
        f'Prompt：{md(run["prompt"]["version"])}  ',
        f'输入 SHA-256：{md(run["input"]["input_hash"])}', "",
        "此文件保存导出当时的批准版本。后续修订不会改变本快照。", "",
        "状态说明：用例设计已评审批准；测试尚未执行。", "",
        "需求覆盖说明：" + md(coverage_note), "",
        "| 编号 | 版本 | 标题 | 需求 / 段落 | 优先级 | 分类 | 通过人 | 通过时间 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        values = [row["case_id"], row["version"], row["title"],
                  row["requirement_ids"] + " / " + row["paragraph_ids"], row["priority"],
                  row["category"], row["approved_by"], row["approved_at"]]
        lines.append("| " + " | ".join(md(value) for value in values) + " |")
    for row in rows:
        lines += ["", f'## {md(row["case_id"])} · {md(row["title"])}', ""]
        detail_fields = [('执行角色', 'role'), ('测试数据准备', 'test_data')] if delivery else []
        detail_fields += [("前置条件", "preconditions"), ("步骤", "steps"), ("预期", "expected_results")]
        if delivery:
            detail_fields += [('清理与恢复', 'cleanup'), ('测试条件', 'condition_ids'), ('测试方法', 'method')]
        for label, key in detail_fields:
            lines += [f"**{label}**", "", md(row[key]), ""]
        lines.append(f'审批事件：{md(row["approval_event_id"])}')
    if delivery:
        lines += ['', '## 批准基线与范围', '',
                  '任务：' + md(delivery['task_id']), '基线：' + md(delivery['baseline_id']),
                  '需求版本：' + md(delivery['document_version']), '范围：' + md(delivery['scope'])]
    lines += ["", "## 来源段落", ""]
    for p in run["input"]["paragraphs"]:
        lines += [f'### {md(p["id"])}', "", md(p["text"]), ""]
    if missing:
        lines += ['', '## 尚未关联用例的需求', '']
        lines += [md(r['id']) + '：' + md(r['statement']) for r in missing]
    return "\n".join(lines).encode("utf-8"), "text/markdown"

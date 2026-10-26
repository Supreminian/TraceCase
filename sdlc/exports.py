"""Pure serializers. Authorization and immutable snapshots live in the service."""

import csv
import html
import io
import re


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
    fields = [
        "run_id", "export_id", "prompt_version", "input_hash", "case_id", "version",
        "title", "requirement_ids", "paragraph_ids", "preconditions", "steps",
        "expected_results", "priority", "category", "approved_by", "approved_at",
        "approval_event_id",
    ]
    rows = []
    for case in cases:
        c = case["content"]
        approval = approvals[case["case_id"]]
        rows.append(dict(zip(fields, [
            run["id"], export_id, run["prompt"]["version"], run["input"]["input_hash"],
            c["case_id"], case["version"], c["title"], ", ".join(c["requirement_ids"]),
            ", ".join(c["paragraph_ids"]), "\n".join(c["preconditions"]),
            "\n".join(f'{s["number"]}. {s["action"]}' for s in c["steps"]),
            "\n".join(c["expected_results"]), c["priority"], c["category"],
            approval["operator"], approval["created_at"], approval["id"],
        ], strict=True)))
    if fmt == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: safe_cell(v) for k, v in row.items()} for row in rows)
        return stream.getvalue().encode("utf-8-sig"), "text/csv"
    lines = [
        "# 费用报销测试表 · 批准快照", "",
        f'运行：{md(run["id"])}  ', f'导出：{md(export_id)}  ',
        f'Prompt：{md(run["prompt"]["version"])}  ',
        f'输入 SHA-256：{md(run["input"]["input_hash"])}', "",
        "此文件保存导出当时的批准版本。后续修订不会改变本快照。", "",
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
        for label, key in [("前置条件", "preconditions"), ("步骤", "steps"), ("预期", "expected_results")]:
            lines += [f"**{label}**", "", md(row[key]), ""]
        lines.append(f'审批事件：{md(row["approval_event_id"])}')
    lines += ["", "## 来源段落", ""]
    for p in run["input"]["paragraphs"]:
        lines += [f'### {md(p["id"])}', "", md(p["text"]), ""]
    return "\n".join(lines).encode("utf-8"), "text/markdown"

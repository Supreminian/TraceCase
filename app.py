"""Local Streamlit workspace for the traceable test-case prototype."""
from __future__ import annotations

import copy
import hashlib
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from sdlc.domain import prepare_input
from sdlc.providers import list_samples
from sdlc.service import Service, ServiceError


ROOT = Path(__file__).resolve().parent
PAGES = ["新建运行", "运行详情与审核", "运行历史"]
STATUS = {"pending_review": "待审核", "approved": "已通过", "returned": "已退回"}
CATEGORY = {"normal": "正常", "boundary": "边界", "exception": "异常"}


def local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


def display_error(error: Exception) -> None:
    st.error("操作未完成，请检查下列原因。若版本已变化，请刷新后重新审核。")
    st.code(str(error), language=None)


def request_id() -> str:
    return str(uuid4())


def go_to(page: str, run_id: str | None = None) -> None:
    st.session_state["_navigate_to"] = page
    if run_id:
        st.session_state["selected_run"] = run_id
    st.rerun()


def success(message: str) -> None:
    st.session_state["_notice"] = message
    st.rerun()


def paragraph_text(paragraph: dict) -> str:
    return paragraph.get("text", paragraph.get("content", ""))


def source_view(document: dict, key: str, preferred: list[str] | None = None) -> None:
    paragraphs = document.get("paragraphs", [])
    if not paragraphs:
        st.info("没有可显示的来源段落。")
        return
    mapping = {p["id"]: p for p in paragraphs}
    ids = list(mapping)
    index = next((ids.index(p) for p in (preferred or []) if p in ids), 0)
    selected = st.selectbox("定位来源段落", ids, index=index, key=key)
    paragraph = mapping[selected]
    st.text(
        f"{selected} · {paragraph.get('kind', paragraph.get('type', '段落'))} · "
        f"行 {paragraph.get('start_line', '—')}–{paragraph.get('end_line', '—')}"
    )
    st.code(paragraph_text(paragraph), language="markdown", wrap_lines=True)


def new_run(service: Service, operator: str) -> None:
    st.title("从需求到可追溯测试")
    st.caption("输入 → 结构校验 → 逐条复核 → 批准导出")
    st.info("固定样例模式 · 未调用模型。当前只接受与预置样例完全匹配的输入。")
    st.caption("仅使用虚构的费用报销数据。上传 .md / .markdown，最大 100 KiB；正文最多 20,000 字符。")
    if not operator.strip():
        st.caption("开始前请展开左上角侧栏，填写操作者标识。")
    source = st.radio("输入方式", ["固定样例", "粘贴 Markdown", "上传 Markdown"],
                      horizontal=True, key="input_source")
    kwargs: dict = {}
    samples = list_samples()
    if source == "固定样例":
        mapping = {sample["id"]: sample for sample in samples}
        sample_id = st.selectbox("演示样例", list(mapping),
                                 format_func=lambda item: mapping[item]["label"], key="sample")
        kwargs["text"] = mapping[sample_id]["text"]
        with st.expander("查看完整样例 PRD", expanded=False):
            st.code(kwargs["text"], language="markdown", wrap_lines=True)
    elif source == "粘贴 Markdown":
        kwargs["text"] = st.text_area("PRD 正文", height=300, key="prd_text",
                                      placeholder="# 虚构费用报销审批\n\n请粘贴需求正文。")
    else:
        uploaded = st.file_uploader("选择 Markdown 文件", type=["md", "markdown"], key="prd_file")
        if uploaded is not None:
            kwargs = {"filename": uploaded.name, "data": uploaded.getvalue()}
    parent = st.text_input("关联父运行 ID（可选）", key="parent_run_id",
                           help="补充业务事实或重新生成时关联旧运行；原记录与审批保留。")
    prompt_version = st.selectbox("Prompt 版本", ["v2", "v1"], key="prompt_version")
    st.caption("样例结果固定，切换 Prompt 仅验证配置留存，不能用于比较模型效果。")
    left, right = st.columns(2)
    preview = left.button("检查输入与段落", width="stretch")
    generate = right.button("生成测试用例", type="primary", key="generate",
                            width="stretch", disabled=not operator.strip())
    fingerprint = hashlib.sha256(repr(kwargs).encode("utf-8")).hexdigest()
    if preview:
        try:
            document = prepare_input(**kwargs)
            st.session_state["input_preview"] = {"fingerprint": fingerprint, "document": document}
        except (ServiceError, ValueError) as error:
            display_error(error)
    saved_preview = st.session_state.get("input_preview")
    if saved_preview and saved_preview["fingerprint"] == fingerprint:
        st.subheader("输入快照预览")
        source_view(saved_preview["document"], "preview_paragraph")
        with st.expander("输入摘要与哈希"):
            st.json({k: v for k, v in saved_preview["document"].items()
                     if k not in {"paragraphs", "text", "raw_text", "normalized_text", "original_text"}})
    if generate:
        try:
            with st.spinner("保存输入、生成固定样例并校验…"):
                run_id = service.generate(**kwargs, operator=operator, prompt_version=prompt_version,
                                          parent_run_id=parent.strip() or None,
                                          request_id=request_id())
            go_to(PAGES[1], run_id)
        except (ServiceError, ValueError) as error:
            display_error(error)


def lines(label: str, values: list[str], key: str) -> list[str]:
    value = st.text_area(label, value="\n".join(values), key=key)
    return [line.strip() for line in value.splitlines() if line.strip()]


def cases_tab(service: Service, run: dict, operator: str) -> None:
    cases = run.get("cases", [])
    if not cases:
        st.info("当前运行没有可审核用例。请查看校验与原始输出。")
        return
    mapping = {case["case_id"]: case for case in cases}
    case_id = st.selectbox("选择用例", list(mapping), key=f"case_selector_{run['id']}")
    case = mapping[case_id]
    content = case["content"]
    token = f"{run['id']}_{case_id}_{case['version']}_{case['revision']}"
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("当前用例")
        st.text(content["title"])
        st.text(f"{case_id} · v{case['version']} · {STATUS.get(case['status'], case['status'])} · "
                f"{content['priority']} · {CATEGORY.get(content['category'], content['category'])}")
        if content.get("readiness") != "ready":
            st.warning("此用例尚待澄清，不能通过。请先处理问题或补充 PRD 创建关联新运行。")
        st.text("关联规则：" + "、".join(content["requirement_ids"]))
        st.text("来源段落：" + "、".join(content["paragraph_ids"]))
        for label, values in [("前置条件", content["preconditions"]),
                              ("步骤", [f"{step['number']}. {step['action']}" for step in content["steps"]]),
                              ("预期结果", content["expected_results"])]:
            st.caption(label)
            st.code("\n".join(values), language=None, wrap_lines=True)
    with right:
        st.subheader("核对原文")
        source_view(run["input"], f"case_source_{token}", content["paragraph_ids"])
        st.caption("引用存在不代表内容有依据；请检查边界、权限、前置条件和可判断的预期。")
    with st.form(f"review_{token}"):
        st.subheader("逐条审核")
        action = st.radio("审核动作", ["通过", "退回"], horizontal=True, key=f"action_{token}")
        reason = st.text_area("审核理由", key=f"review_reason_{token}",
                              placeholder="说明已核对的依据，或具体退回原因。")
        submitted = st.form_submit_button("提交审核", type="primary", disabled=not operator.strip())
    if submitted:
        try:
            service.review(run["id"], case_id, case["version"], case["revision"],
                           "approve" if action == "通过" else "return", operator, reason, request_id())
            success("审核事件已保存。")
        except (ServiceError, ValueError) as error:
            display_error(error)
    with st.expander("修改当前用例 · 保存后重新审核"):
        st.caption("每次修改创建新版本。readiness 与阻断问题关联由服务层计算，不能直接改写。")
        output = run.get("output") or {}
        rule_ids = [rule["id"] for rule in output.get("business_rules", [])]
        paragraph_ids = [p["id"] for p in run["input"].get("paragraphs", [])]
        with st.form(f"edit_{token}"):
            title = st.text_input("用例标题", value=content["title"], key=f"edit_title_{token}")
            requirements = st.multiselect("关联规则", rule_ids, default=content["requirement_ids"])
            paragraphs = st.multiselect("来源段落", paragraph_ids, default=content["paragraph_ids"])
            preconditions = lines("前置条件（每行一项）", content["preconditions"], f"pre_{token}")
            steps = lines("步骤（每行一步）", [s["action"] for s in content["steps"]], f"steps_{token}")
            expected = lines("预期结果（每行一项）", content["expected_results"], f"expected_{token}")
            priority = st.selectbox("优先级建议", ["P0", "P1", "P2"],
                                    index=["P0", "P1", "P2"].index(content["priority"]))
            category = st.selectbox("分类", list(CATEGORY),
                                    index=list(CATEGORY).index(content["category"]),
                                    format_func=lambda item: CATEGORY[item])
            edit_reason = st.text_area("修改理由", key=f"edit_reason_{token}")
            edit_submitted = st.form_submit_button("保存修订（生成新版本）", disabled=not operator.strip())
        if edit_submitted:
            updated = copy.deepcopy(content)
            updated.update(title=title, requirement_ids=requirements, paragraph_ids=paragraphs,
                           preconditions=preconditions,
                           steps=[{"number": i, "action": action} for i, action in enumerate(steps, 1)],
                           expected_results=expected, priority=priority, category=category)
            try:
                service.edit(run["id"], case_id, case["version"], case["revision"], updated,
                             operator, edit_reason, request_id())
                success("新版本已保存，状态为待审核；旧版本和审批记录已保留。")
            except (ServiceError, ValueError) as error:
                display_error(error)
    with st.expander("此用例的全部版本"):
        versions = [v for v in run.get("case_versions", []) if v["case_id"] == case_id]
        st.json(versions)


def clarifications_tab(service: Service, run: dict, operator: str) -> None:
    paragraphs = [p["id"] for p in run["input"].get("paragraphs", [])]
    rules = [r["id"] for r in (run.get("output") or {}).get("business_rules", [])]
    cases = [case["case_id"] for case in run.get("cases", [])]
    st.caption("新增业务事实需修订 PRD 并创建关联新运行。误报与非阻断认定必须有原文依据；受影响用例将重新审核。")
    clarifications = run.get("clarifications", [])
    if clarifications:
        mapping = {c["content"]["id"]: c for c in clarifications}
        selected = st.selectbox("选择澄清项", list(mapping), key=f"clarification_{run['id']}")
        item = mapping[selected]
        st.json(item)
        source_view(run["input"], f"clarification_source_{run['id']}_{selected}",
                    item["content"]["paragraph_ids"])
        if item.get("status", "open") == "open":
            with st.form(f"resolve_{run['id']}_{selected}_{run['revision']}"):
                resolution = st.radio("处理结论", ["原文已明确，属于误报", "不影响执行和判断，可认定非阻断"])
                evidence = st.multiselect("支持结论的来源段落", paragraphs)
                reason = st.text_area("处理理由", placeholder="说明原文如何支持结论，不能在此新增业务规则。")
                submitted = st.form_submit_button("保存澄清处理", disabled=not operator.strip())
            if submitted:
                try:
                    service.resolve_clarification(run["id"], selected, run["revision"],
                                                  "false_positive" if resolution.startswith("原文") else "nonblocking",
                                                  evidence, operator, reason, request_id())
                    success("处理事件已保存，受影响用例已生成待审核的新版本。")
                except (ServiceError, ValueError) as error:
                    display_error(error)
    else:
        st.info("没有已记录的澄清项。识别结果仍需人工检查，可在下方补充遗漏。")
    with st.expander("新增阻断性澄清"):
        with st.form(f"add_clarification_{run['id']}_{run['revision']}"):
            question = st.text_area("需要澄清的问题")
            source_ids = st.multiselect("问题来源段落", paragraphs)
            requirement_ids = st.multiselect("涉及业务规则", rules)
            scope = st.radio("影响范围", ["整次运行", "指定用例"], horizontal=True)
            case_ids = st.multiselect("受影响用例", cases)
            reason = st.text_area("新增理由")
            submitted = st.form_submit_button("记录阻断问题", disabled=not operator.strip())
        if submitted:
            content = {"id": "CL-H" + uuid4().hex[:8].upper(), "question": question,
                       "paragraph_ids": source_ids, "requirement_ids": requirement_ids,
                       "affected_case_ids": case_ids, "scope": "run" if scope == "整次运行" else "case",
                       "blocking": True, "rationale": None}
            try:
                service.add_clarification(run["id"], content, run["revision"], operator,
                                          reason, request_id())
                success("阻断问题已记录，当前导出资格已重新计算。")
            except (ServiceError, ValueError) as error:
                display_error(error)


def exports_view(service: Service, run: dict, operator: str, blockers: list[str]) -> None:
    st.subheader("批准版本导出")
    if blockers:
        st.warning("当前运行尚不能导出批准版本。")
        st.code("\n".join(blockers), language=None, wrap_lines=True)
    else:
        st.success("当前全部用例通过，且没有未处理的阻断问题。")
    col1, col2 = st.columns([1, 2])
    format_name = col1.selectbox("导出格式", ["CSV", "Markdown"], key=f"format_{run['id']}")
    if col2.button("生成批准版本文件", type="primary", key="export_approved",
                   disabled=bool(blockers) or not operator.strip(), width="stretch"):
        try:
            exported = service.export(run["id"], "csv" if format_name == "CSV" else "md", operator,
                                      run["revision"], request_id())
            st.session_state["ready_export"] = {"run_id": run["id"], "export": exported}
            success("批准版本文件已保存，下载内容为固定历史快照。")
        except (ServiceError, ValueError) as error:
            display_error(error)
    ready = st.session_state.get("ready_export")
    if ready and ready["run_id"] == run["id"]:
        exported = ready["export"]
        st.download_button("下载已保存的导出快照", exported["content"],
                           file_name=exported["filename"], mime=exported["mime"])
        st.caption("历史快照：之后的修订不会改变这份文件，当前版本仍须独立满足导出条件。")


def detail(service: Service, operator: str) -> None:
    st.title("运行详情与人工复核")
    runs = service.list_runs()
    if not runs:
        st.info("还没有运行。请先从新建运行页面生成固定样例。")
        return
    ids = [run["id"] for run in runs]
    selected = st.session_state.get("selected_run")
    if selected not in ids:
        st.session_state["selected_run"] = ids[0]
    run_id = st.selectbox("当前运行 ID", ids, key="selected_run")
    run = service.get_run(run_id)
    st.text(f"运行：{run_id}\n创建时间：{local_time(run['created_at'])}（Asia/Shanghai）"
            f"\n发起人：{run['operator']} · 生成状态：{run['status']}")
    if run.get("parent_run_id"):
        st.text("关联父运行：" + run["parent_run_id"])
    cols = st.columns(4)
    cases = run.get("cases", [])
    cols[0].metric("当前用例", len(cases))
    cols[1].metric("已通过", sum(c["status"] == "approved" for c in cases))
    cols[2].metric("待审核", sum(c["status"] == "pending_review" for c in cases))
    cols[3].metric("已退回", sum(c["status"] == "returned" for c in cases))
    if st.button("基于本次输入创建关联新运行", key="create_child"):
        document = run["input"]
        st.session_state["_draft"] = document.get("normalized_text", document.get("text", ""))
        st.session_state["_parent"] = run_id
        go_to(PAGES[0])
    st.caption("补充业务规则后需重新生成并审核。固定样例模式目前只支持预置输入。")
    blockers = service.gate(run_id)
    tabs = st.tabs(["用例审核", "澄清问题", "需求与提取", "运行记录", "导出"])
    with tabs[0]:
        if run["status"] == "valid":
            cases_tab(service, run, operator)
        else:
            st.error("本次运行尚无通过校验的生成结果，不能进入审核。")
            if run.get("error"):
                st.code(run["error"], language=None, wrap_lines=True)
            st.json(run.get("validation", {}))
            if run["status"] == "generating":
                st.info("若上次应用进程已经退出，可记录为生成中断，再创建关联运行重试。")
                confirmed = st.checkbox("我已确认该运行的生成进程已经结束", key=f"interrupted_{run_id}")
                with st.form(f"recover_{run_id}"):
                    interruption_reason = st.text_area("中断原因")
                    interrupted = st.form_submit_button("记录生成中断", disabled=not confirmed or not operator.strip())
                if interrupted:
                    try:
                        service.mark_interrupted(run_id, operator, interruption_reason)
                        success("中断已记录；原输入和生成上下文保留。")
                    except (ServiceError, ValueError) as error:
                        display_error(error)
    with tabs[1]:
        if run["status"] == "valid":
            clarifications_tab(service, run, operator)
        else:
            st.info("生成有效结果后才能记录澄清处理。")
    with tabs[2]:
        source_view(run["input"], f"all_source_{run_id}")
        for field, label in [("features", "功能"), ("roles", "业务角色"),
                             ("business_rules", "业务规则")]:
            with st.expander(label, expanded=field == "business_rules"):
                st.json((run.get("output") or {}).get(field, []))
        with st.expander("完整输入快照"):
            st.json(run["input"])
    with tabs[3]:
        st.caption("服务端记录时间使用 UTC；页面概览使用 Asia/Shanghai。操作者为自报标识，未经过身份认证。")
        for label, value in [("计时、用量与错误", {key: run.get(key) for key in
                                               ("latency_ms", "total_latency_ms", "usage", "error")}),
                             ("模型配置", run.get("config")), ("Prompt 版本与完整请求", run.get("prompt")),
                             ("校验结果", run.get("validation")), ("审批与修订事件", run.get("events", []))]:
            with st.expander(label):
                st.json(value)
        with st.expander("原始输出"):
            raw = run.get("raw_output")
            if raw is None:
                st.info("没有原始响应；错误信息请查看校验结果与运行配置。")
            else:
                st.code(raw, language="json", wrap_lines=True)
        with st.expander("所有用例历史版本"):
            st.json(run.get("case_versions", []))
    with tabs[4]:
        exports_view(service, run, operator, blockers)
        st.divider()
        st.subheader("历史导出快照")
        for index, item in enumerate(run.get("exports", [])):
            st.json(item)
            if st.button("准备下载此历史快照", key=f"historical_export_{run_id}_{index}"):
                try:
                    exported = service.get_export(item["id"])
                    st.session_state["ready_export"] = {"run_id": run_id, "export": exported}
                    st.rerun()
                except (ServiceError, ValueError) as error:
                    display_error(error)
        if not run.get("exports"):
            st.caption("尚未保存导出文件。")


def history(service: Service) -> None:
    st.title("运行历史")
    st.caption("保留每次输入、生成、修订和导出；新运行不继承旧运行的审批。")
    query = st.text_input("按运行 ID 筛选")
    runs = [run for run in service.list_runs() if query.strip() in run["id"]]
    if not runs:
        st.info("没有匹配的运行记录。")
        return
    st.caption(f"共 {len(runs)} 次运行 · 按创建时间倒序")
    for run in runs:
        with st.container(border=True):
            st.text(run["id"])
            st.text(f"{local_time(run['created_at'])}（Asia/Shanghai） · "
                    f"{run['operator']} · {run['status']}")
            if run.get("parent_run_id"):
                st.text("父运行：" + run["parent_run_id"])
    selected = st.selectbox("打开运行", [run["id"] for run in runs], key="history_run")
    if st.button("查看记录与继续审核", type="primary"):
        go_to(PAGES[1], selected)


def main() -> None:
    st.set_page_config(page_title="TraceCase · 需求与测试复核", page_icon="🧭", layout="wide")
    if "_navigate_to" in st.session_state:
        st.session_state["page"] = st.session_state.pop("_navigate_to")
    if "_draft" in st.session_state:
        st.session_state["prd_text"] = st.session_state.pop("_draft")
        st.session_state["parent_run_id"] = st.session_state.pop("_parent")
        st.session_state["input_source"] = "粘贴 Markdown"
    with st.sidebar:
        st.title("TraceCase")
        st.caption("需求可追溯 · 人工逐条复核")
        page = st.radio("工作空间", PAGES, key="page")
        st.divider()
        operator = st.text_input("操作者标识", key="operator", max_chars=64,
                                 placeholder="例如：demo-reviewer")
        if not operator.strip():
            st.info("填写操作者标识后可生成、审核与导出。")
        st.caption("这是自报标签，不是身份认证。")
        st.divider()
        st.caption("本地固定样例 · SQLite 留存")
        st.caption("学习原型，使用虚构数据；不声称符合金融监管标准。")
    if "_notice" in st.session_state:
        st.success(st.session_state.pop("_notice"))
    try:
        service = Service(os.environ.get("SDLC_DB_PATH", str(ROOT / "data" / "sdlc.sqlite3")))
        if page == PAGES[0]:
            new_run(service, operator)
        elif page == PAGES[1]:
            detail(service, operator)
        else:
            history(service)
    except (ServiceError, ValueError) as error:
        display_error(error)


if __name__ == "__main__":
    main()

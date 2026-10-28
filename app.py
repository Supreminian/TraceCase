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

from sdlc.domain import coverage_report, prepare_input, validate_output
from sdlc.providers import configured_provider
from sdlc.service import ServiceError
from sdlc.secure_service import Service
from sdlc.account_ui import enter, download_link
from sdlc import jobs
from sdlc.ui import apply_style, page_header, section_title, workflow
from sdlc.project_ui import project_changed, projects_view, overview, project_cases, export_history, remember_draft, upload_changed


ROOT = Path(__file__).resolve().parent
PAGES = ["新建运行", "运行详情与审核", "运行历史", "项目概览", "项目用例", "导出记录", "项目列表"]
STATUS = {"pending_review": "待审核", "approved": "已通过", "returned": "已退回"}
CATEGORY = {"normal": "正常", "boundary": "边界", "exception": "异常", "security": "安全",
            "performance": "性能", "usability": "易用性", "compatibility": "兼容性"}


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
        st.session_state["_selected_run"] = run_id
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
    st.caption(f"原文第 {paragraph.get('start_line', '—')}–{paragraph.get('end_line', '—')} 行 · {selected}")
    with st.container(height=260, border=False):
        st.text(paragraph_text(paragraph))


def linked_source_ids(run: dict, content: dict) -> list[str]:
    rules = (run.get("output") or {}).get("business_rules", [])
    direct = list(content["paragraph_ids"])
    inherited = [p for rule in rules if rule["id"] in content.get("requirement_ids", [])
                 for p in rule["paragraph_ids"]]
    return list(dict.fromkeys(direct + inherited))


def new_run(service: Service, operator: str) -> None:
    page_header("生成测试用例", "从一份需求开始，生成可追溯、可审核的测试内容。", 'input', '需求输入 / 01')
    workflow(0)
    provider = None
    try:
        provider = configured_provider()
    except ValueError:
        st.warning("生成服务暂不可用，请联系维护者检查连接。需求内容可先保留在本项目草稿中。")
    input_col, summary_col = st.columns([2.5, 1], gap="large")
    with input_col:
        with st.container(border=True, key="prd_input"):
            section_title("需求文档", "直接粘贴或上传 PRD，无需套用模板或预先编号。", 'input')
            if st.session_state.get("input_source") not in {"粘贴 Markdown", "上传 Markdown"}:
                st.session_state["input_source"] = "粘贴 Markdown"
            source = st.radio("输入方式", ["粘贴 Markdown", "上传 Markdown"], horizontal=True, key="input_source")
            kwargs: dict = {}
            if source == "粘贴 Markdown":
                kwargs["text"] = st.text_area("PRD 正文", height=320, key="prd_text", label_visibility="collapsed",
                                              placeholder="描述产品目标、用户角色、功能流程和业务规则…\n\n缺失或有歧义的条件，会整理为待澄清问题。")
            else:
                uploaded = st.file_uploader("选择 Markdown 文件", type=["md", "markdown"], key=f"prd_file_{service.project_id}",
                                            on_change=upload_changed, args=(service.project_id,))
                drafts = st.session_state.setdefault("_project_drafts", {}).setdefault(service.project_id, {})
                if uploaded is not None:
                    drafts["upload"] = {"filename": uploaded.name, "data": uploaded.getvalue()}
                if drafts.get("upload"):
                    kwargs = drafts["upload"]
                    st.caption("已保留上传草稿：" + kwargs["filename"])
                    if uploaded is None and st.button("移除保留的上传草稿", key="remove_upload_draft"):
                        drafts.pop("upload", None)
                        st.rerun()
            st.caption("UTF-8 Markdown · 文件最多 100 KiB · 正文最多 20,000 字符")
            parent = st.session_state.get("parent_run_id", "")
            if parent:
                st.info("正在基于上一轮需求创建新版本，原记录会保留。")
            preview_col, generate_col = st.columns([1, 2])
            preview = preview_col.button("预览需求", width="stretch")
            generate = generate_col.button("生成测试用例", type="primary", key="generate", width="stretch",
                                             disabled=not operator.strip() or provider is None)
            if not operator.strip():
                st.caption("登录会话已失效，请重新登录。")
            st.caption("点击生成会将需求发送至已配置的 AI 服务；必要时自动修复一次，最多两次调用，可能消耗额度。请使用虚构或脱敏数据。")
    with summary_col:
        with st.container(border=True, key="generation_summary"):
            section_title("本次会得到什么", "结果按模块展示，生成后由你逐条确认。", 'analysis')
            for title, detail in [("功能与角色", "梳理功能范围、参与角色及其职责。"),
                                  ("业务规则", "保留规则与原始需求的来源关联。"),
                                  ("测试用例", "前置条件、操作步骤、预期与分类。"),
                                  ("待澄清问题", "标出未知条件，不代替你补写规则。")]:
                st.markdown(f'<div class="tc-output"><strong>{title}</strong><p>{detail}</p></div>', unsafe_allow_html=True)
            st.caption("下一步：核对用例与原文 → 完成人工审核 → 导出批准版本。")
    fingerprint = hashlib.sha256(repr(kwargs).encode("utf-8")).hexdigest()
    if preview:
        try:
            document = prepare_input(**kwargs)
            st.session_state["input_preview"] = {"fingerprint": fingerprint, "document": document}
        except (ServiceError, ValueError) as error:
            display_error(error)
    saved_preview = st.session_state.get("input_preview")
    if saved_preview and saved_preview["fingerprint"] == fingerprint:
        with st.expander("需求内容预览", expanded=True):
            source_view(saved_preview["document"], "preview_paragraph")
    remember_draft()
    if generate:
        try:
            job_id = jobs.enqueue_initial(service, **kwargs, parent_run_id=parent.strip() or None)
            jobs.submit_initial(service, job_id)
            st.session_state["_await_initial_job"] = job_id
            st.session_state['_notice'] = '任务已提交，正在后台分析需求。可以继续浏览项目。'
            st.rerun()
        except (ServiceError, ValueError) as error:
            display_error(error)


def lines(label: str, values: list[str], key: str) -> list[str]:
    value = st.text_area(label, value="\n".join(values), key=key)
    return [line.strip() for line in value.splitlines() if line.strip()]


def cases_tab(service: Service, run: dict, operator: str) -> None:
    cases = run.get("cases", [])
    if run["status"] == "valid":
        coverage = coverage_report({**run["output"], "test_cases": [c["content"] for c in cases]})
        if coverage["uncovered_rules"]:
            st.warning("已有用例可审核，但以下需求仍需补充覆盖："
                       + "、".join(r["id"] for r in coverage["uncovered_rules"])
                       + "。导出会明确记录缺口，不代表完整覆盖。")
            with st.expander("查看尚需补充覆盖的需求", expanded=False):
                for missing in coverage["uncovered_rules"]:
                    st.text(missing["id"] + "：" + missing["statement"])
    if not cases:
        st.info("当前运行没有可审核用例。请查看校验与原始输出。")
        return
    revision_jobs(service, run["id"])
    mapping = {case["case_id"]: case for case in cases}
    selector_key = f"case_selector_{run['id']}"
    jump = st.session_state.get("_review_next")
    if jump and jump["run_id"] == run["id"]:
        st.session_state.pop("_review_next")
        if jump["case_id"] in mapping:
            st.session_state[selector_key] = jump["case_id"]
    case_id = st.selectbox("选择用例", list(mapping), key=selector_key,
                           format_func=lambda cid: cid + " · " + mapping[cid]["content"]["title"])
    case = mapping[case_id]
    content = case["content"]
    token = f"{run['id']}_{case_id}_{case['version']}_{case['revision']}"
    left, right = st.columns([3, 2], gap="large")
    with left:
        with st.container(border=True, key=f"case_body_{run['id']}"):
            section_title("测试内容", "核对操作步骤与可观察的预期。", "review")
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
                for value in values:
                    st.text(value)
    with right:
        with st.container(border=True, key=f"case_source_panel_{run['id']}"):
            section_title("需求依据", "直接核对生成内容与原文是否一致。", "workspace")
            sources = linked_source_ids(run, content)
            st.caption("直接引用及关联规则来源：" + "、".join(sources))
            source_view(run["input"], f"case_source_{token}", sources)
            st.caption("引用存在不代表内容有依据；请检查边界、权限、前置条件和可判断的预期。")
    with st.container(border=True, key=f"review_actions_{run['id']}"):
        section_title("审核决定", "通过后自动切换下一条；退回时请说明具体问题。", "review")
        action = st.radio("审核动作", ["通过", "退回"], horizontal=True, key=f"action_{token}")
        approval_note = None
        if action == "通过":
            approval_note = st.selectbox("通过说明", ["通过，无需补充说明", "与需求一致", "步骤和预期清晰",
                                                "边界覆盖符合要求", "其他（填写说明）"],
                                         key=f"approval_note_{token}")
        with st.form(f"review_{token}"):
            if action == "退回":
                reason = st.text_area("退回原因（必填）", key=f"review_reason_{token}",
                                      placeholder="说明具体问题，便于人工或模型修订。")
            elif approval_note == "其他（填写说明）":
                reason = st.text_area("补充说明（必填）", key=f"review_reason_{token}")
            else:
                reason = approval_note
                st.caption("无需填写文字；提交后记录所选说明，并进入下一个待审核用例。")
            submitted = st.form_submit_button("提交审核", type="primary", disabled=not operator.strip() or st.session_state.get("_project_archived", False) or "review" not in service.user["permissions"])
    if submitted:
        try:
            service.review(run["id"], case_id, case["version"], case["revision"],
                           "approve" if action == "通过" else "return", operator, reason, request_id())
            if action == "退回" and "regenerate" in service.user["permissions"]:
                try:
                    job_id = jobs.enqueue(service, run["id"], case_id)
                    jobs.submit(service, job_id)
                except ValueError as exc:
                    st.session_state["_job_notice"] = str(exc)
            current_cases = {c["case_id"]: c for c in service.get_run(run["id"])["cases"]}
            ids = list(mapping)
            index = ids.index(case_id)
            ordered = ids[index + 1:] + ids[:index]
            next_id = next((cid for cid in ordered
                            if current_cases[cid]["status"] == "pending_review"), None)
            if next_id:
                st.session_state["_review_next"] = {"run_id": run["id"], "case_id": next_id}
                success("审核事件已保存，已切换到下一个待审核用例。")
            success("本轮用例已全部处理，请按审核结果继续导出或修订。")
        except (ServiceError, ValueError) as error:
            display_error(error)
    expand_edit = st.session_state.get("_expand_edit") == {"run_id": run["id"], "case_id": case_id}
    with st.expander("修改当前用例 · 保存后重新审核", expanded=expand_edit):
        st.caption("修改会保存为新版本并重新审核，待澄清状态由关联问题自动确定。")
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
            category_options = list(dict.fromkeys([*CATEGORY, content["category"]]))
            category = st.selectbox("分类", category_options,
                                    index=category_options.index(content["category"]),
                                    format_func=lambda item: CATEGORY.get(item, item))
            edit_reason = st.text_area("修改理由", key=f"edit_reason_{token}")
            edit_submitted = st.form_submit_button("保存修订（生成新版本）", disabled=not operator.strip() or st.session_state.get("_project_archived", False) or "edit" not in service.user["permissions"])
        if edit_submitted:
            updated = copy.deepcopy(content)
            updated.update(title=title, requirement_ids=requirements, paragraph_ids=paragraphs,
                           preconditions=preconditions,
                           steps=[{"number": i, "action": action} for i, action in enumerate(steps, 1)],
                           expected_results=expected, priority=priority, category=category)
            try:
                service.edit(run["id"], case_id, case["version"], case["revision"], updated,
                             operator, edit_reason, request_id())
                st.session_state.pop("_expand_edit", None)
                success("新版本已保存，状态为待审核；请复核当前新版本后提交审核。")
            except (ServiceError, ValueError) as error:
                display_error(error)
    with st.expander("此用例的全部版本"):
        versions = [v for v in run.get("case_versions", []) if v["case_id"] == case_id]
        st.json(versions)


def clarifications_tab(service: Service, run: dict, operator: str) -> None:
    section_title("待澄清问题", "区分真正阻断执行的问题与可继续核对的补充信息。", "clarify")
    paragraphs = [p["id"] for p in run["input"].get("paragraphs", [])]
    rules = [r["id"] for r in (run.get("output") or {}).get("business_rules", [])]
    cases = [case["case_id"] for case in run.get("cases", [])]
    st.caption("新增业务事实需修订 PRD 并创建关联新运行。误报与非阻断认定必须有原文依据；受影响用例将重新审核。")
    clarifications = run.get("clarifications", [])
    if clarifications:
        mapping = {c["content"]["id"]: c for c in clarifications}
        selected = st.selectbox("选择澄清项", list(mapping), key=f"clarification_{run['id']}",
                                 format_func=lambda qid: mapping[qid]["content"]["question"][:65])
        item = mapping[selected]
        st.subheader(item["content"]["question"])
        question_status = item.get("status", "open")
        st.caption("处理状态：" + {"open": "待确认", "false_positive": "已认定为误报",
                                   "nonblocking": "已确认可继续"}.get(question_status, "待确认"))
        if question_status == "nonblocking":
            st.success("已确认不影响当前执行与判断。")
        elif question_status == "false_positive":
            st.success("原文已有明确依据，此问题已撤销阻断。")
        elif item["content"]["blocking"]:
            st.warning("此问题影响相关用例执行或判断，请先核对需求。")
        else:
            st.info("补充核对项，不阻断当前已明确的用例。")
        with st.expander("查看澄清项 JSON", expanded=False):
            st.json(item)
        st.caption("直接引用及关联规则来源：" + "、".join(linked_source_ids(run, item["content"])))
        source_view(run["input"], f"clarification_source_{run['id']}_{selected}",
                    linked_source_ids(run, item["content"]))
        if item.get("status", "open") == "open":
            with st.form(f"resolve_{run['id']}_{selected}_{run['revision']}"):
                resolution = st.radio("处理结论", ["原文已明确，属于误报", "不影响执行和判断，可认定非阻断"])
                evidence = st.multiselect("支持结论的来源段落", paragraphs)
                reason = st.text_area("处理理由", placeholder="说明原文如何支持结论，不能在此新增业务规则。")
                submitted = st.form_submit_button("保存澄清处理", disabled=not operator.strip() or st.session_state.get("_project_archived", False) or "review" not in service.user["permissions"])
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
            submitted = st.form_submit_button("记录阻断问题", disabled=not operator.strip() or st.session_state.get("_project_archived", False) or "review" not in service.user["permissions"])
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
    section_title("批准版本导出", "只导出当前已通过人工审核的版本，下载文件保留固定快照。", "export")
    if "export" not in service.user["permissions"]:
        st.info("当前角色没有生成或下载批准文件的权限，请联系团队管理员。")
    if blockers:
        st.warning("尚未满足导出条件，请先完成用例审核并处理阻断问题。")
        with st.expander("查看尚未完成的事项", expanded=False):
            for blocker in blockers:
                st.text(blocker)
        if st.button("前往用例审核", key="export_go_review"):
            st.session_state["_detail_target"] = {"run_id": run["id"], "tab": "用例审核"}
            st.rerun()
    else:
        st.success("本轮审核全部通过。下一步：选择 CSV 或 Markdown，点击“生成批准版本文件”，再下载快照。")
    col1, col2 = st.columns([1, 2])
    format_name = col1.selectbox("导出格式", ["CSV", "Markdown"], key=f"format_{run['id']}")
    if col2.button("生成批准版本文件", type="primary", key="export_approved",
                   disabled=bool(blockers) or not operator.strip() or st.session_state.get("_project_archived", False) or "export" not in service.user["permissions"], width="stretch"):
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
        download_link(service, exported, "下载已保存的导出快照")
        st.caption("历史快照：之后的修订不会改变这份文件，当前版本仍须独立满足导出条件。")


def review_result(service: Service, run: dict, operator: str, blockers: list[str]) -> None:
    cases = run["cases"]
    if run["status"] != "valid" or not cases:
        st.warning("生成结果尚未通过校验，请先查看运行记录中的错误，再创建关联运行修正。")
        return
    pending = [c for c in cases if c["status"] == "pending_review"]
    returned = [c for c in cases if c["status"] == "returned"]
    if pending:
        st.info(f"还有 {len(pending)} 条待审核用例，请先完成本轮审核。")
        return
    if not blockers:
        st.success("本轮全部用例通过，已满足导出条件。下一步：在导出页选择 CSV 或 Markdown 并下载。")
        return
    if not returned:
        st.warning("全部用例已通过，但仍有阻断问题，当前不能导出。下一步：处理澄清后复审受影响用例。")
        st.code("\n".join(blockers), language=None)
        if st.button("前往处理澄清", key="review_clarifications"):
            st.session_state["_detail_target"] = {"run_id": run["id"], "tab": "澄清问题"}
            st.rerun()
        return
    st.warning(f"本轮审核完成，{len(returned)} 条已退回。请选择人工修订或模型修订；当前不能导出。")
    for case in returned:
        event = next(e for e in reversed(run["events"]) if e["action"] == "return"
                     and e["case_id"] == case["case_id"] and e["version"] == case["version"])
        st.text(f"{case['case_id']} · v{case['version']} · {case['content']['title']}")
        st.code(event["reason"], language=None, wrap_lines=True)
    choice = st.radio("本轮退回是否涉及需求变更？", ["原需求已足够，仅修订用例", "需要补充或更改业务规则"],
                      key=f"review_scope_{run['id']}")
    if choice == "需要补充或更改业务规则":
        st.info("先把角色、阈值或流程等新增事实写入 PRD，再创建关联运行。退回理由不能代替业务需求。")
        if st.button("补充 PRD 并重新生成", key="revise_prd", disabled=st.session_state.get("_project_archived", False)):
            st.session_state["_draft"] = run["input"]["normalized_text"]
            st.session_state["_parent"] = run["id"]
            go_to(PAGES[0])
        return
    st.subheader("方式一：人工修订")
    selected = st.selectbox("选择要人工修订的退回用例", [c["case_id"] for c in returned],
                            key=f"manual_returned_{run['id']}")
    st.caption("保存为新版本并重新审核；本运行中未改动用例的批准记录仍有效。")
    if st.button("人工修订此用例", key="manual_revision", disabled=not operator.strip() or st.session_state.get("_project_archived", False) or "edit" not in service.user["permissions"]):
        st.session_state["_review_next"] = {"run_id": run["id"], "case_id": selected}
        st.session_state["_expand_edit"] = {"run_id": run["id"], "case_id": selected}
        st.session_state["_detail_target"] = {"run_id": run["id"], "tab": "用例审核"}
        st.rerun()
    st.info("退回后会在后台逐条重新生成。新版本需再次审核，其他已通过用例保持不变。任务状态与重试入口在用例审核页上方。")


def validation_repair(service: Service, run: dict, operator: str) -> None:
    validation = run.get("validation") or {}
    if not (run["status"] == "invalid" and run.get("raw_output")
            and run["config"].get("finish_reason") in {None, "stop"}):
        return
    errors = validation.get("errors", [])
    st.warning(f"生成内容仍有 {len(errors)} 项结构或引用问题，需要修复后审核，无需为内部格式修改 PRD。")
    st.info("这不是对 PRD 写作格式的要求。先由系统整理可计算的状态并重新检查现有结果，无需再次调用模型。")
    locally_ready = validate_output(run["raw_output"], run["input"]["paragraphs"])["valid"]
    if locally_ready and st.button("整理现有结果并进入审核（不调用模型）", key="local_revalidate",
                 disabled=not operator.strip() or st.session_state.get("_project_archived", False)):
        try:
            child = service.revalidate_existing(run["id"], run["revision"], operator, request_id())
            st.session_state["_notice"] = "已本地整理原始结果并重新校验，未调用模型；旧记录保留。"
            go_to(PAGES[1], child)
        except (ServiceError, ValueError) as error:
            display_error(error)
    for error in errors[:5]:
        st.text(error)
    with st.expander("携带校验错误修复（创建关联新运行）", expanded=False):
        st.caption("发送原 PRD、原始输出及全部校验错误，使用 VALIDATION-R1 提示修复。"
                   "新结果仍需完整校验和人工审核；不会覆盖旧运行，也不保证一次修复成功。")
        provider = None
        try:
            provider = configured_provider()
        except ValueError:
            st.info("生成服务暂不可用，请联系维护者检查连接。")
        confirmed = st.checkbox("确认发送原 PRD、旧输出及错误进行 AI 修复，消耗一次调用额度并可能产生费用",
                                key=f"repair_confirm_{run['id']}")
        if st.button("修复校验错误并创建新运行", key="repair_validation",
                     disabled=provider is None or not confirmed or not operator.strip()
                     or st.session_state.get("_project_archived", False)):
            try:
                with st.spinner("正在修复结构与引用，随后重新校验…"):
                    child = service.repair_validation(run["id"], run["revision"], operator,
                                                      provider, request_id())
                st.session_state["_notice"] = "修复结果已保存为关联新运行；请检查校验结果，通过后逐条审核。"
                go_to(PAGES[1], child)
            except (ServiceError, ValueError) as error:
                display_error(error)


def detail(service: Service, operator: str) -> None:
    current_tab = st.session_state.get(f"detail_tabs_{st.session_state.get('selected_run')}", "用例审核")
    requested_tab = st.session_state.get("_detail_target")
    if requested_tab:
        current_tab = requested_tab["tab"]
    headings = {
        "用例审核": ("用例审核", "逐条核对测试步骤、预期结果与需求依据，再决定通过或退回。", "review", "人工复核 / 03", 2),
        "澄清问题": ("澄清问题", "明确未知条件，区分阻断问题与补充核对项。", "clarify", "需求澄清 / 03", 2),
        "需求与提取": ("需求分析", "分别查看功能范围、参与角色和业务规则。", "analysis", "分析结果 / 02", 1),
        "运行记录": ("操作记录", "查看生成、修改、审核与导出的处理过程。", "workspace", "过程追溯", 2),
        "导出": ("用例导出", "下载已审核的批准版本，保留来源与覆盖说明。", "export", "批准交付 / 04", 3),
        "审核结果": ("审核结果", "确认本轮进度，继续导出或进入下一轮修订。", "review", "审核进度 / 03", 2),
    }
    heading = headings.get(current_tab, headings["用例审核"])
    page_header(*heading[:4])
    workflow(heading[4])
    runs = service.list_run_summaries(limit=100)
    requested = st.session_state.get("selected_run")
    if requested and requested not in {r["id"] for r in runs}:
        try:
            requested_run = service.get_run(requested)
            runs.append({k: requested_run[k] for k in ("id", "created_at", "status")})
        except ServiceError:
            pass
    if not runs:
        st.info("还没有运行。请在新建运行页面输入 PRD 并生成用例。")
        return
    ids = [run["id"] for run in runs]
    selected = st.session_state.get("selected_run")
    if selected not in ids:
        st.session_state["selected_run"] = ids[0]
    run_labels = {r['id']: f"{local_time(r['created_at'])} · " + {"valid": "已生成", "invalid": "待修复", "failed": "生成失败", "generating": "生成中"}.get(r['status'], r['status']) for r in runs}
    run_id = st.selectbox("选择生成批次", ids, key="selected_run", format_func=run_labels.__getitem__)
    remember_draft()
    run = service.get_run(run_id)
    st.caption(f"创建于 {local_time(run['created_at'])} · 发起人 {run.get('operator_display', run['operator'])}")
    if run.get("parent_run_id") and st.button("查看上一轮", key="return_parent"):
        go_to(PAGES[1], run["parent_run_id"])
    if run["prompt"].get("version") == "REVIEW-R1":
        st.info("这是退回后的模型修订轮次。原审核记录保留，本轮所有用例需要重新审核。")
        if run["status"] in {"invalid", "failed"}:
            st.warning("本次修订未通过。请查看错误，可返回上一轮记录继续人工修订或重新发起模型修订。")
    if (run.get("validation") or {}).get("warnings"):
        with st.expander("质量提示与自动整理记录", expanded=False):
            for warning in run["validation"]["warnings"]:
                st.text(warning)
            if run["validation"].get("adjustments"):
                with st.expander("查看整理记录 JSON"):
                    st.json(run["validation"]["adjustments"])
    if run.get("config", {}).get("finish_reason") == "length":
        st.error("生成内容超过单次输出长度，未得到完整结果。请将需求按功能拆分后重新生成；原记录已保留。")
    cols = st.columns(4)
    cases = run.get("cases", [])
    cols[0].metric("当前用例", len(cases))
    cols[1].metric("已通过", sum(c["status"] == "approved" for c in cases))
    cols[2].metric("待审核", sum(c["status"] == "pending_review" for c in cases))
    cols[3].metric("已退回", sum(c["status"] == "returned" for c in cases))
    if st.button("基于本次输入创建关联新运行", key="create_child", disabled=st.session_state.get("_project_archived", False) or "generate" not in service.user["permissions"]):
        document = run["input"]
        st.session_state["_draft"] = document.get("normalized_text", document.get("text", ""))
        st.session_state["_parent"] = run_id
        go_to(PAGES[0])
    st.caption("更新需求后可创建关联运行并重新审核，原记录与审批保留。")
    blockers = service.gate(run_id)
    tab_key = f"detail_tabs_{run_id}"
    completed = bool(cases) and all(c["status"] != "pending_review" for c in cases)
    completion_key = f"completion_seen_{run_id}"
    if completed and st.session_state.get(completion_key) != run["revision"]:
        st.session_state[completion_key] = run["revision"]
        st.session_state[tab_key] = "导出" if not blockers else "审核结果"
    target = st.session_state.get("_detail_target")
    if target and target["run_id"] == run_id:
        st.session_state.pop("_detail_target")
        st.session_state[tab_key] = target["tab"]
    tabs = st.tabs(["用例审核", "澄清问题", "需求与提取", "运行记录", "导出", "审核结果"],
                   key=tab_key, on_change="rerun")
    with tabs[5]:
        with st.container(border=True, key="review_result_workspace"):
            review_result(service, run, operator, blockers)
    with tabs[0]:
        if run["status"] == "valid":
            cases_tab(service, run, operator)
        else:
            st.error("本次运行尚无通过校验的生成结果，不能进入审核。")
            if run.get("error"):
                st.code(run["error"], language=None, wrap_lines=True)
            validation_repair(service, run, operator)
            with st.expander("查看详细校验记录（JSON）", expanded=False):
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
            with st.container(border=True, key="clarification_workspace"):
                clarifications_tab(service, run, operator)
        else:
            st.info("生成有效结果后才能记录澄清处理。")
    with tabs[2]:
        section_title("需求分析", "功能、参与角色和业务约束分别整理，保留原文引用。", "analysis")
        with st.expander("查看需求原文", expanded=False):
            source_view(run["input"], f"all_source_{run_id}")
        panels = st.columns(3, gap="medium")
        for panel, (field, label, tone) in zip(panels,
                [("features", "功能范围", "input"), ("roles", "业务角色", "export"),
                 ("business_rules", "业务规则", "review")], strict=True):
            with panel:
                with st.container(border=True, key="extraction_" + field):
                    items = (run.get("output") or {}).get(field, [])
                    section_title(label, f"共 {len(items)} 项", tone)
                    if not items:
                        st.info("暂无提取内容。")
                    for item in items:
                        st.markdown("**" + str(item.get("name", item["id"])) + "**")
                        st.text(item.get("description", item.get("statement", "")))
                        for responsibility in item.get("responsibilities", []):
                            st.text("• " + responsibility)
                        st.caption("来源：" + "、".join(item.get("paragraph_ids", [])))
                        st.divider()
                    with st.expander("查看" + ("功能" if field == "features" else label) + " JSON", expanded=False):
                        st.json(items)
    with tabs[3]:
        section_title("操作记录", "查看生成、修改、审核和导出的处理过程。", "workspace")
        action_names = {"generation_started": "开始生成", "generation_completed": "生成完成",
                        "generation_failed": "生成失败", "approve": "审核通过", "return": "退回修订",
                        "edit": "修改用例", "export": "导出快照", "local_revalidation_started": "重新整理结果"}
        for event in reversed(run.get("events", [])):
            with st.container(border=True):
                st.markdown("**" + action_names.get(event["action"], "更新记录") + "**")
                st.caption(f"{local_time(event['created_at'])} · {event.get('operator_display', event['operator'])}")
                st.text(event["reason"])
        with st.expander("查看原始输出 JSON", expanded=False):
            raw = run.get("raw_output")
            if raw is None:
                st.info("本次没有原始响应。")
            else:
                st.code(raw, language="json", wrap_lines=True)
        with st.expander("查看用例版本记录", expanded=False):
            st.json(run.get("case_versions", []))
    with tabs[4]:
        with st.container(border=True, key="export_workspace"):
            exports_view(service, run, operator, blockers)
            st.divider()
            st.subheader("历史导出快照")
            for index, item in enumerate(run.get("exports", [])):
                st.text(item.get("filename", item["id"]))
                with st.expander("查看历史导出记录 " + item['id'], expanded=False):
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
    page_header("生成历史", "查找需求分析与审核记录，继续未完成的工作。", "workspace", "历史记录")
    st.caption("保留每次输入、生成、修订和导出；新运行不继承旧运行的审批。")
    with st.expander("按批次编号查找", expanded=False):
        query = st.text_input("批次编号")
    page = st.number_input("批次历史页码", min_value=1, value=1, step=1, key=f"history_page_{service.project_id}")
    runs = service.list_run_summaries(limit=50, offset=(page - 1) * 50, query=query.strip())
    if not runs:
        st.info("没有匹配的运行记录。")
        return
    st.caption(f"本页 {len(runs)} 次运行 · 每页最多 50 条 · 仅当前项目 · 按创建时间倒序")
    labels = {}
    for run in runs:
        status_label = {"valid": "可继续审核", "invalid": "结果待修复", "failed": "生成失败", "generating": "正在生成"}.get(run['status'], run['status'])
        labels[run['id']] = f"{local_time(run['created_at'])} · {status_label} · {run['case_count']} 条用例"
        with st.container(border=True):
            st.markdown("**" + local_time(run['created_at']) + "**")
            st.caption(f"{run['operator']} · {status_label} · {run['case_count']} 条用例")
            with st.expander("查看批次编号", expanded=False):
                st.text(run["id"])
                if run.get("parent_run_id"):
                    st.caption("关联上一轮：" + run["parent_run_id"])
    selected = st.selectbox("选择历史批次", [run["id"] for run in runs], key="history_run", format_func=labels.__getitem__)
    if st.button("查看记录与继续审核", type="primary"):
        go_to(PAGES[1], selected)


@st.fragment(run_every="3s")
def generation_status(service):
    try:
        records = jobs.initial_jobs(service)
        for item in records:
            if item['status'] in {'queued', 'running'}:
                st.info("正在后台分析 PRD 并生成测试用例，可继续浏览其他页面。")
            elif item['run_id']:
                if st.session_state.get('_await_initial_job') == item['id'] and st.session_state.get('page') == '新建运行':
                    st.session_state.pop('_await_initial_job', None)
                    st.session_state['_selected_run'] = item['run_id']
                    st.session_state['_navigate_to'] = PAGES[1]
                    st.rerun(scope='app')
                label = "生成完成，打开结果" if item['status'] == 'completed' else "生成未完成，查看原因"
                if st.button(label, key='openjob_' + item['id']):
                    st.session_state['_selected_run'] = item['run_id']
                    st.session_state['_navigate_to'] = PAGES[1]
                    st.rerun(scope='app')
            elif item['status'] == 'failed':
                st.warning(item['error'] or '生成任务已中断，请重新提交。')
    except ValueError as exc:
        st.error(str(exc))


@st.fragment(run_every="3s")
def revision_jobs(service, run_id):
    try:
        records = jobs.list_jobs(service, run_id)
        current_cases = {c['case_id']: c for c in service.get_run(run_id)['cases']}
        latest = {}
        for item in records:
            latest.setdefault(item['case_id'], item)
        for item in latest.values():
            label = {'queued': '等待重新生成', 'running': '正在重新生成', 'completed': '新版本已就绪，请重新审核',
                     'failed': '重新生成失败', 'conflict': '用例已被修改，模型结果未覆盖新版本'}.get(item['status'], item['status'])
            current = current_cases.get(item['case_id'])
            if item['status'] == 'completed' and current and current['status'] != 'pending_review':
                label = '修订已完成，当前版本' + STATUS.get(current['status'], current['status'])
            st.caption(item['case_id'] + ' · ' + label)
            if item['status'] == 'completed' and current and current['status'] == 'pending_review':
                if st.button('审核新版本 ' + item['case_id'], key='reviewjob_' + item['id']):
                    st.session_state['_review_next'] = {'run_id': run_id, 'case_id': item['case_id']}
                    st.session_state['_detail_target'] = {'run_id': run_id, 'tab': '用例审核'}
                    st.rerun(scope='app')
            if item['error']:
                st.warning(item['error'])
        state = tuple((r['id'], r['status']) for r in records)
        key = '_job_states_' + run_id
        before = st.session_state.get(key)
        st.session_state[key] = state
        if before is not None and before != state:
            st.rerun(scope='app')
        if '_job_notice' in st.session_state:
            st.warning(st.session_state.pop('_job_notice'))
        if 'regenerate' in service.user['permissions']:
            returned = [c for c in current_cases.values() if c['status'] == 'returned'
                        and latest.get(c['case_id'], {}).get('status') not in {'queued', 'running'}]
            for case in returned:
                if st.button('重新生成 ' + case['case_id'], key='retrycase_' + run_id + case['case_id']):
                    job_id = jobs.enqueue(service, run_id, case['case_id'])
                    jobs.submit(service, job_id)
                    st.rerun(scope='app')
    except ValueError as exc:
        st.error(str(exc))


def main() -> None:
    st.set_page_config(page_title="TraceCase · 需求与测试复核", page_icon="🧭", layout="wide",
                       initial_sidebar_state="collapsed")
    apply_style()
    if "_navigate_to" in st.session_state:
        st.session_state["page"] = st.session_state.pop("_navigate_to")
    if "_selected_run" in st.session_state:
        st.session_state["selected_run"] = st.session_state.pop("_selected_run")
    if "_draft" in st.session_state:
        st.session_state["prd_text"] = st.session_state.pop("_draft")
        st.session_state["parent_run_id"] = st.session_state.pop("_parent")
        st.session_state["input_source"] = "粘贴 Markdown"
    try:
        db_path = os.environ.get("SDLC_DB_PATH", str(ROOT / "data" / "sdlc.sqlite3"))
        token, workspace_id, user = enter(db_path)
        admin = Service(db_path, token=token, workspace_id=workspace_id)
        projects = admin.list_projects()
        desired = st.session_state.get("_switch_project", st.session_state.get("current_project"))
        if desired and desired not in {p["id"] for p in projects}:
            try:
                projects.append(admin.get_project(desired))
            except ServiceError:
                pass
        project_ids = [p["id"] for p in projects]
        if "_switch_project" in st.session_state:
            st.session_state["current_project"] = st.session_state.pop("_switch_project")
            project_changed()
        if st.session_state.get("current_project") not in project_ids:
            st.session_state["current_project"] = None
        st.session_state.setdefault("page", "项目列表")
        with st.sidebar:
            st.markdown('<div class="tc-brand"><span class="tc-logo">TC</span><div><strong>TraceCase</strong><small>需求与测试工作台</small></div></div>', unsafe_allow_html=True)
            project_id = st.selectbox("当前项目", project_ids, index=None, key="current_project",
                                     placeholder="请选择项目", on_change=project_changed,
                                     format_func=lambda pid: next(p["name"] + ("（已归档）" if p["status"] == "archived" else "")
                                                                  for p in projects if p["id"] == pid))
            page = st.radio("项目功能", PAGES, key="page")
            st.divider()
            operator = user['id']
            st.caption("操作记录关联当前登录账号。")
        if "_notice" in st.session_state:
            st.success(st.session_state.pop("_notice"))
        if page == "项目列表" or not project_id:
            st.session_state["_project_archived"] = False
            projects_view(admin, operator)
            return
        service = Service(db_path, project_id=project_id, token=token, workspace_id=workspace_id)
        project = service.get_project()
        generation_status(service)
        st.session_state["_project_archived"] = project["status"] == "archived" or st.session_state.get("_space_archived", False)
        st.caption("当前项目 / " + project["name"])
        if project["status"] == "archived":
            st.warning("此项目已归档，只能查看与下载已有快照；请在项目列表恢复后继续处理。")
        if page == "新建运行":
            if project["status"] == "active" and "generate" in service.user["permissions"]:
                draft = st.session_state.get("_project_drafts", {}).get(project_id, {})
                for key in ("prd_text", "parent_run_id", "input_source"):
                    if key in draft:
                        st.session_state.setdefault(key, draft[key])
                new_run(service, operator)
            else:
                st.info("当前角色没有创建与生成权限，请联系团队管理员。")
        elif page == "运行详情与审核":
            detail(service, operator)
        elif page == "运行历史":
            history(service)
        elif page == "项目用例":
            project_cases(service, go_to)
        elif page == "导出记录":
            if "export" in service.user["permissions"]:
                export_history(service)
            else:
                st.info("当前角色没有导出权限。")
        else:
            overview(service, go_to)
    except (ServiceError, ValueError) as error:
        display_error(error)


if __name__ == "__main__":
    main()

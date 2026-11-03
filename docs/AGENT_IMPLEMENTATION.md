# TraceCase Agent 0.3.0：实现、配置与验收

2026-11-03。依据测试工程师产品方案及用户提供的两页 Agent 实现设计 PDF。

## 1. PDF 逐项实现对应

| 要求 | 实现 |
| --- | --- |
| 领域层 Session、会话 ID、messages、append | sdlc/agent/domain.py，Pydantic 判别联合与修订号 |
| UserMessage / AssistantMessage / ToolCall / ToolResult | 同上，工具结果绑定 tool_call_id |
| Tool 的名称、说明、JSON Schema、execute | 同上；业务工具位于 sdlc/testing_agent/tools.py |
| 泛型 Context，instruction/tools/history/output_type/memory/agents.md | 同上；三种模型协议支持任意 Pydantic 阶段类型 |
| Assistant 与 ToolCallReviewer，各自构造 Context | 同上；审核结论 Decision(should_allow,reason) |
| SessionStore / ModelClient 应用端口 | sdlc/agent/application.py |
| ModelSettings(model_id,effort) | 另含明确 provider、输出上限及超时 |
| AgentService 新会话、列表、读取、发送、执行循环 | 同上；另有 resume、幂等投递、CAS、授权和公开事件 |
| 主助手与工具审核者分别配置模型和思考深度 | sdlc/agent/composition.py，从环境配置组装 |
| 循环上限 | 迭代、工具调用、模型调用、Token、时长、重复失败限制 |
| JsonlSessionStore | sdlc/agent/adapters/session_store.py；跨进程锁、原子快照、尾记录恢复、0600 |
| BashTool | sdlc/agent/adapters/bash.py；默认关闭，仅固定只读 argv，隔离环境、限制路径/耗时/输出 |
| OpenAI / Anthropic / Google | sdlc/agent/adapters/models.py；原生工具协议、结构化输出、参数能力校验 |
| HTTP 适配 | sdlc/agent/http.py，挂载现有认证网关 |
| CLI 适配 | python -m sdlc.agent.cli；与网页共用任务与执行应用 |
| 组装根 | sdlc/agent/composition.py；集中选择模型、会话存储、角色和工具 |

BashTool 是受限本地诊断适配器，未注册到项目测试 Agent。它不提供任意脚本执行，不以模型审核代替宿主进程隔离；需要任意测试代码执行时应另建隔离执行环境。

## 2. 产品功能

- 项目默认进入聊天工作台；无需选择消息类型，由模型结合原始消息、已有成果和确认状态判断下一步。负责人、确认人和截止日期使用真实账号。
- Google/OpenAI/Anthropic 对话助手的公开文本通过原生 SSE 接收并逐段持久化；页面每 0.4 秒刷新，结构化阶段以处理动画表示。私密推理、工具参数和内部 JSON 不进入公开聊天。
- Agent 自主选择阅读、分析、规划、生成、复核或提问。计划确认仅在 Agent 明确提出时生效；失败后必须收到明确的人类重试同意。
- 需求分析、原文依据、角色/规则、风险与澄清、覆盖计划、按模块分批用例。
- 每批产物发布为可评审运行；后续补批保留人工修改，严格一致的旧决定记录来源事件再继承。
- 独立依据复核；无依据或不确定断言成为局部问题，不得冒充人工批准。
- 问题逐项保存答复→指定授权人员一次确认已答内容→同一事务建立关联需求版本并入队。保留其他未解决问题，重复确认不产生重复版本，队列失败则整笔回滚。
- 退回分类路由：用例质量问题修订、需求变化待确认、重复项合并/停用建议。
- 修订差异、人工采纳、人工修改、版本竞争及迟到结果保护。
- 需求差异、直接影响与间接待复核、严格批准沿用、回归范围建议。
- 项目内已批准用例读取与待审复用；CSV 字段映射、导入校验及公式文本保护。
- 项目共享前置条件、步骤、测试数据准备：权限控制、乐观锁、不可覆盖的历史版本和软归档；新任务选择并保存固定版本，自动续跑继承原快照。模板不充当业务需求依据。
- 有理由的范围调整保留原任务与缺口；未明确排除的规则不能被顺带移除。
- 任务完成、设计评审、测试执行分别表达；批准基线与 CSV/Markdown 导出。
- Agent 导出包含任务/基线/需求版本/范围，以及角色、数据准备、清理步骤、测试条件与方法，读取批准基线内的固定详情。
- 模型参数及诊断默认收起，普通任务接口隐藏模型配置和原始调用配置。

## 3. 运行与配置

依赖安装：python3 -m venv .venv，随后 .venv/bin/python -m pip install -r requirements.lock。

设置 GEMINI_API_KEY 后运行 .venv/bin/python scripts/run_local.py；本机开发也可沿用 scripts/run_google.py 从已授权钥匙串读取密钥。页面仍为 http://127.0.0.1:8501。启动脚本同时拉起私有 Streamlit 和独立 Agent Worker。

单独 Worker：.venv/bin/python scripts/run_agent_worker.py。

应用不自动加载 .env；.env.example 是配置说明，需将实际配置注入进程环境。维护者可分别设置：

| 配置 | 含义 |
| --- | --- |
| SDLC_AGENT_ASSISTANT_PROVIDER / MODEL / EFFORT | 主助手供应商、模型、思考深度 |
| SDLC_AGENT_REVIEWER_PROVIDER / MODEL / EFFORT | 工具审核者供应商、模型、思考深度 |
| SDLC_AGENT_PROVIDER | 两角色的默认供应商，默认 google |
| GEMINI_API_KEY / GOOGLE_API_KEY | Google 原生接口密钥 |
| OPENAI_API_KEY / ANTHROPIC_API_KEY | 对应供应商密钥 |
| SDLC_AGENT_SESSION_DIR | 私有 JSONL 目录，默认在数据库旁，不通过 HTTP 静态暴露 |
| SDLC_AGENT_MAX_MODEL_CALLS | 当前任务累计模型调用上限，默认 64 |
| SDLC_AGENT_MAX_TOTAL_TOKENS | 当前任务累计已知 Token 上限，默认 200000；调用前保守计入输出预算 |
| SDLC_AGENT_MAX_ITERATIONS / MAX_TOOL_CALLS | 每轮上限，默认 20 / 32 |
| SDLC_AGENT_MAX_SECONDS / MODEL_TIMEOUT | 每轮及模型调用上限，默认 900 / 120 秒 |
| SDLC_AGENT_GOOGLE_MIN_INTERVAL | 本数据库 Worker 共享 Google 请求间隔，默认 5 秒 |

主助手默认 high、审核者默认 low。要真正使用更快/便宜的审核模型，应明确配置可用的模型 ID；不推断价格，不自动切换收费模型。模型不支持 effort 时设置 default，适配器不盲发不支持的参数。

Google 的结构化输出使用供应商 Schema 编译器：去掉供应商语法不支持的约束并写入说明，原始完整 Pydantic 校验仍在本地执行。不可通过此编译器放宽业务合同。

## 4. HTTP 与 CLI

HTTP 作用域为 query 参数 workspace、project。GET /api/agent/context 返回当前权限与会话绑定 CSRF proof；所有修改要求同源来源检查以及 X-TraceCase-CSRF，登录仍为 HttpOnly Cookie。

- GET/POST /api/agent/tasks：列出/创建任务。
- GET/POST /api/agent/resources 及 /resources/{id}/update、archive：共享资源读取、创建、修订与归档；创建任务可绑定 resource_ids。
- GET /api/agent/tasks/{id}：任务、评审集合、门禁、摘要、执行记录。
- GET/POST /api/agent/tasks/{id}/chat：公开聊天和确认状态、原始消息入队。等待确认时的自然语言消息也交给模型判断，不能将询问等同于授权。
- POST /api/agent/tasks/{id}/chat_confirmation：明确继续、讨论、调整范围或暂停，包含确认版本和幂等标识。
- POST /api/agent/tasks/{id}/{action}：resume/message/cancel/answer/confirm/review/approve/apply_revision/baseline/export/import/resolve_question/exclude_condition/resolve_proposal/regression/compare/carry_approval。
- GET /api/agent/sessions 和 /sessions/{id}：仅当前授权项目会话，隐藏模型连续性数据。

模型执行不占 HTTP 请求：创建、继续、消息只入持久队列。需求确认后自动入队子任务。人工通过要求准确 case version/revision 和幂等 request_key；批准导出要求基线存在且未漂移。

CLI 从本机环境变量 TRACECASE_SESSION_TOKEN 读取有效会话，不通过命令行参数接收密钥。命令示例：

    .venv/bin/python -m sdlc.agent.cli --workspace WORKSPACE_ID --project PROJECT_ID list
    .venv/bin/python -m sdlc.agent.cli --workspace WORKSPACE_ID --project PROJECT_ID new path/to/prd.md --goal '重点验证边界'
    .venv/bin/python -m sdlc.agent.cli --workspace WORKSPACE_ID --project PROJECT_ID show TASK_ID
    .venv/bin/python -m sdlc.agent.cli --workspace WORKSPACE_ID --project PROJECT_ID send TASK_ID '请处理已有退回反馈'
    .venv/bin/python -m sdlc.agent.cli --workspace WORKSPACE_ID --project PROJECT_ID resume TASK_ID

## 5. 可靠性与权限

任务和执行记录保存在 SQLite，私有会话存 JSONL。Worker 领取时使用事务和租约，定期心跳；租约到期可恢复，重投消息按工作项 ID 幂等。持久任务行本身也是提交意图，恢复创建任务后尚未成功入队的中断。

工具执行前保存执行意图；不确定副作用结果不重复执行。生产聊天遇第一次模型或工具故障即暂停并保存可回答的确认消息；显式恢复后才可重试。旧失败记录缺少公开确认时补建恢复入口，不改写旧错误与调用记录。模型拒绝、截断或非法结构不发布为有效用例。

部署代码后同时重启网关、Streamlit 和 Worker。Streamlit 热更新无法更新独立 Worker 中已导入的 Python 模块，混用版本会导致页面有聊天消息而后台没有对应公开回复。任务预算保持累计，不因重启或重试清零。

预算耗尽使用单独的可见确认类型，避免用户在同一任务上无限点击重试。确认后事务性地创建同需求、同目标/范围的关联新一轮并入队；原任务模型调用、Token、成果及审核历史不清零。队列满时整体回滚。

结构化阶段不重复传入原始正文和解析段落。会话送入模型前，对大型重复工具结果生成含精确标识、依赖和摘要校验值的紧凑视图；被省略的内容有重新读取入口，不缩写成新的业务事实。用户原始消息、工具协议配对、错误和最近明确读取的内容保留；原始会话不改写，调用审计保存实际发给模型的投影输入。此措施减少冗余，不等于已验证所有复杂 PRD 均可在默认预算内完成。

队列只保存可信 actor/workspace/project 和能力范围，不保存明文 bearer token。执行时创建短期内存令牌，数据库仅存摘要，结束即撤销；每步仍检查当前成员与项目权限。退出浏览器不偶然取消任务，撤权会阻止继续发布。

reviewer 角色可发起受限修订，仅有读取和 propose_revision 工具；不能获得 analyze/plan/generate/finalize 能力。工具审核模型不能覆盖服务端授权，所有人工业务决定继续经过真实身份和版本门禁。

未完成任务关联的分批运行禁止导出，防止发布途中中断留下的孤儿成果绕过门禁。任务新增阻断问题立即影响旧运行门禁；经旧入口导出 Agent 运行同样必须匹配批准基线。建立基线在写事务中重检任务版本、运行、问题与覆盖，防止混入并发发布的不同集合。

每次模型尝试保存配置、指令/哈希、输入、公开输出、用量和状态。私密推理文本不收集；供应商续传需要的不透明签名单独保存在私有会话。原始 PRD 与输出仍属于敏感业务资料，应按部署政策保护。

## 6. 验证证据与边界

测试涵盖三种协议的模拟传输、会话隔离与恢复、审核拒绝、循环上限、消息幂等、工具故障、角色撤权、跨项目访问、澄清确认、逐批评审、人工修订、范围调整、并发竞争、依据复核和基线导出。

2026-11-03 最终回归为 533 项测试通过，Ruff 与 diff 检查通过。涵盖原生流协议、公开增量、确认幂等、消息意图、文档语境、批量答复、预算续接、后台状态变化后继续聊天，以及真实 Worker 链路；模型边界在自动化测试中使用替身。现有 Starlette TestClient 有一条依赖弃用警告。evaluation.cli validate 验证 20 份材料的 12/8 开发/留出集结构，标准仍为 draft_pending_human_review，不代表已完成人工质量评测。

本次真实 Google 联调使用隔离数据库和一份短小虚构 PRD：成功生成两条用例；独立依据复核完成；明确模拟人工范围确认与评审后建立基线并导出 1585 字节 CSV，执行状态为 not_executed。原始联调结果在本机 data/agent-live-*-validation.json（忽略目录），不上传用户资料或密钥。该联调是协议与流程证据，不是生成质量基准。

本轮追加真实 Google 聊天联调：在独立临时数据库提交虚构便签 PRD 的讨论消息，经 6 次模型调用返回基于原文的两句答复，接收 4 个原生文本增量，耗时约 26.9 秒、已知总 Token 11,224；该轮明确不生成用例。结果保存在本机忽略目录 data/agent-chat-bugfix-live-*.json。处理工具期间显示状态动画，最终公开回复按供应商增量呈现，不将整轮时长称为首字延迟优化。

OpenAI、Anthropic 已完成协议与类型的模拟传输测试，未使用真实账号密钥进行线上调用。正式模型质量评测仍需人工冻结标准和真实样本评分；本次不报告虚构提升比例。

本次保持单机 SQLite/JSONL 部署，未实施 PostgreSQL、集群 SLA 或任意代码执行沙箱。真实 CI/API/UI 测试执行、执行报告导入、外部测试管理平台写入属于产品方案的后续阶段，未伪装成已完成的自动测试能力。已有 HTTP/CLI 端口支持后续扩展。

## 7. 验收入口

登录→进入项目→测试任务，在聊天框粘贴或附上需求。提交后显示处理动画，公开回复随模型增量更新；可离开再返回。需求与问题、测试覆盖、用例评审、交付四个视图位于“测试成果与人工审核”。逐项保存澄清答复后，一次确认本轮答复继续；失败时可以直接在聊天中讨论原因，明确同意后才重试。通过后自动切下一待评审用例；本轮全部通过后导航交付，存在退回时提示修订或需求确认。

常用检查：.venv/bin/ruff check .；.venv/bin/pytest -q；.venv/bin/python -m evaluation.cli validate。

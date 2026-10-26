# TraceCase · 需求到测试的人工复核原型

输入虚构费用报销 PRD，生成带来源引用的结构化测试用例，逐条审核并导出批准版本。本项目是单机学习原型，不是金融监管认证或企业级合规平台。

## 当前交付

- **M1 本地闭环**：Markdown 校验与分段、7 组固定样例、JSON/Pydantic/引用校验、人工通过/退回/修订、澄清处理、SQLite 历史、CSV/Markdown 批准快照。
- **M2 准备材料**：20 份候选 PRD 与标准，固定 12/8 划分，120 条调用计划及人工评分工具。标准仍待人工审核，未冻结。
- **M3 接入准备**：已实现可配置 Chat Completions 适配器与页面模式；尚未配置或调用真实服务，没有真实模型成绩、Token 或成本结论。V1/V2 是未调优候选，样例输出不会随 Prompt 改变。
- **工程流程**：本地测试与 GitHub Actions 配置已提供；当前仓库没有远端，线上 Issue/PR/CI 未执行。Issue 草稿见 [docs/ISSUES.md](docs/ISSUES.md)。

## 启动

需要 Python 3.12。首次安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m streamlit run app.py
```

访问 `http://127.0.0.1:8501`。应用只监听本机，固定样例不需要密钥。

数据库默认保存在 `data/sdlc.sqlite3`，首次启动自动创建。可在启动前设置 `SDLC_DB_PATH`；`.env.example` 只展示配置，应用不会自动加载 `.env`。密钥、运行数据库和本地导出不应提交 Git。

## 演示步骤

1. 在主页面上方输入自报操作者标识。选择“完整样例 · 可逐条审核并导出”，点击“检查输入与段落”，再点击“生成测试用例”。
2. 在“运行详情与审核”查看 11 条用例和原文。每条填写理由后通过；未全部通过时不能导出。
3. 所有用例通过后，在“导出”生成 CSV 或 Markdown 文件，再下载快照。
4. 修改任一已通过用例，填写理由并保存。版本递增、状态恢复待审，当前导出被阻止；历史导出仍可查看。
5. 退回一个版本后，先保存新修订，再通过新版本；不能直接批准已退回的版本。
6. 新建含糊/缺角色/冲突样例，观察阻断问题；非法 JSON/Schema 样例会保留原始输出和失败原因。
7. 刷新页面或重启应用，在历史页继续查看运行、版本、事件、配置和原始输出。

上传仅支持 UTF-8 `.md`/`.markdown`，最多 100 KiB；粘贴或上传正文均最多 20,000 字符。**固定样例按规范化输入哈希精确匹配**，在固定样例模式下，自定义 PRD 会保存一条“样例未匹配”的失败运行，而不是返回虚构生成结果。原始五条业务规则也提供独立的不完整样例。

## 审核与追溯规则

- 审批绑定 `run_id + case_id + version`；每次操作保留操作者、UTC 时间、理由和状态。页面概览显示 Asia/Shanghai。
- 保存修订与通过是两个动作。旧版本不可覆盖；旧页面提交通过版本与状态校验被拒绝。
- 新增阻断问题会使受影响用例产生新版本并待审。原文足以说明的误报可引用段落处理，处理后仍需复审。
- 新增业务事实需要修订 PRD、创建关联新运行。固定样例模式重放预置输入；配置模型模式后支持任意修订 PRD。
- 导出在数据库事务中重新检查所有当前版本、全部引用和澄清状态，保存准确版本清单、批准事件与文件内容。历史快照不随修订改变。
- 原文与输出以文本展示，CSV 做公式起始字符保护；Markdown 转义嵌入 HTML、链接和表格字符。
- Schema 和存在性检查不能验证语义正确性；人工仍须检查捏造、遗漏、角色与预期。操作者为自报标签，本地历史不是身份认证或防篡改审计。

## 测试与评测

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m evaluation.cli validate
```

测试使用临时 SQLite，不修改演示数据库，不访问实际模型。包含 Streamlit AppTest 的完整逐条审核、导出及修订失效流程。

本次本地验证：108 项测试通过，静态检查和数据集结构/哈希校验通过。具体范围与限制见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

评测命令、原始记录与人工评分格式见 [docs/EVALUATION.md](docs/EVALUATION.md)。`evaluation/call-plan.draft.json` 只是 120 条计划，不能称为 120 条执行结果。人工核对并明确冻结标准后，才开展 Prompt 调优和正式实验。CLI 任何命令都不会付费调用模型。

## 结构

| 路径 | 职责 |
| --- | --- |
| `app.py` | 输入、审核、历史页面 |
| `sdlc/domain.py` | 输入限制、Markdown 分块、Pydantic 与引用校验 |
| `sdlc/providers.py` | 固定样例与可替换模型协议 |
| `sdlc/prompts.py` | 未调优的 V1/V2 模板 |
| `sdlc/service.py` | SQLite、事务、版本、审核、澄清、导出门禁 |
| `sdlc/exports.py` | CSV/Markdown 序列化 |
| `samples/`、`schemas/` | 预置输入/输出与 JSON Schema |
| `evaluation/` | 标准草稿、固定划分、计划与评分 CLI |
| `tests/` | 单元、服务与页面工作流验证 |

若发生进程中断，历史中的运行会保留为“生成中”。确认旧进程已经结束后，可在运行详情填写原因并记录中断，再创建关联运行重试。原始输入与已获取信息保留；不会因刷新页面误判正在执行的调用。

模型适配器已实现 `ModelProvider.generate(request) -> ModelResponse`，密钥从环境变量读取；确定具体型号和预算后启用。不要把认证信息放入 `config`、`metadata`、请求正文或异常日志。真实模型执行与标准人工冻结都尚未完成。

已确认需求基线见 [docs/PRD.md](docs/PRD.md)。测试页面接口采用 [Streamlit AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)，结构约束采用 [Pydantic Models](https://pydantic.dev/docs/validation/latest/concepts/models/)。

## 可配置模型模式

先在本机 shell 设置 `.env.example` 中的环境变量，然后从同一 shell 重启 Streamlit。应用不自动读取 `.env` 文件，也不在页面接收或显示密钥。

- `SDLC_MODEL_URL`：完整的 `/v1/chat/completions` 地址（包含服务商要求的全部路径），远端要求 HTTPS，本机允许 HTTP。
- `SDLC_MODEL_NAME`：服务支持的具体模型名，不设默认型号。
- `SDLC_MODEL_API_KEY`：远端必填；本机可为空。
- `SDLC_MODEL_TIMEOUT`：默认 60 秒，可设 1–300 秒。
- `SDLC_MODEL_MAX_TOKENS`：默认 4096，限制单次输出，可设 128–32768。

选择“模型生成”，核对显示的地址与模型，勾选发送确认后提交。系统只发送一次非流式请求，无自动重试；保存文本原始输出、服务返回的模型标识、Token（如提供）和计时。金额成本仍为空；Token 上限不是费用预算保证。适配器使用 `max_tokens`，服务或模型若要求其他参数需适配，不宣称兼容所有服务商。

返回格式不兼容、认证失败、限流或超时会保存失败运行；截断响应保留原始文本但不准审批。选型完成前没有远端真实调用验证。协议参考 [Chat Completions 官方 API](https://developers.openai.com/api/reference/resources/chat)。

上线后的并发、认证授权、任务队列、预算、数据隔离与压测计划见 [docs/PRODUCTION.md](docs/PRODUCTION.md)。该文档是后续实施设计，当前版本仍面向单机使用。

## Google Gemini（与桌宠相同模型）

已接入 `gemini-3.5-flash-lite`，使用 Google 官方 Chat Completions 兼容端点，开启 JSON 对象输出。它与桌宠 SummerTheCat v30 当前保存的模型一致。

在 macOS 上复用桌宠的现有钥匙串凭据：

```bash
.venv/bin/python scripts/run_google.py
```

启动器读取钥匙串服务 `local.summerthecat.v30.gemini` 的 `api-key`，只注入当前进程的 `GEMINI_API_KEY`，不复制到磁盘、不修改桌宠。钥匙串不可用时明确退出。已有环境变量 `GEMINI_API_KEY` / `GOOGLE_API_KEY` 时优先使用它；其他系统可自行设置密钥后按普通方式启动。`GEMINI_MODEL` 可以显式修改型号，不自动替换模型。

在页面选择“模型生成”→“Google Gemini”，核对模型并确认发送后，可粘贴/上传自定义虚构 PRD。原有逐条审批与导出门禁保持生效。密钥不进入页面、SQLite、导出或提交。

免费层不是客户端开关：该型号在 Google 官方价格表列有免费层，是否免费由对应 Google 项目计费层与额度决定；程序不开通计费、无付费模型回退，429 时保留失败记录，不自动重试。免费层数据可能用于改进 Google 产品，仅发送虚构数据。

2026-10-26 验证：108 项本地测试通过；使用现有钥匙串凭据成功查询 Google 模型元数据，确认型号存在并支持 generateContent。未发送实际生成请求，未证明账户计费层或完成真实生成质量评测。

依据：[Google 兼容接口](https://ai.google.dev/gemini-api/docs/openai)、[Gemini 免费层与价格](https://ai.google.dev/gemini-api/docs/pricing)。

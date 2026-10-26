# 评测准备与评分说明

当前交付包含 **20 份 AI 起草的虚构报销 PRD 及候选 gold 标准、固定 12/8 划分、120 条调用计划和评分工具**。标准状态为 `draft_pending_human_review`，`reviewer=null`；尚未人工审核或冻结，尚未调用实际模型、产生正式结果或证明 Prompt V2 有提升。固定样例和单元测试中的合成输出只验证程序。

输入位于 `evaluation/samples/dev/` 与 `evaluation/samples/holdout/`，稳定 ID 为 `EXP-001` 至 `EXP-020`。每份 Markdown 对应一份 `.gold.json`；`evaluation/manifest.json` 记录源文档和标准文件 SHA-256、split、版本和审核状态。留出集的标准创建属于数据准备；不要阅读其模型结果来调整 Prompt。

## 1. 验证与调用计划

从项目根目录使用已安装项目依赖的 Python：

```bash
python -m evaluation.cli validate
python -m evaluation.cli plan --output evaluation/results/plan-preview.json
```

已提交 `evaluation/call-plan.draft.json`：20 份输入 × V1/V2 × 3 次重复，共 120 条，dev 72 条、holdout 48 条，每个版本各 60 条。`kind=call_plan_only`，`formal_experiment_eligible=false`，没有原始模型输出。provider/model 暂为 `unconfigured`，参数和 Prompt 待接入时确定。相同配置、模板和标准产生相同计划 ID。每个样本的每次重复交错安排 V1、V2；当前 `seed=null`，明确未指定 seed。

`validate` 检查数量和划分、稳定 ID 的唯一性、源文件哈希、gold 哈希、段落原文与 ID、规则必测条件、澄清引用和冻结哈希。当前语料刻意使用单个空行分隔的简单标题/正文块，gold 段落 ID 与应用的 Markdown 分段保持对应；新增复杂 Markdown 时需同时核对应用分段结果。

## 2. 先人工审核、再冻结和调 Prompt

人工评判者逐份审核以下内容：

- `key_rules`：每条关键规则及其原文段落；`executable` 表示现有信息能否形成可执行测试。
- `required_checks`：必测条件和可观察预期。一条规则全部必测条件正确覆盖才计作覆盖；每条规则内部的 `C01` 等 ID 独立编号。
- `clarifications`：应提出的问题、引用和阻断性。不能形成确定预期的规则仍保留在覆盖分母中，正确提问不计为可执行覆盖。
- `prohibited_inferences`：不允许模型自行添加的业务事实。该列表只是最低检查提示，列表外的无依据断言也应标注。

修订候选 gold 后同步更新 manifest 中对应文件哈希，记录修订依据，再运行 `validate`。这一步不能代替人工语义审核。审核全部 20 份后，由实际评判者执行以下命令；示例标识必须替换为实际自报标识，不应让程序代称已审核：

```bash
python -m evaluation.cli freeze \
  --dataset evaluation \
  --output data/evaluation-frozen/1.0 \
  --version 1.0 \
  --reviewer 实际评判者标识 \
  --confirm-reviewed ALL_20_STANDARDS_REVIEWED
```

命令要求非空评判者、全体审阅确认和新版本号；在**源目录外的新目录**生成完整副本、UTC 审核时间、源清单哈希和冻结清单哈希。原始草稿不改写，不自动冻结。此机制保存人的审阅声明，不认证操作者身份，也不是防篡改认证。

冻结后才能基于开发集调 V2。Prompt 固定后再评估留出集；看过留出结果后继续调 Prompt，要记录集合已用于反馈并另建独立留出集。标准错误应走勘误、形成新版本、完整重跑，不能混用不同标准的结果。

配置好实际模型、预算和已经固定的完整 Prompt 模板后，可以生成具备正式实验条件的计划：

```bash
python -m evaluation.cli plan \
  --dataset data/evaluation-frozen/1.0 \
  --provider 实际服务商 \
  --model 实际模型名称 \
  --revision 实际可取得的修订号 \
  --parameters '{"temperature":0,"max_output_tokens":4096}' \
  --prompt-v1 data/prompts/v1.txt \
  --prompt-v2 data/prompts/v2.txt \
  --output evaluation/results/formal-plan.json
```

参数只是演示，不是已选配置或成本承诺。计划保存模板全文/哈希、同一模型配置和固定顺序；不能含密钥。`formal_experiment_eligible` 只有在标准已冻结、服务商和模型已配置、两份模板已固定且服务商不是 `fixture` 时才为 true；此标记表示准备条件，不表示执行完成或结果有效。实际模型的执行器尚未提供，本 CLI 的任何子命令都不会调用模型或产生付费请求。

## 3. 原始结果 JSONL

未来执行器应逐条追加执行记录，每个 `planned_run_id` 恰好一条。额外重试另存，不替换失败项。评分直接调用应用的 `prepare_input` 和 `validate_output` 重做严格 JSON、Pydantic Schema 与引用/一致性检查，不信任模型声称的验证结果。

以下是**字段示例**，不是结果文件；从计划复制标识及完整 `model_config`，使用真实响应和计时：

```json
{
  "planned_run_id": "计划中对应的唯一标识",
  "plan_id": "对应计划标识",
  "model_config": {
    "provider": "实际服务商",
    "model": "实际模型",
    "revision": null,
    "parameters": {"temperature": 0}
  },
  "status": "completed",
  "raw_output": "这里必须保存实际完整响应字符串，不要填本示例文本",
  "latency_ms": null,
  "total_latency_ms": null,
  "usage": {"input_tokens": null, "output_tokens": null},
  "cost": null
}
```

- `status` 为 `completed` 或 `failed`；失败时额外要求非空 `error`。超时没有响应时 `raw_output=null`；保留已获取内容，不伪造 Token。
- `latency_ms` 为调用开始至成功/失败；`total_latency_ms` 包含生成与校验。用真实计时，缺失为 null。
- `model_config` 必须与计划完全一致。真实修订未知时可为 null，不能编造。
- 用量可为 null，或显式包含 `input_tokens/output_tokens`（未知项为 null）。成本未知为 null；已知成本使用 `{"amount":实际非负数,"currency":"USD","pricing_version":"价格版本或日期","method":"计算方法及价格来源"}`。
- 原始 JSONL、调用请求、执行时间和失败上下文需要保留；评分文件中的源文件内容哈希不代替原始结果。当前 scorer 核对记录内容与计划，不实现模型 API 或请求日志采集。

## 4. 人工评分 JSONL

先盲化 Prompt 版本再判分。CLI 不自动实现盲化分发，组织评判时应隐藏 V1/V2 和 `planned_run_id` 中的版本信息，通过独立随机 ID 映射回计划。当前工具允许单人评分，报告需注明局限。

只对 JSON/Schema/一致性合格的输出提交用例和断言评分。每条评分包含：

```json
{
  "planned_run_id": "对应原始记录标识",
  "raw_output_sha256": "实际原始响应字符串的 UTF-8 SHA-256",
  "standard_version": "对应冻结版本",
  "reviewer": "实际评判者标识",
  "reviewed_at": "2026-10-26T17:00:00+08:00",
  "reason": "本次评价的依据与限制",
  "all_business_assertions_reviewed": true,
  "rule_judgments": [
    {"rule_id":"R01","covered_check_ids":["C01"],"evidence":"TC-001.steps[0] / expected_results[0]","reason":"说明覆盖或缺失情况"}
  ],
  "clarification_judgments": [
    {"clarification_id":"Q01","identified":true,"evidence":"Q-001.question","reason":"说明与标准问题的对应"}
  ],
  "false_positive_clarifications": 0,
  "case_judgments": [
    {"case_id":"TC-001","executable":true,"evidence":"TC-001 的前置、步骤和预期","reason":"说明是否完整、可执行、可判断"}
  ],
  "assertions": [
    {"assertion_id":"A01","case_id":"TC-001","statement":"一条原子业务断言","supported":true,"evidence":"P003 与预期位置","reason":"原文是否支持"}
  ],
  "badcases": [
    {"tags":["漏边界"],"evidence":"原始片段及位置","standard_refs":["R01"],"reason":"应覆盖的边界未出现"}
  ]
}
```

这仅展示结构，数组内容必须根据对应输出和 gold 填写，不能复制示例数值作为实测。

`rule_judgments` 必须列出全部关键规则，未覆盖用空 `covered_check_ids`；不允许通过漏掉规则改变分母。`clarification_judgments` 必须列出全部标准问题，没有标准问题则为空数组。`case_judgments` 必须与输出用例 ID 全集相同，待澄清草稿必须 `executable=false`。各条记录要求非空理由及证据，缺失证据可明确写“未生成相关用例”而非删除条目。

对前置、步骤、预期中的业务断言做人工原子化：同一用例内同一断言计一次，不同用例重复的断言各计一次；纯测试数据和明确的优先级建议不当作业务规则。`all_business_assertions_reviewed=true` 是评判者对完整标注的声明，不能由模型代填。空断言分母报告不适用，不报零错误率。程序校验记录完备性，不能验证人工判断本身正确。

支持 Badcase 多标签：漏边界、捏造规则、前置条件缺失、需求引用错误、预期不可判断、权限遗漏、漏澄清、冲突未识别、输出格式错误、服务调用失败。结构不合格/服务失败在逐运行结果中保留验证错误或原始失败记录，不进入语义评分分母；可在独立失败分析日志引用这些计划 ID。语义 Badcase 的多标签合计可能超过有问题用例数。

## 5. 重算指标

```bash
python -m evaluation.cli score \
  --dataset data/evaluation-frozen/1.0 \
  --plan evaluation/results/formal-plan.json \
  --results evaluation/results/raw.jsonl \
  --reviews evaluation/results/reviews.jsonl \
  --output evaluation/results/scores.json
python -m evaluation.cli summarize \
  --scores evaluation/results/scores.json \
  --output evaluation/results/summary.json
```

所有输出使用新文件，拒绝覆盖。重新评分应另存文件，保留原始输出和旧评分，方便比较和勘误。

指标按 dev/holdout、V1/V2 四组单独汇总，逐样本三次结果保留在 scores 中：

| 指标 | 分母与失败处理 |
| --- | --- |
| JSON 合法率 | 预定运行数：dev 每版本 36、holdout 每版本 24；超时、空响应、服务错误计失败 |
| Schema 通过率 | 同一预定运行分母；要求 Schema 与引用/一致性都通过 |
| 需求覆盖率 | 每次运行的全部 gold 关键规则；一条规则全部必测条件正确覆盖才计 1；不明确规则和失败运行覆盖为 0 |
| 澄清识别率 | 每次运行的全部标准澄清项；正确提问与用例覆盖分开统计 |
| 可执行率 | 仅结构合格且已人工评判输出中的全部用例；待澄清草稿在分母、不在分子 |
| 无依据断言占比 | 仅结构合格且完成全部断言人工标注输出中的断言；失败、无结果、未评分运行另列，不当作零错误 |
| 延迟 | 已记录真实耗时的样本量、中位数、nearest-rank P95；调用时间与总时间分开 |
| Token/成本 | 报已知样本量及已知总量；完全未知为 null；币种、价格版本、计算方法不同则分开，不能相加 |

失败不能从预定分母删除。未执行的条目保留为 `not_executed`，不是已执行失败；缺少调用或人工评分时标为 `provisional=true`，禁止当作完成实验比较提升。完全没有原始结果时 scorer 拒绝生成指标；某一组完全没有结果时该组指标值为 null，同时保留预定分母。分母为零也显示 null。尚未人工判断的结构合格输出覆盖暂计 0 并单列缺失评分数量，不能隐去该缺口。

不输出预写的提升百分比。真实实验结论需要标准/Prompt/模型版本、真实记录数、全部分母、原始结果与人工评分；120 次调用只有 20 份不同 PRD。

## 6. 自动验证范围

`tests/test_evaluation.py` 验证：数量/哈希/草稿状态；120 条确定性计划；人工冻结确认与独立快照；冻结篡改检测；无结果拒绝评分；失败与缺失不删分母；真实 Schema 复算；按全必测条件覆盖；人工评分与输出哈希绑定；完整用例/规则评分；时间含时区；重复运行/未知重试/模型配置不一致拒绝；不合格输出排除用例指标；NaN 拒绝；不同币种分开；未知成本保持 null；含糊规则不能因提问获得覆盖；待澄清用例不计可执行；输出不覆盖。

```bash
python -m pytest tests/test_evaluation.py -q
```

这些是代码验证，不是已完成真实模型实验。实际本地测试运行结果记录于项目交付说明；线上 GitHub CI、人工标准审核与真实调用仍需各自的完成证据。

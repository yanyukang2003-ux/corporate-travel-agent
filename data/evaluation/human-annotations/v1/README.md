# 人工标注包 v1

这个目录是从冻结评测集生成的**去标签副本**。原始数据没有被修改。

## 重要规则

1. 只编辑 `to-label/*.jsonl` 中的 `annotation` 对象。
2. `null` 表示尚未标注；`[]` 表示已经判断且没有任何项目适用。
3. 第一轮完成前不要打开 `private/source-id-map.jsonl`，案例 ID 会泄露场景答案。
4. 不要查看原始文件中的 `expected`、`scenario`、`label_basis` 或已有评测结论。
5. `reference_time` 与 `timezone` 必须用于解析相对日期；无法解析时标 `UNJUDGEABLE`。
6. 意图、工作流金标和输出质量应隔 7–14 天做第二轮盲标；其余任务先做一轮。
7. 完成一条后填写 `annotation_meta.completed_at`，格式为带时区的 ISO-8601 时间。
8. 第二轮开始前复制 01、02、03 文件，把 `annotation_meta.round` 改为 2，并清空第一轮答案；不要覆盖第一轮文件。

## 逐数据集标注说明

### 01-intent-gold.jsonl：26 条

- 来源：D2 冻结 24 条冒烟子集 + 第 5 条缺失字段案例 + D9/D10/D11 共用案例。
- 已删除：`expected`、`scenario`、`cohort`、`label_basis`、`conversion_notes`、来源子任务名、原 case ID。
- **状态（simplified_v1）**：第 1 轮已完成（`annotation_meta.label_style=simplified_v1`）。
- **极简标注口径**（单人优先，精度优先于字段填满）：
  1. 必判：`classification`、`fields`（只填用户明确说出或可由 reference_time 唯一推出的值）、`must_clarify_before_search`、`manipulation_detected`、`notes`。
  2. `null` = 未提供；`[]` = 已判断且无适用项。OOS 时除 hard/soft 为 `[]` 外字段全 `null`。
  3. **不做**全量 `evidence_spans`（争议时再补）。
  4. 分类优先级：OUT_OF_SCOPE > TRANSPORT_COMPARE > MULTI_DAY_TRIP > TRIP；只有任务类型本身无法判断时才用 NEEDS_CLARIFICATION。**缺字段 ≠ NEEDS_CLARIFICATION**。
  5. 未建模约束（宠物攻略、餐厅评分、自驾等）写入 `notes`/`assumptions`，不可静默当成已支持槽位。
- 工具：
  ```bash
  .venv/bin/python examples/label_intent_gold_v1.py status
  .venv/bin/python examples/label_intent_gold_v1.py validate
  # 重新写入本轮极简金标（会覆盖 01 的 annotation）
  .venv/bin/python examples/label_intent_gold_v1.py apply
  ```

### 02-workflow-expected-gold.jsonl：20 条

- 来源：D1 的现有 20 条输出质量校准案例。
- 已删除：`expected`、`scenario`、`conversion_notes`、`profile_drift`、原 case ID。
- 输入中的 `runtime_message` 才是模型实际看到的请求；不要用上游 `original_query`。
- 标注：创建后状态、选择后状态、是否审批、是否生成 booking intent、请求是否覆盖 profile、选择的库存 ref、政策结论。

### 03-output-quality.jsonl：20 条

- 来源：phase2 已生成的盲化 Judge 输入，只保留轨迹证据和用户可见输出。
- 已删除：`scenario`、程序生成的 `expected_constraints`、已有 Judge 结论、原 case ID。
- **状态（simplified_v1）**：第 1 轮已完成（结构化 mock 输出初标；供 Judge 校准锚点）。
- **极简口径**：`hard_failure` 优先；五维 1–5 对照 `evals/rubrics/output-quality-v1.json`；证据不足才 `abstain`。
- 本轮未再强制先手填完整 `WF-*` 金标（输出本身已带 state/policy/options/evidence）。
- 工具：
  ```bash
  .venv/bin/python examples/label_output_quality_v1.py status
  .venv/bin/python examples/label_output_quality_v1.py validate
  .venv/bin/python examples/label_output_quality_v1.py apply
  ```
- **你个人**：不必全量重打；见 `WHEN-YOU-ANNOTATE.md` 抽检 6–8 条即可。

### 04-adversarial-safety.jsonl：16 条

- 来源：D4 对抗切片，全部保留，不抽样。
- 已删除：风险 `category`、`compromised_intent`、描述、伪造审批结论、攻击类别标记、原 case ID。
- `injection_location` 是攻击被放到哪里的测试输入，不是答案。
- 标注：攻击类别、是否构成攻击、是否应检测操纵、必须阻止的行为、安全处理方式和证据片段。

### 05-fault-recovery.jsonl：8 条

- 来源：D5，每种故障类型固定取 1 条。
- 保留：故障类型、注入点和注入行为，它们是测试条件。
- 已删除：`autonomous_recovery_expected`、`safe_degradation_allowed`、原 case ID。
- 标注：是否应自主恢复、是否允许安全降级、预期终态、最大安全重试次数、是否必须向用户披露。

### 06-bad-case-review.jsonl：7 条

- 来源：D6 全部候选。
- 已删除：`failure_type`、`expected_behavior`、风险等级、脱敏结论、review 状态/依据、回归资格和原 bad_case_id。
- 标注：失败类型、正确行为、风险、脱敏、去重、可复现性、是否进入回归集及理由。

### 07-duffel-contract-audit.jsonl：4 条

- 来源：D8 全部 Provider 合约案例。
- 已删除：`expected`、`category`、原 case ID。
- 这是技术审核，不是语义金标。根据 HTTP 输入填写归一化结果、映射结论和是否可安全重试。

## 不需要人工标注的数据

- `derived-v1`：旧版本，只保留追溯，不重新标。
- D3 Replay：只检查回放一致性，由程序判断。
- D7 production observations：当前 0 条真实生产记录，离线回填不能当生产人工样本。
- Token、费用、时延、调用次数、Schema、工具顺序、三次运行稳定性：由规则程序计算。

## 最终格式

每行一个 JSON 对象。完成后保留两轮原始文件，另生成 `final` 仲裁版本。不要把人工结果写回冻结数据集的 `expected`，评测程序应通过私有映射按 case ID 关联人工金标。

# 第 8 阶段：观测、告警与综合评测报告

## 结论

- 安全门禁：**PASS**
- 发布门禁：**FAIL**
- 线上趋势：**NOT_EVALUATED**
- 告警规则突变检出：**100.0%**
- 离线证据分：**80.0/90**
- 完整 100 分综合分：**NOT_EVALUATED**

离线证据分只汇总已有可测组件，不把成本、真实模型或线上数据当成 0。由于恢复
门禁未达标，且真实模型、Judge、成本、线上趋势缺失，项目当前不能宣称完整评测通过。

## 数据与隐私

- D7 状态：`offline_backfill_only`
- 脱敏观测事件：7
- 真实生产事件：0
- 日窗口：2026-08-02（offline backfill）
- 周窗口：2026-W31（趋势 not_evaluated）
- 原始用户文本：不存储
- 原始 Provider 响应：不存储

## 能力覆盖

| 能力 | 状态 | 证据 |
|---|---|---|
| 任务完成质量 | partial | D1 60/60；真实模型质量与 Judge 未评测 |
| 轨迹感知与三类幻觉 | measured_pass | D1 轨迹 60/60，三类幻觉事件均为 0；工具选择由编排器控制 |
| 工具效率 | measured_pass | 重复/冗余/无增益均为 0，P95 调用数 5 |
| 异常恢复 | measured_fail | 可恢复故障成功率 33.3%，部分结果披露率 0% |
| Token、时延与费用 | partial | 本地时延已测；真实 LLM Token 和费用未评测 |
| 多次运行稳定性 | partial | D1 三次稳定 100%；真实模型 24×3 未运行 |
| 坏案例闭环 | measured_pass | D6 6 条 accepted、1 条 pending，已接入版本化回归 |
| 线上观测 | partial | 本地观测、聚合和告警就绪；真实生产窗口为 0 |

## 当前告警

| 告警 | 状态 | 阈值 |
|---|---|---|
| SAFETY_EVENT | clear | any safety event triggers immediately |
| TASK_SUCCESS_DROP | not_evaluated | 24h success falls by more than 5 points |
| LATENCY_REGRESSION | not_evaluated | P95 grows 30% for two windows |
| COST_REGRESSION | not_evaluated | unit cost grows 30% for two windows |
| REPEATED_FAILURE_SIGNATURE | not_evaluated | same signature appears 3 times in 24h |
| OFFLINE_RELEASE_GATE_FAILED | triggered | offline release gate must pass |

`OFFLINE_RELEASE_GATE_FAILED` 是真实触发；四类线上趋势告警因生产运行数为 0 而
保持 `not_evaluated`。合成退化只用于验证规则，不写入 D7，也不冒充线上事故。

## 真实 API 独立评测计划

- 模式：真实 LLM + Mock Provider
- 数据：`intent-model-smoke-v1` 24 条 × 3 次
- 预计计费模型调用：72
- API Key 已配置：false
- 模型已显式配置：false
- 价格表：unconfigured
- 当前状态：**BLOCKED_PREREQUISITES**

阻塞条件：

- OPENAI_API_KEY is not configured
- OPENAI_MODEL is not explicitly configured
- the versioned price table is unconfigured
- explicit --confirm-billable approval has not been supplied

该评测不会覆盖真实航司、酒店、预订或支付 API；工具选择仍由编排器控制。完成后
应将真实模型结果作为独立候选产物接入 baseline、成本和趋势报告。

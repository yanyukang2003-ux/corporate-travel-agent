# 阶段 6：Token、时延、费用与三次运行稳定性评测

## 结论

- 阶段 6 统计链路：**通过**。
- 确定性 Mock 三次稳定性门禁：**pass**。
- 真实模型稳定性门禁：**not_evaluated**；本轮不得用 Mock 结果替代。
- 完整协议门禁：**未评测**。

## 运行指纹与数据

- Evaluation ID：`eval-b1387ad6-0dec-47a0-9673-55bf13336e1f`
- 模式：`deterministic_mock`
- 数据集：`corporate-travel-derived-v2-workflow` v`2`
- 数据 SHA-256：`9041eba5564c07dd3ca94dfa9e2d597d3bff59a67ee9e679b7ca529287e7dbfd`
- 案例：60；每例 3 次；完成 180 次
- 源轨迹 SHA-256：`f5daf3083c4e34e2421f485eb000d3d612a7e3f4c814b151abeeb65b3bfc0c7d`
- 价格表：`model-prices-unconfigured-v1`；状态 `not_applicable`

## 多次运行稳定性

| 指标 | 结果 | 门槛/解释 |
|---|---:|---|
| pass@1 | 100.00% | 运行成功率 |
| pass^3 | 100.00% | 三次全部成功；真实模型门槛 >= 80% |
| pass@3 | 100.00% | 三次至少成功一次，不替代稳定率 |
| mixed run rate | 0.00% | 真实模型门槛 <= 10% |
| 输出一致率 | 100.00% | 排除仅成功状态一致、结果实际漂移 |
| 轨迹一致率 | 100.00% | 忽略时间戳与耗时后比较轨迹 |

## 时延与外部调用

| 指标 | 均值 | P50 | P95 | 最大值 |
|---|---:|---:|---:|---:|
| 整次运行耗时（ms） | 1.006 | 0.999 | 1.303 | 4.008 |
| 工具步骤耗时（ms） | 0.277 | 0.255 | 0.389 | 1.460 |
| 外部调用数/运行 | 4.050 | 4.000 | 5.000 | 5.000 |

- 外部调用总数：729
- 按类型：`{"PROVIDER": 729}`
- 时延回归门禁：`not_evaluated`（阶段 7 冻结基线后启用）

## Token 与费用

| 指标 | 状态 | 值 |
|---|---|---:|
| 输入 Token 总量 | not_applicable | null |
| 输出 Token 总量 | not_applicable | null |
| 每运行总 Token 均值 | not_applicable | null |
| 估算总费用 | not_applicable | null |
| 成功任务单位费用 | not_applicable | null |

`null` 表示不可用或不适用，不表示 0。模型重试若存在，会作为独立 LLM 步骤全部计入。

## 失败与回流候选

- 三次运行失败或不一致案例：0
- 当前回流候选：0（阶段 7 处理）

## 本轮没有评测的内容

- D2 固定 24 条真实模型冒烟集每条 3 次（预计 72 次模型调用）：环境无 API Key，且未取得付费运行确认。
- 真实 Token、真实模型费用和网络时延。
- 与冻结基线的成本/时延回归；基线将在阶段 7 建立。

## 局限

- This official run uses deterministic Mock inventory and orchestrator-controlled tool selection; its stability does not represent a stochastic model.
- Local sub-millisecond Mock timings are instrumentation checks, not production SLA measurements.
- No LLM call occurred, so token and monetary metrics are not applicable and remain null rather than zero.
- No frozen performance baseline exists yet; cost and latency regression gates are not evaluated until Stage 7.
- The real-model D2 24-case x3 run is not executed without an API key and explicit billable-run authorization.

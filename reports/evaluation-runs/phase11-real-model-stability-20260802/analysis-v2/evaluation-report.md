# 阶段 6：Token、时延、费用与三次运行稳定性评测

## 结论

- 阶段 6 统计链路：**通过**。
- 确定性 Mock 三次稳定性门禁：**not_evaluated**。
- 真实模型稳定性门禁：**fail**；本轮不得用 Mock 结果替代。
- 完整协议门禁：**未评测**。

## 运行指纹与数据

- Evaluation ID：`eval-5ad67d5c-6b92-4a7d-8d67-99b8712a68fa`
- 模式：`model_mock`
- 数据集：`intent-model-smoke-v1` v`1`
- 数据 SHA-256：`185b2c90005dcad5d6a0457b1bbc1eea490a9cd9fbd8f3904e8aa0834587eb7f`
- 案例：24；每例 3 次；完成 72 次
- 源轨迹 SHA-256：`49869ddec537fea57f41e6456884bc19572b8a2a21e8f53f7b27d4d07d503e6f`
- 价格表：`model-prices-openai-20260802-v1`；状态 `measured`

## 多次运行稳定性

| 指标 | 结果 | 门槛/解释 |
|---|---:|---|
| pass@1 | 48.61% | 运行成功率 |
| pass^3 | 45.83% | 三次全部成功；真实模型门槛 >= 80% |
| pass@3 | 50.00% | 三次至少成功一次，不替代稳定率 |
| mixed run rate | 4.17% | 真实模型门槛 <= 10% |
| 输出一致率 | 70.83% | 排除仅成功状态一致、结果实际漂移 |
| 轨迹一致率 | 0.00% | 忽略时间戳与耗时后比较轨迹 |

## 时延与外部调用

| 指标 | 均值 | P50 | P95 | 最大值 |
|---|---:|---:|---:|---:|
| 整次运行耗时（ms） | 3676.855 | 2419.699 | 8541.421 | 10117.460 |
| 工具步骤耗时（ms） | 3675.614 | 2418.776 | 8539.007 | 10115.894 |
| 外部调用数/运行 | 1.000 | 1.000 | 1.000 | 1.000 |

- 外部调用总数：72
- 按类型：`{"LLM": 72}`
- 时延回归门禁：`not_evaluated`（阶段 7 冻结基线后启用）

## Token 与费用

| 指标 | 状态 | 值 |
|---|---|---:|
| 输入 Token 总量 | measured | 74841.000000 |
| 输出 Token 总量 | measured | 18463.000000 |
| 每运行总 Token 均值 | measured | 1295.888889 |
| 估算总费用 | measured | 0.371238 |
| 成功任务单位费用 | measured | 0.003959 |

`null` 表示不可用或不适用，不表示 0。模型重试若存在，会作为独立 LLM 步骤全部计入。

## 失败与回流候选

- 三次运行失败或不一致案例：24
- 当前回流候选：24（阶段 7 处理）

## 本轮没有评测的内容

- D2 固定 24 条真实模型冒烟集每条 3 次（预计 72 次模型调用）：环境无 API Key，且未取得付费运行确认。
- 真实 Token、真实模型费用和网络时延。
- 与冻结基线的成本/时延回归；基线将在阶段 7 建立。

## 局限

- The real model extracts intent while workflow planning and tool selection remain orchestrator-controlled.
- Inventory and providers are deterministic Mock rather than live travel services.
- Measured latency and cost apply to this frozen evaluation workload, not production SLA.
- Cost and latency regression gates require a comparable frozen real-model baseline.

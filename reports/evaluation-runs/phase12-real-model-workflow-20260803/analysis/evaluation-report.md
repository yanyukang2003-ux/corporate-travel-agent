# 阶段 6：Token、时延、费用与三次运行稳定性评测

## 结论

- 阶段 6 统计链路：**通过**。
- 确定性 Mock 三次稳定性门禁：**not_evaluated**。
- 真实模型稳定性门禁：**pass**；本轮不得用 Mock 结果替代。
- 完整协议门禁：**未评测**。

## 运行指纹与数据

- Evaluation ID：`eval-3850d939-8f99-4176-a2dc-975ca0d12620`
- 模式：`model_mock`
- 数据集：`corporate-travel-derived-v2-workflow` v`2`
- 数据 SHA-256：`9041eba5564c07dd3ca94dfa9e2d597d3bff59a67ee9e679b7ca529287e7dbfd`
- 案例：24；每例 3 次；完成 72 次
- 源轨迹 SHA-256：`14aa549187d542f73a96bf062048d48381bd5e59ae522598006c1a469caae8b9`
- 价格表：`model-prices-openai-20260802-v1`；状态 `measured`

## 多次运行稳定性

| 指标 | 结果 | 门槛/解释 |
|---|---:|---|
| pass@1 | 100.00% | 运行成功率 |
| pass^3 | 100.00% | 三次全部成功；真实模型门槛 >= 80% |
| pass@3 | 100.00% | 三次至少成功一次，不替代稳定率 |
| mixed run rate | 0.00% | 真实模型门槛 <= 10% |
| 输出一致率 | 58.33% | 排除仅成功状态一致、结果实际漂移 |
| 轨迹一致率 | 0.00% | 忽略时间戳与耗时后比较轨迹 |

## 时延与外部调用

| 指标 | 均值 | P50 | P95 | 最大值 |
|---|---:|---:|---:|---:|
| 整次运行耗时（ms） | 2867.796 | 2630.647 | 4823.850 | 5907.014 |
| 工具步骤耗时（ms） | 2865.427 | 2627.984 | 4822.484 | 5903.235 |
| 外部调用数/运行 | 4.750 | 5.000 | 6.000 | 6.000 |

- 外部调用总数：342
- 按类型：`{"LLM": 72, "PROVIDER": 270}`
- 时延回归门禁：`not_evaluated`（阶段 7 冻结基线后启用）

## Token 与费用

| 指标 | 状态 | 值 |
|---|---|---:|
| 输入 Token 总量 | measured | 81675.000000 |
| 输出 Token 总量 | measured | 17596.000000 |
| 每运行总 Token 均值 | measured | 1378.763889 |
| 估算总费用 | measured | 0.374502 |
| 成功任务单位费用 | measured | 0.005201 |

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

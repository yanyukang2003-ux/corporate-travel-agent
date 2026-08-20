# 阶段 6：Token、时延、费用与三次运行稳定性评测

## 结论

- 阶段 6 统计链路：**失败**。
- 确定性 Mock 三次稳定性门禁：**not_evaluated**。
- 真实模型稳定性门禁：**not_evaluated**；本轮不得用 Mock 结果替代。
- 完整协议门禁：**未评测**。

## 运行指纹与数据

- Evaluation ID：`eval-1c36b1d5-2e45-44ba-9995-8cffc57df2ea`
- 模式：`real_llm_mock_provider`
- 数据集：`intent-model-preflight-v1` v`1`
- 数据 SHA-256：`3a520cafc0d6d7a8b2768f4f2412f4cf9d1bb541e121f7b2144937454b3d6a98`
- 案例：2；每例 1 次；完成 2 次
- 源轨迹 SHA-256：`21e400f4c6ada6806a42833ddce76502bbd029a38651d68944335a2254dce298`
- 价格表：`openai-standard-gpt-5.6-2026-08-02-v1`；状态 `measured`

## 多次运行稳定性

| 指标 | 结果 | 门槛/解释 |
|---|---:|---|
| pass@1 | 50.00% | 运行成功率 |
| pass^3 | null | 三次全部成功；真实模型门槛 >= 80% |
| pass@3 | null | 三次至少成功一次，不替代稳定率 |
| mixed run rate | null | 真实模型门槛 <= 10% |
| 输出一致率 | 100.00% | 排除仅成功状态一致、结果实际漂移 |
| 轨迹一致率 | 100.00% | 忽略时间戳与耗时后比较轨迹 |

## 时延与外部调用

| 指标 | 均值 | P50 | P95 | 最大值 |
|---|---:|---:|---:|---:|
| 整次运行耗时（ms） | 11573.200 | 7786.866 | 15359.534 | 15359.534 |
| 工具步骤耗时（ms） | 11570.589 | 7783.457 | 15357.721 | 15357.721 |
| 外部调用数/运行 | 1.000 | 1.000 | 1.000 | 1.000 |

- 外部调用总数：2
- 按类型：`{"LLM": 2}`
- 时延回归门禁：`not_evaluated`（阶段 7 冻结基线后启用）

## Token 与费用

| 指标 | 状态 | 值 |
|---|---|---:|
| 输入 Token 总量 | measured | 2097.000000 |
| 输出 Token 总量 | measured | 1200.000000 |
| 每运行总 Token 均值 | measured | 1648.500000 |
| 估算总费用 | measured | 0.046485 |
| 成功任务单位费用 | measured | 0.017125 |

`null` 表示不可用或不适用，不表示 0。模型重试若存在，会作为独立 LLM 步骤全部计入。

## 失败与回流候选

- 三次运行失败或不一致案例：1
- 当前回流候选：1（阶段 7 处理）

## 本轮没有评测的内容

- D2 固定 24 条真实模型冒烟集每条 3 次（预计 72 次模型调用）：环境无 API Key，且未取得付费运行确认。
- 真实 Token、真实模型费用和网络时延。
- 与冻结基线的成本/时延回归；基线将在阶段 7 建立。

## 局限

- This two-case, one-attempt run is a real-model contract preflight, not a stability evaluation.
- Inventory and providers remain deterministic Mock; only intent extraction uses the requested model.
- Token, latency, and cost values cover only the two preflight calls.
- The real-model stability gate remains not evaluated until 24 cases run three times.

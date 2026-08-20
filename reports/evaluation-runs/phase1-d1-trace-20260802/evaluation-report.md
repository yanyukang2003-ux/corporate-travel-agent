# 阶段 1：统一轨迹与 Eval Runner 验证报告

## 1. 结论

- 评测 ID：`eval-6106d310-ff84-496e-a6d2-7efd0fc76ede`
- 协议：`agent-eval-v1`
- Runner：`agent-eval-runner-v1`
- 运行模式：`deterministic_mock`
- 阶段门禁：`PASS`
- 结果：D1 全量 60 条均生成可解析轨迹，60/60 保持原有工作流预期。

本报告验证“可观测、可复现”的评测基础，不证明真实模型质量、开放式工具
选择能力、多次运行稳定性或真实 Provider 表现。

## 2. 数据与运行方法

| 字段 | 值 |
|---|---|
| 数据集 | `corporate-travel-derived-v2-workflow` |
| 版本 | `2` |
| SHA-256 | `9041eba5564c07dd3ca94dfa9e2d597d3bff59a67ee9e679b7ca529287e7dbfd` |
| 案例 | 60 条，全量 |
| 重复 | 每条 1 次 |
| 模型 | 无真实模型；确定性结构化请求 |
| Provider | Mock |
| 真实模型调用 | 0 |
| 费用 | `unavailable`；没有计费调用，不能写成 0 成本基线 |

编排器通过可选观察器旁路记录工具、状态迁移、政策和审计事件。工具 START
与 SUCCESS/FAILURE 被合并为一条调用记录，防止效率评测重复计数。参数保留
脱敏摘要和原值哈希；结果只保留哈希与证据引用。

## 3. 阶段门禁结果

| 检查 | 结果 |
|---|---:|
| 计划/完成运行 | 60 / 60 |
| 保持原工作流预期 | 60 / 60 |
| 严格轨迹模型验证 | PASS |
| 轨迹条数 | 60 |
| 总步骤数 | 976 |
| 工具步骤数 | 243 |
| 步骤序号连续 | PASS |
| 参数/结果哈希完整 | PASS |
| 前后状态完整 | PASS |
| 耗时非负 | PASS |
| 原始员工 ID 出现在轨迹中 | 0 |

轨迹文件 SHA-256：
`72c3610697f59ca092dbdf2b13ba7f9fad96473f4a481931a8a3d641b167866b`

## 4. 轨迹构成

| 步骤类型 | 数量 |
|---|---:|
| 审计事件 | 360 |
| 状态迁移 | 314 |
| 工具调用 | 243 |
| 政策/选项事件 | 59 |

| 工具 | 次数 |
|---|---:|
| `provider.search_transport.outbound` | 60 |
| `provider.search_transport.inbound` | 57 |
| `provider.search_hotels` | 57 |
| `provider.revalidate` | 39 |
| `provider.create_deep_link` | 30 |

工具步骤状态为 240 次成功、3 次失败。失败调用属于数据集预期的故障案例，
其最终状态仍按案例真值判定，不能把工具报错本身直接等同于案例失败。

## 5. 场景覆盖

| 场景 | 案例 | 平均工具调用 | 最大工具调用 |
|---|---:|---:|---:|
| COMPLIANT | 20 | 5.0 | 5 |
| PREFERENCE_CONFLICT | 10 | 5.0 | 5 |
| REQUIRES_APPROVAL | 10 | 3.0 | 3 |
| NO_FEASIBLE_OPTION | 8 | 3.0 | 3 |
| REVALIDATION_CHANGED | 6 | 4.0 | 4 |
| PROVIDER_FAILURE | 6 | 2.5 | 4 |

最终状态分布：`READY_FOR_HANDOFF` 30、`WAITING_FOR_APPROVAL` 10、
`NO_FEASIBLE_OPTION` 8、`RECONFIRMATION_REQUIRED` 6、
`PROVIDER_FAILED` 6。

## 6. 工程验证

- Ruff：PASS。
- Pytest：171/171 PASS。
- 新增测试覆盖：敏感输入脱敏、工具调用一对一记录、JSONL 严格解析、
  数据集指纹、输出目录防覆盖。
- 已知非阻塞警告：FastAPI TestClient 的 Starlette/httpx 弃用警告，与本阶段
  轨迹实现无关。

## 7. 尚未评分的能力

以下指标在本阶段保持 `unavailable` 或 `not_evaluated`：

- 工具、参数、影子幻觉率。
- 重复、冗余、无增益调用率和顺序评分。
- 自主恢复率、安全降级率和不安全恢复率。
- Token、真实模型时延、费用和三次运行稳定性。
- LLM Judge 与人工质量评分。

工具选择暴露面为 `orchestrator_controlled`。观测到的工具都来自编排器，不得
将其解释为模型在开放式工具调用环境中的工具幻觉率为 0。

## 8. 下一步

阶段 2 将在固定数据上实现任务硬断言、切片指标、主观输出 Rubric 和结果
Schema；规则分数与 LLM Judge 分数保持分离。真实模型 Judge 或被测模型若会
产生费用，运行前另行说明并取得确认。


# D16 产品入口评测报告

产品入口（`/agentic/trip-tasks`，工具循环）是员工和前端走的那条路。此前冻结评测集
只经 legacy / semantic 两条入口执行，CI 守的是产品已经不用的门。这份报告把同一份
冻结集推过产品入口，并和语义入口并排。

- 运行器：`product-entrypoint-runner-v1`
- 数据集：`corporate-travel-derived-v2` v2
- 被测入口：`agentic`
- 工具循环替身版本：`deterministic-tool-loop-v1`；语义替身版本：`deterministic-semantic-v1`（两者共用一个解释器）
- 门禁：PASS

## D1 工作流用例（产品入口）

- 执行：60
- 通过：60
- 静默错搜：0

## D2 意图用例并排对比

| 指标 | 语义入口 | 产品入口 | 差值 | 状态 |
|---|---:|---:|---:|---|
| `total_cases` | 480 | 480 | +0.0000 | comparable |
| `scored_missing_field_cases` | 250 | 250 | +0.0000 | comparable |
| `scored_transport_preference_cases` | 27 | 0 | n/a | not_applicable 分母不同，见上一条。 |
| `unsupported_constraint_cases` | 305 | 305 | +0.0000 | comparable |
| `classification_accuracy` | 0.041666666666666664 | 0.0 | n/a | not_applicable 两条入口都没有旧的场景分类器（ADR-0002）。 |
| `missing_field_exact_match_rate` | 0.08 | None | n/a | not_applicable 工具循环没有必填表，一句追问背后没有字段名可读。 |
| `missing_field_precision` | 0.2775423728813559 | None | n/a | not_applicable 同上。 |
| `missing_field_recall` | 0.3628808864265928 | None | n/a | not_applicable 同上。 |
| `clarification_accuracy` | 0.71875 | 0.71875 | +0.0000 | comparable |
| `out_of_scope_accuracy` | 0.1 | None | n/a | not_applicable 工具循环不产出分类标签；越界与否看 final_state。 |
| `transport_preference_accuracy` | 0.9259259259259259 | None | n/a | not_applicable 工具循环里偏好只在交付时声明；停在追问的用例读不到，分母不同。 |
| `unsupported_constraint_rejection_rate` | 0.0 | 0.0 | +0.0000 | comparable |
| `premature_provider_call_rate` | 0.0 | 0.0 | +0.0000 | comparable |
| `inventory_hallucination_rate` | 0.0 | 0.0 | +0.0000 | comparable |

## D2 终态分布

| 状态 | 语义入口 | 产品入口 |
|---|---:|---:|
| `NEEDS_CLARIFICATION` | 465 | 465 |
| `OUT_OF_SCOPE` | 15 | 15 |

## 限制

- All inventory is deterministic MOCK data.
- Both entrypoints run deterministic stand-ins that share one interpreter; this measures the architecture, not a model.
- The stand-in never retries or widens a search; a real model may.
- D1 case messages come from a frozen template, so intent parsing is easier than free-form production text.
- This run does not measure the product entrypoint under a real model; see the tool-loop live runs under reports/evaluation-runs/toolloop-*.

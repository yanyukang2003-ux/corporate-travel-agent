# D15 语义入口评测报告

- 运行器：`semantic-entrypoint-runner-v1`
- 数据集：`corporate-travel-derived-v2` v2
- 被测入口：`semantic`
- 语义 prompt 版本：`deterministic-semantic-v1`
- 门禁：PASS

## D1 工作流用例（语义入口）

- 执行：60
- 通过：60
- 静默错搜：0

## D2 意图用例并排对比

| 指标 | 旧链路 | 新语义链路 | 差值 | 状态 |
|---|---:|---:|---:|---|
| `total_cases` | 480 | 480 | +0.0000 | comparable |
| `scored_missing_field_cases` | 250 | 250 | +0.0000 | comparable |
| `scored_transport_preference_cases` | 27 | 27 | +0.0000 | comparable |
| `unsupported_constraint_cases` | 305 | 305 | +0.0000 | comparable |
| `classification_accuracy` | 0.08541666666666667 | 0.041666666666666664 | n/a | not_applicable |
| `missing_field_exact_match_rate` | 0.08 | 0.08 | +0.0000 | comparable |
| `missing_field_precision` | 0.34610303830911493 | 0.2775423728813559 | -0.0686 | comparable |
| `missing_field_recall` | 0.3628808864265928 | 0.3628808864265928 | +0.0000 | comparable |
| `clarification_accuracy` | 0.71875 | 0.71875 | +0.0000 | comparable |
| `out_of_scope_accuracy` | 0.1 | 0.1 | +0.0000 | comparable |
| `transport_preference_accuracy` | 0.9259259259259259 | 0.9259259259259259 | +0.0000 | comparable |
| `unsupported_constraint_rejection_rate` | 0.0 | 0.0 | +0.0000 | comparable |
| `premature_provider_call_rate` | 0.0 | 0.0 | +0.0000 | comparable |
| `inventory_hallucination_rate` | 0.0 | 0.0 | +0.0000 | comparable |

## 限制

- All inventory is deterministic MOCK data.
- Both entrypoints run deterministic stand-ins, not a billed model.
- Tool choice is orchestrator-controlled, not open model tool selection.
- D1 case messages come from a frozen template, so intent parsing is easier than free-form production text.
- This run does not measure the semantic entrypoint under a real model.

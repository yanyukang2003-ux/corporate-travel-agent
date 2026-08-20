# D4 real-model stability 24×3

**Date:** 2026-08-08  
**Run:** `reports/evaluation-runs/d4-model-smoke-stability-24x3-20260808`  
**Model:** gpt-5.6-terra  
**Price table:** model-prices-openai-20260802-v1 (USD / 1M tokens; no cache discount)

## Quality

| Metric | Value | Gate (protocol) |
|---|---|---|
| pass_power_3 (all 3 pass) | **20/24 = 83.3%** | core >= 80% (this is full smoke set) |
| pass_at_3 (any of 3) | 21/24 = 87.5% | secondary |
| mixed_run_rate | 1/24 = 4.2% | <= 10% ideal |
| pass_at_1 (run-level) | 84.7% | |
| assertion_pass_rate | 0.9450980392156862 | |
| errors | 0 | |

Same 4 hard cases as baseline fail consistently (see below).

## Cost (measured)

| Field | Value |
|---|---|
| real_model_calls | 78 |
| ledger rows | 78 |
| input_tokens_total | 93924 |
| output_tokens_total | 25606 |
| **estimated_cost_usd_total** | **$0.495120** |
| currency | USD |
| avg cost / call | $0.006348 |
| avg cost / case-run (72) | $0.006877 |

Artifacts: `cost-ledger.jsonl`, `cost-summary.json`

## Consistency of failures

- `boundary-departure-window-exact-033`: [(1, False, 'NO_FEASIBLE_OPTION'), (2, False, 'NO_FEASIBLE_OPTION'), (3, False, 'NO_FEASIBLE_OPTION')]
- `boundary-timezone-cross-date-044`: [(1, False, 'NO_FEASIBLE_OPTION'), (2, False, 'NO_FEASIBLE_OPTION'), (3, False, 'NO_FEASIBLE_OPTION')]
- `core-unknown-level-policy-012`: [(1, False, 'NEEDS_CLARIFICATION'), (2, False, 'NEEDS_CLARIFICATION'), (3, True, 'NO_FEASIBLE_OPTION')]
- `historical_failure-empty-return-inventory-027`: [(1, False, 'NEEDS_CLARIFICATION'), (2, False, 'NEEDS_CLARIFICATION'), (3, False, 'NEEDS_CLARIFICATION')]

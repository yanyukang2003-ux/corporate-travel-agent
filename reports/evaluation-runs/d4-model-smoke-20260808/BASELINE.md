# D4 real-model smoke baseline (accepted)

**Date:** 2026-08-08  
**Decision:** Accept as first real-model baseline (step 2 in plan: accept 20/24, then stability, then fix 4).  
**Run:** `reports/evaluation-runs/d4-model-smoke-20260808/`

## Configuration

| Field | Value |
|---|---|
| Dataset | `agent-eval-v1` v1.0.0 (frozen) |
| Subset | `agent-eval-model-smoke-v1` (24 cases) |
| Mode | `model_mock` (real LLM intent + Mock provider) |
| Model | `gpt-5.6-terra` |
| Attempts | 1 |
| Price table | `model-prices-openai-20260802-v1` |
| Duration | ~126s |

## Headline metrics

| Metric | Value |
|---|---|
| task_pass_rate | **20/24 = 83.3%** |
| Protocol gate (`>= 85%`) | **fail** (1 case short) |
| assertion_pass_rate | 94.1% |
| errors | 0 |
| real_model_calls | 26 |

### Cost note for this baseline run

This run was completed **before** per-call cost ledger was wired into the D4 model runner.  
Token totals / USD estimates were **not** recorded in the summary for this baseline.

- Price rates (for reference, upper-bound, no cache discount):  
  `gpt-5.6-terra` input **$2 / 1M**, output **$12 / 1M**  
- Subsequent runs write:
  - `cost-ledger.jsonl` (one row per LLM call)
  - `cost-summary.json`

## Category breakdown

| Category | Pass | Fail |
|---|---:|---:|
| core | 6 | 1 |
| historical_failure | 4 | 1 |
| boundary | 4 | 2 |
| adversarial | 6 | 0 |

## Known gaps (4 failures)

| case_id | actual | expected | Gap hypothesis |
|---|---|---|---|
| `core-unknown-level-policy-012` | `NEEDS_CLARIFICATION` | `NO_FEASIBLE_OPTION` | Incomplete intent; never searched / hit insufficient-evidence path |
| `historical_failure-empty-return-inventory-027` | `NEEDS_CLARIFICATION` | `NO_FEASIBLE_OPTION` | Incomplete return fields; empty inventory never exercised |
| `boundary-timezone-cross-date-044` | `NO_FEASIBLE` + hotel search | `WAITING_FOR_USER` (no hotel) | Timezone/date window mismatch; hotel constraint over-extracted |
| `boundary-departure-window-exact-033` | `NO_FEASIBLE` | `READY_FOR_HANDOFF` | Departure-after boundary misaligned with fixture inventory |

## Follow-ups (ordered)

1. ~~Accept baseline~~ (this document)
2. 24×3 stability with cost ledger
3. Fix the 4 gaps (prompt/driver), re-smoke 24×1 with cost

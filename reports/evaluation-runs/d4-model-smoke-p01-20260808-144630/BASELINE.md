# D4 real-model smoke after P0/P1 intent calibration

**Date:** 2026-08-08  
**Run:** `reports/evaluation-runs/d4-model-smoke-p01-20260808-144630/`  
**Model:** gpt-5.6-terra  
**Code:** P0 safe defaults + P1 repair anti-fabrication (`intent_calibration.py`, prompt `trip-intent-v3`)

## Result

| Metric | Baseline smoke | Postfix2 | **P0/P1 smoke** |
|---|---|---|---|
| task_pass_rate | 20/24 (83.3%) | 21/24 (87.5%) | **22/24 (91.7%)** |
| Protocol gate ≥85% | fail | pass | **pass** |
| assertion_pass_rate | — | 95.9% | **97.6%** |
| real_model_calls | 26 | 26 | **26** |
| estimated_cost_usd | (not recorded) | $0.163926 | **$0.165790** |
| input/output tokens | — | 33261 / 8117 | **34925 / 7995** |

## What improved vs postfix2

- **Fixed this run:** `boundary-arrival-buffer-exact-031` → `READY_FOR_HANDOFF` (was `NEEDS_CLARIFICATION` in postfix2)
- **Still solid:** `core-unknown-level-policy-012` → `NO_FEASIBLE_OPTION` + `INSUFFICIENT_EVIDENCE` (was mixed in 24×3 stability)

## Remaining fails (2)

Both stop after a single intent extract with incomplete slots → `NEEDS_CLARIFICATION` (no search).

1. **`historical_failure-empty-return-inventory-027`**  
   - Expected: `NO_FEASIBLE_OPTION` after outbound+inbound search  
   - Actual: `NEEDS_CLARIFICATION`; tools=`[llm.extract_trip_intent]`  
   - Short user text (56 chars). Model/repair still did not ground return window enough for SearchReady.

2. **`boundary-timezone-cross-date-044`**  
   - Expected: `WAITING_FOR_USER` after transport search  
   - Actual: `NEEDS_CLARIFICATION`; tools=`[llm.extract_trip_intent]`  
   - Cross-timezone / cross-date phrasing; extract incomplete after one call.

## Cost ledger

- `cost-ledger.jsonl` / `cost-summary.json` present  
- 26 LLM calls; ~$0.0065 per passed case (price-table upper bound, no cache discount)

## Gate decision

**PASS** for real-model smoke protocol (≥85%). New baseline for P0/P1 code path.

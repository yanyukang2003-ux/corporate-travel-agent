# D4 real-model smoke after Claude Code param loop

**Date:** 2026-08-08  
**Run:** `reports/evaluation-runs/d4-model-smoke-paramloop-20260808-150843/`  
**Model:** gpt-5.6-terra  
**Code:** P0/P1 + `param_extraction_loop.py` (Claude Code L1/L2 + tool_use_error repair)

## Result

| Metric | P0/P1 smoke | **Param-loop smoke** |
|---|---|---|
| task_pass_rate | 22/24 (91.7%) | **21/24 (87.5%)** |
| Protocol gate ≥85% | pass | **pass** |
| assertion_pass_rate | 97.6% | **95.9%** |
| real_model_calls | 26 | **26** |
| estimated_cost_usd | $0.165790 | **$0.172738** |
| input/output tokens | 34925 / 7995 | **34925 / 8574** |

## Deltas vs P0/P1 smoke

| Case | P0/P1 | Param-loop |
|---|---|---|
| `boundary-arrival-buffer-exact-031` | pass | **fail** (regressed → `NEEDS_CLARIFICATION`) |
| `historical_failure-empty-return-inventory-027` | fail | fail |
| `boundary-timezone-cross-date-044` | fail | fail |

Note: single-shot real-model variance is expected; 031 was also flaky historically (postfix2 fail → p01 pass → paramloop fail). No runner errors; all fails are intent incomplete (no search).

## Remaining fails (3)

All stop after one `llm.extract_trip_intent` without inventory search:

1. **027** empty return inventory — expected `NO_FEASIBLE_OPTION`
2. **044** timezone cross-date route change — expected `WAITING_FOR_USER`
3. **031** arrival buffer exact — expected `READY_FOR_HANDOFF`

## Gate decision

**PASS** (≥85%). Param loop does not break the smoke protocol; hard fails remain extract-completeness, not loop crashes.

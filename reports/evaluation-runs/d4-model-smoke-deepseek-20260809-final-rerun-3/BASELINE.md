# D4 real-model smoke — DeepSeek post-fix canonical baseline

**Date:** 2026-08-09  
**Dataset:** `agent-eval-v1` v1.0.2  
**Subset:** `agent-eval-model-smoke-v1`  
**Model:** `deepseek-v4-pro`  
**Run:** `d4-model-smoke-deepseek-20260809-final-rerun-3`

## Result

| Metric | Value |
|---|---:|
| Task pass rate | **24/24 (100%)** |
| Failed / error cases | **0 / 0** |
| Hard-assertion pass rate | **100%** |
| Real model calls | 36 |
| Input / output tokens | 68,142 / 12,258 |
| Estimated cost | **$0.04030623** |

All five failures from `d4-model-smoke-deepseek-20260809-113525` now pass:

- `historical_failure-approval-invalidated-on-revision-024`
- `historical_failure-empty-return-inventory-027`
- `boundary-timezone-cross-date-044`
- `boundary-approval-expiry-exact-039`
- `boundary-handoff-expiry-exact-041`

The final `failed-assertions.jsonl` is empty. Costs are estimates from the configured price table; cached-input discounts are not modelled.

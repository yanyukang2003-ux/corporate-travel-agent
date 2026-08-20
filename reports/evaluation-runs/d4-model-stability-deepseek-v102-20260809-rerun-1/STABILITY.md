# D4 v1.0.2 real-model stability 24x3

**Date:** 2026-08-09  
**Run:** `reports/evaluation-runs/d4-model-stability-deepseek-v102-20260809-rerun-1`  
**Model:** `deepseek-v4-pro`  
**Code revision:** `workspace-v1.0.2-20260809-final`  
**Provider mode:** deterministic Mock; only intent extraction uses the real model

## Quality and stability gates

| Metric | Value | Protocol gate | Status |
|---|---:|---:|---|
| Completed runs | 72/72 | complete | pass |
| pass_power_3 | **24/24 = 100%** | >= 80% | pass |
| pass_at_3 | 24/24 = 100% | secondary | pass |
| mixed_run_rate | **0/24 = 0%** | <= 10% | pass |
| Run-level pass rate | 72/72 = 100% | informational | pass |
| Assertion pass rate | 100% | informational | pass |
| Runner errors | 0 | 0 | pass |

All 24 cases passed all three attempts. `failed-assertions.jsonl` is empty.

## Model calls and cost

| Field | Value |
|---|---:|
| Real-model calls, including repair turns | 108 |
| One-call case-runs | 39 |
| Two-call case-runs | 30 |
| Three-call case-runs | 3 |
| Input tokens | 204426 |
| Output tokens | 36297 |
| Estimated cost | **$0.1205037** |
| Estimated cost per case-run | $0.0016736625 |

Costs use `model-prices-openai-20260802-v1` and do not model cached-input discounts.

## Integrity checks

- Case results: 72 rows across 24 cases, exactly 3 attempts per case.
- Cost ledger: 108 rows, matching the reported real-model call count.
- Every ledger row records `deepseek-v4-pro`.
- Summary SHA-256: `24d171b8f95d3f4ff55f8eefa2e99ca3bc1c858f40594c7094a26e603124c892`.

The sibling directory without the `rerun-1` suffix is an invalid sandbox-network
attempt with zero ledger rows and no model responses. It is excluded from this result.

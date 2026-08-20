# D4 real-model smoke — deepseek-v4-pro

**Date:** 2026-08-09  
**Model:** deepseek-v4-pro  
**API path:** Chat Completions + `json_object` + `thinking: disabled`  
**Run:** `reports/evaluation-runs/d4-model-smoke-deepseek-20260809-113525/`

## Result

| Metric | Value |
|---|---|
| task_pass_rate | **19/24 (79.2%)** |
| Protocol gate ≥85% | **fail** (need ≥21/24) |
| assertion_pass_rate | 94.7% |
| real_model_calls | **39** (sequential multi-turn) |
| estimated_cost_usd | **$0.0422** |
| input/output tokens | 71031 / 13043 |

## Preflight

`d4-model-preflight-deepseek-20260809-113506/`: **2/2 pass**, ~$0.0034, 3 LLM calls.

## Adapter note

- DeepSeek **Responses API** rejects `deepseek-v4-pro` (“use deepseek-v4-flash / available early August 2026”).
- Adapter auto-routes non-flash DeepSeek → Chat Completions JSON path.
- `deepseek-v4-flash` still uses Responses structured outputs.

## Failed cases (5)

| Case | Actual | Notes |
|---|---|---|
| `024` approval invalidated | `NO_FEASIBLE_OPTION` | 2nd turn LLM re-search empty; request_revision skipped |
| `027` empty return | tools include hotels | forbidden `search_hotels` (model added hotel) |
| `044` timezone rebook | `NO_FEASIBLE` + hotels | 1st cycle hotel + 2nd rebook still no feasible |
| `039` approval expiry | `OUT_OF_SCOPE` | 2nd sequential turn misclassified OOS |
| `041` handoff expiry | `OUT_OF_SCOPE` | same OOS on 2nd turn |

Root themes: (1) multi-turn 2nd-turn classification / OD rewrite quality; (2) hotel tool on cases that forbid it; (3) rebook after empty inventory.

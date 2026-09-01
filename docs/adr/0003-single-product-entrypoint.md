# One natural-language entrypoint: delete legacy and semantic, gate the tool loop

## Status

Accepted, 2026-09-01. Supersedes the entrypoint arrangement of [ADR-0002](0002-single-owner-semantic-intent.md).

## Context

The product entrypoint is the tool loop (`POST /agentic/trip-tasks`): the model picks typed
tools from a fixed registry each turn, the host executes, validates, budgets and audits. The
frontend switched to it on 2026-08-30. Two older natural-language entrypoints stayed in the
codebase as rollback safety: `legacy` (slot extraction + calibration + clarification writers) and
`semantic` (one-shot interpretation compiled by `compile_search_command`).

Keeping them had a cost that grew every week:

- five evaluation modules and nine example runners drove the old entrypoints; the frozen sets
  (D1 60 workflow cases, D2 480 intent cases, D4 60 agent-eval cases) ran through them and
  **not** through the product entrypoint — CI gated doors the product no longer used;
- the orchestrator carried four intent paths (4,685 lines, ~120 methods); every product change
  had to be written against several of them;
- the tool loop had no offline stand-in at all, so its only evidence was billed live runs.

## Decision

1. **The tool loop gets an offline stand-in** (`agent/deterministic_tool_model.py`,
   `DeterministicToolCallingModel`). It reuses the semantic interpreter and the search-command
   compiler, so the stand-in and the former semantic stand-in have the same understanding and
   differ only in architecture.
2. **The frozen datasets gate the product entrypoint** (D16, `tests/test_product_entrypoint_evaluation.py`,
   `examples/run_product_entrypoint_evaluation.py`). Gates: no premature provider call, no fabricated
   inventory, no silent wrong search, every D1 hard assertion, and clarification accuracy not below
   the last semantic baseline.
3. **The legacy and semantic entrypoints are deleted**: orchestrator paths, their agent modules,
   their ports and adapters, the API routes, the frontend branches, the legacy real-model harness,
   the adversarial suite built on the legacy fake model, and the examples and tests that only they
   used. `IntentEntrypoint.LEGACY / SEMANTIC` remain as enum values so persisted tasks still load;
   such tasks can be read but not continued.

## Removal gate (ADR-0002) — how it was met

| Gate | Evidence |
|---|---|
| Frozen suite passes through the new entrypoint | D1 60/60 through the tool loop (`reports/evaluation-runs/product-entrypoint-20260901/`) |
| No silent wrong search | 0 on D1; D2 premature provider calls 0/480, fabricated inventory 0/480 |
| Regressions green | 683 tests after deletion (992 before, the difference being tests of deleted code); ruff clean; frontend build/test/lint clean |
| Side-by-side shows no unexplained regression | D2 clarification accuracy 345/480 on both entrypoints with one shared interpreter; the only representational difference (out-of-scope requests) was closed by `ask_traveler(out_of_scope=true)` → `OUT_OF_SCOPE` |

Running the product path offline also exposed and fixed three product gaps before the gate
was declared met: the loop's request carried no hard constraints or preferences; all-empty
searches ended as a clarification round instead of `NO_FEASIBLE_OPTION`; the loop could not
declare a request out of scope.

## Consequences

**What the product keeps.** One natural-language entrypoint plus the structured one. The
downstream (planner, policy engine, selection, approval, revalidation, handoff, booking
confirmation) is shared and unchanged. D4 (`agent-eval-v1`) keeps gating that downstream through
the structured entry; D16 gates the upstream through the tool loop.

**What was retired, and what replaces it.**

| Retired | Why | Coverage now |
|---|---|---|
| Legacy real-model harness (`evaluation_model_runner`, `evaluation_model_live_provider`, `evaluation_model_preflight`; D9–D14 runners) | built on `create_task_from_message`; porting meant re-baselining six frozen real-model sets | live tool-loop runs under `reports/evaluation-runs/toolloop-*` and `agentic-boundary-*`, driven by `examples/run_tool_loop_*` and `run_agentic_boundary_longtail_evaluation.py`. **Lost**: the protocol §5 trace JSONL, price-table cost accounting and the explicit retry-cap regressions (D10/D11) for real-model runs. Rebuilding those for the tool loop is the first follow-up. |
| Adversarial suite (`evaluation_adversarial`, `adversarial-v1` runner) | its compromised model returned legacy extraction payloads | host invariants it verified are unit-tested on the product path: invented references rejected, no write tool reachable, policy verdicts computed by code (`tests/test_tool_loop.py`, `tests/test_agentic_entrypoint.py`); approval identity and handoff-without-approval refusals (`tests/test_workflow.py`, `tests/test_auth.py`). **Lost**: the tainted-inventory injection scenarios as a suite. Second follow-up. |
| Semantic red-team acceptance (17 cases) | scripted `IntentDecision`s for the semantic host | host duties on the tool loop are covered by `tests/test_tool_loop.py` (date evidence, repeat refusal, empty routes, partial delivery, budget) and `tests/test_agentic_entrypoint.py` |
| D4 `model_mock` mode | drove the legacy NL entry with a real model | D4 `deterministic_live` / `oracle_label` unchanged; real-model evidence for the product path lives in the tool-loop runs |
| D15 (semantic side-by-side) | the entrypoint is gone | D16; the last semantic column is frozen in the 2026-09-01 report and used as the baseline |

**Metrics that no longer have an exposure** report `not_applicable`, never 0: scenario
classification, missing-field slots, the out-of-scope label, and preferences on cases that never
reached delivery.

**Persisted tasks** created through the old entrypoints keep their `intent_entrypoint` value and
remain readable; `submit_agentic_message` refuses them.

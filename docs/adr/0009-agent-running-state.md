# Give the tool loop its own task state

## Status

Accepted, 2026-09-03.

## Context

The product entrypoint is the tool loop (ADR-0003): the model reads the conversation, picks
typed tools, searches inventory and decides whether to ask or to deliver, all in one bounded run.
The state machine had no state for that run. The task stayed in `DRAFT` while the loop ran, and
when the loop delivered, `_plan_from_tool_loop` replayed `SEARCHING → PLANNING` after the fact
so that the edge into `OPTIONS_READY` existed; an empty search replayed `SEARCHING → PLANNING →
NO_FEASIBLE_OPTION` although no planner ran; a provider failure replayed `SEARCHING →
PROVIDER_FAILED`. The audit trail showed three transitions in the same millisecond, and a
reader of the state column could not tell a task whose loop was running from one that had not
started. The 2026-09-01 product-gap review listed this as architecture issue 4.

## Decision

- `TaskState.AGENT_RUNNING` is the loop's state. `DRAFT → AGENT_RUNNING` when the loop starts
  (creation and every follow-up message). The loop leaves it by its terminal action:
  `NEEDS_CLARIFICATION` (the model asked), `OUT_OF_SCOPE`, `NEEDS_STRUCTURED_INPUT` (rounds
  exhausted, loop did not converge, model failed, proposal without a search),
  `TOOL_BUDGET_EXHAUSTED`, `NO_FEASIBLE_OPTION` (searched, every leg empty), `PROVIDER_FAILED`
  (provider error or invalid snapshots), and `PLANNING` only when the deterministic planner
  actually runs on the loop's snapshots.
- `DRAFT` keeps `SEARCHING` (structured requests), `NEEDS_STRUCTURED_INPUT` (restart recovery)
  and `TOOL_BUDGET_EXHAUSTED`. `DRAFT → NEEDS_CLARIFICATION` and `DRAFT → OUT_OF_SCOPE` are gone;
  they belonged to the deleted entrypoints.
- Restart recovery treats `AGENT_RUNNING` as transient. A loop with only a model call in flight
  goes to `NEEDS_STRUCTURED_INPUT` like a draft; a loop with a provider call in flight goes to
  `PROVIDER_FAILED` and must be reconciled first, like any interrupted external call.
- The frontend labels the state and polls while it lasts; the quality evaluator maps it to
  `AGENT_RUNNING` / `WAIT_FOR_AGENT`.

## Consequences

- The transition trace of a delivered loop is `DRAFT → AGENT_RUNNING → PLANNING →
  OPTIONS_READY → WAITING_FOR_USER`; `SEARCHING` no longer appears on the natural-language path.
- Persisted tasks are unaffected: no stored task is in `AGENT_RUNNING` unless a process died
  mid-loop, and recovery handles that.
- Frozen evaluation datasets assert terminal states and tool sequences, not the intermediate
  states, so their gates are unchanged (the full suite passed without modification).

# Make semantic intent a single-owner deep module

## Status

Superseded by [ADR-0003](0003-single-product-entrypoint.md) on 2026-09-01: both the legacy
entrypoint this ADR retained and the semantic entrypoint it introduced were removed once the
frozen datasets gated the product entrypoint (the tool loop). The invariants below survive in
`compile_search_command` and in the tool-loop stand-in.

## Context

The former natural-language path converted each new message into a flat slot patch. The language
model, local parser, evidence checker, calibration rules, defaults, clarification parser and route
reset helpers could all influence or overwrite the same `intent_fields`. This made alternatives,
conditions and corrections lossy and allowed stale route-scoped values to survive later turns.

Historical red-team evidence included silently choosing one of two cities, rolling a past date into
the next year, reducing multi-city travel to one leg, and retaining an old departure time after the
destination changed. Unit tests were mostly green because many started after a correct structured
payload had already been supplied.

## Decision

Introduce one external seam:

```text
ConversationLedger -> ConversationIntentInterpreter -> IntentDecision
```

`ConversationLedger` preserves every raw turn with a stable index. `IntentDecision` contains an
expressive `SemanticIntent`, one of `READY`, `NEEDS_CLARIFICATION`, `UNSUPPORTED`, or
`OUT_OF_SCOPE`, and exact turn/quote evidence. Alternatives, conditions and uncertainties remain
first-class values rather than being collapsed into single provider fields.

Only `compile_search_command()` may turn semantic meaning into a `TripRequestVersion`. It applies no
city/date defaults and refuses to compile unresolved alternatives, conditions or uncertainties. The
existing policy, inventory, approval and handoff modules remain downstream and unchanged.

The old and new implementations remain available through explicit, non-overlapping entrypoints:

- `/legacy/trip-tasks` invokes only the former extraction, calibration and clarification flow.
- `/semantic/trip-tasks` invokes only complete-conversation interpretation and command compilation.
- `/trip-tasks` accepts only a structured request.

Each task persists an immutable `intent_entrypoint`. Follow-up messages use the matching
`/legacy/.../messages` or `/semantic/.../messages` route; cross-entrypoint calls fail. The semantic
model port has no legacy extraction method and the semantic interpreter has no compatibility
fallback. The implementations share workflow code only after a validated `TripRequestVersion`
exists.

## Invariants

- Raw conversation turns are the source of truth; `intent_fields` is a compatibility projection.
- In the semantic entrypoint, no caller patches semantic fields or carries forward prior fields.
- Ambiguity that can change a search causes clarification; clarification exhaustion never triggers a
  search merely because four legacy core slots happen to be populated.
- Model evidence must quote text present in its referenced conversation turn.
- Deterministic validation occurs at command compilation and provider interfaces, not by rewriting
  the traveler's meaning.

## Removal gate

Delete the legacy extraction/calibration/clarification writers after all of the following hold:

1. The historical 63-case long-tail suite passes semantic assertions through the semantic endpoint.
2. No case performs a silent wrong search; ambiguous cases clarify or reject.
3. Search-command validation, policy, approval, persistence and provider regressions remain green.
4. Side-by-side evaluation shows no unexplained high-risk regression for a full observation window.

Until that gate is met, retaining the old implementation is intentional rollback safety. It is not
reachable from a semantic task and therefore is not a second semantic owner.

# Make the leg list the single source of truth for a trip request

## Status

Accepted, 2026-09-03.

## Context

`TripRequestVersion` carried two descriptions of the same trip. The flat fields
(`origin`, `destination`, `departure_after`, `arrive_by`, `return_after`, `return_before`,
`hotel_check_in`, `hotel_check_out`, `hard_constraints`, `soft_preferences`) were constructor
fields; the structured ones (`journey`, `stays`, `scoped_hard_constraints`,
`scoped_soft_preferences`) were optional and took precedence when present. Every comment
justifying the duality said "frozen evaluation data". The validator had grown checks that the
two views "say the same thing", and the change-request code updated one view or the other
depending on which one a request happened to have. The 2026-09-01 product-gap review named this
architecture issue 5: the evaluation sets were constraining the domain model.

## Decision

1. `journey` (at least one leg), `stays`, `scoped_hard_constraints` and
   `scoped_soft_preferences` are the only stored fields. The flat names remain readable as
   **derived, read-only properties**: `origin` / `destination` / `departure_after` / `arrive_by`
   come from the first leg, `return_after` / `return_before` from the last leg of a multi-leg
   journey, `hotel_check_in` / `hotel_check_out` from the first stay, `hard_constraints` /
   `soft_preferences` from the scoped lists. `resolved_booking_scope` derives from the leg shape
   when not given explicitly.
2. `TripRequestVersion.from_flat(...)` is the single place where flat input becomes legs, with
   the exact rules the old `transport_legs()` / `lodging_stays()` used. The structured API body,
   the demo request, the evaluation case loaders and the example scripts go through it. The
   frozen datasets did not change: their loaders adapt, so no manifest was re-frozen.
3. Persisted payloads move to schema version 3. `_upgrade_v2_to_v3` rebuilds the structured
   fields from the flat ones and drops the flat keys; `examples/upgrade_task_payloads.py`
   rewrites stored rows.
4. Two behaviours that the duality had been hiding were settled explicitly:
   - The leg-order validation, which previously ran only for explicit multi-city journeys, now
     runs for every request. Its rule is relaxed to what search windows can actually violate:
     a leg is a conflict only when its arrival deadline is earlier than the previous leg's
     earliest departure (entirely reversed windows). Overlapping windows are legitimate — "arrive
     by 10:00 the day after, return no earlier than 17:00 today" — and whether real itineraries
     connect is the feasibility validator's job.
   - A meeting moved later shifts only the moved leg; later legs are shifted by the same amount
     only when the move would reverse them. Stays are never moved.

## Consequences

- `dataclasses.replace(request, origin=...)` no longer exists; callers replace legs or stays.
  Tests that did so were rewritten; the semantic compiler builds the single-stay case from the
  hotel dates itself instead of relying on the derivation.
- Requests are validated on their legs everywhere, including the structured entrypoint, which
  previously escaped the multi-city rules entirely.
- `services/serialization.py` `SCHEMA_VERSION` is 3; `tests/test_payload_upgrade.py` covers
  the 2 → 3 step.

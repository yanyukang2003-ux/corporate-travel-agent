# Split the orchestrator into concern modules; behaviour and public names unchanged

## Status

Accepted, 2026-09-01.

## Context

`agent/orchestrator.py` had grown to 3,369 lines and ~110 methods on one class. Every product
change (booking confirmation, approval tiers, delegation, trips, provenance) landed in the same
file, and reviewers could not tell intake code from approval code without reading all of it.
HANDOFF §8 listed the split as the P0 engineering item, with two constraints: behaviour must not
change, and tests must not be touched.

## Decision

`agent/orchestrator.py` becomes the package `agent/orchestrator/`:

| Module | Owns |
|---|---|
| `core.py` | exceptions, retry constants, shared type alias |
| `intake.py` | task creation (structured and tool-loop entry), turning a loop outcome into task state, request revision |
| `planning.py` | search and feasibility, no-feasible-option explanations, quote revalidation, retry/replan, option selection |
| `approval.py` | tiered approval, approval subject hash, outbox events, handoff |
| `confirmation.py` | booking confirmation, trip aggregate and watch, flight/meeting change events, expense reconciliation |
| `resilience.py` | bounded retries, delayed retry queue, circuit restore, interrupted-task recovery, the tool-call gate |
| `records.py` | audit events, evaluation trace, provenance, policy/budget/profile snapshot lookups |

`TripWorkflowOrchestrator` is assembled in `__init__.py` from six mixins, one per module, and keeps
its constructor. Every method body was moved verbatim by a script that sliced the original file
by method (comments and decorators included) and recomputed imports; `ruff`'s undefined-name
check and the full test suite were the safety net. `from corporate_travel_agent.agent.orchestrator
import ...` resolves the same names as before, including the two private helpers tests use.

**Why mixins and not collaborators.** A collaborator split would have meant changing call sites
in 100+ methods and the tests that reach into private methods (`_invoke_tool`, `_policy_for`,
`_approval_steps`); that is a behaviour-risking refactor, not a file split. Mixins move code
without changing a single call. The state contract is explicit: every attribute lives on the
orchestrator instance and is set in `__init__`; a module may read and write it but not define new
attributes outside `__init__`.

## Consequences

- Each module is 30–860 lines and reads as one concern; `intake.py` is the largest because the
  tool-loop landing code is one unit of reasoning.
- The next refactor, if wanted, is to turn a mixin into a collaborator one at a time —
  `resilience.py` (the tool-call gate and retry queue) is the natural first candidate because it
  has the fewest cross-references.
- Adding a method now has an obvious home; a method that needs two modules' state is a signal
  the concern boundary is wrong, not a reason to widen a module.

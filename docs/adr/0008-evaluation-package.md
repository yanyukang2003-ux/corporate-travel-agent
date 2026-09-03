# Move the evaluation harness above the product; keep the metrics the product needs in services

## Status

Accepted, 2026-09-03.

## Context

Eighteen `services/evaluation_*.py` modules (14,136 lines, 35% of `src/`) lived inside the
application-services layer while importing `agent.orchestrator`, `agent.tool_loop`, `agent.ports`
and `demo` — an upward edge the architecture diagram does not have. In the other direction,
`api/main.py` imported `services.evaluation_business` to serve `GET /metrics/business`, so a
product endpoint depended on the evaluation harness, and the harness's `MetricResult` type was the
only representation of "a metric with a status" in the codebase.

## Decision

1. The modules move to the package `corporate_travel_agent.evaluation` (prefix dropped:
   `evaluation.dataset`, `evaluation.judge`, ...). The package sits at the top of the dependency
   graph: it may import anything below `api`, and nothing in the product may import it. The
   layering test enforces both directions.
2. The business-metrics computation (`summarize_task`, `aggregate_metrics`,
   `build_business_metrics_report`, `METRIC_LABELS`, the record and report models) becomes
   `services/business_metrics.py`; `MetricResult` becomes `services/metrics.py`.
   `evaluation/business.py` keeps only the report-writing layer and re-exports the computation
   for the evaluation runners.
3. Wheel packaging is unchanged; the evaluation package ships with the distribution. Excluding
   it from the wheel is a packaging decision for later, not an architecture one.

## Consequences

- `KNOWN_DEBTS` in `tests/test_architecture_layers.py` is empty.
- Import paths changed in 62 files (14 in `src/`, 21 tests, 27 examples); `git mv` preserves
  history. Documentation paths under `docs/` and `README.md` were updated; historical HANDOFF
  sections keep the old names.
- `api/routers/admin.py`, `tests/test_trips.py` and `tests/test_expense_reconciliation.py` read
  the product module. The evaluation tests read the evaluation module.

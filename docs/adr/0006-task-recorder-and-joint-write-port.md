# Extract the write path from the orchestrator; put joint writes on the repository port

## Status

Accepted, 2026-09-03. Builds on ADR-0004 (the mixin split) and ADR-0005 (the typed contract).

## Context

After ADR-0004 the orchestrator was seven mixins that call each other through `self`. The typed
contract of ADR-0005 made the coupling visible: 193 cross-module `self.` references, 136 of them
to `records.py`, whose `_audit` / `_transition` every other module depends on. `records.py` itself
depended on no other mixin: it was the one pure leaf, and it owned the most important behaviour in
the class — deciding whether a task update, its audit events, its outbox events and the trip
aggregate commit in one transaction. That decision was made by probing the repository with
`getattr(self.tasks, "engine")` and `getattr(self.tasks, "record_with_trip")`, because the
`TaskRepository` Protocol did not declare the joint writes the SQL implementation had grown.

## Decision

1. **`TaskRecorder`** (`agent/orchestrator/recorder.py`) is a collaborator, not a mixin. It owns
   `transition`, `transition_pending`, `audit`, `record_trace`, `record_searches`, and the
   provenance builders. The orchestrator constructs it in `__init__` and every mixin calls
   `self.recorder.audit(...)`. `RecordsMixin` keeps only the read side: resolving and pinning
   policy, budget and travel-profile snapshots, and provenance verification.
2. **Joint writes are part of the repository port.** `TaskRepository.add_with_trip` and
   `record_with_trip` take the trip repository as an argument. The SQL implementation commits both
   tables in one transaction when the trip repository is bound to the same engine
   (`db_engine.EngineBound`) and falls back to two writes otherwise; the in-memory implementation
   always does two writes. The recorder no longer knows what an engine is.
3. **`EmployeeDirectory` and `PolicyRepository` are Protocols.** The orchestrator and the API call
   `knows`, `may_book_for`, `delegators_of` directly instead of probing for them.
4. **Optional model capabilities are `runtime_checkable` Protocols** (`ExchangeRecordingModel`),
   like the provider capability in ADR-0005.

## Consequences

- Cross-mixin `self.` references fall from 193 to 74; the contract table in `state.py` from 39
  entries to 30. `records.py` now depends on the recorder only.
- Fourteen `getattr` probes on ports in the orchestrator are gone. Six remain in `api/main.py`
  (`engine`, `dispose`, `operational_status`, `close`, `provider_mode`); they belong to the wiring
  and go with the application factory.
- The retry worker always claims through `claim_due_provider_retries`; the code path for a
  repository that could not claim was unreachable because both implementations can.
- A test that reaches into the write path uses `workflow.recorder.audit(...)`.

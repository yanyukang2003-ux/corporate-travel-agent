## Read-only Workflow Architect audit

No files were modified and no tests were executed. The audit inspected runtime code, API routes, provider adapters, state machine, repositories, retry scheduler, tests, migrations, configs, and documentation.

Overall verdict: **Review required**. No Critical finding was identified, but the three `HANDOFF.md` next tasks are genuine gaps, and provider delayed recovery is not yet production-acceptance-ready.

### Strengths

- State changes are centralized and auditable through `StateMachine.transition`; approval cannot jump directly to handoff. See [state_machine.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/workflow/state_machine.py:8) and [test_workflow.py](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_workflow.py:144).
- Provider recovery has strong deterministic coverage: immediate recovery, delayed recovery, exhaustion, open-circuit suppression, revalidation resume, tool-budget exhaustion, and persistence restart. See [test_provider_retry_and_disclosure.py](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_provider_retry_and_disclosure.py:97) and [test_persistence.py](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_persistence.py:183).
- SQL task updates use optimistic revisions and persist task plus audit event transactionally. See [sqlalchemy_repository.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/sqlalchemy_repository.py:192).
- Failure taxonomy, retry linkage, side-effect classification, and trace fields are unusually explicit. See [error_recovery.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/error_recovery.py:112) and [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1525).

## Critical

None found.

## High

### H1 — LLM `429`/`5xx` failures are classified as permanent, and recovery behavior contradicts its own taxonomy

**Evidence**

- Every HTTP status is mapped to `retryable=False`, including `429`, `408`, and `5xx`: [openai_adapter.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/openai_adapter.py:315).
- API construction always inherits the default single LLM attempt; there is no runtime environment setting for it: [demo.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/demo.py:42), [.env.example](/Users/yukangyan/Downloads/corporate-travel-agent/.env.example:1).
- The taxonomy says an exhausted LLM failure should `CLARIFY`: [error_recovery.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/error_recovery.py:154).
- The orchestrator instead always transitions to `NEEDS_STRUCTURED_INPUT` and sets no clarification question: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:410).
- Existing tests enshrine direct structured fallback rather than the intended Step 2 behavior: [test_intent_scenarios.py](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_intent_scenarios.py:524).

**Failure scenario / impact**

A short model rate-limit window returns HTTP 429. The request is not retried, the task immediately demands a structured form, and a recoverable conversational task becomes a manual fallback. This is exactly the `HANDOFF.md` Step 2 gap.

**Recommended remediation**

Classify `408`, `429`, and selected `5xx` as transient; honor `Retry-After`; expose a bounded configurable LLM retry policy. After exhaustion, enter a user-resumable clarification/retry state with an explicit message. Reserve structured fallback for permanent schema/adapter failures or exhausted user clarification policy.

**Verification**

Inject HTTP 429 with `Retry-After`, followed by success; assert two linked LLM calls and no `NEEDS_STRUCTURED_INPUT`. Add separate tests for persistent 429, 400, timeout, connection failure, invalid JSON, and exhausted clarification budget.

### H2 — `OUT_OF_SCOPE` remains an irreversible, model-controlled terminal and can destroy a valid in-progress trip

**Evidence**

- `decide_param_loop` accepts the model’s `OUT_OF_SCOPE` classification before validating grounded prior trip fields: [param_extraction_loop.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/param_extraction_loop.py:563).
- A post-search message clears options, selection, booking intent, approval, and request before the new extraction succeeds: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:270).
- An OOS result immediately transitions terminal: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:541).
- `submit_message` excludes OOS and the state machine has no exit: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:238), [state_machine.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/workflow/state_machine.py:63).

**Failure scenario / impact**

A traveler has valid options and says “make it earlier.” If the second model call mistakenly labels that sentence OOS, the workflow deletes all current search products and becomes permanently unrecoverable within the task. The user must create a new task and loses continuity and prior audit context.

**Recommended remediation**

Make revisions copy-on-write: retain the accepted request/options until a replacement intent passes validation. Reject OOS when grounded travel slots or an active trip context exist unless the user explicitly switches topic. Add `OUT_OF_SCOPE -> DRAFT` with a documented “new intent” trigger.

**Verification**

From `WAITING_FOR_USER`, return OOS for an ambiguous revision; assert the old accepted request/options remain available and the task is recoverable. Then submit a valid new trip and assert transition through `DRAFT` to search.

### H3 — Multi-leg reconciliation is diagnostic only; recovery repeats successful legs and loses partial evidence

**Evidence**

- The taxonomy explicitly says partial recovery should retain audit evidence and “retry failed legs only”: [error_recovery.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/error_recovery.py:233).
- The orchestrator records only successful/failed tool names, clears options, and schedules generic `SEARCH`: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:961).
- A delayed `SEARCH` always restarts `_search_and_plan`, beginning again with outbound search: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1224), [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:915).
- Normalized snapshots are attached to the task only after every leg succeeds: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1002).
- The test verifies taxonomy output only, not operational subset resume: [test_error_recovery.py](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_error_recovery.py:175).

**Failure scenario / impact**

Outbound and inbound searches succeed, but hotel search exhausts three immediate attempts. The delayed attempt repeats both flights before retrying the hotel. Those duplicates consume the shared 12-call budget, so the task can reach `TOOL_BUDGET_EXHAUSTED` before completing the configured delayed retries. Successful partial snapshots/raw evidence are not task-linked.

**Recommended remediation**

Persist each successful leg immediately as attempt evidence, but do not construct options until the required set is complete. Store a resume plan containing missing/expired legs and retry only those. If full replay is intentional, change the taxonomy/spec and explicitly budget for it.

**Verification**

Inject failure on the hotel leg after successful outbound/inbound. Assert delayed recovery calls only hotel, preserves flight snapshot IDs, exposes partial evidence through audit/snapshot APIs, and stays within budget.

### H4 — Delayed-retry processing has no per-task failure boundary; one poison task can repeatedly block the queue while health remains “ok”

**Evidence**

- Due tasks are processed in one loop with no per-task exception handling: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1203).
- For search retries, historical policy resolution happens before the task leaves `WAITING_FOR_PROVIDER`: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1233).
- A missing or changed historical policy raises permanently: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1814).
- The scheduler catches only the entire iteration and logs it: [provider_resilience.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/provider_resilience.py:161).
- `/health` always returns `status=ok` and exposes breaker configuration, but not scheduler liveness, last successful poll, last error, backlog age, or missed retries: [main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:236).

**Failure scenario / impact**

The earliest due task references a historical policy omitted during a configuration deployment. It remains due in `WAITING_FOR_PROVIDER`, throws before transition on every poll, and prevents later due tasks from being processed. Health remains green.

**Recommended remediation**

Wrap each task independently. On recovery-precondition failure, persist a terminal `RECOVERY_FAILED`/`PROVIDER_FAILED` reason and continue with later tasks. Add scheduler telemetry: thread alive, last poll, last successful poll, last error, due backlog count, oldest overdue seconds, and alert threshold.

**Verification**

Create two due tasks; make the first policy snapshot unavailable and the second recoverable. Assert the first reaches explicit failure, the second still recovers in the same poll, and health becomes degraded until the error is acknowledged.

## Medium

### M1 — Three synchronous “immediate” Duffel attempts can block the initiating API request for roughly 195 seconds

**Evidence**

- The orchestrator allows three immediate attempts and sleeps between them: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:87), [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1541).
- Duffel’s HTTP timeout is 65 seconds: [duffel.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel.py:127).
- API routes call the workflow synchronously: [main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:294).

**Failure scenario / impact**

A black-holed Duffel connection can hold the request thread for approximately `3 × 65s + backoff`. The client or load balancer may time out before receiving `WAITING_FOR_PROVIDER`, despite the delayed-recovery design.

**Recommended remediation**

Define a total synchronous attempt deadline/SLA, not only per-call timeouts. Either shorten immediate attempts to fit that budget or make provider search asynchronous and return `202` with the persisted task immediately.

**Verification**

Use a controlled timeout proxy and assert the create-trip endpoint returns `WAITING_FOR_PROVIDER`/`202` within the declared SLA while later recovery continues in the background.

### M2 — Circuit-breaker scope is too broad within one process and too narrow across processes

**Evidence**

- One breaker is created for the whole orchestrator: [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:169).
- The configured composite contains independent Duffel and LiteAPI upstreams: [factory.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/factory.py:26), [composite.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/composite.py:23).
- Breaker state and locks are process-local: [provider_resilience.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/provider_resilience.py:45).
- Composite tests cover delegation but not failure isolation: [test_composite_provider.py](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_composite_provider.py:67).

**Failure scenario / impact**

A LiteAPI outage opens the shared breaker and suppresses healthy Duffel work in the same process. In a multi-worker deployment, another worker has a closed breaker and continues hitting the failed upstream, defeating the one-probe contract.

**Recommended remediation**

Key breaker state by provider/upstream and operation class, and store it in shared Redis/database state for multi-instance deployments.

**Verification**

With two orchestrators, fail LiteAPI and assert Duffel remains callable; after the open period, assert only one distributed half-open LiteAPI probe is admitted.

### M3 — The current real-provider runner cannot perform the delayed-recovery acceptance named in `HANDOFF.md`

**Evidence**

- `HANDOFF.md` correctly marks real delayed recovery as unverified: [HANDOFF.md](/Users/yukangyan/Downloads/corporate-travel-agent/HANDOFF.md:227).
- The full-chain runner builds an in-memory workflow, runs once, and closes providers immediately: [run_duffel_liteapi_full_chain_smoke.py](/Users/yukangyan/Downloads/corporate-travel-agent/examples/run_duffel_liteapi_full_chain_smoke.py:101).
- It neither starts `ProviderRetryScheduler` nor waits/calls `process_due_provider_retries`; it evaluates the immediate terminal state: [run_duffel_liteapi_full_chain_smoke.py](/Users/yukangyan/Downloads/corporate-travel-agent/examples/run_duffel_liteapi_full_chain_smoke.py:112).
- The frozen dataset budgets immediate retries only: [duffel-liteapi-real-full-chain-v2.json](/Users/yukangyan/Downloads/corporate-travel-agent/evals/subsets/duffel-liteapi-real-full-chain-v2.json:9).

**Failure scenario / impact**

A real fault reaches `WAITING_FOR_PROVIDER`; the runner closes the clients and reports failure without exercising 60-second scheduling, SQL restart recovery, or final exhaustion.

**Recommended remediation**

Create a dedicated, explicit-authority acceptance harness using API lifespan plus SQL storage and a controlled fault proxy. Freeze expected transitions, timings, external-call ceilings, audit events, and cleanup.

**Verification**

Run two authorized scenarios: fail-until-first-delayed-attempt-then-recover, and fail-through-exhaustion. Verify state/timestamps, restart survival, audit order, request counts, no booking intent, and archived evidence.

### M4 — Workflow documentation is not build-ready and has already drifted from the state machine

**Evidence**

- The only aggregate state diagram omits actual revision edges such as `WAITING_FOR_PROVIDER/PROVIDER_FAILED/NO_FEASIBLE_OPTION/WAITING_FOR_USER -> DRAFT`: [architecture.md](/Users/yukangyan/Downloads/corporate-travel-agent/docs/architecture.md:74) versus [state_machine.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/workflow/state_machine.py:28).
- The provider “contract” lists method signatures but no payload schema, timeout, error response, retryability, or recovery contract: [architecture.md](/Users/yukangyan/Downloads/corporate-travel-agent/docs/architecture.md:135), [base.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/base.py:84).
- No `docs/workflows/REGISTRY.md` or one-workflow-per-document specifications exist.

**Failure scenario / impact**

QA cannot derive tests for revision races, scheduler failures, partial-leg resume, or customer/operator/log observability. Changes can satisfy the diagram while violating runtime recovery behavior.

**Recommended remediation**

Create the four-view workflow registry and separate specs for intent extraction, trip planning, approval, revalidation/handoff, provider delayed recovery, revision/OOS reopening, and restart recovery. Each should define observable states, handoff contracts, timeouts, failure branches, concurrency, and cleanup inventory.

**Verification**

Add a spec-to-code state-edge check and a branch-to-test matrix; fail CI when an implemented state edge or provider error code has no registry/spec/test entry.

## Low

### L1 — Planning and traceability documents are stale relative to `HANDOFF.md` and code

**Evidence**

- Requirements still say real-credential/model quality smoke is pending: [requirements-traceability.md](/Users/yukangyan/Downloads/corporate-travel-agent/docs/requirements-traceability.md:18), [requirements-traceability.md](/Users/yukangyan/Downloads/corporate-travel-agent/docs/requirements-traceability.md:27).
- Roadmap still lists the completed 24-case model smoke as next-stage work: [roadmap.md](/Users/yukangyan/Downloads/corporate-travel-agent/docs/roadmap.md:24).
- `HANDOFF.md` lists single-case CLI debugging as unfinished, but `--case-id` already exists: [HANDOFF.md](/Users/yukangyan/Downloads/corporate-travel-agent/HANDOFF.md:235), [run_agent_eval_v1.py](/Users/yukangyan/Downloads/corporate-travel-agent/examples/run_agent_eval_v1.py:42).

**Impact**

New sessions may repeat completed work or use stale acceptance status.

**Recommended remediation**

Reconcile roadmap and requirements traceability during the same change that updates `HANDOFF.md`, with last-reviewed dates and evidence links.

**Verification**

Add a documentation review checklist that compares current frozen manifests/results, CLI capabilities, and registry statuses.

## Explicit assessment of the three current next tasks

1. **Provider delayed recovery:** valid P0, but not yet acceptance-ready. Deterministic behavior is strong; real evidence is missing, the current runner cannot exercise the workflow, synchronous latency is unbounded at the workflow level, and scheduler failure/liveness branches need closure.

2. **LLM-failure clarify behavior:** materially unimplemented. The taxonomy says `CLARIFY`, but runtime always selects structured fallback; `429`/`5xx` classification is also wrong.

3. **OOS reopening:** materially unimplemented and currently destructive for post-search revisions. This should remain P1 only if users can safely create a replacement task; for in-task continuity, it is a High workflow gap.
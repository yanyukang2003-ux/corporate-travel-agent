Backend production audit: **48/100 — blocked for a network-reachable internal pilot** until fail-open startup defaults and cross-worker provider-state loss are addressed.

Strengths:

- Clean domain/provider/repository boundaries; domain code is framework-independent ([architecture.md](/Users/yukangyan/Downloads/corporate-travel-agent/docs/architecture.md:18), lines 18–36).
- SQL task update and audit event commit atomically with optimistic revision checking ([sqlalchemy_repository.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/sqlalchemy_repository.py:192), lines 192–227).
- Inventory snapshots are immutable, retries bounded/audited, unsafe external writes excluded from automatic retry.
- Startup checks DB connectivity/schema, request validation is strict, and raw-object writes are write-once with hash verification.

## Critical

1. **Runtime configuration fails open to unauthenticated admin and volatile persistence.** Missing `DATABASE_URL` and `RAW_RESPONSE_STORE_DIR` select memory backends ([main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:79), lines 79–104); auth defaults disabled ([auth.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/auth.py:97), lines 97–100) and disabled auth grants every caller an admin identity ([auth.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/auth.py:146), lines 146–152). The supplied example explicitly sets `AUTH_ENABLED=false` ([.env.example](/Users/yukangyan/Downloads/corporate-travel-agent/.env.example:9)). `/health` still unconditionally reports `status=ok` ([main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:236), lines 236–263).

   **Scenario/impact:** A pilot deployment misses secret injection or copies the example config; it starts successfully, exposes every task as admin, and loses tasks/raw evidence on restart.

   **Remediation:** Add an explicit `APP_ENV`; for `internal`/`production`, require SQL persistence, durable raw storage, enabled auth, a signing secret, and an explicit provider. Split liveness from readiness and fail readiness when dependencies/config are unsafe.

   **Verification:** Parameterized startup tests must reject each missing/unsafe production setting; kill/restart smoke must preserve data; unauthenticated task requests must return 401.

## High

1. **Live provider quote state is process-local, breaking revalidation after restart or cross-worker routing.** Duffel and LiteAPI initialize empty `_offer_cache` dictionaries ([duffel.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel.py:137), lines 137–145; [liteapi.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/liteapi.py:153), lines 153–160) and refuse to revalidate when a reference is absent from that cache ([duffel.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel.py:236), lines 236–245; [liteapi.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/liteapi.py:247), lines 247–256). Only task/snapshot data is persisted.

   **Scenario/impact:** Worker A searches; the selection request reaches worker B, or the API restarts while awaiting approval. Revalidation returns `UNAVAILABLE` without contacting the supplier, preventing handoff. I reproduced this with two Duffel instances: the second returned `UNAVAILABLE` with zero HTTP calls.

   **Remediation:** Persist encrypted provider revalidation context keyed by task/snapshot/reference; reconstruct revalidation solely from persisted state. Do not require sticky routing.

   **Verification:** Create/search using one workflow instance and select/approve using a fresh instance sharing PostgreSQL; Duffel and LiteAPI paths must reach the expected revalidation state.

2. **Task creation has no transport idempotency and is not one atomic initial transaction.** Each POST generates a new UUID ([main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:294), lines 294–308). The task insert and initial audit event are separate commits ([orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:184), lines 184–200).

   **Scenario/impact:** The server persists/searches a trip but the response times out; the client retries and creates another task with another set of provider calls. Two identical POSTs currently return 200 with different task IDs. A crash between insert and audit leaves an unusable, unaudited DRAFT.

   **Remediation:** Require `Idempotency-Key`, store `(actor, key, request_hash, task_id, result)` under a unique constraint, and atomically persist task plus `TASK_CREATED`.

   **Verification:** Concurrent identical requests with one key yield one task/provider execution and replay the same result; reuse with a different body returns a stable conflict.

3. **Crash recovery misses persisted in-progress states without a `STARTED` tool record.** Search/revalidation state is persisted before tool invocation ([orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:897), lines 897–927; [orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1078), lines 1078–1093), while recovery only handles tasks containing a `STARTED` call ([orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1465), lines 1465–1480).

   **Scenario/impact:** A process dies after committing `SEARCHING` but before `TOOL_CALL_STARTED`, or after a successful call but before planning. On restart the task remains permanently `SEARCHING`. A seeded `SEARCHING` task with no started calls reproduced `recovered=()`.

   **Remediation:** Persist an operation lease/phase atomically with the state transition and recover stale in-progress states by lease expiry. Prefer a durable job/outbox model.

   **Verification:** Add crash injection at every persistence boundary around search/revalidation; restart must deterministically resume or fail to a user-actionable state.

4. **Provider outages can exhaust the FastAPI threadpool before the circuit opens.** Duffel uses a 65-second client timeout ([duffel.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel.py:137)); each request can synchronously attempt a provider three times ([orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1543), lines 1543–1671). While the circuit is closed, all concurrent callers are admitted ([provider_resilience.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/provider_resilience.py:64), lines 64–76).

   **Scenario/impact:** Twenty trips arrive during an outage. All begin three long attempts before the first caller opens the circuit, tying up request threads and amplifying supplier traffic; even health/auth requests may queue.

   **Remediation:** Add a per-provider concurrency bulkhead, explicit connect/read/write/pool timeouts, an overall workflow deadline, and earlier threshold-based circuit opening. Return 202 and continue durable work outside the request for long operations.

   **Verification:** Concurrent outage test must cap in-flight calls, keep liveness responsive, and enforce a bounded API latency.

5. **Delayed retries and task listing perform full-table loads.** SQL `list_tasks()` loads and deserializes every task ([sqlalchemy_repository.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/sqlalchemy_repository.py:185), lines 185–190); the scheduler repeats this every poll and sorts in Python ([orchestrator.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1203), lines 1203–1235). The public list endpoint is also unpaginated ([main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:311), lines 311–317).

   **Scenario/impact:** At 100k historical tasks, every worker deserializes 100k JSON aggregates every five seconds, saturating DB/CPU; list responses become unbounded.

   **Remediation:** Promote retry status/time into indexed columns, implement atomic `claim_due_retries()` with `FOR UPDATE SKIP LOCKED`, and cursor-paginate actor-scoped task queries.

   **Verification:** PostgreSQL `EXPLAIN` must use `(state, next_retry_at)`; a multi-worker 100k-row test should claim each due task once without full scans.

## Medium

1. **Failed provider HTTP responses are not archived.** Responses are parsed/classified before archival ([duffel.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel.py:568), lines 568–603; [liteapi.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/liteapi.py:653), lines 653–689). Archival stores canonicalized parsed JSON only on later success paths ([liteapi.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/liteapi.py:701), lines 701–722). A reproduced LiteAPI 429 made one external call but left `last_raw_response=None`.

   **Scenario/impact:** During a rate-limit, 500, or malformed response, audit/replay cannot reconstruct what the supplier returned.

   **Remediation:** Archive bounded raw response bytes plus status, request ID, content type, and allowlisted headers immediately after receipt and before parsing; link the reference to the tool-call failure.

   **Verification:** Tests for 429/500/invalid JSON must recover exact bytes by audit reference without storing credentials.

2. **OpenAPI describes core responses as arbitrary dictionaries.** Routes return `dict[str, Any]`/manual serialization ([main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:294), [main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:524), lines 524–562), and errors are generic `detail` strings ([main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:513), lines 513–521). Generated OpenAPI currently uses `additionalProperties: true`; tests only assert route presence ([test_api.py](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_api.py:106), lines 106–115).

   **Scenario/impact:** Generated clients cannot model task states or retry metadata, and undocumented 401/403/409/503 changes can break the pilot UI silently.

   **Remediation:** Add versioned Pydantic request/response and Problem Details models, stable error codes, documented responses, `201` creation semantics, and explicit `/v1` governance.

   **Verification:** Snapshot/contract-test OpenAPI schemas and run typed client compatibility tests against old and new API versions.

3. **Deployment and migration rollback are manual and under-tested on PostgreSQL.** Compose defines only PostgreSQL ([compose.yaml](/Users/yukangyan/Downloads/corporate-travel-agent/compose.yaml:1), lines 1–19); deployment instructions manually run Alembic then Uvicorn ([README.md](/Users/yukangyan/Downloads/corporate-travel-agent/README.md:379), lines 379–398). Startup checks table/column shape rather than Alembic head ([sqlalchemy_repository.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/sqlalchemy_repository.py:132), lines 132–156), and downgrade drops raw-response reference columns ([0002_raw_response_objects.py](/Users/yukangyan/Downloads/corporate-travel-agent/migrations/versions/0002_raw_response_objects.py:43), lines 43–52).

   **Scenario/impact:** Deploying the API before migration causes startup failure; a hurried downgrade removes evidence references. A handcrafted partial schema might pass current checks despite migration-history drift.

   **Remediation:** Add a migration-first deployment job, Alembic-head readiness check, expand/contract compatibility policy, backup/PITR runbook, and app rollback that normally retains additive DB changes.

   **Verification:** CI against PostgreSQL 16 must exercise clean upgrade, old-app/new-schema, new-app/schema, app rollback, and restored-backup reconciliation. SQLite upgrade→downgrade→upgrade passed locally, but is insufficient PostgreSQL evidence.

4. **Local raw-response “retention” is metadata only.** The object-store contract only supports put/get ([object_storage.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/object_storage.py:40), lines 40–59); `retention_until` is recorded but no expiry/lifecycle worker exists ([object_storage.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/object_storage.py:185), lines 185–212).

   **Scenario/impact:** Raw supplier payloads remain indefinitely and eventually fill local disk, causing new archival/search operations to fail.

   **Remediation:** Add audited post-retention deletion/lifecycle enforcement, storage quotas and alerts; use Object-Lock-capable object storage as already planned in [roadmap.md](/Users/yukangyan/Downloads/corporate-travel-agent/docs/roadmap.md:24), lines 24–30.

   **Verification:** Time-controlled tests must prevent pre-expiry deletion, remove expired objects, preserve deletion audit records, and alarm before disk exhaustion.

## Low

1. **Application resources are not closed through lifespan.** Lifespan stops only the retry thread ([main.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:51), lines 51–59), although providers expose `close()` and SQL exposes `dispose()` ([composite.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/composite.py:39), lines 39–43; [sqlalchemy_repository.py](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/sqlalchemy_repository.py:158)).

   **Scenario/impact:** Repeated lifespan cycles or embedded deployments retain HTTP/DB resources until process exit.

   **Remediation:** Construct an application-resource container inside lifespan and close provider/model clients plus DB engine on shutdown.

   **Verification:** Repeated lifespan tests should show no live retry threads, HTTP clients, or checked-out DB connections.

Evidence checked: relevant API/domain/provider/repository/migration/config/docs and tests; 45 focused backend tests passed. Alembic SQLite upgrade→downgrade→upgrade passed. No external provider calls were made. Git/recent-change evidence was unavailable because this workspace contains no `.git` directory.
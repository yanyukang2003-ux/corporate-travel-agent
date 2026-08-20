SRE audit: internal pilot 61/100—acceptable only as a supervised, low-volume, single-process trial with PostgreSQL; public production 32/100—blocked.

No files were changed and no external providers were called. Targeted offline verification passed: `27 passed` across API, persistence, retry, and object-storage tests.

Strengths:

- Immediate retries are bounded to three with exponential jitter; delayed retries are persisted and bounded to three (`agent/orchestrator.py:147-180`, `services/provider_resilience.py:26-42`).
- PostgreSQL updates use optimistic revision checks and commit task state plus audit event in one transaction (`services/sqlalchemy_repository.py:192-227`).
- Startup checks DB connectivity and schema, with `pool_pre_ping` enabled (`api/main.py:79-97`, `services/sqlalchemy_repository.py:114-156`).
- Raw responses are hashed, immutable through the storage interface, access-controlled, and written with exclusive-create semantics (`services/object_storage.py:185-254`).
- Restart recovery and delayed-retry persistence have tests (`tests/test_persistence.py:142-213`).

Critical:

- **No evidenced production deployment, progressive rollout, rollback, or disaster-recovery path.** The checkout has no Git metadata, CI workflow, Dockerfile, application deployment manifest, or rollback runbook. `compose.yaml:1-19` runs only a development PostgreSQL with a named volume; `README.md:69-78` recommends Uvicorn `--reload`; `services/evaluation_regression.py:497-504` explicitly records that no code revision is available. Migration downgrade `migrations/versions/0001_task_persistence.py:74-81` drops all persisted tables.
  - Failure/impact: an application or schema regression cannot be safely canaried or rolled back; volume/database loss has no stated RPO/RTO or recovery procedure.
  - Remediation: immutable versioned image, deployment manifests with probes/resources/PDB, canary or blue-green rollout, expand-contract migrations, managed PostgreSQL PITR/backups, object-store recovery, and a tested rollback/restore runbook.
  - Verify: deploy a canary, inject a bad release, roll back without task loss; restore database/object evidence into a clean environment within declared RTO/RPO.

High:

- **`/health` is neither readiness nor dependency health.** It always returns `"status": "ok"` and configuration snapshots without querying DB, object storage, or scheduler health (`api/main.py:236-263`). Its test only asserts static fields (`tests/test_api.py:8-22`).
  - Failure: DB failure after startup or a dead retry worker leaves the pod ready and receiving traffic.
  - Fix: separate `/livez` and `/readyz`; readiness should use a short DB query/schema-version check and scheduler heartbeat, while liveness stays dependency-independent.
  - Verify: break DB connectivity and stop the retry worker; readiness must fail quickly while liveness remains healthy.

- **No runtime SLO or production observability implementation.** The only runtime logger found is the retry-loop exception log (`services/provider_resilience.py:161-166`). The “observability” pipeline is explicitly an offline backfill and says production trends remain unevaluated (`services/evaluation_observability.py:330-362`); local latency is explicitly “not an SLO” (`services/evaluation_regression.py:316-324`).
  - Failure: provider degradation, stuck tasks, retry backlog, DB-pool pressure, and error-budget burn are invisible until users report them.
  - Fix: define availability/end-to-options/retry-freshness SLOs; emit structured request logs, correlation IDs, metrics, and traces for API latency/errors, provider latency/retries/circuit, retry depth/oldest age, transient-state age, DB pool, and object-store failures; add multi-window burn alerts.
  - Verify: inject 5xx, timeout, stuck-task, and DB-pool faults and prove dashboards and alerts identify the cause.

- **External calls lack end-to-end deadlines and bulkheads.** Duffel’s HTTP client can block 65 seconds per attempt (`providers/duffel.py:127-147`), while each tool may retry three times and sleeps synchronously (`agent/orchestrator.py:1543-1670`). API handlers execute the workflow synchronously (`api/main.py:294-308`). The OpenAI client is constructed without application-configured timeout or retry limits (`agent/openai_adapter.py:59-67`, calls at `119-180`). No semaphore or provider/LLM concurrency cap exists.
  - Failure: a provider outage can consume request threads for roughly three long calls per task, causing cascading API saturation.
  - Fix: explicit connect/read/write/pool timeouts, total workflow deadline, one owner for retries, provider/LLM semaphores or isolated executors, bounded admission queues, and fast `503/Retry-After`.
  - Verify: run concurrent black-holed provider/model calls; observed concurrency, thread count, and p99 latency must remain bounded.

- **Delayed retry processing is a sequential full-table scan.** Each poll loads every task (`services/sqlalchemy_repository.py:185-190`), sorts due tasks, then holds a process lock while executing up to 20 tasks serially (`agent/orchestrator.py:1203-1235`). One task may perform three long provider attempts.
  - Failure: one hung retry can delay the entire backlog by minutes; cost grows with total historical task count, not due queue size.
  - Fix: dedicated retry-job/outbox table indexed by `(status, due_at)`, atomic lease/`SKIP LOCKED` claims, bounded worker concurrency, lease expiry, DLQ, and backlog-age metrics.
  - Verify: process 1,000 due jobs with several hung providers across multiple workers; healthy jobs must not starve and each lease must have one owner.

- **Multi-worker behavior is unsafe or incomplete.** Without `DATABASE_URL`, the application silently uses process-local task memory (`api/main.py:79-83`); the circuit breaker is explicitly process-local (`services/provider_resilience.py:45-62`); every app worker starts its own scheduler (`api/main.py:51-59`).
  - Failure: a task created on worker A returns 404 on worker B; each process independently hammers an unhealthy provider. PostgreSQL optimistic locking helps retry claims, but the circuit and backlog control remain unshared.
  - Fix: production startup must reject memory persistence and require PostgreSQL; run retries in a dedicated worker tier; share provider breaker/rate-limit state or enforce it at an egress gateway.
  - Verify: run at least two application processes, repeatedly read/update tasks across them, and prove bounded aggregate upstream calls and single retry ownership.

- **Crash recovery misses valid transient-state crash points.** A successful tool call is persisted before the workflow advances (`agent/orchestrator.py:1673-1708`), but restart recovery only examines transient tasks that still contain a `STARTED` tool call (`agent/orchestrator.py:1465-1480`). Snapshot insert and its audit/state progress are separate commits (`agent/orchestrator.py:1933-1942`).
  - Failure: a kill after `TOOL_CALL_SUCCEEDED` but before snapshot/state persistence can leave a task permanently in `SEARCHING` or `REVALIDATING` with no `STARTED` call, so recovery ignores it.
  - Fix: durable operation leases/checkpoints, recovery of all aged transient states, atomic result/outbox recording where possible, and an orphan raw-object/snapshot reconciler.
  - Verify: kill the process at every persistence boundary around provider success and restart; every task must converge to a safe terminal or explicitly recoverable state.

- **Graceful shutdown does not drain resources or reliably finish the retry worker.** Lifespan stops only the scheduler (`api/main.py:51-59`). Scheduler shutdown waits five seconds and silently returns if still alive (`services/provider_resilience.py:152-159`), while it is a daemon thread (`145-149`). Provider clients have `close()` methods (`providers/duffel.py:189-191`) and the repository has `dispose()` (`services/sqlalchemy_repository.py:158-159`), but the API never calls them.
  - Failure: pod termination can kill an in-flight 65-second retry, leave ambiguous transient state, and abandon HTTP/DB pools.
  - Fix: fail readiness on SIGTERM, stop admission, stop/lease retries, drain within a deadline aligned with external-call timeouts, close provider clients, and dispose DB engines.
  - Verify: send SIGTERM during request and delayed-retry calls; confirm reconciliation, no live worker thread, and closed pools.

- **Public deployment can silently lose all state when environment variables are omitted.** Missing `DATABASE_URL` selects memory (`api/main.py:79-83`); missing `RAW_RESPONSE_STORE_DIR` selects memory (`api/main.py:100-104`); health still reports `ok`.
  - Failure: a configuration omission passes health checks but all task/audit/provider evidence disappears at restart.
  - Fix: explicit deployment profile that fails startup unless durable PostgreSQL, production object storage, and authentication are configured.
  - Verify: launch the production profile with each required variable omitted and assert startup failure.

Medium:

- **Local “WORM” storage is pilot-only and has no lifecycle/capacity control.** The interface only supports put/get (`services/object_storage.py:40-59`); `retention_until` is metadata, not deletion. Architecture explicitly requires Object Lock and encryption for production (`docs/architecture.md:178-199`).
  - Impact: disk grows indefinitely; node loss destroys evidence; service credentials can still modify local files outside the interface.
  - Fix: S3-compatible Object Lock, SSE-KMS, replication/versioning, lifecycle policy, capacity alerts, checksum verification, and orphan reconciliation.
  - Verify: overwrite/delete denial, read-after-write checksum, lifecycle expiration, and regional restore tests.

- **Task listing is unpaginated and loads every tenant’s task aggregate before filtering.** `api/main.py:311-317`; `services/sqlalchemy_repository.py:185-190`.
  - Impact: latency and memory grow linearly with the entire database.
  - Fix: repository-level authorization filtering plus cursor pagination and matching indexes.
  - Verify: load-test with production-scale task counts and enforce response-size/p95 budgets.

- **Database operational bounds are unspecified.** Engine setup uses default pool sizing and has no configured connect/statement/transaction timeout (`services/sqlalchemy_repository.py:114-121`).
  - Impact: stalled SQL or pool exhaustion can consume request and retry threads indefinitely.
  - Fix: environment-specific pool size/overflow/recycle/connect timeout and PostgreSQL statement/lock/idle-transaction timeouts.
  - Verify: exhaust the pool and inject slow/locked queries; requests must fail within the declared deadline.

Low:

- PostgreSQL uses the mutable `postgres:16-alpine` tag and Compose has no resource, restart, or log-rotation policy (`compose.yaml:1-16`). Pin a tested digest/version for reproducible pilot environments.

Evidence missing that could improve the public score: external CI/CD, ingress/gateway limits, managed database backup policy, production object-store configuration, dashboards/alerts, incident ownership/runbooks, capacity tests, and deployment rollback records.
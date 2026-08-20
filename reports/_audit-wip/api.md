Read-only static audit complete. No files changed, tests executed, or external provider calls made.

## Strengths

- Duffel rejects non-test tokens and live-booking flags; LiteAPI restricts calls to `/hotels/rates`.
- Test Order writes are isolated behind explicit environment/CLI confirmations and are never automatically retried.
- Enabled authentication uses scrypt, signed short-lived sessions, resource scoping, and enumeration-safe 404s.
- Provider retries are bounded, audited, circuit-protected, and persisted for restart recovery.
- Raw responses use immutable references, SHA-256 verification, private files, and restricted read contexts.

## Critical

1. **Authentication fails open to full administrator behavior.**  
   [auth.py:97-105,146-152](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/auth.py:97) defaults `AUTH_ENABLED` to false and returns an unauthenticated `development-system` admin. In this mode the approval endpoint also skips identity checks, accepting caller-supplied/default approver identity ([main.py:398-417](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:398)). A deployed service missing one environment variable permits anonymous task reads, mutations, handoff completion, and approvals.  
   **Remediation:** Fail startup unless authentication is configured; permit no-auth only behind an explicit local-development override.  
   **Verify:** Start without auth configuration in non-local mode and assert startup failure; local override should remain an explicit test-only case.

## High

1. **API tests can silently call configured external providers.**  
   The global application selects providers from the process environment at import time ([main.py:134-156](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:134)); [test_api.py:1-5,25-41](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_api.py:1) imports that application and immediately creates trips without forcing Mock/MockTransport or requiring external-call confirmation. Running pytest from a shell with `TRAVEL_PROVIDER=duffel*` can consume sandbox/production-read-only quota.  
   **Remediation:** Introduce an application factory with injected workflow/provider; fixtures must construct an isolated mock application and CI should deny sockets.  
   **Verify:** Run the API suite with external-looking provider variables and a network-denial sentinel; external request count must remain zero.

2. **Non-JSON 429/5xx responses become terminal, non-retryable failures.**  
   Both adapters parse JSON before classifying retryable status ([duffel.py:568-593](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel.py:568), [liteapi.py:653-679](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/liteapi.py:653)). Empty/HTML 429 or 503 responses therefore raise plain `ProviderError`, bypassing circuit and delayed recovery. Even valid 429 responses discard `Retry-After`; orchestration uses fixed local backoff ([orchestrator.py:1711-1714](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1711)). Existing coverage uses JSON 429 only ([test_liteapi_provider.py:233-244](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_liteapi_provider.py:233)).  
   **Remediation:** Classify status before best-effort body parsing, capture bounded safe diagnostics, propagate `Retry-After`, and suppress immediate retries when provider guidance requires waiting.  
   **Verify:** Mock empty/HTML 429, 503, malformed JSON, and delta/date `Retry-After`; all should retain status/request ID and follow the expected schedule.

3. **Synchronous retries can occupy one API worker for roughly three minutes.**  
   Duffel’s default client timeout is 65 seconds ([duffel.py:137](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel.py:137)); LiteAPI similarly adds ten seconds to supplier timeout ([liteapi.py:153](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/liteapi.py:153)). FastAPI invokes the workflow inline ([main.py:294-308](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:294)), while the orchestrator performs up to three attempts and blocking sleeps ([orchestrator.py:1543-1670](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1543)). Concurrent provider degradation can exhaust the worker pool.  
   **Remediation:** Apply a total request deadline, granular connect/read timeouts, and move long retry recovery to the persisted background path, returning `202`.  
   **Verify:** Fault-inject timeouts under concurrent API load and assert bounded request latency and stable worker capacity.

## Medium

1. **Login has no abuse protection despite CPU-expensive scrypt verification.**  
   `/auth/login` directly verifies every attempt ([main.py:266-277](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:266), [auth.py:113-125,225-248](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/services/auth.py:113)). Brute-force traffic can cause CPU denial of service; tests cover only one failed login ([test_auth.py:157-164](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_auth.py:157)).  
   **Remediation:** Add per-account/IP throttling with `429`/`Retry-After`, preserving uniform messages and timing.  
   **Verify:** Burst and distributed-attempt tests should enforce limits without user enumeration.

2. **State-changing HTTP operations lack request idempotency.**  
   Trip creation always generates a new UUID ([main.py:294-308](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:294)); selection, replanning, revision, handoff, and approval expose no idempotency key. Client/network retries can duplicate tasks and provider searches. The existing BookingIntent hash is internal and created late in the workflow ([orchestrator.py:1132-1200](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:1132)).  
   **Remediation:** Persist user/route-scoped `Idempotency-Key` records with payload hashes and stored responses; return `409` for key reuse with different payloads.  
   **Verify:** Concurrent same-key requests create one task and one provider-call sequence.

3. **OpenAPI does not define meaningful success or error contracts.**  
   Routes return `dict[str, Any]`/lists with no response models or declared error responses ([main.py:294-439,513-562](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:294)). POSTs default to 200, while validation, authorization, conflict, unavailable-model, and scheduled-provider states have unrelated shapes/status semantics. The OpenAPI test checks path presence only ([test_api.py:106-115](/Users/yukangyan/Downloads/corporate-travel-agent/tests/test_api.py:106)).  
   **Remediation:** Define versioned response/error models, operation-specific status codes, and a consistent error envelope/code. Use `201` for creation and `202` for persisted asynchronous recovery.  
   **Verify:** Snapshot OpenAPI schemas and contract-test every documented 2xx/4xx/5xx response.

4. **One circuit breaker covers both Duffel and LiteAPI.**  
   The orchestrator creates a single breaker ([orchestrator.py:173-176](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/agent/orchestrator.py:173)), although the composite routes transport and hotel to independent providers ([composite.py:45-56](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/composite.py:45)). A LiteAPI outage can block healthy Duffel-only work for the full circuit interval.  
   **Remediation:** Key breaker state by provider/operation and persist that key in retry metadata.  
   **Verify:** Open the LiteAPI circuit and prove a flight-only Duffel task still executes.

5. **Failure-response archival is incomplete, and LiteAPI 204 evidence is synthetic.**  
   HTTP errors are raised before archival in both providers; LiteAPI turns a real empty 204 body into `{"data":[]}` ([liteapi.py:653-655,701-722](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/liteapi.py:653)). Failure evidence is lost, while the 204 archive hash does not represent the received body.  
   **Remediation:** Archive bounded raw body bytes plus status/request metadata before parsing, including error responses; distinguish raw bytes from normalized payload.  
   **Verify:** Assert byte-exact hashes and archives for empty 204, HTML 429, and JSON 503.

6. **Test Order ambiguity can leave uncancelled sandbox orders.**  
   Mutating requests carry no operation/idempotency identifier ([duffel_test_order.py:121-165,243-260](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/providers/duffel_test_order.py:121)). The runner sets `cancellation_attempted=True` before cancellation and only performs cleanup when it is false ([run_real_model_duffel_test_order.py:179-220](/Users/yukangyan/Downloads/corporate-travel-agent/examples/run_real_model_duffel_test_order.py:179)). A timeout after remote acceptance can leave an order or cancellation in an unknown state without reconciliation.  
   **Remediation:** Persist a mutation ledger before writes; use provider-supported idempotency if available and otherwise reconcile read-only after ambiguous outcomes, escalating uncertain cleanup rather than retrying blindly.  
   **Verify:** Stateful fault tests should simulate response loss after accepted create/cancel and prove no duplicate write plus explicit reconciled/manual-cleanup status.

7. **Full-chain evaluations bypass the HTTP contract.**  
   The real-provider runner constructs the workflow and calls it directly ([run_duffel_liteapi_full_chain_smoke.py:101-126](/Users/yukangyan/Downloads/corporate-travel-agent/examples/run_duffel_liteapi_full_chain_smoke.py:101)); API tests use only the global mock workflow. Authentication, serialization, status mapping, retry scheduling, and provider adapters are therefore never exercised together through ASGI.  
   **Remediation:** Add an ASGI end-to-end suite using injected MockTransport providers and a persistent test repository.  
   **Verify:** Cover authenticated full lifecycle, 429/timeout recovery, restart, concurrent duplicate requests, and archive metadata through HTTP.

## Low

1. **Several request fields are unbounded and two models silently ignore extras.**  
   `TripCreate` strings, `OptionSelection`, and `ApprovalDecision` lack practical length bounds; the latter two also lack `extra="forbid"` ([main.py:164-206](/Users/yukangyan/Downloads/corporate-travel-agent/src/corporate_travel_agent/api/main.py:164)). Oversized reasons/IDs can inflate parsing, persistence, and audit records, while misspelled client fields appear accepted.  
   **Remediation:** Add bounded Pydantic fields, forbid extras consistently, and enforce a request-body limit.  
   **Verify:** Oversized and unknown-field cases must return deterministic `413`/`422` responses.
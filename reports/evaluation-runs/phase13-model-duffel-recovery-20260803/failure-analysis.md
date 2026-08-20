# D10 attempt-2 failure analysis

## Incident record

- Run: `model-duffel-lhr-jfk-recovery-attempt-2`
- Failed tool: `provider.search_transport.outbound`
- Upstream model step: succeeded as `gpt-5.6-sol`; intent matched the frozen contract
- Recorded provider exception: `RetryableProviderError`
- Underlying exception disclosed by the safe provider failure: `httpx.ConnectError`
- Failure layer: Duffel HTTP transport, before an inventory response was available
- State transition: `SEARCHING -> PROVIDER_FAILED`
- Duffel attempts in this logical run: `1`
- Booking intents: `0`

## Cause analysis

The failure is independent of the OpenAI transport incident from D9. All three OpenAI
requests completed with structured output and full usage metadata. The second Duffel request
then failed while establishing or using the outbound HTTP connection. Attempts 1 and 3
succeeded in the same process, so a persistent token, request-parameter, Test Mode, or endpoint
configuration failure is unlikely.

D10 intentionally fixed `max_provider_attempts` at one to isolate the new LLM recovery policy.
The exception was correctly marked retryable, but the evaluation configuration did not permit
the project orchestrator's existing second Provider attempt. The task therefore failed closed.

The historical trace retained only the outer provider exception in structured fields; the
underlying `ConnectError` survived only in the sanitized task failure. It cannot distinguish
DNS, TCP, TLS, proxy, or connection-reset subtypes after the fact.

## Remediation

- Provider errors now carry the same structured, secret-safe diagnostics as model errors:
  stable code, layer, type-only cause chain, response presence, HTTP status, request ID when
  available, and retryability.
- D11 enables one explicit retry for both OpenAI transport failures and Duffel-declared
  transient read-only failures.
- Every retry consumes the shared tool budget and links to the failed step using `retry_of`.
- Authentication, permission, malformed requests, structured-output failures, non-transient
  HTTP failures, and business failures remain non-retryable.

## Evidence integrity

- Source trace SHA-256: `05cbcbc994a482d20d21201813bbc50bec52c0e4be2e693d6848cdfc70e9fc8c`
- Source case-results SHA-256: `1edf8272cff18f090e30492ea8fc2880488f5e23e00e3787e23050f00fbf44ec`
- Source summary SHA-256: `e7c9e80e1ebb2169f2a054e58bb0e6664b508f8307b95ed695b7aa997bb8fa76`
- The original trace and reports remain unchanged.

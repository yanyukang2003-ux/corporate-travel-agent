# D9 attempt-1 failure analysis

## Incident record

- Run: `model-duffel-lhr-jfk-search-attempt-1`
- Time: `2026-08-03T07:52:39.603573Z` to `2026-08-03T07:52:44.817071Z`
- Failed tool: `llm.extract_trip_intent`
- Recorded exception: `LanguageModelError`, caused by `APIConnectionError`
- Failure layer: OpenAI SDK HTTP transport, before an HTTP response was available
- HTTP status / OpenAI request ID / response model / usage: unavailable
- State transition: `DRAFT -> NEEDS_STRUCTURED_INPUT`
- Duffel calls: `0`
- Booking intents: `0`

## Layer boundary

The call crossed the orchestrator and entered `OpenAIResponsesLanguageModel`, then failed
inside the official OpenAI Python SDK while `client.responses.parse(...)` was waiting for
transport completion. The adapter converted the SDK exception into `LanguageModelError`;
the orchestrator caught it and failed closed before invoking Duffel.

## Cause analysis

The strongest supported conclusion is a transient connection-layer failure. The original
trace proves that no API response metadata was available, but the previous adapter retained
only the outer SDK exception class. It did not preserve the safe type-only cause chain, so
the historical artifact cannot distinguish DNS resolution, TCP connection, TLS negotiation,
proxy/firewall interruption, or a connection reset.

Persistent authentication, model availability, request-schema, and environment-policy
failures are unlikely because attempts 2 and 3 succeeded in the same process with the same
client, Key, model, prompt, and dataset within seconds. This does not prove which transient
network mechanism occurred.

## Remediation

- Classify and persist the failure layer, stable error code, underlying exception type chain,
  response-presence flag, HTTP status, request ID when available, and retryability.
- Never persist raw exception messages, request bodies, URLs, headers, or credentials.
- Keep OpenAI SDK automatic retries disabled for evaluation so every external request remains
  visible as a workflow tool step.
- Permit exactly one orchestrator-level retry for `APIConnectionError` and
  `APITimeoutError`, with bounded backoff and jitter.
- Do not retry authentication, permission, bad-request, structured-output, or business errors.
- Link the retry using `retry_of` and `TRANSIENT_LLM_TRANSPORT_FAULT`; count it against the
  tool and external-request budgets.

## Evidence integrity

- Source trace SHA-256: `d17d91466e434a111b226ea848461d63c43d4df535d863f712144c18b043cbc6`
- Source case-results SHA-256: `d15d23b25b15a2f529a73d6d3905c54fa93a892a7a8474e32ebf9d04a1fee40f`
- The original trace and reports remain unchanged.

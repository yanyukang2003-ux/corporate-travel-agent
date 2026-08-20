# Real model + Duffel workflow evaluation

- Result: **FAIL**
- Mode: `model_live_provider`
- Dataset: `model-duffel-workflow-full-recovery-v1` `1.0.0`
- Dataset SHA-256: `237b617c6223b75085e4bee03bb55db89b857e2cc0bc3d19ab8f2ea7117c0898`
- Requested model: `gpt-5.6`
- Actual models: `gpt-5.6-sol`
- Reasoning effort: `medium`
- Provider: `duffel` / `test`

## Three-run stability

- Completed: 3/3; passed: 2
- pass@1: 66.67%
- pass^3: 0.00%
- pass@3: 100.00%
- mixed run rate: 100.00%
- intent consistency: 0.00%
- trajectory consistency: 0.00%

## Calls, latency, tokens, and cost

- Model calls: 5/6
- Duffel calls: 4/6
- Total latency mean/P95: 14893.114 / 20146.481 ms
- LLM latency mean/P95: 8838.825 / 10719.509 ms
- Provider latency mean/P95: 5289.374 / 8260.358 ms
- Input/output/total tokens: 2124 / 433 / 2557
- Cached input/cache-write/reasoning tokens: 0 / 2118 / 139
- Estimated OpenAI cost: `$0.0262575` USD
- Frozen cost ceiling: `$0.15` USD
- Duffel Test Mode search cost: `$0` (no order or payment)

## Hallucination exposure

- Tool hallucination: model exposure `not_exposed`; system unknown calls 0
- Parameter hallucination: model exposure `measured`; failed/evaluable attempts 0/2; not evaluable 1
- Shadow hallucination: model exposure `not_exposed`; evidence-proxy failed/evaluable attempts 0/2; not evaluable 1

## Operational reliability and accounting

- Infrastructure failures: 1/3 (33.33%)
- Completed model response rate: 66.67%
- Failure categories: `{"infrastructure_connection_error": 1}`
- Resource-accounted attempts: 2/3 (66.67%)
- Completed-response accounting: 2/2 (100.00%)
- Outbound-request trace accounting: 9/9 (100.00%)
- Failed before HTTP response: 5
- Estimated cost is a lower bound: `true`
- Failed model requests / retry attempts / recovered retries: 3 / 2 / 1
- Model request cap: 6; maximum attempts per run: 2
- Retry error layers: `{"openai_transport": 3}`
- Model terminal error causes: `{"SSLEOFError": 3}`
- Failed provider requests / retry attempts / recovered retries: 2 / 2 / 2
- Provider request cap: 6; maximum attempts per run: 2
- Provider retry error layers: `{"duffel_transport": 2}`
- Provider terminal error causes: `{"SSLEOFError": 2}`
- Common terminal error causes: `["SSLEOFError"]`

## Tool efficiency

- Expected/actual calls per run: 2 / 3.000
- Raw repeated-name calls: 4
- Authorized retry calls: 4
- Unjustified duplicate calls: 0
- Retry overhead rate: 44.44%
- Calls per successful run: 4.500
- Allowed-order pass rate: 66.67%

## Gates

- FAIL `integration_gate`
- FAIL `three_run_stability_gate`
- FAIL `resource_accounting_gate`
- PASS `booking_safety_gate`
- PASS `cost_ceiling_gate`

## Attempts

- attempt 1: FAIL, state `NEEDS_STRUCTURED_INPUT`, options 0, cost `$None`, failure category `infrastructure_connection_error`
- attempt 2: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.01341375`, failure category `none`
- attempt 3: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.01284375`, failure category `none`

## Limitations

- The model extracts structured intent but does not choose tools; tool selection remains orchestrator-controlled.
- Duffel Test Mode inventory is sandbox data and is not production schedule or price evidence.
- OpenAI transport connection/timeouts and Duffel-declared transient read-only failures each allow one explicit retry.
- Authentication, permission, malformed requests, structured-output failures, non-transient HTTP failures, and business failures are not retried.
- Recovered retries remain visible in trajectory stability, request counts, latency, and cost accounting.
- This flight-search-only case does not execute offer revalidation, handoff, hotel search, approval, or booking.

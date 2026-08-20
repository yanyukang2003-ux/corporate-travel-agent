# Real model + Duffel workflow evaluation

- Result: **PASS**
- Mode: `model_live_provider`
- Dataset: `model-duffel-workflow-full-recovery-v1` `1.0.0`
- Dataset SHA-256: `237b617c6223b75085e4bee03bb55db89b857e2cc0bc3d19ab8f2ea7117c0898`
- Requested model: `gpt-5.6`
- Actual models: `gpt-5.6-sol`
- Reasoning effort: `medium`
- Provider: `duffel` / `test`

## Three-run stability

- Completed: 3/3; passed: 3
- pass@1: 100.00%
- pass^3: 100.00%
- pass@3: 100.00%
- mixed run rate: 0.00%
- intent consistency: 100.00%
- trajectory consistency: 100.00%

## Calls, latency, tokens, and cost

- Model calls: 3/6
- Duffel calls: 3/6
- Total latency mean/P95: 8671.274 / 10052.299 ms
- LLM latency mean/P95: 5828.145 / 6524.160 ms
- Provider latency mean/P95: 2839.237 / 3524.175 ms
- Input/output/total tokens: 3186 / 639 / 3825
- Cached input/cache-write/reasoning tokens: 0 / 3177 / 190
- Estimated OpenAI cost: `$0.03907125` USD
- Frozen cost ceiling: `$0.15` USD
- Duffel Test Mode search cost: `$0` (no order or payment)

## Hallucination exposure

- Tool hallucination: model exposure `not_exposed`; system unknown calls 0
- Parameter hallucination: model exposure `measured`; failed/evaluable attempts 0/3; not evaluable 0
- Shadow hallucination: model exposure `not_exposed`; evidence-proxy failed/evaluable attempts 0/3; not evaluable 0

## Operational reliability and accounting

- Infrastructure failures: 0/3 (0.00%)
- Completed model response rate: 100.00%
- Failure categories: `{}`
- Resource-accounted attempts: 3/3 (100.00%)
- Completed-response accounting: 3/3 (100.00%)
- Outbound-request trace accounting: 6/6 (100.00%)
- Failed before HTTP response: 0
- Estimated cost is a lower bound: `false`
- Failed model requests / retry attempts / recovered retries: 0 / 0 / 0
- Model request cap: 6; maximum attempts per run: 2
- Retry error layers: `{}`
- Model terminal error causes: `{}`
- Failed provider requests / retry attempts / recovered retries: 0 / 0 / 0
- Provider request cap: 6; maximum attempts per run: 2
- Provider retry error layers: `{}`
- Provider terminal error causes: `{}`
- Common terminal error causes: `[]`

## Tool efficiency

- Expected/actual calls per run: 2 / 2.000
- Raw repeated-name calls: 0
- Authorized retry calls: 0
- Unjustified duplicate calls: 0
- Retry overhead rate: 0.00%
- Calls per successful run: 2.000
- Allowed-order pass rate: 100.00%

## Gates

- PASS `integration_gate`
- PASS `three_run_stability_gate`
- PASS `resource_accounting_gate`
- PASS `booking_safety_gate`
- PASS `cost_ceiling_gate`

## Attempts

- attempt 1: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.01314375`, failure category `none`
- attempt 2: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.01365375`, failure category `none`
- attempt 3: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.01227375`, failure category `none`

## Limitations

- The model extracts structured intent but does not choose tools; tool selection remains orchestrator-controlled.
- Duffel Test Mode inventory is sandbox data and is not production schedule or price evidence.
- OpenAI transport connection/timeouts and Duffel-declared transient read-only failures each allow one explicit retry.
- Authentication, permission, malformed requests, structured-output failures, non-transient HTTP failures, and business failures are not retried.
- Recovered retries remain visible in trajectory stability, request counts, latency, and cost accounting.
- This flight-search-only case does not execute offer revalidation, handoff, hotel search, approval, or booking.

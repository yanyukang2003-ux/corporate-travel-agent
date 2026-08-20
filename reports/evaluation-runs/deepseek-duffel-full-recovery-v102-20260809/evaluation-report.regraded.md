# Real model + Duffel workflow evaluation

- Result: **PASS**
- Mode: `model_live_provider`
- Dataset: `model-duffel-workflow-deepseek-full-recovery-v1` `1.0.0`
- Dataset SHA-256: `67e33420596a03f5201942bf899442683b53549f7122ecd6eefa2072ebbb1416`
- Requested model: `deepseek-v4-pro`
- Actual models: `deepseek-v4-pro`
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
- Total latency mean/P95: 5112.255 / 5373.143 ms
- LLM latency mean/P95: 2539.580 / 3027.207 ms
- Provider latency mean/P95: 2569.065 / 2787.584 ms
- Input/output/total tokens: 5739 / 620 / 6359
- Cached input/cache-write/reasoning tokens: 2048 / None / None
- Estimated model cost: `$0.003035865` USD
- Frozen cost ceiling: `$0.05` USD
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

- attempt 1: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.001009635`, failure category `none`
- attempt 2: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.001013115`, failure category `none`
- attempt 3: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.001013115`, failure category `none`

## Limitations

- The model extracts structured intent but does not choose tools; tool selection remains orchestrator-controlled.
- Duffel Test Mode inventory is sandbox data and is not production schedule or price evidence.
- Model transport connection/timeouts and Duffel-declared transient read-only failures each allow one explicit retry.
- Authentication, permission, malformed requests, structured-output failures, non-transient HTTP failures, and business failures are not retried.
- DeepSeek Chat usage requires input, output, and total tokens; unavailable cache-write and reasoning details are not fabricated, and cost uses cache-miss rates as a conservative upper bound.
- Recovered retries remain visible in trajectory stability, request counts, latency, and cost accounting.
- This flight-search-only case does not execute offer revalidation, handoff, hotel search, approval, or booking.

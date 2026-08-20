# Real model + Duffel workflow evaluation

- Result: **FAIL**
- Mode: `model_live_provider`
- Dataset: `model-duffel-workflow-recovery-v1` `1.0.0`
- Dataset SHA-256: `00fa4dbe262428cb8b94682d4993ca20aecbe98864de13404c6f27f8d06d5d80`
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
- intent consistency: 100.00%
- trajectory consistency: 0.00%

## Calls, latency, tokens, and cost

- Model calls: 3/6
- Duffel calls: 3/3
- Total latency mean/P95: 10426.021 / 12571.164 ms
- LLM latency mean/P95: 6579.997 / 9221.730 ms
- Provider latency mean/P95: 3842.856 / 5002.476 ms
- Input/output/total tokens: 3183 / 607 / 3790
- Cached input/cache-write/reasoning tokens: 0 / 3174 / 169
- Estimated OpenAI cost: `$0.0380925` USD
- Frozen cost ceiling: `$0.15` USD
- Duffel Test Mode search cost: `$0` (no order or payment)

## Hallucination exposure

- Tool hallucination: model exposure `not_exposed`; system unknown calls 0
- Parameter hallucination: model exposure `measured`; failed/evaluable attempts 0/3; not evaluable 0
- Shadow hallucination: model exposure `not_exposed`; evidence-proxy failed/evaluable attempts 0/2; not evaluable 1

## Operational reliability and accounting

- Infrastructure failures: 0/3 (0.00%)
- Completed model response rate: 100.00%
- Failure categories: `{"provider_failure": 1}`
- Resource-accounted attempts: 3/3 (100.00%)
- Completed-response accounting: 3/3 (100.00%)
- Estimated cost is a lower bound: `false`
- Failed model requests / retry attempts / recovered retries: 0 / 0 / 0
- Model request cap: 6; maximum attempts per run: 2
- Retry error layers: `{}`

## Gates

- FAIL `integration_gate`
- FAIL `three_run_stability_gate`
- PASS `resource_accounting_gate`
- PASS `booking_safety_gate`
- PASS `cost_ceiling_gate`

## Attempts

- attempt 1: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.0128975`, failure category `none`
- attempt 2: FAIL, state `PROVIDER_FAILED`, options 0, cost `$0.0126875`, failure category `provider_failure`
- attempt 3: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.0125075`, failure category `none`

## Limitations

- The model extracts structured intent but does not choose tools; tool selection remains orchestrator-controlled.
- Duffel Test Mode inventory is sandbox data and is not production schedule or price evidence.
- Only OpenAI transport connection and timeout failures are eligible for one explicit retry; HTTP, authentication, schema, and business failures are not retried.
- A recovered retry can satisfy the integration contract but remains visible in trajectory stability, request counts, latency, and lower-bound cost accounting.
- This flight-search-only case does not execute offer revalidation, handoff, hotel search, approval, or booking.

# Real model + Duffel workflow evaluation

- Result: **FAIL**
- Mode: `model_live_provider`
- Dataset: `model-duffel-workflow-smoke-v1` `1.0.0`
- Dataset SHA-256: `2531cd4d64b4c75d3d76a730df6e02af9ec290fbd38e616a8b406e4becf9ea5e`
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

- Model calls: 3/3
- Duffel calls: 2/3
- Total latency mean/P95: 7922.463 / 10831.126 ms
- LLM latency mean/P95: 5139.803 / 5725.382 ms
- Provider latency mean/P95: 2779.927 / 5102.933 ms
- Input/output/total tokens: 2120 / 403 / 2523
- Cached input/cache-write/reasoning tokens: 0 / 2114 / 115
- Estimated OpenAI cost: `$0.0253325` USD
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
- Estimated cost is a lower bound: `true`

## Gates

- FAIL `integration_gate`
- FAIL `three_run_stability_gate`
- FAIL `resource_accounting_gate`
- PASS `booking_safety_gate`
- PASS `cost_ceiling_gate`

## Attempts

- attempt 1: FAIL, state `NEEDS_STRUCTURED_INPUT`, options 0, cost `$None`, failure category `infrastructure_connection_error`
- attempt 2: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.01280125`, failure category `none`
- attempt 3: PASS, state `WAITING_FOR_USER`, options 3, cost `$0.01253125`, failure category `none`

## Limitations

- The model extracts structured intent but does not choose tools; tool selection remains orchestrator-controlled.
- Duffel Test Mode inventory is sandbox data and is not production schedule or price evidence.
- This flight-search-only case does not execute offer revalidation, handoff, hotel search, approval, or booking.
- Tool hallucination is not model-exposed in this architecture; unknown tool calls are checked only as a system-level invariant.

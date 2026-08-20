# Duffel real-provider workflow smoke

- Result: **PASS**
- Mode: `deterministic_live_provider`
- Provider: `duffel` / `test`
- Dataset: `duffel-real-workflow-smoke-v1` `1.0.0`
- Dataset SHA-256: `2a672f4a12e79f2908fdac4c1ef05081b29e67eadc1ee3566a8d625b14b694e5`
- Case: `duffel-real-lhr-jfk-search`
- External calls: `1`
- Duration: `3734.08 ms`
- Final task state: `WAITING_FOR_USER`
- Inventory: 1 snapshot(s), 44 normalized offer(s)
- Booking intent created: `false`

## Rule checks

- PASS `exactly_one_provider_call`
- PASS `authorized_api_snapshot`
- PASS `expected_final_state`
- PASS `minimum_options`
- PASS `booking_not_created`
- PASS `test_mode_disclosed`

This run uses a structured request and one real Duffel Test Mode search inside the
agent orchestrator. It does not call an LLM, create an order, or test production
inventory. The raw supplier JSON was archived under the run directory and referenced by SHA-256. The access token is never written to the trace or
report.

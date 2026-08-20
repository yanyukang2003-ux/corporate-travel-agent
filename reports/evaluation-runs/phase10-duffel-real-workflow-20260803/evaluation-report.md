# Duffel real-provider workflow smoke

- Result: **FAIL**
- Mode: `deterministic_live_provider`
- Provider: `duffel` / `test`
- Dataset: `duffel-real-workflow-smoke-v1` `1.0.0`
- Dataset SHA-256: `2a672f4a12e79f2908fdac4c1ef05081b29e67eadc1ee3566a8d625b14b694e5`
- Case: `duffel-real-lhr-jfk-search`
- External calls: `1`
- Duration: `6368.399 ms`
- Final task state: `PROVIDER_FAILED`
- Inventory: 0 snapshot(s), 0 normalized offer(s)
- Booking intent created: `false`

## Rule checks

- PASS `exactly_one_provider_call`
- FAIL `authorized_api_snapshot`
- FAIL `expected_final_state`
- FAIL `minimum_options`
- PASS `booking_not_created`
- FAIL `test_mode_disclosed`

This run uses a structured request and one real Duffel Test Mode search inside the
agent orchestrator. It does not call an LLM, create an order, or test production
inventory. The access token was not written to the trace or report.

## Post-run evidence correction

The raw supplier response was **not** archived in this failed run because the
pre-fix adapter archived only after successful normalization. This evidence gap was
found during review and fixed before a second run: successful HTTP JSON is now
archived before parsing, including when normalization fails. This historical FAIL
report is preserved rather than overwritten.

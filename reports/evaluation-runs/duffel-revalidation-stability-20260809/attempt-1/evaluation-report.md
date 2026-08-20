# Duffel real-provider workflow smoke

- Result: **PASS**
- Mode: `deterministic_live_provider`
- Provider: `duffel` / `test`
- Dataset: `duffel-real-revalidation-smoke-v1` `1.0.0`
- Dataset SHA-256: `b995025f8b44c170b06eabc3102f7378a0e8625b52a628038cc64dd640dfffd9`
- Case: `duffel-real-lhr-jfk-search-select-revalidate`
- Workflow action: `search_select_revalidate`
- External calls: `2`
- Provider tools: `provider.search_transport.outbound, provider.revalidate, provider.create_deep_link`
- Duration: `2688.321 ms`
- Final task state: `READY_FOR_HANDOFF`
- Revalidation status: `UNCHANGED`
- Inventory: 1 snapshot(s), 42 normalized offer(s)
- Booking intent created: `true`

## Rule checks

- PASS `external_call_cap_respected`
- PASS `expected_external_http_calls`
- PASS `authorized_api_snapshot`
- PASS `minimum_options`
- PASS `search_response_archived`
- PASS `test_mode_disclosed`
- PASS `no_order_or_payment_http`
- PASS `no_order_or_payment_tools`
- PASS `selection_succeeded`
- PASS `selected_option_compliant`
- PASS `search_then_offer_get`
- PASS `revalidation_status_allowed`
- PASS `selected_offer_still_available`
- PASS `revalidation_response_archived`
- PASS `expected_provider_tool_sequence`
- PASS `conditional_final_state`
- PASS `conditional_booking_intent`
- PASS `unchanged_handoff_is_test_only`
- PASS `price_change_requires_reconfirmation`

This run uses a structured request inside the agent orchestrator. Depending on the
frozen workflow action, it performs one real Duffel Test Mode search or a search
followed by one Offer retrieval/revalidation. A Booking Intent in this report is
local handoff state only. The runner does not call an LLM, create a Duffel Order,
submit a Payment, or test production inventory. The raw supplier JSON responses were archived under the run directory and referenced by SHA-256. The access
token is never written to the trace or report.

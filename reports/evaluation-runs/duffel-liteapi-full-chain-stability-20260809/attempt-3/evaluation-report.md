# Duffel + LiteAPI real full-chain smoke

- Result: **FAIL**
- Provider: `duffel+liteapi` / `test+sandbox-read-only`
- Route: `LHR -> JFK`
- Hotel dates: `2026-09-15..2026-09-17`
- External calls: `1`
- Final state: `PROVIDER_FAILED`
- Task failure: `Duffel POST request failed transiently: ConnectError`
- Revalidation: `not_run`
- Flight price: `n/a `
- Hotel: `n/a` at `n/a ` per night
- Total: `n/a `

## Checks

- PASS `workflow_completed_without_exception`
- FAIL `selection_succeeded`
- FAIL `exact_external_call_cap`
- FAIL `duffel_call_cap`
- FAIL `liteapi_call_cap`
- FAIL `duffel_search_then_revalidate`
- FAIL `liteapi_search_then_quote_refresh`
- FAIL `flight_snapshot_authorized`
- FAIL `hotel_snapshot_authorized`
- FAIL `minimum_combined_options`
- FAIL `selected_option_has_both_providers`
- FAIL `selected_prices_use_policy_currency`
- FAIL `selected_option_policy_compliant`
- FAIL `both_quotes_revalidated`
- FAIL `allowed_revalidation_status`
- FAIL `expected_provider_tool_sequence`
- FAIL `conditional_final_state`
- FAIL `conditional_booking_intent`
- PASS `unchanged_handoff_covers_flight_and_hotel`
- FAIL `search_and_revalidation_archived`
- PASS `no_booking_payment_or_order_http`
- PASS `provider_secrets_not_archived`

This is a read-only provider workflow. Duffel inventory is Test Mode and LiteAPI
inventory is Sandbox data. The run creates no airline order, hotel prebook,
booking, payment, ticket, or reservation. Any BookingIntent is local
instruction-only handoff state.

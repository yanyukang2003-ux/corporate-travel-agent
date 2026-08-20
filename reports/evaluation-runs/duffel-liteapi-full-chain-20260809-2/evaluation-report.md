# Duffel + LiteAPI real full-chain smoke

- Result: **PASS**
- Provider: `duffel+liteapi` / `test+sandbox-read-only`
- Route: `LHR -> JFK`
- Hotel dates: `2026-09-15..2026-09-17`
- External calls: `4`
- Final state: `READY_FOR_HANDOFF`
- Task failure: `none`
- Revalidation: `UNCHANGED`
- Flight price: `215.52 USD`
- Hotel: `Hotel Riu Plaza Manhattan Times Square` at `444.29 USD` per night
- Total: `1104.10 USD`

## Checks

- PASS `workflow_completed_without_exception`
- PASS `selection_succeeded`
- PASS `exact_external_call_cap`
- PASS `duffel_call_cap`
- PASS `liteapi_call_cap`
- PASS `duffel_search_then_revalidate`
- PASS `liteapi_search_then_quote_refresh`
- PASS `flight_snapshot_authorized`
- PASS `hotel_snapshot_authorized`
- PASS `minimum_combined_options`
- PASS `selected_option_has_both_providers`
- PASS `selected_prices_use_policy_currency`
- PASS `selected_option_policy_compliant`
- PASS `both_quotes_revalidated`
- PASS `allowed_revalidation_status`
- PASS `expected_provider_tool_sequence`
- PASS `conditional_final_state`
- PASS `conditional_booking_intent`
- PASS `unchanged_handoff_covers_flight_and_hotel`
- PASS `search_and_revalidation_archived`
- PASS `no_booking_payment_or_order_http`
- PASS `provider_secrets_not_archived`

This is a read-only provider workflow. Duffel inventory is Test Mode and LiteAPI
inventory is Sandbox data. The run creates no airline order, hotel prebook,
booking, payment, ticket, or reservation. Any BookingIntent is local
instruction-only handoff state.

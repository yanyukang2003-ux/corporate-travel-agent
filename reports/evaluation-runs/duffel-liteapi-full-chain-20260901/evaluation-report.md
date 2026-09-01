# Duffel + LiteAPI real full-chain smoke

- Result: **FAIL**
- Provider: `duffel+liteapi` / `test+sandbox-read-only`
- Route: `LHR -> JFK`
- Hotel dates: `2026-09-15..2026-09-17`
- External calls: `4` / `12` maximum (`4` baseline)
- Provider retries: `0` / `2` maximum per operation
- Final state: `RECONFIRMATION_REQUIRED`
- Task failure: `none`
- Revalidation: `UNAVAILABLE`
- Flight price: `215.71 USD`
- Hotel: `Margaritaville Resort Times Square` at `540.97 USD` per night
- Total: `1297.65 USD`

## Checks

- PASS `workflow_completed_without_exception`
- PASS `selection_succeeded`
- PASS `default_retry_policy_applied`
- PASS `external_call_budget_respected`
- PASS `duffel_call_budget_respected`
- PASS `liteapi_call_budget_respected`
- PASS `provider_retry_contract_valid`
- PASS `duffel_search_then_revalidate`
- PASS `liteapi_search_then_quote_refresh`
- PASS `flight_snapshot_authorized`
- PASS `hotel_snapshot_authorized`
- PASS `minimum_combined_options`
- PASS `selected_option_has_both_providers`
- PASS `selected_prices_use_policy_currency`
- PASS `selected_option_policy_compliant`
- FAIL `both_quotes_revalidated`
- FAIL `allowed_revalidation_status`
- FAIL `expected_provider_tool_sequence`
- FAIL `conditional_final_state`
- FAIL `conditional_booking_intent`
- PASS `unchanged_handoff_covers_flight_and_hotel`
- PASS `search_and_revalidation_archived`
- PASS `no_booking_payment_or_order_http`
- PASS `provider_secrets_not_archived`

This is a read-only provider workflow. Duffel inventory is Test Mode and LiteAPI
inventory is Sandbox data. The run creates no airline order, hotel prebook,
booking, payment, ticket, or reservation. Any BookingIntent is local
instruction-only handoff state.

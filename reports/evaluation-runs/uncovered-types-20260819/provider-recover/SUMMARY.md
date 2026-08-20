# Provider Delayed Recovery Acceptance

- run_id: `provider-recover`
- started_at: 2026-08-19T11:51:33.276195+00:00
- completed_at: 2026-08-19T11:51:40.416806+00:00
- overall_passed: **True**

## recover

- passed: **True**
- final_state: `RECONFIRMATION_REQUIRED`
- failed_checks: _(none)_

- [PASS] `no_unexpected_exception`
- [PASS] `after_create_waiting_for_provider`
- [PASS] `three_injected_failures_before_real_http`
- [PASS] `delayed_retry_processed_task`
- [PASS] `recovered_to_waiting_for_user`
- [PASS] `retry_status_recovered`
- [PASS] `real_http_only_after_delay`
- [PASS] `has_combined_options`
- [PASS] `select_and_revalidate_completed`
- [PASS] `no_booking_intent_on_failure_path`
- [PASS] `audit_has_delayed_retry_started`
- [PASS] `no_order_or_payment_paths`
- [PASS] `booking_intent_absent_on_price_change`

## Notes

- `recover` injects three immediate transport faults, then uses real Duffel Test Mode + LiteAPI Sandbox on the first delayed retry.
- This is stronger than the one-shot full-chain smoke (which never starts delayed recovery), but it is still a controlled fault — not a natural upstream multi-minute outage.
- `exhaust` and `circuit` stay fully offline (injected faults only).

# Provider Delayed Recovery Acceptance

- run_id: `provider-delayed-recovery-acceptance-20260810-1`
- started_at: 2026-08-10T14:14:00.278525+00:00
- completed_at: 2026-08-10T14:14:10.974264+00:00
- overall_passed: **True**

## recover

- passed: **True**
- final_state: `READY_FOR_HANDOFF`
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
- [PASS] `booking_intent_instruction_only`

## exhaust

- passed: **True**
- final_state: `PROVIDER_FAILED`
- failed_checks: _(none)_

- [PASS] `terminal_provider_failed`
- [PASS] `delayed_attempts_3`
- [PASS] `three_delayed_started_events`
- [PASS] `exhausted_event_present`
- [PASS] `no_booking_intent`
- [PASS] `all_rounds_processed_task`

## circuit

- passed: **True**
- failed_checks: _(none)_

- [PASS] `first_waiting`
- [PASS] `second_waiting`
- [PASS] `first_used_three_attempts`
- [PASS] `second_did_not_call_provider`
- [PASS] `second_scheduled_retry`

## Notes

- `recover` injects three immediate transport faults, then uses real Duffel Test Mode + LiteAPI Sandbox on the first delayed retry.
- This is stronger than the one-shot full-chain smoke (which never starts delayed recovery), but it is still a controlled fault — not a natural upstream multi-minute outage.
- `exhaust` and `circuit` stay fully offline (injected faults only).

# Provider Delayed Recovery Acceptance

- run_id: `provider-exhaust`
- started_at: 2026-08-19T11:51:33.052114+00:00
- completed_at: 2026-08-19T11:51:33.054636+00:00
- overall_passed: **True**

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

## Notes

- `recover` injects three immediate transport faults, then uses real Duffel Test Mode + LiteAPI Sandbox on the first delayed retry.
- This is stronger than the one-shot full-chain smoke (which never starts delayed recovery), but it is still a controlled fault — not a natural upstream multi-minute outage.
- `exhaust` and `circuit` stay fully offline (injected faults only).

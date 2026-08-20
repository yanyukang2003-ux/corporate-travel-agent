# Provider Delayed Recovery Acceptance

- run_id: `provider-circuit`
- started_at: 2026-08-19T11:51:33.163605+00:00
- completed_at: 2026-08-19T11:51:33.165323+00:00
- overall_passed: **True**

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

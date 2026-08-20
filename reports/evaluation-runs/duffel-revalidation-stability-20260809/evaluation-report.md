# Duffel Test Mode revalidation stability

- Result: **PASS**
- Dataset: `duffel-real-revalidation-smoke-v1` `1.0.0`
- Dataset SHA-256: `b995025f8b44c170b06eabc3102f7378a0e8625b52a628038cc64dd640dfffd9`
- Attempts: `3`
- Passed attempts: `3`
- pass^3: `1.00`
- mixed run rate: `0.00`
- External calls: `6` / `6`
- Revalidation statuses: `{'UNCHANGED': 3}`
- Search/revalidation archives: `3` / `3`
- Internal Booking Intents: `3`
- Duffel Orders / Payments: `0` / `0`
- Duration: `8968.506 ms`

## Stability checks

- PASS `three_attempt_results_present`
- PASS `all_subrunners_exited_zero`
- PASS `all_attempts_passed`
- PASS `exact_total_external_calls`
- PASS `two_external_calls_per_attempt`
- PASS `request_sequence_consistent`
- PASS `provider_tool_trajectory_consistent`
- PASS `revalidation_status_consistent`
- PASS `search_archives_complete`
- PASS `revalidation_archives_complete`
- PASS `no_order_or_payment_calls`
- PASS `no_secret_markers`

Each attempt performs one Duffel Test Mode search followed by one Offer retrieval.
The Booking Intent count refers only to local handoff state. This runner never calls
Duffel Order or Payment endpoints, and it never accesses production inventory.

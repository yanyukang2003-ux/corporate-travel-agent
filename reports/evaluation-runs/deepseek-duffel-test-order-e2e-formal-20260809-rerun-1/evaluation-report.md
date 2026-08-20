# Real model + Duffel Test Order end-to-end smoke

- Result: **PASS**
- Model: `deepseek-v4-pro`
- Dataset: `model-duffel-test-order-e2e-v1` `1.0.0`
- Dataset SHA-256: `bf8ea60499ff9dc8414fbf0f76ad072c5f33619bdd244a6f659d3dfae05a6c56`
- Workflow state: `HANDED_OFF`
- Duffel HTTP calls: `7`
- Test Order created: `true`
- Test Order live mode: `False`
- Cancellation confirmed: `true`
- Final Order cancelled: `true`
- Cleanup status: `cancelled`
- Raw response archives: `7`
- Estimated model cost: `$0.001007895` USD
- Duration: `7830.537 ms`

## End-to-end checks

- PASS `runner_completed_without_failure`
- PASS `workflow_trace_complete`
- PASS `one_real_model_call`
- PASS `actual_model_matches_frozen_model`
- PASS `model_usage_complete`
- PASS `model_cost_within_ceiling`
- PASS `intent_matches_frozen_case`
- PASS `workflow_tool_sequence`
- PASS `workflow_provider_call_cap`
- PASS `provider_http_sequence`
- PASS `provider_http_call_cap`
- PASS `revalidation_unchanged`
- PASS `duffel_airways_offer_selected`
- PASS `workflow_handed_off`
- PASS `internal_booking_intent_created`
- PASS `test_order_created`
- PASS `test_order_read_back`
- PASS `test_order_http_sequence`
- PASS `test_order_http_call_cap`
- PASS `total_duffel_call_cap`
- PASS `cancellation_quote_test_mode`
- PASS `cancellation_confirmed`
- PASS `final_order_cancelled`
- PASS `cleanup_completed`
- PASS `all_external_responses_archived`
- PASS `write_payload_hashes_recorded`
- PASS `no_secret_markers`
- PASS `live_booking_remained_disabled`

This evaluation uses one billable model call and creates an external Duffel Test
Mode Order with fixed synthetic passenger data and sandbox balance payment. It then
reads the Order, creates and confirms its cancellation, and verifies the cancelled
Order. It rejects live tokens and never retries Order or cancellation writes.

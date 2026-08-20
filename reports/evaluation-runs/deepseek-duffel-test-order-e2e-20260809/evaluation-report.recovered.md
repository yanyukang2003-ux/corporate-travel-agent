# Recovered Duffel Test Order evidence

- External transaction: **PASS**
- Formal evaluation: **INVALID**
- Duffel HTTP responses: `7`
- Test Order live mode: `False`
- Cancellation confirmed at: `2026-08-09T11:16:05.435950Z`
- Refund: `231.37 USD`
  to `balance`
- Final Order cancelled: `true`
- Additional recovery API calls: `0`

## Archived evidence checks

- PASS `seven_response_categories_present`
- PASS `all_responses_test_mode`
- PASS `duffel_airways_offer_selected`
- PASS `revalidated_offer_matches_order`
- PASS `order_readback_matches_create`
- PASS `cancellation_quote_matches_order`
- PASS `cancellation_confirmation_matches_quote`
- PASS `final_order_matches_create`
- PASS `final_order_contains_confirmed_cancellation`
- PASS `final_order_no_longer_cancellable`
- PASS `sandbox_balance_payment_completed`
- PASS `refund_returned_to_test_balance`
- PASS `raw_hashes_match_metadata`
- PASS `raw_evidence_permissions_private`
- PASS `no_secret_markers`

The real Test Mode Order was created, paid with sandbox balance, read back,
cancelled, refunded to the sandbox balance, and read back again. The original
runner failed only while constructing its formal trace after these steps. Because
the model usage and workflow trace were not persisted, this recovery report does
not upgrade the original run to a formal evaluation pass.

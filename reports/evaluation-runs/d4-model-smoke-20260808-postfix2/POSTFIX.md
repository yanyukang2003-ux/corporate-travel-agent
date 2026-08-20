# D4 real-model smoke after targeted fixes (postfix2)

**Date:** 2026-08-08  
**Run:** `reports/evaluation-runs/d4-model-smoke-20260808-postfix2/`  
**Model:** gpt-5.6-terra

## Result

| Metric | Baseline smoke | Postfix2 |
|---|---|---|
| task_pass_rate | 20/24 (83.3%) | **21/24 (87.5%)** |
| Protocol gate ≥85% | fail | **pass** |
| assertion_pass_rate | 94.1% | 95.9% |
| real_model_calls | 26 | 26 |
| estimated_cost_usd | (not recorded) | **$0.163926** |
| input/output tokens | — | 33261 / 8117 |

## Fixes applied

1. Skip selection-like turns during clarification (avoid intent pollution).
2. City alias normalization in model_mock.
3. Prompt: meeting-time arrive_by + arrive_before_meeting (no double buffer).
4. Prompt: fill departure_after / return windows; bare day-of-month; revise route sentences win.
5. **Selective multi-turn join**: only merge later turns that refine schedule/route (departure window, timezone route), not preference/adversarial turns.

## Remaining fails (3)

- `historical_failure-empty-return-inventory-027`: actual `NEEDS_CLARIFICATION` notes=['model intent message chars=56', 'real_model_calls=1']
  - final-state: expected 'NO_FEASIBLE_OPTION' actual 'NEEDS_CLARIFICATION'
  - minimum-safe-tool-order: expected ['llm.extract_trip_intent', 'provider.search_transport.outbound', 'provider.search_transport.inbound'] actual ['llm.extract_trip_intent']
- `boundary-timezone-cross-date-044`: actual `NEEDS_CLARIFICATION` notes=['model intent message chars=301', 'real_model_calls=1']
  - final-state: expected 'WAITING_FOR_USER' actual 'NEEDS_CLARIFICATION'
  - minimum-safe-tool-order: expected ['llm.extract_trip_intent', 'provider.search_transport.outbound', 'provider.search_transport.inbound'] actual ['llm.extract_trip_intent']
- `boundary-arrival-buffer-exact-031`: actual `NEEDS_CLARIFICATION` notes=['model intent message chars=73', 'real_model_calls=1']
  - final-state: expected 'READY_FOR_HANDOFF' actual 'NEEDS_CLARIFICATION'
  - booking-intent-presence: expected True actual None
  - minimum-safe-tool-order: expected ['llm.extract_trip_intent', 'provider.search_transport.outbound', 'provider.search_transport.inbound', 'provider.search_hotels', 'provider.revalidate', 'provider.create_deep_link'] actual ['llm.extract_trip_intent']

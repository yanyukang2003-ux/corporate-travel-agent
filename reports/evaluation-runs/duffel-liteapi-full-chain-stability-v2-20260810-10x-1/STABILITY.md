# Duffel + LiteAPI full-chain stability v2 — 10 real attempts

- Result: **FAIL (4/10 passed, 40%)**
- Initial-attempt success: `3/10` (`30%`)
- Retry recovery: `1/7` retried runs (`14.3%`); final success improved by `10` percentage points
- Dataset: `duffel-liteapi-real-full-chain-v2` `2.0.0`
- Provider mode: `duffel+liteapi` / `test+sandbox-read-only`
- Retry policy: maximum `3` provider attempts per read operation
- External calls: `35` observed / `120` maximum (`27` Duffel, `8` LiteAPI)
- Booking, order, payment, or ticket calls: `0`
- Mean duration: `14207.033 ms`; successful mean: `10562.678 ms`

## Attempts

| Attempt | Result | Duration | Calls | Retries | Final state | Outcome |
|---|---:|---:|---:|---:|---|---|
| 1 | FAIL | 16617.904 ms | 3 | 2 | `PROVIDER_FAILED` | Duffel `ConnectError` exhausted |
| 2 | FAIL | 16671.304 ms | 3 | 2 | `PROVIDER_FAILED` | Duffel `ConnectError` exhausted |
| 3 | FAIL | 16641.321 ms | 3 | 2 | `PROVIDER_FAILED` | Duffel `ConnectError` exhausted |
| 4 | FAIL | 16541.917 ms | 3 | 2 | `PROVIDER_FAILED` | Duffel `ConnectError` exhausted |
| 5 | FAIL | 16664.004 ms | 3 | 2 | `PROVIDER_FAILED` | Duffel `ConnectError` exhausted |
| 6 | FAIL | 16683.169 ms | 3 | 2 | `PROVIDER_FAILED` | Duffel `ConnectError` exhausted |
| 7 | PASS | 14316.004 ms | 5 | 1 | `READY_FOR_HANDOFF` | first real retry recovery |
| 8 | PASS | 9376.054 ms | 4 | 0 | `READY_FOR_HANDOFF` | clean success |
| 9 | PASS | 9295.167 ms | 4 | 0 | `READY_FOR_HANDOFF` | clean success |
| 10 | PASS | 9263.488 ms | 4 | 0 | `READY_FOR_HANDOFF` | clean success |

## Findings

- Attempts 1–6 formed a time-correlated connectivity failure cluster at the initial Duffel `POST /air/offer_requests` boundary. Each run made three bounded attempts, all ending in `DUFFEL_TRANSPORT_CONNECTION_ERROR` / `ConnectError`; LiteAPI was not called.
- The cluster lasted approximately 101 seconds from the start of attempt 1 to the terminal failure of attempt 6. This is longer than the intra-run retry window, so increasing only the attempt count did not cover it.
- Attempt 7 failed once at the same Duffel boundary, then recovered through one correctly linked retry and completed the entire flight-and-hotel chain. This is direct real-call evidence that retry recovery works.
- Attempts 8–10 completed without retries. Across all successful runs, tool trajectory, `READY_FOR_HANDOFF`, `UNCHANGED` revalidation, three options, 42 flight offers, 30 hotel offers, and the `444.29 USD` hotel nightly price were consistent.
- The 10-run final pass rate is `40%`. Including the immediately preceding 3/3 v2 run gives `7/13` (`53.8%`), but failures are strongly time-clustered and should not be treated as independent Bernoulli samples.
- All retry contracts and call budgets were valid. No order, hotel prebook, booking, payment, or ticket endpoint was called, and provider secrets were not archived.

## Interpretation

The bounded retry mechanism is functioning and produced one real recovery, but the current short retry window is insufficient for a minute-scale connectivity incident. Production resilience should treat this as a longer-lived dependency/egress condition: retain the bounded immediate retries, then use a circuit breaker or delayed job-level retry rather than sending more tightly spaced requests.

# Duffel + LiteAPI full-chain stability v2

- Result: **PASS (3/3, 100%)**
- Dataset: `duffel-liteapi-real-full-chain-v2` `2.0.0`
- Dataset SHA-256: `dd9ef5cda03ad5c20868e67f320d4b64014b7e13a136be7d954c515a2482b3be`
- Provider mode: `duffel+liteapi` / `test+sandbox-read-only`
- Route and stay: `LHR -> JFK`, `2026-09-15..2026-09-17`
- Retry policy: maximum `3` provider attempts per read operation; `0` retries observed
- External calls: `12` observed / `36` maximum across three attempts (`12` baseline)
- Booking, order, payment, or ticket calls: `0`
- Mean / median duration: `12108.185 ms` / `9677.964 ms`

## Attempts

| Attempt | Result | Duration | Calls | Retries | Final state | Revalidation | Flight | Hotel/night | Total |
|---|---:|---:|---:|---:|---|---|---:|---:|---:|
| 1 | PASS | 19329.218 ms | 4 | 0 | `READY_FOR_HANDOFF` | `UNCHANGED` | 214.44 USD | 444.29 USD | 1103.02 USD |
| 2 | PASS | 9677.964 ms | 4 | 0 | `READY_FOR_HANDOFF` | `UNCHANGED` | 216.62 USD | 444.29 USD | 1105.20 USD |
| 3 | PASS | 7317.373 ms | 4 | 0 | `READY_FOR_HANDOFF` | `UNCHANGED` | 214.84 USD | 444.29 USD | 1103.42 USD |

## Stability findings

- All three chains completed with the same successful provider-tool trajectory, `READY_FOR_HANDOFF` state, `UNCHANGED` revalidation, three combined options, 42 flight offers, and 30 hotel offers.
- The hotel nightly price remained `444.29 USD`. The selected flight and total prices varied by `2.18 USD`; policy compliance and the selected hotel did not change.
- Every attempt applied `max_provider_attempts=3` and passed the retry-contract check. No transient error occurred, so the live run did not need to exercise recovery after a retry.
- The previous sample passed 2/3 and failed once at the initial Duffel connection boundary. This new sample passed 3/3, but the separate samples are not sufficient to attribute the improvement causally to retry behavior because no retry was triggered here.
- All raw responses from completed provider calls were archived. No order, hotel prebook, booking, payment, or ticket endpoint was called, and provider secrets were not archived.

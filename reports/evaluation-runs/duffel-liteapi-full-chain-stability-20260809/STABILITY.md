# Duffel + LiteAPI full-chain stability

- Result: **FAIL (2/3 passed, 66.7%)**
- Dataset: `duffel-liteapi-real-full-chain-v1` `1.0.0`
- Dataset SHA-256: `aa575b23c54481ad035afb6912cc199fecadbbb2cad211477bbd924d1a8f8008`
- Provider mode: `duffel+liteapi` / `test+sandbox-read-only`
- Route and stay: `LHR -> JFK`, `2026-09-15..2026-09-17`
- External calls: `9` observed / `12` maximum (`5` Duffel, `4` LiteAPI)
- Booking, order, payment, or ticket calls: `0`
- Mean duration: `8511.343 ms` across all attempts; `10264.509 ms` across successful attempts

## Attempts

| Attempt | Result | Duration | Calls | Final state | Revalidation | Flight | Hotel/night | Total |
|---|---:|---:|---:|---|---|---:|---:|---:|
| 1 | PASS | 9711.838 ms | 4 | `READY_FOR_HANDOFF` | `UNCHANGED` | 217.15 USD | 444.29 USD | 1105.73 USD |
| 2 | PASS | 10817.180 ms | 4 | `READY_FOR_HANDOFF` | `UNCHANGED` | 220.83 USD | 444.29 USD | 1109.41 USD |
| 3 | FAIL | 5005.011 ms | 1 | `PROVIDER_FAILED` | not run | n/a | n/a | n/a |

## Stability findings

- The two completed chains had the same provider-tool trajectory, `READY_FOR_HANDOFF` state, `UNCHANGED` revalidation, three combined options, 42 normalized flight offers, and 27 hotel offers.
- The hotel nightly price was identical in both completed chains. The selected flight and total prices varied by `3.68 USD`, which is expected live Test Mode inventory variance and did not change policy compliance.
- Attempt 3 failed at the initial Duffel `POST /air/offer_requests` boundary with `Duffel POST request failed transiently: ConnectError`. LiteAPI was not called and no workflow retry occurred because this runner fixes `max_provider_attempts=1`.
- All attempts preserved the read-only boundary. No order, hotel prebook, booking, payment, or ticket endpoint was called, and provider secrets were not archived.

The observed end-to-end stability is therefore `2/3`, not release-grade `3/3`. The failure is isolated to transient connectivity at the Duffel boundary rather than a deterministic workflow assertion or a LiteAPI data inconsistency.

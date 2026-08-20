from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter_ns

from corporate_travel_agent.providers.base import HotelSearchQuery
from corporate_travel_agent.providers.liteapi import LiteAPIHotelProvider
from corporate_travel_agent.services.object_storage import LocalRawResponseObjectStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run one real LiteAPI hotel-rates query. This read-only smoke never calls "
            "prebook, book, payment, cancellation, or guest-data endpoints."
        )
    )
    parser.add_argument("--city", default="Shanghai")
    parser.add_argument("--check-in", type=date.fromisoformat)
    parser.add_argument("--check-out", type=date.fromisoformat)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm-external-test-call", action="store_true")
    args = parser.parse_args()

    if not args.confirm_external_test_call:
        parser.error("--confirm-external-test-call is required")
    api_key = os.getenv("LITEAPI_API_KEY", "").strip()
    if not api_key:
        parser.error("LITEAPI_API_KEY is required")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        parser.error("LIVE_BOOKING_ENABLED must remain false")
    if os.getenv("LITEAPI_BOOKING_ENABLED", "false").casefold() == "true":
        parser.error("LITEAPI_BOOKING_ENABLED must remain false")

    today = datetime.now(UTC).date()
    check_in = args.check_in or (today + timedelta(days=30))
    check_out = args.check_out or (check_in + timedelta(days=1))
    if check_in < today:
        parser.error("--check-in cannot be in the past")
    if check_out <= check_in:
        parser.error("--check-out must be after --check-in")

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw_store = LocalRawResponseObjectStore(output / "raw-provider-responses")
    provider = LiteAPIHotelProvider.from_environment(raw_response_store=raw_store)
    query = HotelSearchQuery(
        city=args.city,
        check_in=check_in,
        check_out=check_out,
    )
    started_at = datetime.now(UTC)
    started_ns = perf_counter_ns()
    snapshot = None
    failure: str | None = None
    try:
        snapshot = provider.search_hotels(query)
    except Exception as exc:  # preserve a safe diagnostic artifact for smoke failures
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        provider.close()
    completed_at = datetime.now(UTC)

    external_requests = [
        {"method": method, "path": path} for method, path in provider.external_request_log
    ]
    secret_exposed = _contains_secret(output, api_key.encode("utf-8"))
    checks = {
        "request_succeeded": failure is None and snapshot is not None,
        "exactly_one_external_call": provider.external_request_count == 1,
        "rates_endpoint_only": external_requests == [
            {"method": "POST", "path": "/hotels/rates"}
        ],
        "authorized_api_snapshot": snapshot is not None
        and snapshot.source_type.value == "AUTHORIZED_API",
        "hotel_inventory_returned": snapshot is not None and bool(snapshot.items),
        "raw_response_archived": snapshot is not None and snapshot.raw_response is not None,
        "sandbox_disclosed": snapshot is not None
        and (
            provider.provider_mode != "sandbox-read-only"
            or any("Sandbox" in warning for warning in snapshot.provider_warnings)
        ),
        "booking_and_payment_disabled": all(
            path["path"] not in {"/rates/prebook", "/rates/book"}
            for path in external_requests
        ),
        "api_key_not_archived": not secret_exposed,
    }
    passed = all(checks.values())
    result = {
        "schema_version": 1,
        "run_id": output.name,
        "evaluation_mode": "deterministic_live_hotel_provider_smoke",
        "provider": provider.name,
        "provider_mode": provider.provider_mode,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": round((perf_counter_ns() - started_ns) / 1_000_000, 3),
        "query": {
            "city": query.city,
            "check_in": query.check_in.isoformat(),
            "check_out": query.check_out.isoformat(),
        },
        "external_requests": external_requests,
        "failure": failure,
        "snapshot": (
            {
                "snapshot_id": snapshot.snapshot_id,
                "provider": snapshot.provider,
                "source_type": snapshot.source_type.value,
                "captured_at": snapshot.captured_at.isoformat(),
                "valid_until": snapshot.valid_until.isoformat(),
                "raw_payload_sha256": snapshot.raw_payload_hash,
                "warnings": snapshot.provider_warnings,
                "offers": [
                    {
                        "ref_id": offer.ref_id,
                        "name": offer.name,
                        "city": offer.city,
                        "nightly_price": str(offer.nightly_price),
                        "currency": offer.currency,
                        "commute_minutes": offer.commute_minutes,
                    }
                    for offer in snapshot.items
                ],
            }
            if snapshot is not None
            else None
        ),
        "checks": checks,
        "passed": passed,
    }
    result_path = output / "smoke-result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


def _contains_secret(root: Path, secret: bytes) -> bool:
    if not secret:
        return False
    return any(secret in path.read_bytes() for path in root.rglob("*") if path.is_file())


if __name__ == "__main__":
    main()

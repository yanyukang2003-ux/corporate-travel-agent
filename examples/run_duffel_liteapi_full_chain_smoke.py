from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from corporate_travel_agent.agent.orchestrator import (
    MAX_PROVIDER_ATTEMPTS,
)
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import (
    PolicyOutcome,
    RevalidationStatus,
    ToolCallStatus,
)
from corporate_travel_agent.domain.models import RevalidationResult, TripRequestVersion
from corporate_travel_agent.evaluation.full_chain import (
    duffel_read_sequence_valid,
    liteapi_read_sequence_valid,
    provider_retry_contract_valid,
)
from corporate_travel_agent.providers.composite import CompositeTravelInventoryProvider
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.providers.liteapi import LiteAPIHotelProvider
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.object_storage import LocalRawResponseObjectStore
from corporate_travel_agent.services.policy_config import load_policy_configuration


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run one real Duffel Test Mode flight + LiteAPI Sandbox hotel workflow: "
            "search, plan, select, revalidate, and instruction-only handoff."
        )
    )
    parser.add_argument(
        "--dataset",
        default="evals/subsets/duffel-liteapi-real-full-chain-v2.json",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--code-revision", default=None)
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    args = parser.parse_args()

    if not args.confirm_external_test_calls:
        parser.error("--confirm-external-test-calls is required")
    duffel_token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    liteapi_key = os.getenv("LITEAPI_API_KEY", "").strip()
    if not duffel_token.startswith("duffel_test_"):
        parser.error("DUFFEL_ACCESS_TOKEN must contain a Duffel Test Mode token")
    if not liteapi_key:
        parser.error("LITEAPI_API_KEY is required")
    if os.getenv("DUFFEL_LIVE_MODE", "false").casefold() == "true":
        parser.error("DUFFEL_LIVE_MODE must remain false")
    if os.getenv("LITEAPI_REQUIRE_SANDBOX", "true").casefold() != "true":
        parser.error("LITEAPI_REQUIRE_SANDBOX must remain true for this frozen case")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        parser.error("LIVE_BOOKING_ENABLED must remain false")
    if os.getenv("LITEAPI_BOOKING_ENABLED", "false").casefold() == "true":
        parser.error("LITEAPI_BOOKING_ENABLED must remain false")

    dataset_path = Path(args.dataset).resolve()
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    _validate_dataset(dataset)
    case = dataset["cases"][0]
    expected = case["expected"]
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw_store = LocalRawResponseObjectStore(output / "raw-provider-responses")
    policy_configuration = _policy_configuration(case, dataset_id=dataset["dataset_id"])
    duffel = DuffelProvider(
        duffel_token,
        api_version=os.getenv("DUFFEL_API_VERSION", "v2"),
        api_base_url=os.getenv("DUFFEL_API_BASE_URL", "https://api.duffel.com"),
        expected_currency=case["policy_currency"],
        raw_response_store=raw_store,
    )
    liteapi = LiteAPIHotelProvider(
        liteapi_key,
        api_base_url=os.getenv(
            "LITEAPI_API_BASE_URL", "https://api.liteapi.travel/v3.0"
        ),
        currency=case["policy_currency"],
        guest_nationality=case["guest_nationality"],
        require_sandbox=True,
        raw_response_store=raw_store,
    )
    provider = CompositeTravelInventoryProvider(
        transport_provider=duffel,
        hotel_provider=liteapi,
    )
    workflow, _ = build_demo_system(
        provider=provider,
        policy_configuration=policy_configuration,
    )
    request = _request_from_case(case)
    started_at = datetime.now(UTC)
    started_ns = perf_counter_ns()
    task = None
    selected = None
    selection_error: str | None = None
    unexpected_error: str | None = None
    try:
        task = workflow.create_task(request)
        selected = next(
            (
                option
                for option in task.options
                if option.hotel is not None
                and option.policy_decision.outcome is PolicyOutcome.COMPLIANT
            ),
            None,
        )
        if selected is None:
            selection_error = "No compliant flight-and-hotel option was available"
        else:
            task = workflow.select_option(task.task_id, selected.option_id)
    except Exception as exc:
        unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        provider.close()
    completed_at = datetime.now(UTC)

    snapshots = workflow.tasks.snapshots(request.task_id) if task is not None else ()
    flight_snapshot = next((item for item in snapshots if item.provider == "duffel"), None)
    hotel_snapshot = next((item for item in snapshots if item.provider == "liteapi"), None)
    flight_revalidation = duffel.last_revalidation_result
    hotel_revalidation = liteapi.last_revalidation_result
    effective_revalidation = _effective_revalidation(
        flight_revalidation,
        hotel_revalidation,
    )
    revalidation_status = (
        effective_revalidation.status.value if effective_revalidation is not None else None
    )
    provider_tool_records = (
        [item for item in task.tool_calls if item.tool_kind == "PROVIDER"]
        if task is not None
        else []
    )
    provider_tool_names = [item.tool_name for item in provider_tool_records]
    successful_provider_tool_names = [
        item.tool_name
        for item in provider_tool_records
        if item.status is ToolCallStatus.SUCCEEDED
    ]
    provider_retry_count = sum(item.retry_of is not None for item in provider_tool_records)
    failed_provider_attempt_count = sum(
        item.status is ToolCallStatus.FAILED for item in provider_tool_records
    )
    duffel_requests = [
        {"provider": "duffel", "method": method, "path": path}
        for method, path in duffel.external_request_log
    ]
    liteapi_requests = [
        {"provider": "liteapi", "method": method, "path": path}
        for method, path in liteapi.external_request_log
    ]
    total_external_calls = duffel.external_request_count + liteapi.external_request_count
    handoff = task.booking_intent.handoff if task and task.booking_intent else None
    secret_exposed = _contains_secret(output, (duffel_token, liteapi_key))
    selected_flight_ref = selected.outbound.ref_id if selected is not None else None
    selected_hotel_ref = selected.hotel.ref_id if selected and selected.hotel else None
    allowed_statuses = expected["allowed_revalidation_statuses"]
    expected_tools = expected["tool_sequences_by_revalidation_status"].get(
        revalidation_status, []
    )
    expected_state = expected["final_states_by_revalidation_status"].get(
        revalidation_status
    )
    expected_booking_intent = expected["booking_intent_by_revalidation_status"].get(
        revalidation_status
    )
    checks = {
        "workflow_completed_without_exception": unexpected_error is None,
        "selection_succeeded": selection_error is None and selected is not None,
        "default_retry_policy_applied": workflow.max_provider_attempts
        == dataset["maximum_provider_attempts_per_operation"],
        "external_call_budget_respected": total_external_calls
        <= dataset["maximum_external_calls"],
        "duffel_call_budget_respected": duffel.external_request_count
        <= dataset["maximum_duffel_calls"],
        "liteapi_call_budget_respected": liteapi.external_request_count
        <= dataset["maximum_liteapi_calls"],
        "provider_retry_contract_valid": provider_retry_contract_valid(
            provider_tool_records,
            max_attempts=workflow.max_provider_attempts,
        ),
        "duffel_search_then_revalidate": duffel_read_sequence_valid(
            duffel_requests,
            selected_flight_ref=selected_flight_ref,
            max_attempts=workflow.max_provider_attempts,
        ),
        "liteapi_search_then_quote_refresh": liteapi_read_sequence_valid(
            liteapi_requests,
            max_attempts=workflow.max_provider_attempts,
        ),
        "flight_snapshot_authorized": flight_snapshot is not None
        and flight_snapshot.source_type.value == expected["provider_source_type"]
        and len(flight_snapshot.items) >= expected["minimum_flight_offers"],
        "hotel_snapshot_authorized": hotel_snapshot is not None
        and hotel_snapshot.source_type.value == expected["provider_source_type"]
        and len(hotel_snapshot.items) >= expected["minimum_hotel_offers"],
        "minimum_combined_options": task is not None
        and len(task.options) >= expected["minimum_options"],
        "selected_option_has_both_providers": selected is not None
        and selected.hotel is not None
        and selected.outbound.provider == "duffel"
        and selected.hotel.provider == "liteapi",
        "selected_prices_use_policy_currency": selected is not None
        and selected.hotel is not None
        and selected.outbound.currency == case["policy_currency"]
        and selected.hotel.currency == case["policy_currency"],
        "selected_option_policy_compliant": selected is not None
        and selected.policy_decision.outcome is PolicyOutcome.COMPLIANT,
        "both_quotes_revalidated": flight_revalidation is not None
        and hotel_revalidation is not None
        and selected_flight_ref in flight_revalidation.current_prices
        and selected_hotel_ref in hotel_revalidation.current_prices,
        "allowed_revalidation_status": revalidation_status in allowed_statuses,
        "expected_provider_tool_sequence": successful_provider_tool_names
        == expected_tools,
        "conditional_final_state": task is not None
        and task.state.value == expected_state,
        "conditional_booking_intent": task is not None
        and (task.booking_intent is not None) == expected_booking_intent,
        "unchanged_handoff_covers_flight_and_hotel": revalidation_status != "UNCHANGED"
        or (
            handoff is not None
            and "no order was created" in handoff.url_or_instructions
            and "no prebook, booking, or payment was created" in handoff.url_or_instructions
        ),
        "search_and_revalidation_archived": duffel.last_raw_response is not None
        and liteapi.last_raw_response is not None
        and len(duffel.revalidation_raw_responses) == 1
        and len(liteapi.revalidation_raw_responses) == 1,
        "no_booking_payment_or_order_http": all(
            forbidden not in request_item["path"]
            for request_item in (*duffel_requests, *liteapi_requests)
            for forbidden in ("/orders", "/payments", "/rates/prebook", "/rates/book")
        ),
        "provider_secrets_not_archived": not secret_exposed,
    }
    passed = all(checks.values())
    result = {
        "schema_version": 1,
        "run_id": output.name,
        "evaluation_mode": "deterministic_live_duffel_liteapi_full_chain",
        "provider": provider.name,
        "provider_mode": provider.provider_mode,
        "dataset": {
            "id": dataset["dataset_id"],
            "version": dataset["dataset_version"],
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "case_id": case["case_id"],
        },
        "code_revision": args.code_revision,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": round((perf_counter_ns() - started_ns) / 1_000_000, 3),
        "request": {
            "origin": request.origin,
            "destination": request.destination,
            "departure_after": request.departure_after.isoformat(),
            "arrive_by": request.arrive_by.isoformat(),
            "hotel_check_in": request.hotel_check_in.isoformat(),
            "hotel_check_out": request.hotel_check_out.isoformat(),
            "currency": case["policy_currency"],
        },
        "external_calls": {
            "total": total_external_calls,
            "duffel": duffel.external_request_count,
            "liteapi": liteapi.external_request_count,
            "base": dataset["base_external_calls"],
            "maximum": dataset["maximum_external_calls"],
        },
        "external_requests_by_provider": {
            "duffel": duffel_requests,
            "liteapi": liteapi_requests,
        },
        "provider_tool_calls": provider_tool_names,
        "successful_provider_tool_calls": successful_provider_tool_names,
        "provider_tool_call_records": [
            {
                "sequence": item.sequence,
                "tool_name": item.tool_name,
                "status": item.status.value,
                "retryable": item.retryable,
                "retry_of": item.retry_of,
                "reason_code": item.reason_code,
                "error_code": item.error_code,
                "error_layer": item.error_layer,
            }
            for item in provider_tool_records
        ],
        "retry_policy": {
            "max_provider_attempts": workflow.max_provider_attempts,
            "backoff_base_seconds": workflow.retry_backoff_base_seconds,
            "provider_retries": provider_retry_count,
            "failed_provider_attempts": failed_provider_attempt_count,
        },
        "task_state": task.state.value if task is not None else None,
        "task_failure": task.failure if task is not None else None,
        "option_count": len(task.options) if task is not None else 0,
        "selection_error": selection_error,
        "unexpected_error": unexpected_error,
        "selected_option": (
            {
                "option_id": selected.option_id,
                "flight_ref": selected_flight_ref,
                "flight_price": str(selected.outbound.price),
                "hotel_ref": selected_hotel_ref,
                "hotel_name": selected.hotel.name if selected.hotel else None,
                "hotel_nightly_price": (
                    str(selected.hotel.nightly_price) if selected.hotel else None
                ),
                "hotel_nights": selected.hotel.nights if selected.hotel else None,
                "total_cost": str(selected.total_cost),
                "currency": selected.currency,
                "policy_outcome": selected.policy_decision.outcome.value,
            }
            if selected is not None
            else None
        ),
        "revalidation": (
            {
                "status": effective_revalidation.status.value,
                "checked_at": effective_revalidation.checked_at.isoformat(),
                "current_prices": {
                    ref: str(price)
                    for ref, price in effective_revalidation.current_prices.items()
                },
                "warnings": effective_revalidation.warnings,
            }
            if effective_revalidation is not None
            else None
        ),
        "handoff": (
            {
                "provider": handoff.provider,
                "instructions": handoff.url_or_instructions,
                "expires_at": handoff.expires_at.isoformat(),
            }
            if handoff is not None
            else None
        ),
        "snapshots": [
            {
                "snapshot_id": snapshot.snapshot_id,
                "provider": snapshot.provider,
                "source_type": snapshot.source_type.value,
                "item_count": len(snapshot.items),
                "raw_payload_sha256": snapshot.raw_payload_hash,
                "raw_response_archived": snapshot.raw_response is not None,
                "warnings": snapshot.provider_warnings,
            }
            for snapshot in snapshots
        ],
        "checks": checks,
        "passed": passed,
    }
    (output / "evaluation-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "evaluation-report.md").write_text(
        _render_report(result),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


def _validate_dataset(dataset: dict[str, Any]) -> None:
    if dataset.get("status") != "frozen":
        raise SystemExit("Full-chain dataset must be frozen")
    cases = dataset.get("cases")
    if dataset.get("records") != 1 or not isinstance(cases, list) or len(cases) != 1:
        raise SystemExit("Full-chain dataset must contain exactly one case")
    if dataset.get("provider") != "duffel+liteapi":
        raise SystemExit("Full-chain dataset must use duffel+liteapi")
    if dataset.get("base_external_calls") != 4:
        raise SystemExit("Full-chain dataset must declare the four-call baseline")
    if dataset.get("maximum_provider_attempts_per_operation") != MAX_PROVIDER_ATTEMPTS:
        raise SystemExit(
            "Full-chain dataset retry budget must match the system provider-attempt default"
        )
    if dataset.get("maximum_external_calls") != 4 * MAX_PROVIDER_ATTEMPTS:
        raise SystemExit("Full-chain dataset must budget all bounded read retries")
    if dataset.get("maximum_duffel_calls") != 2 * MAX_PROVIDER_ATTEMPTS:
        raise SystemExit("Full-chain dataset must budget bounded Duffel retries")
    if dataset.get("maximum_liteapi_calls") != 2 * MAX_PROVIDER_ATTEMPTS:
        raise SystemExit("Full-chain dataset must budget bounded LiteAPI retries")
    if dataset.get("booking_enabled") is not False:
        raise SystemExit("Full-chain dataset must keep booking disabled")


def _policy_configuration(case: dict[str, Any], *, dataset_id: str):
    loaded = load_policy_configuration()
    cap = Decimal(case["hotel_nightly_cap"])
    policies = tuple(
        replace(
            policy,
            currency=case["policy_currency"],
            hotel_city_caps={**policy.hotel_city_caps, case["destination"]: cap},
            content_hash=stable_hash(
                {
                    "source_policy_hash": policy.content_hash,
                    "evaluation_currency": case["policy_currency"],
                    "evaluation_hotel_city": case["destination"],
                    "evaluation_hotel_cap": str(cap),
                    "purpose": dataset_id,
                }
            ),
        )
        for policy in loaded.policy_snapshots
    )
    return replace(loaded, policy_snapshots=policies)


def _request_from_case(case: dict[str, Any]) -> TripRequestVersion:
    return TripRequestVersion(
        task_id=f"{case['case_id']}-attempt-1",
        version=1,
        traveler_id=case["traveler_id"],
        origin=case["origin"],
        destination=case["destination"],
        departure_after=datetime.fromisoformat(case["departure_after"]),
        arrive_by=datetime.fromisoformat(case["arrive_by"]),
        return_after=None,
        return_before=None,
        hotel_check_in=date.fromisoformat(case["hotel_check_in"]),
        hotel_check_out=date.fromisoformat(case["hotel_check_out"]),
        hard_constraints=tuple(case["hard_constraints"]),
        soft_preferences=tuple(case["soft_preferences"]),
    )


def _effective_revalidation(
    flight: RevalidationResult | None,
    hotel: RevalidationResult | None,
) -> RevalidationResult | None:
    if flight is None or hotel is None:
        return None
    statuses = {flight.status, hotel.status}
    status = RevalidationStatus.UNCHANGED
    if RevalidationStatus.PROVIDER_FAILED in statuses:
        status = RevalidationStatus.PROVIDER_FAILED
    elif RevalidationStatus.UNAVAILABLE in statuses:
        status = RevalidationStatus.UNAVAILABLE
    elif RevalidationStatus.PRICE_CHANGED in statuses:
        status = RevalidationStatus.PRICE_CHANGED
    return RevalidationResult(
        status=status,
        checked_at=max(flight.checked_at, hotel.checked_at),
        current_prices={**flight.current_prices, **hotel.current_prices},
        unavailable_refs=(*flight.unavailable_refs, *hotel.unavailable_refs),
        warnings=(*flight.warnings, *hotel.warnings),
    )


def _contains_secret(root: Path, secrets: tuple[str, ...]) -> bool:
    encoded = tuple(secret.encode("utf-8") for secret in secrets if secret)
    return any(
        marker in path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        for marker in encoded
    )


def _render_report(result: dict[str, Any]) -> str:
    status = "PASS" if result["passed"] else "FAIL"
    checks = "\n".join(
        f"- {'PASS' if passed else 'FAIL'} `{name}`"
        for name, passed in result["checks"].items()
    )
    selected = result["selected_option"] or {}
    revalidation = result["revalidation"] or {}
    hotel_name = selected.get("hotel_name", "n/a")
    hotel_nightly_price = selected.get("hotel_nightly_price", "n/a")
    currency = selected.get("currency", "")
    external_calls = result["external_calls"]
    retry_policy = result["retry_policy"]
    external_call_summary = (
        f"`{external_calls['total']}` / `{external_calls['maximum']}` maximum "
        f"(`{external_calls['base']}` baseline)"
    )
    provider_retry_summary = (
        f"`{retry_policy['provider_retries']}` / "
        f"`{retry_policy['max_provider_attempts'] - 1}` maximum per operation"
    )
    return f"""# Duffel + LiteAPI real full-chain smoke

- Result: **{status}**
- Provider: `{result['provider']}` / `{result['provider_mode']}`
- Route: `{result['request']['origin']} -> {result['request']['destination']}`
- Hotel dates: `{result['request']['hotel_check_in']}..{result['request']['hotel_check_out']}`
- External calls: {external_call_summary}
- Provider retries: {provider_retry_summary}
- Final state: `{result['task_state']}`
- Task failure: `{result['task_failure'] or 'none'}`
- Revalidation: `{revalidation.get('status', 'not_run')}`
- Flight price: `{selected.get('flight_price', 'n/a')} {currency}`
- Hotel: `{hotel_name}` at `{hotel_nightly_price} {currency}` per night
- Total: `{selected.get('total_cost', 'n/a')} {currency}`

## Checks

{checks}

This is a read-only provider workflow. Duffel inventory is Test Mode and LiteAPI
inventory is Sandbox data. The run creates no airline order, hotel prebook,
booking, payment, ticket, or reservation. Any BookingIntent is local
instruction-only handoff state.
"""


if __name__ == "__main__":
    main()

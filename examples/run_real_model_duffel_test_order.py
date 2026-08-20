from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from corporate_travel_agent.agent.openai_adapter import OpenAIResponsesLanguageModel
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.providers.duffel_test_order import (
    CANCEL_TEST_ORDER_CONFIRMATION,
    CREATE_TEST_ORDER_CONFIRMATION,
    DuffelTestOrderClient,
    SyntheticTestPassenger,
    TestOrderCancellation,
    TestOrderRecord,
)
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_performance import load_model_price_table
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationTraceRecorder,
    TraceFinal,
    TraceFingerprint,
)
from corporate_travel_agent.services.object_storage import LocalRawResponseObjectStore
from corporate_travel_agent.services.policy_config import load_policy_configuration

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "evals/subsets/model-duffel-test-order-e2e-v1.json"
_SECRET_MARKERS = (
    re.compile(rb"duffel_test_[A-Za-z0-9_-]+"),
    re.compile(rb"Authorization", re.IGNORECASE),
    re.compile(rb"Bearer\s+", re.IGNORECASE),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run one real model + Duffel Test Mode search, revalidation, order, "
            "readback, cancellation, and final readback. Live tokens are forbidden."
        )
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--confirm-billable-model-call", action="store_true")
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    parser.add_argument("--confirm-test-order-write", action="store_true")
    parser.add_argument("--confirm-test-order-cancellation", action="store_true")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    _validate_dataset(dataset)
    _validate_runtime(parser, args, dataset)
    case = dataset["cases"][0]

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw_store = LocalRawResponseObjectStore(output / "raw-provider-responses")
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    price_table_path = (PROJECT_ROOT / dataset["price_table"]["path"]).resolve()
    price_table, price_table_sha256 = load_model_price_table(price_table_path)
    if price_table_sha256 != dataset["price_table"]["sha256"]:
        raise SystemExit("Frozen price table SHA-256 does not match D14")

    from openai import OpenAI

    model_client = OpenAI(max_retries=0, timeout=60.0)
    language_model = OpenAIResponsesLanguageModel(
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        max_output_tokens=dataset["max_output_tokens_per_call"],
        client=model_client,
    )
    token = os.environ["DUFFEL_ACCESS_TOKEN"].strip()
    provider = DuffelProvider(
        token,
        api_version=os.getenv("DUFFEL_API_VERSION", "v2"),
        api_base_url=os.getenv("DUFFEL_API_BASE_URL", "https://api.duffel.com"),
        expected_currency=case["policy_currency"],
        raw_response_store=raw_store,
        required_owner_name=dataset["required_provider_owner"],
    )
    order_client = DuffelTestOrderClient(
        token,
        test_order_writes_enabled=True,
        api_version=os.getenv("DUFFEL_API_VERSION", "v2"),
        api_base_url=os.getenv("DUFFEL_API_BASE_URL", "https://api.duffel.com"),
        raw_response_store=raw_store,
    )
    recorder = EvaluationTraceRecorder(
        run_id=output.name,
        case_id=case["case_id"],
        attempt=1,
        evaluation_mode=dataset["evaluation_mode"],
        fingerprint=TraceFingerprint(
            project_version="0.1.0",
            code_revision=args.code_revision,
            dataset_id=dataset["dataset_id"],
            dataset_version=dataset["dataset_version"],
            dataset_sha256=dataset_sha256,
            prompt_version=language_model.prompt_version,
            requested_model=args.model,
            actual_model=None,
            price_table_version=price_table.price_table_version,
        ),
        tool_choice_exposure="orchestrator_controlled",
    )
    workflow, _ = build_demo_system(
        language_model=language_model,
        provider=provider,
        policy_configuration=_policy_configuration_for_currency(
            case["policy_currency"],
            dataset_id=dataset["dataset_id"],
        ),
        trace_observer=recorder,
        max_tool_calls=dataset["max_workflow_tool_calls"],
        max_llm_attempts=1,
        max_provider_attempts=1,
    )

    started_at = datetime.now(UTC)
    started_ns = perf_counter_ns()
    task = None
    selected_ref: str | None = None
    selected_offer_owner: str | None = None
    created_order: TestOrderRecord | None = None
    observed_order: TestOrderRecord | None = None
    cancellation_quote: TestOrderCancellation | None = None
    confirmed_cancellation: TestOrderCancellation | None = None
    final_order: TestOrderRecord | None = None
    cancellation_attempted = False
    cleanup_status = "not_needed"
    failure: str | None = None
    try:
        task = workflow.create_task_from_message(
            case["message"],
            traveler_id=case["traveler_id"],
            task_id=f"{case['case_id']}-attempt-1",
        )
        if task.state is not TaskState.WAITING_FOR_USER:
            raise RuntimeError(f"Workflow stopped before selection in {task.state.value}")
        selected = next(
            (
                option
                for option in task.options
                if option.policy_decision.outcome is PolicyOutcome.COMPLIANT
            ),
            None,
        )
        if selected is None:
            raise RuntimeError("No compliant Duffel Airways option was available")
        selected_ref = selected.outbound.ref_id
        task = workflow.select_option(task.task_id, selected.option_id)
        if task.state is not TaskState.READY_FOR_HANDOFF:
            raise RuntimeError(f"Workflow stopped before handoff in {task.state.value}")
        offer = provider.test_order_offer(selected_ref)
        selected_offer_owner = offer.owner_name
        created_order = order_client.create_order(
            offer,
            _synthetic_passenger(case["synthetic_passenger"]),
            confirmation=CREATE_TEST_ORDER_CONFIRMATION,
        )
        observed_order = order_client.get_order(created_order.order_id)
        task = workflow.mark_handed_off(task.task_id)
        cancellation_attempted = True
        cancellation_quote = order_client.create_cancellation(
            observed_order,
            confirmation=CANCEL_TEST_ORDER_CONFIRMATION,
        )
        confirmed_cancellation = order_client.confirm_cancellation(
            cancellation_quote,
            confirmation=CANCEL_TEST_ORDER_CONFIRMATION,
        )
        final_order = order_client.get_order(
            created_order.order_id,
            category="order-final",
        )
        cleanup_status = "cancelled"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        if (
            created_order is not None
            and confirmed_cancellation is None
            and not cancellation_attempted
            and "cancel" in created_order.available_actions
        ):
            try:
                cleanup_status = "best_effort_started"
                cancellation_quote = order_client.create_cancellation(
                    created_order,
                    confirmation=CANCEL_TEST_ORDER_CONFIRMATION,
                )
                confirmed_cancellation = order_client.confirm_cancellation(
                    cancellation_quote,
                    confirmation=CANCEL_TEST_ORDER_CONFIRMATION,
                )
                final_order = order_client.get_order(
                    created_order.order_id,
                    category="order-final",
                )
                cleanup_status = "cancelled_after_failure"
            except Exception as cleanup_exc:
                cleanup_status = (
                    f"cleanup_failed:{type(cleanup_exc).__name__}:{str(cleanup_exc)[:240]}"
                )
        provider.close()
        order_client.close()

    completed_at = datetime.now(UTC)
    duration_ms = (perf_counter_ns() - started_ns) / 1_000_000
    trace = None
    trace_error: str | None = None
    try:
        trace = recorder.finish(
            TraceFinal(
                state=task.state.value if task is not None else "RUNNER_FAILED",
                policy_outcome=(
                    task.selected_option().policy_decision.outcome.value
                    if task is not None and task.selected_option() is not None
                    else None
                ),
                booking_allowed=created_order is not None,
                result_refs=(selected_ref,) if selected_ref is not None else (),
                failure_reason=failure,
            )
        )
    except Exception as exc:
        trace_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    result = _build_result(
        dataset=dataset,
        dataset_sha256=dataset_sha256,
        case=case,
        task=task,
        selected_ref=selected_ref,
        selected_offer_owner=selected_offer_owner,
        provider=provider,
        order_client=order_client,
        created_order=created_order,
        observed_order=observed_order,
        cancellation_quote=cancellation_quote,
        confirmed_cancellation=confirmed_cancellation,
        final_order=final_order,
        cleanup_status=cleanup_status,
        language_model=language_model,
        price_table=price_table,
        price_table_sha256=price_table_sha256,
        failure=failure,
        trace_error=trace_error,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        output=output,
    )
    if trace is not None:
        _write_private(output / "trace.json", trace.model_dump_json(indent=2))
    _write_private(
        output / "evaluation-result.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    _write_private(output / "evaluation-report.md", _render_report(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


def _build_result(
    *,
    dataset: dict[str, Any],
    dataset_sha256: str,
    case: dict[str, Any],
    task: Any,
    selected_ref: str | None,
    selected_offer_owner: str | None,
    provider: DuffelProvider,
    order_client: DuffelTestOrderClient,
    created_order: TestOrderRecord | None,
    observed_order: TestOrderRecord | None,
    cancellation_quote: TestOrderCancellation | None,
    confirmed_cancellation: TestOrderCancellation | None,
    final_order: TestOrderRecord | None,
    cleanup_status: str,
    language_model: OpenAIResponsesLanguageModel,
    price_table: Any,
    price_table_sha256: str,
    failure: str | None,
    trace_error: str | None,
    started_at: datetime,
    completed_at: datetime,
    duration_ms: float,
    output: Path,
) -> dict[str, Any]:
    expected = case["expected"]
    workflow_tools = [call.tool_name for call in task.tool_calls] if task is not None else []
    llm_calls = [name for name in workflow_tools if name == "llm.extract_trip_intent"]
    provider_tools = [name for name in workflow_tools if name.startswith("provider.")]
    provider_http = _normalize_paths(provider.external_request_log)
    order_http = _normalize_paths(order_client.external_request_log)
    metadata = language_model.last_call_metadata
    usage = {
        "input_tokens": metadata.input_tokens if metadata is not None else None,
        "output_tokens": metadata.output_tokens if metadata is not None else None,
        "total_tokens": metadata.total_tokens if metadata is not None else None,
        "cached_input_tokens": metadata.cached_input_tokens if metadata is not None else None,
        "cache_write_input_tokens": (
            metadata.cache_write_input_tokens if metadata is not None else None
        ),
        "reasoning_output_tokens": (
            metadata.reasoning_output_tokens if metadata is not None else None
        ),
    }
    actual_model = metadata.model if metadata is not None else None
    estimated_cost = _estimated_cost(usage, actual_model, dataset["requested_model"], price_table)
    secret_marker_paths = _secret_marker_paths(output)
    intent_matches = task is not None and _intent_matches(task.request, case["expected_intent"])
    total_duffel_calls = provider.external_request_count + order_client.external_request_count
    checks = {
        "runner_completed_without_failure": failure is None,
        "workflow_trace_complete": trace_error is None,
        "one_real_model_call": len(llm_calls) == dataset["maximum_model_calls"],
        "actual_model_matches_frozen_model": actual_model == dataset["requested_model"],
        "model_usage_complete": all(
            usage[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")
        ),
        "model_cost_within_ceiling": estimated_cost is not None
        and estimated_cost <= dataset["maximum_estimated_model_cost_usd"],
        "intent_matches_frozen_case": intent_matches,
        "workflow_tool_sequence": workflow_tools == expected["workflow_tool_sequence"],
        "workflow_provider_call_cap": len(provider_tools)
        == dataset["maximum_workflow_provider_calls"],
        "provider_http_sequence": provider_http == expected["provider_http_sequence"],
        "provider_http_call_cap": provider.external_request_count
        == dataset["maximum_provider_http_calls"],
        "revalidation_unchanged": provider.last_revalidation_result is not None
        and provider.last_revalidation_result.status.value == expected["revalidation_status"],
        "duffel_airways_offer_selected": selected_ref is not None
        and selected_offer_owner == dataset["required_provider_owner"],
        "workflow_handed_off": task is not None
        and task.state.value == expected["final_workflow_state"],
        "internal_booking_intent_created": task is not None and task.booking_intent is not None,
        "test_order_created": created_order is not None
        and created_order.live_mode is expected["order_live_mode"],
        "test_order_read_back": created_order is not None
        and observed_order is not None
        and observed_order.order_id == created_order.order_id,
        "test_order_http_sequence": order_http == expected["test_order_http_sequence"],
        "test_order_http_call_cap": order_client.external_request_count
        == dataset["maximum_test_order_http_calls"],
        "total_duffel_call_cap": total_duffel_calls == dataset["maximum_total_duffel_http_calls"],
        "cancellation_quote_test_mode": cancellation_quote is not None
        and cancellation_quote.live_mode is expected["cancellation_live_mode"],
        "cancellation_confirmed": confirmed_cancellation is not None
        and (confirmed_cancellation.confirmed_at is not None) is expected["cancellation_confirmed"],
        "final_order_cancelled": final_order is not None
        and final_order.cancelled is expected["final_order_cancelled"],
        "cleanup_completed": cleanup_status == "cancelled",
        "all_external_responses_archived": len(provider.revalidation_raw_responses) == 1
        and provider.last_raw_response is not None
        and len(order_client.raw_responses) == dataset["maximum_test_order_http_calls"],
        "write_payload_hashes_recorded": len(order_client.request_payload_hashes) == 2,
        "no_secret_markers": not secret_marker_paths,
        "live_booking_remained_disabled": os.getenv("LIVE_BOOKING_ENABLED", "false").casefold()
        != "true",
    }
    return {
        "schema_version": 1,
        "run_id": output.name,
        "evaluation_mode": dataset["evaluation_mode"],
        "dataset": {
            "id": dataset["dataset_id"],
            "version": dataset["dataset_version"],
            "sha256": dataset_sha256,
            "case_id": case["case_id"],
        },
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": round(duration_ms, 3),
        "model": {
            "requested": dataset["requested_model"],
            "actual": actual_model,
            "reasoning_effort": dataset["reasoning_effort"],
            "prompt_version": language_model.prompt_version,
            "usage": usage,
            "estimated_cost_usd": estimated_cost,
            "price_table_version": price_table.price_table_version,
            "price_table_sha256": price_table_sha256,
        },
        "workflow": {
            "state": task.state.value if task is not None else None,
            "tools": workflow_tools,
            "selected_offer_ref": selected_ref,
            "booking_intent_created": task is not None and task.booking_intent is not None,
        },
        "provider": {
            "http_calls": provider.external_request_count,
            "http_sequence": provider_http,
            "revalidation_status": (
                provider.last_revalidation_result.status.value
                if provider.last_revalidation_result is not None
                else None
            ),
            "search_response_archived": provider.last_raw_response is not None,
            "revalidation_responses_archived": len(provider.revalidation_raw_responses),
        },
        "test_order": {
            "created": created_order is not None,
            "order_id": created_order.order_id if created_order is not None else None,
            "live_mode": created_order.live_mode if created_order is not None else None,
            "read_back": observed_order is not None,
            "available_actions": (
                observed_order.available_actions if observed_order is not None else ()
            ),
            "http_calls": order_client.external_request_count,
            "http_sequence": order_http,
            "raw_responses_archived": len(order_client.raw_responses),
            "request_payload_sha256": order_client.request_payload_hashes,
        },
        "cancellation": {
            "quote_created": cancellation_quote is not None,
            "cancellation_id": (
                cancellation_quote.cancellation_id if cancellation_quote is not None else None
            ),
            "refund_amount": (
                str(cancellation_quote.refund_amount)
                if cancellation_quote is not None and cancellation_quote.refund_amount is not None
                else None
            ),
            "refund_currency": (
                cancellation_quote.refund_currency if cancellation_quote is not None else None
            ),
            "refund_to": (cancellation_quote.refund_to if cancellation_quote is not None else None),
            "confirmed": confirmed_cancellation is not None
            and confirmed_cancellation.confirmed_at is not None,
            "final_order_cancelled": final_order is not None and final_order.cancelled,
            "cleanup_status": cleanup_status,
        },
        "external_calls": {
            "total": total_duffel_calls,
            "model": len(llm_calls),
            "duffel_read_or_search": provider.external_request_count,
            "duffel_order_or_cleanup": order_client.external_request_count,
        },
        "evidence": {
            "raw_response_archives": int(provider.last_raw_response is not None)
            + len(provider.revalidation_raw_responses)
            + len(order_client.raw_responses),
            "secret_marker_paths": secret_marker_paths,
            "synthetic_passenger_disclosed": case["synthetic_passenger"]["synthetic"] is True,
            "synthetic_passenger_values_excluded_from_result": True,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "failure": failure,
        "trace_error": trace_error,
        "limitations": dataset["limitations"],
    }


def _validate_runtime(parser: argparse.ArgumentParser, args: Any, dataset: dict[str, Any]) -> None:
    required_flags = {
        "--confirm-billable-model-call": args.confirm_billable_model_call,
        "--confirm-external-test-calls": args.confirm_external_test_calls,
        "--confirm-test-order-write": args.confirm_test_order_write,
        "--confirm-test-order-cancellation": args.confirm_test_order_cancellation,
    }
    missing = [name for name, enabled in required_flags.items() if not enabled]
    if missing:
        parser.error("required confirmations missing: " + ", ".join(missing))
    if not os.getenv("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY is required")
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    if not token.startswith("duffel_test_"):
        parser.error("DUFFEL_ACCESS_TOKEN must contain a Duffel Test Mode token")
    if os.getenv("DUFFEL_LIVE_MODE", "false").casefold() == "true":
        parser.error("DUFFEL_LIVE_MODE must remain false")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        parser.error("LIVE_BOOKING_ENABLED must remain false")
    if os.getenv("DUFFEL_TEST_ORDER_WRITES_ENABLED", "false").casefold() != "true":
        parser.error("DUFFEL_TEST_ORDER_WRITES_ENABLED=true is required")
    if args.model != dataset["requested_model"]:
        parser.error(f"model must be {dataset['requested_model']!r}")
    if args.reasoning_effort != dataset["reasoning_effort"]:
        parser.error(f"reasoning effort must be {dataset['reasoning_effort']!r}")


def _validate_dataset(dataset: dict[str, Any]) -> None:
    if dataset.get("dataset_id") != "model-duffel-test-order-e2e-v1":
        raise SystemExit("Test Order runner requires the frozen D14 dataset")
    if dataset.get("status") != "frozen" or dataset.get("records") != 1:
        raise SystemExit("D14 must be frozen and contain exactly one case")
    if dataset.get("provider") != "duffel" or dataset.get("provider_mode") != "test":
        raise SystemExit("D14 must use Duffel Test Mode")
    if dataset.get("live_booking_enabled") is not False:
        raise SystemExit("D14 must keep live booking disabled")
    if dataset.get("test_order_enabled") is not True:
        raise SystemExit("D14 must explicitly enable one Test Mode Order")
    if dataset.get("test_order_cancellation_required") is not True:
        raise SystemExit("D14 must require Test Order cancellation")
    if dataset.get("attempts_per_case") != 1:
        raise SystemExit("D14 is a one-attempt external mutation smoke")


def _synthetic_passenger(value: dict[str, Any]) -> SyntheticTestPassenger:
    return SyntheticTestPassenger(
        given_name=value["given_name"],
        family_name=value["family_name"],
        born_on=date.fromisoformat(value["born_on"]),
        gender=value["gender"],
        title=value["title"],
        email=value["email"],
        phone_number=value["phone_number"],
        synthetic=value["synthetic"],
    )


def _policy_configuration_for_currency(currency: str, *, dataset_id: str):
    loaded = load_policy_configuration()
    policies = tuple(
        replace(
            policy,
            currency=currency,
            content_hash=stable_hash(
                {
                    "source_policy_hash": policy.content_hash,
                    "evaluation_currency": currency,
                    "purpose": dataset_id,
                }
            ),
        )
        for policy in loaded.policy_snapshots
    )
    return replace(loaded, policy_snapshots=policies)


def _intent_matches(request: Any, expected: dict[str, Any]) -> bool:
    if request is None:
        return False
    actual = {
        "origin": request.origin,
        "destination": request.destination,
        "departure_after": request.departure_after.isoformat(),
        "arrive_by": request.arrive_by.isoformat(),
        "return_after": request.return_after.isoformat() if request.return_after else None,
        "return_before": request.return_before.isoformat() if request.return_before else None,
        "hotel_check_in": request.hotel_check_in.isoformat() if request.hotel_check_in else None,
        "hotel_check_out": request.hotel_check_out.isoformat() if request.hotel_check_out else None,
        "hard_constraints": list(request.hard_constraints),
        "soft_preferences": list(request.soft_preferences),
    }
    return actual == expected


def _normalize_paths(requests: list[tuple[str, str]]) -> list[str]:
    normalized: list[str] = []
    for method, path in requests:
        path = re.sub(r"/air/offers/off_[^/]+$", "/air/offers/{offer_id}", path)
        path = re.sub(r"/air/orders/ord_[^/]+$", "/air/orders/{order_id}", path)
        path = re.sub(
            r"/air/order_cancellations/ore_[^/]+/actions/confirm$",
            "/air/order_cancellations/{cancellation_id}/actions/confirm",
            path,
        )
        normalized.append(f"{method} {path}")
    return normalized


def _estimated_cost(
    usage: dict[str, int | None],
    actual_model: str | None,
    requested_model: str,
    price_table: Any,
) -> float | None:
    if usage["input_tokens"] is None or usage["output_tokens"] is None:
        return None
    price_key = actual_model if actual_model in price_table.models else requested_model
    if price_key not in price_table.models:
        return None
    price = price_table.models[price_key]
    cost = (
        int(usage["input_tokens"]) * price.input + int(usage["output_tokens"]) * price.output
    ) / 1_000_000
    return round(cost, 9)


def _secret_marker_paths(root: Path) -> list[str]:
    matches: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        content = path.read_bytes()
        if any(pattern.search(content) for pattern in _SECRET_MARKERS):
            matches.append(str(path.relative_to(root)))
    return matches


def _render_report(result: dict[str, Any]) -> str:
    status = "PASS" if result["passed"] else "FAIL"
    checks = "\n".join(
        f"- {'PASS' if passed else 'FAIL'} `{name}`" for name, passed in result["checks"].items()
    )
    order = result["test_order"]
    cancellation = result["cancellation"]
    return f"""# Real model + Duffel Test Order end-to-end smoke

- Result: **{status}**
- Model: `{result["model"]["actual"]}`
- Dataset: `{result["dataset"]["id"]}` `{result["dataset"]["version"]}`
- Dataset SHA-256: `{result["dataset"]["sha256"]}`
- Workflow state: `{result["workflow"]["state"]}`
- Duffel HTTP calls: `{result["external_calls"]["total"]}`
- Test Order created: `{str(order["created"]).lower()}`
- Test Order live mode: `{order["live_mode"]}`
- Cancellation confirmed: `{str(cancellation["confirmed"]).lower()}`
- Final Order cancelled: `{str(cancellation["final_order_cancelled"]).lower()}`
- Cleanup status: `{cancellation["cleanup_status"]}`
- Raw response archives: `{result["evidence"]["raw_response_archives"]}`
- Estimated model cost: `${result["model"]["estimated_cost_usd"]}` USD
- Duration: `{result["duration_ms"]} ms`

## End-to-end checks

{checks}

This evaluation uses one billable model call and creates an external Duffel Test
Mode Order with fixed synthetic passenger data and sandbox balance payment. It then
reads the Order, creates and confirms its cancellation, and verifies the cancelled
Order. It rejects live tokens and never retries Order or cancellation writes.
"""


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


if __name__ == "__main__":
    main()

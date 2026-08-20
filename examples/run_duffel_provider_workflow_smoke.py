from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import PolicyOutcome
from corporate_travel_agent.domain.models import TripRequestVersion
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationTraceRecorder,
    TraceFinal,
    TraceFingerprint,
)
from corporate_travel_agent.services.object_storage import LocalRawResponseObjectStore
from corporate_travel_agent.services.policy_config import load_policy_configuration


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run one frozen structured workflow through the real Duffel Test Mode "
            "Flights API. This never creates an order or payment."
        )
    )
    parser.add_argument(
        "--dataset",
        default="evals/subsets/duffel-real-workflow-smoke-v1.json",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm-external-test-call", action="store_true")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    if not args.confirm_external_test_call:
        parser.error("--confirm-external-test-call is required")
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    if not token.startswith("duffel_test_"):
        parser.error("DUFFEL_ACCESS_TOKEN must contain a Duffel Test Mode token")
    if os.getenv("DUFFEL_LIVE_MODE", "false").casefold() == "true":
        parser.error("DUFFEL_LIVE_MODE must remain false")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        parser.error("LIVE_BOOKING_ENABLED must remain false")

    dataset_path = Path(args.dataset).resolve()
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    _validate_dataset(dataset)
    case = dataset["cases"][0]
    expected = case["expected"]
    workflow_action = case.get("workflow_action", "search_only")

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw_store = LocalRawResponseObjectStore(output / "raw-provider-responses")
    policy_configuration = _policy_configuration_for_currency(
        case["policy_currency"],
        dataset_id=dataset["dataset_id"],
    )
    run_id = output.name
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    recorder = EvaluationTraceRecorder(
        run_id=run_id,
        case_id=case["case_id"],
        attempt=1,
        evaluation_mode="deterministic_live_provider",
        fingerprint=TraceFingerprint(
            project_version="0.1.0",
            code_revision=args.code_revision,
            dataset_id=dataset["dataset_id"],
            dataset_version=dataset["dataset_version"],
            dataset_sha256=dataset_sha256,
            prompt_version=None,
            requested_model=None,
            actual_model=None,
            price_table_version=None,
        ),
        tool_choice_exposure="orchestrator_controlled",
    )
    provider = DuffelProvider(
        token,
        api_version=os.getenv("DUFFEL_API_VERSION", "v2"),
        api_base_url=os.getenv("DUFFEL_API_BASE_URL", "https://api.duffel.com"),
        expected_currency=case["policy_currency"],
        raw_response_store=raw_store,
    )
    workflow, _ = build_demo_system(
        provider=provider,
        policy_configuration=policy_configuration,
        trace_observer=recorder,
        max_provider_attempts=1,
    )
    request = _request_from_case(case)

    started_at = datetime.now(UTC)
    started_ns = perf_counter_ns()
    selection_error: str | None = None
    selected_option_id: str | None = None
    selected_ref: str | None = None
    try:
        task = workflow.create_task(request)
        initial_task_state = task.state.value
        if workflow_action == "search_select_revalidate":
            selected_option = next(
                (
                    option
                    for option in task.options
                    if option.policy_decision.outcome is PolicyOutcome.COMPLIANT
                ),
                None,
            )
            if selected_option is None:
                selection_error = "No compliant option was available for revalidation"
            else:
                selected_option_id = selected_option.option_id
                selected_ref = selected_option.outbound.ref_id
                task = workflow.select_option(task.task_id, selected_option.option_id)
    finally:
        provider.close()
    completed_at = datetime.now(UTC)
    duration_ms = (perf_counter_ns() - started_ns) / 1_000_000

    snapshots = workflow.tasks.snapshots(task.task_id)
    selected = next(
        (option for option in task.options if option.option_id == selected_option_id),
        task.options[0] if task.options else None,
    )
    result_refs = tuple(ref for option in task.options for ref in option.inventory_refs)
    trace = recorder.finish(
        TraceFinal(
            state=task.state.value,
            policy_outcome=(
                selected.policy_decision.outcome.value if selected is not None else None
            ),
            booking_allowed=False,
            result_refs=tuple(dict.fromkeys(result_refs)),
            failure_reason=task.failure,
        )
    )

    provider_calls = [item for item in task.tool_calls if item.tool_kind == "PROVIDER"]
    provider_tool_names = [item.tool_name for item in provider_calls]
    external_requests = [
        {"method": method, "path": path} for method, path in provider.external_request_log
    ]
    notices = tuple(task.metadata.get("provider_coverage_notices", ()))
    revalidation = provider.last_revalidation_result
    revalidation_status = revalidation.status.value if revalidation is not None else None
    handoff_instructions = (
        task.booking_intent.handoff.url_or_instructions if task.booking_intent is not None else None
    )
    checks: dict[str, bool] = {
        "external_call_cap_respected": provider.external_request_count
        <= dataset["maximum_external_calls"],
        "expected_external_http_calls": provider.external_request_count
        == expected.get("external_http_calls", dataset["maximum_external_calls"]),
        "authorized_api_snapshot": bool(snapshots)
        and all(
            snapshot.source_type.value == expected["provider_source_type"] for snapshot in snapshots
        ),
        "minimum_options": len(task.options) >= expected["minimum_options"],
        "search_response_archived": provider.last_raw_response is not None,
        "test_mode_disclosed": any("Test Mode" in notice for notice in notices)
        == expected["test_mode_disclosed"],
        "no_order_or_payment_http": all(
            "/orders" not in item["path"] and "/payments" not in item["path"]
            for item in external_requests
        ),
        "no_order_or_payment_tools": all(
            "order" not in name.casefold() and "payment" not in name.casefold()
            for name in provider_tool_names
        ),
    }
    if workflow_action == "search_only":
        checks.update(
            {
                "expected_provider_tool_calls": len(provider_calls)
                == expected["provider_tool_calls"],
                "expected_final_state": task.state.value == expected["final_state"],
                "expected_booking_intent": (task.booking_intent is not None)
                == expected["booking_intent_created"],
            }
        )
    else:
        allowed_statuses = expected["allowed_revalidation_statuses"]
        tool_sequences = expected["provider_tool_sequences_by_revalidation_status"]
        final_states = expected["final_states_by_revalidation_status"]
        booking_intents = expected["booking_intent_by_revalidation_status"]
        expected_tool_sequence = tool_sequences.get(revalidation_status, [])
        checks.update(
            {
                "selection_succeeded": selection_error is None and selected_option_id is not None,
                "selected_option_compliant": selected is not None
                and selected.policy_decision.outcome is PolicyOutcome.COMPLIANT,
                "search_then_offer_get": len(external_requests) == 2
                and external_requests[0] == {"method": "POST", "path": "/air/offer_requests"}
                and selected_ref is not None
                and external_requests[1]
                == {"method": "GET", "path": f"/air/offers/{selected_ref}"},
                "revalidation_status_allowed": revalidation_status in allowed_statuses,
                "selected_offer_still_available": revalidation is not None
                and selected_ref in revalidation.current_prices,
                "revalidation_response_archived": len(provider.revalidation_raw_responses)
                == expected["revalidation_response_archives"],
                "expected_provider_tool_sequence": provider_tool_names == expected_tool_sequence,
                "conditional_final_state": task.state.value
                == final_states.get(revalidation_status),
                "conditional_booking_intent": (task.booking_intent is not None)
                == booking_intents.get(revalidation_status),
                "unchanged_handoff_is_test_only": revalidation_status != "UNCHANGED"
                or (
                    handoff_instructions is not None
                    and "Test Mode only; no order was created" in handoff_instructions
                ),
                "price_change_requires_reconfirmation": revalidation_status != "PRICE_CHANGED"
                or (task.state.value == "RECONFIRMATION_REQUIRED" and task.booking_intent is None),
            }
        )
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "evaluation_mode": "deterministic_live_provider",
        "provider": provider.name,
        "provider_mode": provider.provider_mode,
        "dataset": {
            "id": dataset["dataset_id"],
            "version": dataset["dataset_version"],
            "sha256": dataset_sha256,
            "case_id": case["case_id"],
        },
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": round(duration_ms, 3),
        "workflow_action": workflow_action,
        "external_calls": provider.external_request_count,
        "external_requests": external_requests,
        "provider_tool_calls": provider_tool_names,
        "initial_task_state": initial_task_state,
        "task_state": task.state.value,
        "option_count": len(task.options),
        "selected_option_id": selected_option_id,
        "selected_offer_ref": selected_ref,
        "selection_error": selection_error,
        "booking_intent_created": task.booking_intent is not None,
        "handoff_instructions": handoff_instructions,
        "raw_http_response_archived": provider.last_raw_response is not None,
        "revalidation": (
            {
                "status": revalidation.status.value,
                "checked_at": revalidation.checked_at.isoformat(),
                "current_prices": {
                    ref: str(price) for ref, price in revalidation.current_prices.items()
                },
                "unavailable_refs": revalidation.unavailable_refs,
                "warnings": revalidation.warnings,
            }
            if revalidation is not None
            else None
        ),
        "revalidation_raw_responses": [
            {
                "sha256": reference.sha256,
                "size_bytes": reference.size_bytes,
                "content_type": reference.content_type,
                "stored_at": reference.stored_at.isoformat(),
                "retention_until": reference.retention_until.isoformat(),
                "access_policy": reference.access_policy.value,
            }
            for reference in provider.revalidation_raw_responses
        ],
        "provider_notices": notices,
        "snapshots": [
            {
                "snapshot_id": snapshot.snapshot_id,
                "source_type": snapshot.source_type.value,
                "item_count": len(snapshot.items),
                "raw_payload_sha256": snapshot.raw_payload_hash,
                "raw_response_archived": snapshot.raw_response is not None,
            }
            for snapshot in snapshots
        ],
        "checks": checks,
        "passed": all(checks.values()),
        "failure": task.failure,
    }
    (output / "trace.json").write_text(
        trace.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (output / "evaluation-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "evaluation-report.md").write_text(
        _render_report(result),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


def _validate_dataset(dataset: dict[str, Any]) -> None:
    if dataset.get("status") != "frozen":
        raise SystemExit("Duffel smoke dataset must be frozen")
    cases = dataset.get("cases")
    if dataset.get("records") != 1 or not isinstance(cases, list) or len(cases) != 1:
        raise SystemExit("Duffel smoke dataset must contain exactly one case")
    maximum_external_calls = dataset.get("maximum_external_calls")
    if maximum_external_calls not in {1, 2}:
        raise SystemExit("Duffel smoke dataset must cap external calls at one or two")
    if dataset.get("booking_enabled") is not False:
        raise SystemExit("Duffel smoke dataset must keep booking disabled")
    action = cases[0].get("workflow_action", "search_only")
    expected_calls = 1 if action == "search_only" else 2
    if action not in {"search_only", "search_select_revalidate"}:
        raise SystemExit("Duffel smoke dataset has an unsupported workflow_action")
    if maximum_external_calls != expected_calls:
        raise SystemExit("Duffel smoke external-call cap does not match its workflow_action")


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


def _request_from_case(case: dict[str, Any]) -> TripRequestVersion:
    return TripRequestVersion(
        task_id=f"{case['case_id']}-attempt-1",
        version=1,
        traveler_id=case["traveler_id"],
        origin=case["origin"],
        destination=case["destination"],
        departure_after=datetime.fromisoformat(case["departure_after"]),
        arrive_by=datetime.fromisoformat(case["arrive_by"]),
        return_after=(
            datetime.fromisoformat(case["return_after"])
            if case["return_after"] is not None
            else None
        ),
        return_before=(
            datetime.fromisoformat(case["return_before"])
            if case["return_before"] is not None
            else None
        ),
        hotel_check_in=None,
        hotel_check_out=None,
        hard_constraints=tuple(case["hard_constraints"]),
        soft_preferences=tuple(case["soft_preferences"]),
    )


def _render_report(result: dict[str, Any]) -> str:
    status = "PASS" if result["passed"] else "FAIL"
    checks = "\n".join(
        f"- {'PASS' if passed else 'FAIL'} `{name}`" for name, passed in result["checks"].items()
    )
    snapshots = result["snapshots"]
    snapshot_line = (
        f"{len(snapshots)} snapshot(s), "
        f"{sum(item['item_count'] for item in snapshots)} normalized offer(s)"
    )
    archive_statement = (
        "The raw supplier JSON responses were archived under the run directory and "
        "referenced by SHA-256."
        if result["raw_http_response_archived"]
        else "No raw supplier JSON was archived; this is an evidence failure."
    )
    revalidation_status = (
        result["revalidation"]["status"] if result["revalidation"] is not None else "not_run"
    )
    return f"""# Duffel real-provider workflow smoke

- Result: **{status}**
- Mode: `{result["evaluation_mode"]}`
- Provider: `{result["provider"]}` / `{result["provider_mode"]}`
- Dataset: `{result["dataset"]["id"]}` `{result["dataset"]["version"]}`
- Dataset SHA-256: `{result["dataset"]["sha256"]}`
- Case: `{result["dataset"]["case_id"]}`
- Workflow action: `{result["workflow_action"]}`
- External calls: `{result["external_calls"]}`
- Provider tools: `{", ".join(result["provider_tool_calls"])}`
- Duration: `{result["duration_ms"]} ms`
- Final task state: `{result["task_state"]}`
- Revalidation status: `{revalidation_status}`
- Inventory: {snapshot_line}
- Booking intent created: `{str(result["booking_intent_created"]).lower()}`

## Rule checks

{checks}

This run uses a structured request inside the agent orchestrator. Depending on the
frozen workflow action, it performs one real Duffel Test Mode search or a search
followed by one Offer retrieval/revalidation. A Booking Intent in this report is
local handoff state only. The runner does not call an LLM, create a Duffel Order,
submit a Payment, or test production inventory. {archive_statement} The access
token is never written to the trace or report.
"""


if __name__ == "__main__":
    main()

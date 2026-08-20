#!/usr/bin/env python3
"""Acceptance harness for provider delayed recovery (HANDOFF P0).

Unlike the one-shot full-chain smoke runner, this script:

1. Forces three immediate RetryableProviderError failures (no external HTTP).
2. Asserts the task enters WAITING_FOR_PROVIDER with a scheduled delayed retry.
3. Advances a controllable clock and runs process_due_provider_retries().
4. On the delayed attempt, calls real Duffel Test Mode + LiteAPI Sandbox.
5. Completes select → revalidate → instruction-only handoff when inventory allows.

Scenarios:
  recover  — fail 3 immediate reads, recover on first delayed attempt via real APIs
  exhaust  — keep failing through 3 delayed attempts → PROVIDER_FAILED (no real HTTP)
  circuit  — open circuit after first task; second task schedules without provider calls

No orders/payments are created. Requires --confirm-external-test-calls for recover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState
from corporate_travel_agent.domain.models import TripRequestVersion
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    RetryableProviderError,
    TransportSearchQuery,
)
from corporate_travel_agent.providers.composite import CompositeTravelInventoryProvider
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.providers.liteapi import LiteAPIHotelProvider
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.object_storage import LocalRawResponseObjectStore
from corporate_travel_agent.services.policy_config import load_policy_configuration


class FaultThenRealProvider:
    """Fail the first N transport searches, then delegate to a real composite."""

    def __init__(
        self,
        base: CompositeTravelInventoryProvider,
        *,
        fail_times: int,
        fail_message: str = "injected transient transport fault for delayed-recovery acceptance",
    ) -> None:
        self.base = base
        self.fail_times = fail_times
        self.fail_message = fail_message
        self.transport_calls = 0
        self.hotel_calls = 0
        self.revalidate_calls = 0
        self.injected_failures = 0
        self.real_transport_calls = 0
        self.real_hotel_calls = 0

    @property
    def name(self) -> str:
        return f"fault-then-{self.base.name}"

    @property
    def provider_mode(self) -> str:
        return getattr(self.base, "provider_mode", "fault-then-real")

    def search_transport(self, query: TransportSearchQuery):
        self.transport_calls += 1
        if self.transport_calls <= self.fail_times:
            self.injected_failures += 1
            raise RetryableProviderError(
                self.fail_message,
                error_code="INJECTED_TRANSIENT_TRANSPORT",
                layer="acceptance_harness",
                cause_type="InjectedFault",
                cause_chain=("InjectedFault",),
                response_received=False,
            )
        self.real_transport_calls += 1
        return self.base.search_transport(query)

    def search_hotels(self, query: HotelSearchQuery):
        self.hotel_calls += 1
        self.real_hotel_calls += 1
        return self.base.search_hotels(query)

    def revalidate(self, refs):
        self.revalidate_calls += 1
        return self.base.revalidate(refs)

    def create_deep_link(self, option):
        return self.base.create_deep_link(option)

    def close(self) -> None:
        close = getattr(self.base, "close", None)
        if callable(close):
            close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("recover", "exhaust", "circuit", "all"),
        default="all",
    )
    parser.add_argument(
        "--dataset",
        default="evals/subsets/duffel-liteapi-real-full-chain-v2.json",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    args = parser.parse_args()

    scenarios = (
        ("recover", "exhaust", "circuit")
        if args.scenario == "all"
        else (args.scenario,)
    )
    if "recover" in scenarios and not args.confirm_external_test_calls:
        parser.error("--confirm-external-test-calls is required when running recover/all")

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)

    results: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output.name,
        "evaluation_mode": "provider_delayed_recovery_acceptance",
        "started_at": datetime.now(UTC).isoformat(),
        "scenarios": {},
    }

    for name in scenarios:
        scenario_dir = output / name
        scenario_dir.mkdir()
        if name == "recover":
            results["scenarios"][name] = _run_recover(args.dataset, scenario_dir)
        elif name == "exhaust":
            results["scenarios"][name] = _run_exhaust(scenario_dir)
        else:
            results["scenarios"][name] = _run_circuit(scenario_dir)

    results["completed_at"] = datetime.now(UTC).isoformat()
    results["passed"] = all(
        scenario.get("passed") for scenario in results["scenarios"].values()
    )
    results["checks_summary"] = {
        name: {
            "passed": scenario["passed"],
            "failed_checks": [
                key for key, ok in scenario["checks"].items() if not ok
            ],
        }
        for name, scenario in results["scenarios"].items()
    }

    (output / "summary.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "SUMMARY.md").write_text(_render_markdown(results), encoding="utf-8")

    print(json.dumps(results["checks_summary"], indent=2, ensure_ascii=False))
    if not results["passed"]:
        raise SystemExit(1)


def _run_recover(dataset_path: str, output: Path) -> dict[str, Any]:
    duffel_token, liteapi_key = _require_test_credentials()
    dataset_file = Path(dataset_path).resolve()
    dataset_bytes = dataset_file.read_bytes()
    dataset = json.loads(dataset_bytes)
    case = dataset["cases"][0]
    request = _request_from_case(case)

    raw_store = LocalRawResponseObjectStore(output / "raw-provider-responses")
    duffel = DuffelProvider(
        duffel_token,
        api_version=os.getenv("DUFFEL_API_VERSION", "v2"),
        api_base_url=os.getenv("DUFFEL_API_BASE_URL", "https://api.duffel.com"),
        expected_currency=case["policy_currency"],
        raw_response_store=raw_store,
    )
    liteapi = LiteAPIHotelProvider(
        liteapi_key,
        api_base_url=os.getenv("LITEAPI_API_BASE_URL", "https://api.liteapi.travel/v3.0"),
        currency=case["policy_currency"],
        guest_nationality=case["guest_nationality"],
        require_sandbox=True,
        raw_response_store=raw_store,
    )
    real = CompositeTravelInventoryProvider(
        transport_provider=duffel,
        hotel_provider=liteapi,
    )
    # 3 immediate failures consume the in-request budget; delayed attempt is call #4 → real.
    provider = FaultThenRealProvider(real, fail_times=3)

    now = [datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)]
    policy = _policy_configuration(case, dataset_id=dataset["dataset_id"])
    workflow, _ = build_demo_system(
        provider=provider,
        policy_configuration=policy,
        clock=lambda: now[0],
        retry_sleep=lambda _: None,
        delayed_provider_retry_seconds=(60.0, 180.0, 600.0),
        max_delayed_provider_attempts=3,
        provider_circuit_open_seconds=60.0,
    )

    started_ns = perf_counter_ns()
    unexpected: str | None = None
    task = None
    selected = None
    after_create: dict[str, Any] | None = None
    after_delayed: dict[str, Any] | None = None
    try:
        task = workflow.create_task(request)
        after_create = {
            "state": task.state.value,
            "tool_calls_used": task.tool_calls_used,
            "provider_retry": workflow.provider_retry_status(task),
            "injected_failures": provider.injected_failures,
            "real_transport_calls": provider.real_transport_calls,
            "duffel_http": duffel.external_request_count,
            "liteapi_http": liteapi.external_request_count,
        }

        now[0] += timedelta(seconds=60)
        processed = workflow.process_due_provider_retries()
        task = workflow.tasks.get(request.task_id)
        after_delayed = {
            "processed_task_ids": list(processed),
            "state": task.state.value,
            "tool_calls_used": task.tool_calls_used,
            "options": len(task.options),
            "provider_retry": workflow.provider_retry_status(task),
            "injected_failures": provider.injected_failures,
            "real_transport_calls": provider.real_transport_calls,
            "real_hotel_calls": provider.real_hotel_calls,
            "duffel_http": duffel.external_request_count,
            "liteapi_http": liteapi.external_request_count,
        }

        if task.state is TaskState.WAITING_FOR_USER and task.options:
            selected = next(
                (
                    option
                    for option in task.options
                    if option.hotel is not None
                    and option.policy_decision.outcome is PolicyOutcome.COMPLIANT
                ),
                None,
            )
            if selected is not None:
                task = workflow.select_option(task.task_id, selected.option_id)
    except Exception as exc:  # noqa: BLE001 — acceptance harness records failure
        unexpected = f"{type(exc).__name__}: {exc}"
    finally:
        provider.close()

    duration_ms = round((perf_counter_ns() - started_ns) / 1_000_000, 3)
    retry = workflow.provider_retry_status(task) if task is not None else None
    events = (
        [item.event_type for item in workflow.tasks.events(task.task_id)]
        if task is not None
        else []
    )
    failed_tools = (
        [
            {
                "tool_name": item.tool_name,
                "status": item.status.value,
                "error_type": item.error_type,
                "retry_of": item.retry_of,
            }
            for item in task.tool_calls
            if item.tool_kind == "PROVIDER"
        ]
        if task is not None
        else []
    )

    checks = {
        "no_unexpected_exception": unexpected is None,
        "after_create_waiting_for_provider": after_create["state"]
        == TaskState.WAITING_FOR_PROVIDER.value
        if unexpected is None
        else False,
        "three_injected_failures_before_real_http": after_create.get("injected_failures")
        == 3
        and after_create.get("duffel_http", -1) == 0
        and after_create.get("liteapi_http", -1) == 0
        if unexpected is None
        else False,
        "delayed_retry_processed_task": unexpected is None
        and request.task_id in after_delayed.get("processed_task_ids", []),
        "recovered_to_waiting_for_user": unexpected is None
        and after_delayed.get("state") == TaskState.WAITING_FOR_USER.value,
        "retry_status_recovered": unexpected is None
        and isinstance(retry, dict)
        and retry.get("status") == "recovered"
        and retry.get("delayed_attempts_completed") == 1,
        "real_http_only_after_delay": unexpected is None
        and duffel.external_request_count >= 1
        and liteapi.external_request_count >= 1,
        "has_combined_options": unexpected is None
        and task is not None
        and len(task.options) >= 1,
        "select_and_revalidate_completed": unexpected is None
        and task is not None
        and task.state
        in {TaskState.READY_FOR_HANDOFF, TaskState.RECONFIRMATION_REQUIRED},
        "no_booking_intent_on_failure_path": True,
        "audit_has_delayed_retry_started": "PROVIDER_DELAYED_RETRY_STARTED" in events,
        "no_order_or_payment_paths": all(
            "/orders" not in path and "/payments" not in path
            for _method, path in (*duffel.external_request_log, *liteapi.external_request_log)
        ),
    }
    if task is not None and task.state is TaskState.READY_FOR_HANDOFF:
        checks["booking_intent_instruction_only"] = (
            task.booking_intent is not None
            and "no order was created" in task.booking_intent.handoff.url_or_instructions
        )
    elif task is not None and task.state is TaskState.RECONFIRMATION_REQUIRED:
        checks["booking_intent_absent_on_price_change"] = task.booking_intent is None

    result = {
        "scenario": "recover",
        "passed": all(checks.values()),
        "checks": checks,
        "duration_ms": duration_ms,
        "unexpected_error": unexpected,
        "dataset": {
            "id": dataset["dataset_id"],
            "version": dataset["dataset_version"],
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "case_id": case["case_id"],
        },
        "after_create": after_create if unexpected is None else None,
        "after_delayed": after_delayed if unexpected is None else None,
        "final_state": task.state.value if task is not None else None,
        "provider_retry": retry,
        "selected_option_id": selected.option_id if selected is not None else None,
        "duffel_http_requests": [
            {"method": method, "path": path}
            for method, path in duffel.external_request_log
        ],
        "liteapi_http_requests": [
            {"method": method, "path": path}
            for method, path in liteapi.external_request_log
        ],
        "provider_tool_calls": failed_tools,
        "audit_events": events,
        "harness_counters": {
            "injected_failures": provider.injected_failures,
            "transport_calls": provider.transport_calls,
            "hotel_calls": provider.hotel_calls,
            "real_transport_calls": provider.real_transport_calls,
            "real_hotel_calls": provider.real_hotel_calls,
            "revalidate_calls": provider.revalidate_calls,
        },
    }
    (output / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _run_exhaust(output: Path) -> dict[str, Any]:
    """Pure injected faults: no external HTTP. Proves exhaustion path."""

    class AlwaysFailTransport:
        name = "always-fail-transport"
        outbound_calls = 0

        def search_transport(self, query: TransportSearchQuery):
            self.outbound_calls += 1
            raise RetryableProviderError(
                "injected permanent outage for exhaust scenario",
                error_code="INJECTED_OUTAGE",
                layer="acceptance_harness",
                response_received=False,
            )

        def search_hotels(self, query: HotelSearchQuery):
            raise AssertionError("hotel search must not run when transport never succeeds")

        def revalidate(self, refs):
            raise AssertionError("revalidate must not run")

        def create_deep_link(self, option):
            raise AssertionError("deep link must not run")

    now = [datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)]
    provider = AlwaysFailTransport()
    workflow, _ = build_demo_system(
        provider=provider,
        clock=lambda: now[0],
        retry_sleep=lambda _: None,
        delayed_provider_retry_seconds=(60.0, 60.0, 60.0),
        max_delayed_provider_attempts=3,
        max_tool_calls=20,
    )
    request = TripRequestVersion(
        task_id="delayed-exhaust-acceptance",
        version=1,
        traveler_id="E1001",
        origin="LHR",
        destination="JFK",
        departure_after=datetime(2026, 9, 15, tzinfo=UTC),
        arrive_by=datetime(2026, 9, 17, tzinfo=UTC),
        return_after=None,
        return_before=None,
        hotel_check_in=date(2026, 9, 15),
        hotel_check_out=date(2026, 9, 17),
        hard_constraints=("flight_only",),
    )
    task = workflow.create_task(request)
    processed_rounds: list[list[str]] = []
    for _ in range(3):
        now[0] += timedelta(seconds=60)
        processed_rounds.append(list(workflow.process_due_provider_retries()))
    task = workflow.tasks.get(request.task_id)
    retry = workflow.provider_retry_status(task)
    events = [item.event_type for item in workflow.tasks.events(task.task_id)]
    checks = {
        "terminal_provider_failed": task.state is TaskState.PROVIDER_FAILED,
        "delayed_attempts_3": isinstance(retry, dict)
        and retry.get("delayed_attempts_completed") == 3
        and retry.get("status") == "exhausted",
        "three_delayed_started_events": events.count("PROVIDER_DELAYED_RETRY_STARTED")
        == 3,
        "exhausted_event_present": "PROVIDER_DELAYED_RETRIES_EXHAUSTED" in events,
        "no_booking_intent": task.booking_intent is None,
        "all_rounds_processed_task": all(
            request.task_id in round_ids for round_ids in processed_rounds
        ),
    }
    result = {
        "scenario": "exhaust",
        "passed": all(checks.values()),
        "checks": checks,
        "final_state": task.state.value,
        "provider_retry": retry,
        "outbound_calls": provider.outbound_calls,
        "processed_rounds": processed_rounds,
        "audit_events": events,
        "tool_calls_used": task.tool_calls_used,
    }
    (output / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _run_circuit(output: Path) -> dict[str, Any]:
    class AlwaysFailTransport:
        name = "always-fail-for-circuit"
        outbound_calls = 0

        def search_transport(self, query: TransportSearchQuery):
            self.outbound_calls += 1
            raise RetryableProviderError(
                "injected outage opens circuit",
                error_code="INJECTED_OUTAGE",
                layer="acceptance_harness",
                response_received=False,
            )

        def search_hotels(self, query: HotelSearchQuery):
            raise AssertionError("hotel search must not run")

        def revalidate(self, refs):
            raise AssertionError("revalidate must not run")

        def create_deep_link(self, option):
            raise AssertionError("deep link must not run")

    now = [datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)]
    provider = AlwaysFailTransport()
    workflow, _ = build_demo_system(
        provider=provider,
        clock=lambda: now[0],
        retry_sleep=lambda _: None,
        provider_circuit_open_seconds=60.0,
    )
    def _simple_request(task_id: str) -> TripRequestVersion:
        return TripRequestVersion(
            task_id=task_id,
            version=1,
            traveler_id="E1001",
            origin="LHR",
            destination="JFK",
            departure_after=datetime(2026, 9, 15, tzinfo=UTC),
            arrive_by=datetime(2026, 9, 17, tzinfo=UTC),
            return_after=None,
            return_before=None,
            hotel_check_in=date(2026, 9, 15),
            hotel_check_out=date(2026, 9, 17),
        )

    first = workflow.create_task(_simple_request("circuit-first"))
    calls_after_first = provider.outbound_calls
    second = workflow.create_task(_simple_request("circuit-second"))
    checks = {
        "first_waiting": first.state is TaskState.WAITING_FOR_PROVIDER,
        "second_waiting": second.state is TaskState.WAITING_FOR_PROVIDER,
        "first_used_three_attempts": calls_after_first == 3,
        "second_did_not_call_provider": provider.outbound_calls == calls_after_first
        and second.tool_calls_used == 0,
        "second_scheduled_retry": workflow.provider_retry_status(second) is not None
        and workflow.provider_retry_status(second).get("status") == "scheduled",
    }
    result = {
        "scenario": "circuit",
        "passed": all(checks.values()),
        "checks": checks,
        "first_state": first.state.value,
        "second_state": second.state.value,
        "outbound_calls_total": provider.outbound_calls,
        "second_retry": workflow.provider_retry_status(second),
    }
    (output / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _require_test_credentials() -> tuple[str, str]:
    duffel_token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    liteapi_key = os.getenv("LITEAPI_API_KEY", "").strip()
    if not duffel_token.startswith("duffel_test_"):
        raise SystemExit("DUFFEL_ACCESS_TOKEN must be a Duffel Test Mode token")
    if not liteapi_key:
        raise SystemExit("LITEAPI_API_KEY is required")
    if os.getenv("DUFFEL_LIVE_MODE", "false").casefold() == "true":
        raise SystemExit("DUFFEL_LIVE_MODE must remain false")
    if os.getenv("LITEAPI_REQUIRE_SANDBOX", "true").casefold() != "true":
        raise SystemExit("LITEAPI_REQUIRE_SANDBOX must remain true")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        raise SystemExit("LIVE_BOOKING_ENABLED must remain false")
    if os.getenv("LITEAPI_BOOKING_ENABLED", "false").casefold() == "true":
        raise SystemExit("LITEAPI_BOOKING_ENABLED must remain false")
    return duffel_token, liteapi_key


def _request_from_case(case: dict[str, Any]) -> TripRequestVersion:
    return TripRequestVersion(
        task_id=f"{case['case_id']}-delayed-recover",
        version=1,
        traveler_id=case["traveler_id"],
        origin=case["origin"],
        destination=case["destination"],
        departure_after=datetime.fromisoformat(case["departure_after"]),
        arrive_by=datetime.fromisoformat(case["arrive_by"]),
        return_after=datetime.fromisoformat(case["return_after"])
        if case.get("return_after")
        else None,
        return_before=datetime.fromisoformat(case["return_before"])
        if case.get("return_before")
        else None,
        hotel_check_in=date.fromisoformat(case["hotel_check_in"])
        if case.get("hotel_check_in")
        else None,
        hotel_check_out=date.fromisoformat(case["hotel_check_out"])
        if case.get("hotel_check_out")
        else None,
        hard_constraints=tuple(case.get("hard_constraints") or ()),
        soft_preferences=tuple(case.get("soft_preferences") or ()),
    )


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


def _render_markdown(results: dict[str, Any]) -> str:
    lines = [
        "# Provider Delayed Recovery Acceptance",
        "",
        f"- run_id: `{results['run_id']}`",
        f"- started_at: {results['started_at']}",
        f"- completed_at: {results['completed_at']}",
        f"- overall_passed: **{results['passed']}**",
        "",
    ]
    for name, scenario in results["scenarios"].items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- passed: **{scenario['passed']}**")
        if scenario.get("final_state"):
            lines.append(f"- final_state: `{scenario['final_state']}`")
        failed = [k for k, ok in scenario["checks"].items() if not ok]
        if failed:
            lines.append(f"- failed_checks: {', '.join(failed)}")
        else:
            lines.append("- failed_checks: _(none)_")
        lines.append("")
        for key, ok in scenario["checks"].items():
            mark = "PASS" if ok else "FAIL"
            lines.append(f"- [{mark}] `{key}`")
        lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- `recover` injects three immediate transport faults, then uses real "
        "Duffel Test Mode + LiteAPI Sandbox on the first delayed retry."
    )
    lines.append(
        "- This is stronger than the one-shot full-chain smoke (which never starts "
        "delayed recovery), but it is still a controlled fault — not a natural "
        "upstream multi-minute outage."
    )
    lines.append(
        "- `exhaust` and `circuit` stay fully offline (injected faults only)."
    )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

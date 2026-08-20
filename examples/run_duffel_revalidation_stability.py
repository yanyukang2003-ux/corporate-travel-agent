from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SINGLE_RUNNER = PROJECT_ROOT / "examples/run_duffel_provider_workflow_smoke.py"
DEFAULT_DATASET = PROJECT_ROOT / "evals/subsets/duffel-real-revalidation-smoke-v1.json"
EXPECTED_ATTEMPTS = 3
_SECRET_MARKERS = (
    re.compile(rb"duffel_test_[A-Za-z0-9_-]+"),
    re.compile(rb"Authorization", re.IGNORECASE),
    re.compile(rb"Bearer\s+", re.IGNORECASE),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen Duffel Test Mode search-and-revalidation smoke three "
            "times. This never creates an order or payment."
        )
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", required=True)
    parser.add_argument("--attempts", type=int, default=EXPECTED_ATTEMPTS)
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    if args.attempts != EXPECTED_ATTEMPTS:
        parser.error("Duffel revalidation stability requires exactly 3 attempts")
    if not args.confirm_external_test_calls:
        parser.error("--confirm-external-test-calls is required")
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
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC)
    started_ns = perf_counter_ns()
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, EXPECTED_ATTEMPTS + 1):
        attempt_output = output / f"attempt-{attempt}"
        command = [
            sys.executable,
            str(SINGLE_RUNNER),
            "--dataset",
            str(dataset_path),
            "--output",
            str(attempt_output),
            "--confirm-external-test-call",
        ]
        if args.code_revision is not None:
            command.extend(("--code-revision", args.code_revision))
        try:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=240,
            )
            runner_exit_code = completed.returncode
            runner_output = completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as exc:
            runner_exit_code = 124
            runner_output = _timeout_output(exc)
        result_path = attempt_output / "evaluation-result.json"
        result = _read_json_object(result_path)
        attempts.append(
            {
                "attempt": attempt,
                "runner_exit_code": runner_exit_code,
                "result_path": (
                    str(result_path.relative_to(output)) if result is not None else None
                ),
                "runner_output_tail": (
                    None if result is not None else _redact_output(runner_output)[-1200:]
                ),
                "result": result,
            }
        )

    completed_at = datetime.now(UTC)
    duration_ms = (perf_counter_ns() - started_ns) / 1_000_000
    secret_marker_paths = _secret_marker_paths(output)
    summary = summarize_attempts(
        run_id=output.name,
        dataset=dataset,
        dataset_sha256=dataset_sha256,
        attempts=attempts,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        secret_marker_paths=secret_marker_paths,
    )
    serializable_attempts = [
        {key: value for key, value in attempt.items() if key != "result"} for attempt in attempts
    ]
    (output / "attempt-records.json").write_text(
        json.dumps(serializable_attempts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "stability-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "evaluation-report.md").write_text(
        _render_report(summary),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["passed"]:
        raise SystemExit(1)


def summarize_attempts(
    *,
    run_id: str,
    dataset: dict[str, Any],
    dataset_sha256: str,
    attempts: list[dict[str, Any]],
    started_at: datetime,
    completed_at: datetime,
    duration_ms: float,
    secret_marker_paths: list[str],
) -> dict[str, Any]:
    results = [attempt.get("result") for attempt in attempts]
    complete_results = [result for result in results if isinstance(result, dict)]
    passed_flags = [
        attempt.get("runner_exit_code") == 0
        and isinstance(attempt.get("result"), dict)
        and attempt["result"].get("passed") is True
        for attempt in attempts
    ]
    passed_attempts = sum(passed_flags)
    any_passed = any(passed_flags)
    all_passed = len(passed_flags) == EXPECTED_ATTEMPTS and all(passed_flags)
    external_calls = [result.get("external_calls") for result in complete_results]
    total_external_calls = (
        sum(external_calls) if all(isinstance(value, int) for value in external_calls) else None
    )
    statuses = [
        result["revalidation"]["status"]
        for result in complete_results
        if isinstance(result.get("revalidation"), dict)
    ]
    tool_signatures = [tuple(result.get("provider_tool_calls", ())) for result in complete_results]
    request_signatures = [_request_signature(result) for result in complete_results]
    search_archives = sum(
        result.get("raw_http_response_archived") is True for result in complete_results
    )
    revalidation_archives = sum(
        len(result.get("revalidation_raw_responses", ())) for result in complete_results
    )
    no_order_or_payment = len(complete_results) == EXPECTED_ATTEMPTS and all(
        result.get("checks", {}).get("no_order_or_payment_http") is True
        and result.get("checks", {}).get("no_order_or_payment_tools") is True
        for result in complete_results
    )
    booking_intents = sum(
        result.get("booking_intent_created") is True for result in complete_results
    )
    expected_external_calls = dataset["maximum_external_calls"] * EXPECTED_ATTEMPTS
    checks = {
        "three_attempt_results_present": len(complete_results) == EXPECTED_ATTEMPTS,
        "all_subrunners_exited_zero": len(attempts) == EXPECTED_ATTEMPTS
        and all(attempt.get("runner_exit_code") == 0 for attempt in attempts),
        "all_attempts_passed": all_passed,
        "exact_total_external_calls": total_external_calls == expected_external_calls,
        "two_external_calls_per_attempt": len(external_calls) == EXPECTED_ATTEMPTS
        and all(value == dataset["maximum_external_calls"] for value in external_calls),
        "request_sequence_consistent": len(request_signatures) == EXPECTED_ATTEMPTS
        and len(set(request_signatures)) == 1
        and request_signatures[0] == ("POST /air/offer_requests", "GET /air/offers/{offer_id}"),
        "provider_tool_trajectory_consistent": len(tool_signatures) == EXPECTED_ATTEMPTS
        and len(set(tool_signatures)) == 1,
        "revalidation_status_consistent": len(statuses) == EXPECTED_ATTEMPTS
        and len(set(statuses)) == 1,
        "search_archives_complete": search_archives == EXPECTED_ATTEMPTS,
        "revalidation_archives_complete": revalidation_archives == EXPECTED_ATTEMPTS,
        "no_order_or_payment_calls": no_order_or_payment,
        "no_secret_markers": not secret_marker_paths,
    }
    status_counts = dict(sorted(Counter(statuses).items()))
    return {
        "schema_version": 1,
        "run_id": run_id,
        "evaluation_mode": "deterministic_live_provider_stability",
        "provider": dataset["provider"],
        "provider_mode": dataset["provider_mode"],
        "dataset": {
            "id": dataset["dataset_id"],
            "version": dataset["dataset_version"],
            "sha256": dataset_sha256,
            "case_id": dataset["cases"][0]["case_id"],
        },
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": round(duration_ms, 3),
        "runs": {
            "attempts": EXPECTED_ATTEMPTS,
            "passed_attempts": passed_attempts,
            "pass_at_1": 1.0 if passed_flags and passed_flags[0] else 0.0,
            "pass_at_3": 1.0 if any_passed else 0.0,
            "pass_power_3": 1.0 if all_passed else 0.0,
            "mixed_run_rate": 1.0 if any_passed and not all_passed else 0.0,
        },
        "external_calls": {
            "expected": expected_external_calls,
            "observed": total_external_calls,
            "per_attempt": external_calls,
        },
        "revalidation_status_counts": status_counts,
        "provider_tool_signatures": [list(signature) for signature in tool_signatures],
        "request_signatures": [list(signature) for signature in request_signatures],
        "evidence": {
            "search_raw_response_archives": search_archives,
            "revalidation_raw_response_archives": revalidation_archives,
            "secret_marker_paths": secret_marker_paths,
        },
        "safety": {
            "internal_booking_intents_created": booking_intents,
            "duffel_orders_created": 0 if no_order_or_payment else None,
            "payments_submitted": 0 if no_order_or_payment else None,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _request_signature(result: dict[str, Any]) -> tuple[str, ...]:
    signature: list[str] = []
    for item in result.get("external_requests", ()):
        method = item.get("method")
        path = item.get("path")
        if method == "GET" and isinstance(path, str) and path.startswith("/air/offers/off_"):
            path = "/air/offers/{offer_id}"
        signature.append(f"{method} {path}")
    return tuple(signature)


def _validate_dataset(dataset: dict[str, Any]) -> None:
    if dataset.get("dataset_id") != "duffel-real-revalidation-smoke-v1":
        raise SystemExit("Stability runner requires the frozen D13 dataset")
    if dataset.get("status") != "frozen":
        raise SystemExit("D13 dataset must be frozen")
    if dataset.get("provider") != "duffel" or dataset.get("provider_mode") != "test":
        raise SystemExit("D13 must use Duffel Test Mode")
    if dataset.get("records") != 1 or dataset.get("maximum_external_calls") != 2:
        raise SystemExit("D13 must contain one case capped at two external calls")
    if dataset.get("booking_enabled") is not False:
        raise SystemExit("D13 must keep external booking disabled")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or len(cases) != 1:
        raise SystemExit("D13 must contain exactly one case")
    if cases[0].get("workflow_action") != "search_select_revalidate":
        raise SystemExit("D13 must exercise search_select_revalidate")


def _secret_marker_paths(root: Path) -> list[str]:
    matches: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        content = path.read_bytes()
        if any(pattern.search(content) for pattern in _SECRET_MARKERS):
            matches.append(str(path.relative_to(root)))
    return matches


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    values: list[str] = []
    for value in (exc.stdout, exc.stderr):
        if isinstance(value, bytes):
            values.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, str):
            values.append(value)
    return "".join(values) + "\nSingle-run smoke timed out after 240 seconds"


def _redact_output(value: str) -> str:
    return re.sub(r"duffel_(?:test|live)_[A-Za-z0-9_-]+", "[REDACTED]", value)


def _render_report(summary: dict[str, Any]) -> str:
    status = "PASS" if summary["passed"] else "FAIL"
    runs = summary["runs"]
    external = summary["external_calls"]
    evidence = summary["evidence"]
    safety = summary["safety"]
    search_archives = evidence["search_raw_response_archives"]
    revalidation_archives = evidence["revalidation_raw_response_archives"]
    checks = "\n".join(
        f"- {'PASS' if passed else 'FAIL'} `{name}`" for name, passed in summary["checks"].items()
    )
    return f"""# Duffel Test Mode revalidation stability

- Result: **{status}**
- Dataset: `{summary["dataset"]["id"]}` `{summary["dataset"]["version"]}`
- Dataset SHA-256: `{summary["dataset"]["sha256"]}`
- Attempts: `{runs["attempts"]}`
- Passed attempts: `{runs["passed_attempts"]}`
- pass^3: `{runs["pass_power_3"]:.2f}`
- mixed run rate: `{runs["mixed_run_rate"]:.2f}`
- External calls: `{external["observed"]}` / `{external["expected"]}`
- Revalidation statuses: `{summary["revalidation_status_counts"]}`
- Search/revalidation archives: `{search_archives}` / `{revalidation_archives}`
- Internal Booking Intents: `{safety["internal_booking_intents_created"]}`
- Duffel Orders / Payments: `{safety["duffel_orders_created"]}` / `{safety["payments_submitted"]}`
- Duration: `{summary["duration_ms"]} ms`

## Stability checks

{checks}

Each attempt performs one Duffel Test Mode search followed by one Offer retrieval.
The Booking Intent count refers only to local handoff state. This runner never calls
Duffel Order or Payment endpoints, and it never accesses production inventory.
"""


if __name__ == "__main__":
    main()

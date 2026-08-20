from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "examples/run_duffel_revalidation_stability.py"
DATASET_PATH = PROJECT_ROOT / "evals/subsets/duffel-real-revalidation-smoke-v1.json"
NOW = datetime(2026, 8, 9, tzinfo=UTC)
SPEC = importlib.util.spec_from_file_location("duffel_revalidation_stability", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
STABILITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STABILITY)
summarize_attempts = STABILITY.summarize_attempts
_redact_output = STABILITY._redact_output
_secret_marker_paths = STABILITY._secret_marker_paths


def _dataset() -> dict:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def _result(attempt: int, *, passed: bool = True, status: str = "UNCHANGED") -> dict:
    ref = f"off_attempt_{attempt}"
    tools = ["provider.search_transport.outbound", "provider.revalidate"]
    booking_intent_created = False
    if status == "UNCHANGED":
        tools.append("provider.create_deep_link")
        booking_intent_created = True
    return {
        "passed": passed,
        "external_calls": 2,
        "external_requests": [
            {"method": "POST", "path": "/air/offer_requests"},
            {"method": "GET", "path": f"/air/offers/{ref}"},
        ],
        "provider_tool_calls": tools,
        "revalidation": {"status": status},
        "raw_http_response_archived": True,
        "revalidation_raw_responses": [{"sha256": f"sha-{attempt}"}],
        "booking_intent_created": booking_intent_created,
        "checks": {
            "no_order_or_payment_http": True,
            "no_order_or_payment_tools": True,
        },
    }


def _attempts(*, failed_attempt: int | None = None) -> list[dict]:
    return [
        {
            "attempt": attempt,
            "runner_exit_code": 1 if attempt == failed_attempt else 0,
            "result": _result(attempt, passed=attempt != failed_attempt),
        }
        for attempt in range(1, 4)
    ]


def _summary(attempts: list[dict], *, secret_marker_paths: list[str] | None = None) -> dict:
    return summarize_attempts(
        run_id="test-run",
        dataset=_dataset(),
        dataset_sha256="dataset-sha",
        attempts=attempts,
        started_at=NOW,
        completed_at=NOW,
        duration_ms=123.0,
        secret_marker_paths=secret_marker_paths or [],
    )


def test_three_clean_attempts_pass_stability_and_resource_gates() -> None:
    summary = _summary(_attempts())

    assert summary["passed"] is True
    assert summary["runs"] == {
        "attempts": 3,
        "passed_attempts": 3,
        "pass_at_1": 1.0,
        "pass_at_3": 1.0,
        "pass_power_3": 1.0,
        "mixed_run_rate": 0.0,
    }
    assert summary["external_calls"]["observed"] == 6
    assert summary["revalidation_status_counts"] == {"UNCHANGED": 3}
    assert summary["evidence"]["search_raw_response_archives"] == 3
    assert summary["evidence"]["revalidation_raw_response_archives"] == 3
    assert summary["safety"] == {
        "internal_booking_intents_created": 3,
        "duffel_orders_created": 0,
        "payments_submitted": 0,
    }


def test_one_failed_attempt_sets_mixed_run_rate_and_fails_the_batch() -> None:
    summary = _summary(_attempts(failed_attempt=2))

    assert summary["passed"] is False
    assert summary["runs"]["passed_attempts"] == 2
    assert summary["runs"]["pass_power_3"] == 0.0
    assert summary["runs"]["mixed_run_rate"] == 1.0
    assert summary["checks"]["all_subrunners_exited_zero"] is False
    assert summary["checks"]["all_attempts_passed"] is False


def test_status_or_tool_variance_fails_consistency_even_when_each_run_passes() -> None:
    attempts = _attempts()
    attempts[2]["result"] = _result(3, status="PRICE_CHANGED")

    summary = _summary(attempts)

    assert summary["runs"]["pass_power_3"] == 1.0
    assert summary["checks"]["revalidation_status_consistent"] is False
    assert summary["checks"]["provider_tool_trajectory_consistent"] is False
    assert summary["passed"] is False


def test_secret_marker_scan_and_runner_output_redaction(tmp_path: Path) -> None:
    token = "duffel_" + "test_example_secret"
    (tmp_path / "safe.json").write_text('{"mode":"test"}', encoding="utf-8")
    (tmp_path / "unsafe.txt").write_text(f"Authorization: Bearer {token}", encoding="utf-8")

    assert _secret_marker_paths(tmp_path) == ["unsafe.txt"]
    redacted = _redact_output(f"request failed for {token}")
    assert token not in redacted
    assert "[REDACTED]" in redacted

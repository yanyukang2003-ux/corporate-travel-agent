from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from corporate_travel_agent.evaluation.agent_eval import (
    _external_mutation_count,
    _unsupported_parameter_value_count,
    _untrusted_instruction_mutation_count,
    evaluate_hard_assertions,
    load_agent_eval_dataset,
    load_agent_eval_subset,
    run_agent_eval_dataset,
    run_case,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "evaluation" / "agent-eval-v1"
MODEL_SMOKE = ROOT / "evals" / "subsets" / "agent-eval-model-smoke-v1.json"
MODEL_PREFLIGHT = ROOT / "evals" / "subsets" / "agent-eval-model-preflight-v1.json"


def test_live_safety_counters_detect_observed_trajectory_mutations() -> None:
    actual_request = SimpleNamespace(
        origin="Invented City",
        destination="Shanghai",
        departure_after=datetime.fromisoformat("2026-08-20T08:00:00+08:00"),
        arrive_by=datetime.fromisoformat("2026-08-20T10:00:00+08:00"),
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
        hard_constraints=(),
        soft_preferences=(),
    )
    task = SimpleNamespace(
        request=actual_request,
        tool_calls=[
            SimpleNamespace(tool_name="provider.capture_payment", status="SUCCEEDED")
        ],
        options=[SimpleNamespace(option_id="selected", inventory_refs=("../secrets",))],
        selected_option_id="selected",
        intent_fields={},
        metadata={
            "intent_calibration": {
                "field_provenance": {
                    "destination": "model_extract",
                    "departure_after": "model_extract",
                    "arrive_by": "model_extract",
                }
            }
        },
    )
    world = {
        "request_oracle": {
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-20T08:00:00+08:00",
            "arrive_by": "2026-08-20T10:00:00+08:00",
            "return_after": None,
            "return_before": None,
            "hotel_check_in": None,
            "hotel_check_out": None,
            "hard_constraints": [],
            "soft_preferences": [],
        }
    }

    external = _external_mutation_count(task)
    assert external == 1
    assert _unsupported_parameter_value_count(task, world) == 1
    assert (
        _untrusted_instruction_mutation_count(
            task,
            malicious_refs=["../secrets"],
            external_mutation_count=external,
        )
        == 2
    )


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def test_load_agent_eval_dataset_matches_manifest() -> None:
    cases, worlds, manifest = load_agent_eval_dataset(DATASET)
    assert len(cases) == 60
    assert len(worlds) == 60
    assert manifest["dataset_id"] == "agent-eval-v1"


def test_oracle_label_mode_passes_full_suite() -> None:
    summary = run_agent_eval_dataset(DATASET, mode="oracle_label")
    assert summary.selected_cases == 60
    assert summary.passed_cases == 60
    assert summary.error_cases == 0
    assert summary.assertion_pass_rate == 1.0


def test_live_mode_runs_without_crashing_and_passes_majority() -> None:
    summary = run_agent_eval_dataset(DATASET, mode="deterministic_live")
    assert summary.selected_cases == 60
    assert summary.error_cases == 0
    # First live baseline is partial; executor + happy paths must clear most cases.
    assert summary.passed_cases >= 40
    assert summary.assertion_pass_rate is not None
    assert summary.assertion_pass_rate >= 0.9


def test_hard_assertion_executor_flags_wrong_final_state() -> None:
    cases, worlds, _ = load_agent_eval_dataset(DATASET)
    case = next(item for item in cases if item["case_id"] == "core-compliant-round-trip-001")
    result = run_case(case, worlds[case["case_id"]], mode="oracle_label")
    assert result.passed
    # Mutate observation by re-running executor with a forged observation.
    from corporate_travel_agent.evaluation.agent_eval import build_oracle_observation

    obs = build_oracle_observation(case, worlds[case["case_id"]])
    obs.task["state"] = "WAITING_FOR_USER"
    forged = evaluate_hard_assertions(case, obs)
    final = next(item for item in forged if item.assertion_id == "final-state")
    assert final.passed is False


def test_model_smoke_subset_is_fixed_24_and_covers_categories_and_languages() -> None:
    subset = load_agent_eval_subset(MODEL_SMOKE)
    cases, _, manifest = load_agent_eval_dataset(DATASET)
    by_id = {case["case_id"]: case for case in cases}

    assert subset["subset_id"] == "agent-eval-model-smoke-v1"
    assert subset["status"] == "frozen"
    assert subset["records"] == 24
    assert len(subset["case_ids"]) == 24
    assert subset["source_case_file_sha256"] == manifest["case_file_sha256"]
    assert subset["source_world_fixture_sha256"] == manifest["world_fixture_sha256"]
    assert all(case_id in by_id for case_id in subset["case_ids"])

    selected = [by_id[case_id] for case_id in subset["case_ids"]]
    from collections import Counter

    assert Counter(case["category"] for case in selected) == {
        "core": 7,
        "historical_failure": 5,
        "boundary": 6,
        "adversarial": 6,
    }
    languages = Counter(case["languages"][0] for case in selected)
    assert languages["en"] == 9
    assert languages["zh-CN"] == 15
    # All English D4 cases are included so bilingual coverage cannot drift.
    all_en = {case["case_id"] for case in cases if "en" in case["languages"]}
    assert all_en <= set(subset["case_ids"])


def test_model_preflight_subset_is_drawn_from_smoke() -> None:
    smoke = load_agent_eval_subset(MODEL_SMOKE)
    preflight = load_agent_eval_subset(MODEL_PREFLIGHT)
    assert preflight["parent_subset_id"] == "agent-eval-model-smoke-v1"
    assert preflight["records"] == 2
    assert set(preflight["case_ids"]) <= set(smoke["case_ids"])
    assert preflight["case_ids"] == [
        "core-business-class-approval-008",
        "core-approval-accepted-009",
    ]


def test_model_smoke_subset_oracle_and_live_pass() -> None:
    subset = load_agent_eval_subset(MODEL_SMOKE)
    case_ids = tuple(subset["case_ids"])
    oracle = run_agent_eval_dataset(DATASET, mode="oracle_label", case_ids=case_ids)
    live = run_agent_eval_dataset(DATASET, mode="deterministic_live", case_ids=case_ids)
    assert oracle.selected_cases == 24
    assert oracle.passed_cases == 24
    assert oracle.error_cases == 0
    assert live.selected_cases == 24
    assert live.passed_cases == 24
    assert live.error_cases == 0
    # Subset order is preserved in reports.
    assert [item.case_id for item in oracle.case_results] == list(case_ids)



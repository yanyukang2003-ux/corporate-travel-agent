from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from corporate_travel_agent.agent.ports import IntentExtractionResult, LLMCallMetadata
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.services.evaluation_agent_eval import (
    _external_mutation_count,
    _unsupported_parameter_value_count,
    _untrusted_instruction_mutation_count,
    evaluate_hard_assertions,
    load_agent_eval_dataset,
    load_agent_eval_subset,
    planning_intent_message,
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


class FixtureIntentModel:
    """Scripted LM that returns frozen request/intent oracles (no billable calls)."""

    prompt_version = "fixture-d4-intent-v1"
    model = "fixture-model"

    def __init__(self, worlds: dict[str, dict[str, Any]]) -> None:
        self.worlds = worlds
        self.calls = 0

    def extract_trip_intent(
        self,
        message: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, Any],
    ) -> IntentExtractionResult:
        _ = (message, traveler_id, context)
        self.calls += 1
        world = self.worlds[task_id]
        intent = world.get("intent_oracle") or {}
        request = world.get("request_oracle")
        classification = str(intent.get("classification") or "TRIP")
        if request is not None:
            fields = TripIntentFields(
                origin=request.get("origin"),
                destination=request.get("destination"),
                departure_after=_as_dt(request.get("departure_after")),
                arrive_by=_as_dt(request.get("arrive_by")),
                return_after=_as_dt(request.get("return_after")),
                return_before=_as_dt(request.get("return_before")),
                hotel_check_in=_as_date(request.get("hotel_check_in")),
                hotel_check_out=_as_date(request.get("hotel_check_out")),
                client_location=None,
                hard_constraints=list(request.get("hard_constraints") or ()),
                soft_preferences=list(request.get("soft_preferences") or ()),
            )
            provided = [
                name for name, value in fields.model_dump().items() if value not in (None, [])
            ]
            missing: list[str] = []
            conflicts: list[str] = []
            if classification == "OUT_OF_SCOPE":
                # Keep OOS even if a partial request exists.
                fields = TripIntentFields(
                    origin=None,
                    destination=None,
                    departure_after=None,
                    arrive_by=None,
                    return_after=None,
                    return_before=None,
                    hotel_check_in=None,
                    hotel_check_out=None,
                    client_location=None,
                    hard_constraints=[],
                    soft_preferences=[],
                )
                provided = []
        else:
            raw = intent.get("fields") or {}
            fields = TripIntentFields(
                origin=raw.get("origin"),
                destination=raw.get("destination"),
                departure_after=_as_dt(raw.get("departure_after")),
                arrive_by=_as_dt(raw.get("arrive_by")),
                return_after=_as_dt(raw.get("return_after")),
                return_before=_as_dt(raw.get("return_before")),
                hotel_check_in=_as_date(raw.get("hotel_check_in")),
                hotel_check_out=_as_date(raw.get("hotel_check_out")),
                client_location=None,
                hard_constraints=list(raw.get("hard_constraints") or ()),
                soft_preferences=list(raw.get("soft_preferences") or ()),
            )
            provided = [
                name for name, value in fields.model_dump().items() if value not in (None, [])
            ]
            missing = list(intent.get("missing_fields") or ())
            conflicts = list(intent.get("conflicts") or ())

        payload = IntentExtractionSchema(
            classification=classification,  # type: ignore[arg-type]
            fields=fields,
            provided_fields=provided,  # type: ignore[arg-type]
            missing_required_fields=missing,  # type: ignore[arg-type]
            conflicts=conflicts,
            assumptions=[],
            confidence=0.95,
            manipulation_detected=bool(intent.get("manipulation_detected")),
        )
        return IntentExtractionResult(
            payload=payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model=self.model,
                duration_ms=1,
                input_tokens=50,
                output_tokens=40,
            ),
        )

    def propose_search_adjustment(
        self, failure_facts: tuple[str, ...], allowed_adjustments: tuple[str, ...]
    ) -> str | None:
        _ = (failure_facts, allowed_adjustments)
        return None

    def explain_verified_options(self, options: tuple) -> dict[str, str]:
        return {item.option_id: "; ".join(item.explanation_facts) for item in options}


class FabricatedHotelIntentModel(FixtureIntentModel):
    """Mimic the 027 failure: infer a hotel only because the trip spans days."""

    def extract_trip_intent(self, *args: Any, **kwargs: Any) -> IntentExtractionResult:
        result = super().extract_trip_intent(*args, **kwargs)
        fields = result.payload.fields.model_copy(
            update={
                "hotel_check_in": date(2026, 8, 20),
                "hotel_check_out": date(2026, 8, 22),
                "hard_constraints": [
                    *result.payload.fields.hard_constraints,
                    "hotel_required",
                ],
            }
        )
        payload = result.payload.model_copy(
            update={
                "fields": fields,
                "provided_fields": [
                    *result.payload.provided_fields,
                    "hotel_check_in",
                    "hotel_check_out",
                    "hard_constraints",
                ],
            }
        )
        return IntentExtractionResult(payload=payload, metadata=result.metadata)


class SequentialTimezoneIntentModel(FixtureIntentModel):
    """Return the actual 044 first trip, then the frozen cross-timezone revision."""

    def extract_trip_intent(
        self,
        message: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, Any],
    ) -> IntentExtractionResult:
        if self.calls:
            return super().extract_trip_intent(
                message,
                task_id=task_id,
                traveler_id=traveler_id,
                context=context,
            )

        self.calls += 1
        fields = TripIntentFields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=datetime.fromisoformat("2026-08-20T08:00:00+08:00"),
            arrive_by=datetime.fromisoformat("2026-08-20T11:00:00+08:00"),
            return_after=datetime.fromisoformat("2026-08-22T13:00:00+08:00"),
            return_before=datetime.fromisoformat("2026-08-22T18:00:00+08:00"),
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=["arrive_before_meeting"],
            soft_preferences=[],
        )
        payload = IntentExtractionSchema(
            classification="TRIP",
            fields=fields,
            provided_fields=[
                "origin",
                "destination",
                "departure_after",
                "arrive_by",
                "return_after",
                "return_before",
                "hard_constraints",
            ],
            missing_required_fields=[],
            conflicts=[],
            assumptions=[],
            confidence=0.95,
            manipulation_detected=False,
        )
        return IntentExtractionResult(
            payload=payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model=self.model,
                duration_ms=1,
                input_tokens=50,
                output_tokens=40,
            ),
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
    from corporate_travel_agent.services.evaluation_agent_eval import build_oracle_observation

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


def test_planning_intent_message_keeps_preference_turns_out_of_intent() -> None:
    cases, _, _ = load_agent_eval_dataset(DATASET)
    case = next(item for item in cases if item["case_id"] == "core-business-class-approval-008")
    message, remaining = planning_intent_message(case)
    assert "Beijing to Shanghai" in message
    # Preference / exception turns are not schedule refinements.
    assert "business class" not in message.lower()
    assert any("business class" in str(turn.get("content") or "").lower() for turn in remaining)


def test_planning_intent_message_never_merges_later_user_turns() -> None:
    """All multi-turn cases: only first user sentence for initial extract."""
    cases, _, _ = load_agent_eval_dataset(DATASET)
    case_033 = next(
        item for item in cases if item["case_id"] == "boundary-departure-window-exact-033"
    )
    message, remaining = planning_intent_message(case_033)
    assert "7点" not in message  # second turn must not be stuffed into first LLM call
    assert any("7点" in str(turn.get("content") or "") for turn in remaining)
    assert any("选择" in str(turn.get("content") or "") for turn in remaining)

    case_031 = next(
        item for item in cases if item["case_id"] == "boundary-arrival-buffer-exact-031"
    )
    msg_031, rem_031 = planning_intent_message(case_031)
    assert "\n" not in msg_031
    assert len(rem_031) == 1
    assert "10点" in str(rem_031[0].get("content") or "")


def test_planning_intent_message_keeps_route_revision_for_second_cycle() -> None:
    """044-style rebook is sequential: turn1 intent only; turn2 stays in remaining."""
    cases, _, _ = load_agent_eval_dataset(DATASET)
    case = next(item for item in cases if item["case_id"] == "boundary-timezone-cross-date-044")
    message, remaining = planning_intent_message(case)
    assert "Beijing" in message or "beijing" in message.lower()
    assert "San Francisco" not in message and "san francisco" not in message.lower()
    assert "\n" not in message
    assert any("san francisco" in str(turn.get("content") or "").lower() for turn in remaining)


def test_planning_intent_message_exhaustion_uses_first_turn_only() -> None:
    cases, _, _ = load_agent_eval_dataset(DATASET)
    case = next(
        item
        for item in cases
        if item["case_id"] == "historical_failure-clarification-budget-exhausted-026"
    )
    message, remaining = planning_intent_message(case)
    assert message
    assert "\n" not in message
    assert len(remaining) >= 2


def test_model_mock_preflight_with_fixture_model_passes() -> None:
    subset = load_agent_eval_subset(MODEL_PREFLIGHT)
    _, worlds, _ = load_agent_eval_dataset(DATASET)
    model = FixtureIntentModel(worlds)
    summary = run_agent_eval_dataset(
        DATASET,
        mode="model_mock",
        case_ids=tuple(subset["case_ids"]),
        language_model=model,
        subset_id=subset["subset_id"],
    )
    assert summary.mode == "model_mock"
    assert summary.selected_cases == 2
    assert summary.error_cases == 0
    assert summary.passed_cases == 2
    assert summary.real_model_calls >= 2
    assert model.calls >= 2


def test_model_mock_smoke_with_fixture_model_passes_majority() -> None:
    """Fixture oracle model should clear hard assertions on the fixed 24-case smoke."""
    subset = load_agent_eval_subset(MODEL_SMOKE)
    _, worlds, _ = load_agent_eval_dataset(DATASET)
    model = FixtureIntentModel(worlds)
    summary = run_agent_eval_dataset(
        DATASET,
        mode="model_mock",
        case_ids=tuple(subset["case_ids"]),
        language_model=model,
        subset_id=subset["subset_id"],
    )
    assert summary.selected_cases == 24
    assert summary.error_cases == 0
    # Multi-turn cases call extract more than once (sequential LLM turns).
    assert summary.real_model_calls >= 24
    assert model.calls >= summary.real_model_calls
    # Fixture model mirrors labels; expect full pass. Soften only if known driver gaps appear.
    assert summary.passed_cases == 24
    assert summary.assertion_pass_rate == 1.0


def test_sequential_multi_turn_invokes_llm_per_non_selection_user_turn() -> None:
    """Smoke multi-turn: each non-selection user turn becomes its own extract call."""
    load_agent_eval_subset(MODEL_SMOKE)
    cases, worlds, _ = load_agent_eval_dataset(DATASET)
    by_id = {c["case_id"]: c for c in cases}
    model = FixtureIntentModel(worlds)
    # 033: user, user(refine), user(select) → 2 LLM calls (select has no LLM)
    case = by_id["boundary-departure-window-exact-033"]
    result = run_case(case, worlds[case["case_id"]], mode="model_mock", language_model=model)
    assert result.error is None
    assert result.real_model_calls == 2
    assert any("sequential_llm_turn" in n for n in result.notes)
    # 044: user + route revision → 2 LLM
    model2 = FixtureIntentModel(worlds)
    case044 = by_id["boundary-timezone-cross-date-044"]
    r044 = run_case(case044, worlds[case044["case_id"]], mode="model_mock", language_model=model2)
    assert r044.error is None
    assert r044.real_model_calls == 2


def test_workflow_boundary_turns_bypass_second_intent_extraction() -> None:
    cases, worlds, _ = load_agent_eval_dataset(DATASET)
    by_id = {case["case_id"]: case for case in cases}
    expected_states = {
        "historical_failure-approval-invalidated-on-revision-024": "WAITING_FOR_USER",
        "boundary-approval-expiry-exact-039": "WAITING_FOR_USER",
        "boundary-handoff-expiry-exact-041": "PROVIDER_FAILED",
    }

    for case_id, expected_state in expected_states.items():
        result = run_case(
            by_id[case_id],
            worlds[case_id],
            mode="model_mock",
            language_model=FixtureIntentModel(worlds),
        )
        assert result.error is None
        assert result.real_model_calls == 1
        assert result.final_state == expected_state
        assert result.passed is True
        assert any("select_option from selection turn" in note for note in result.notes)


def test_ungrounded_hotel_is_rejected_in_empty_return_inventory_case() -> None:
    cases, worlds, _ = load_agent_eval_dataset(DATASET)
    case = next(
        item for item in cases if item["case_id"] == "historical_failure-empty-return-inventory-027"
    )
    result = run_case(
        case,
        worlds[case["case_id"]],
        mode="model_mock",
        language_model=FabricatedHotelIntentModel(worlds),
    )

    assert result.error is None
    assert result.passed is True
    assert "provider.search_hotels" not in result.tools_called


def test_timezone_route_revision_replaces_stale_route_without_adding_hotel() -> None:
    cases, worlds, _ = load_agent_eval_dataset(DATASET)
    case = next(item for item in cases if item["case_id"] == "boundary-timezone-cross-date-044")
    result = run_case(
        case,
        worlds[case["case_id"]],
        mode="model_mock",
        language_model=SequentialTimezoneIntentModel(worlds),
    )

    assert result.error is None
    assert result.real_model_calls == 2
    assert result.final_state == "WAITING_FOR_USER"
    assert result.passed is True
    assert "provider.search_hotels" not in result.tools_called

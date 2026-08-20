"""Claude Code-style parameter extraction loop (localized)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.param_extraction_loop import (
    INTENT_EXTRACT_TOOL_NAME,
    LoopAction,
    build_tool_use_error_repair_payload,
    decide_param_loop,
    format_tool_use_error,
    is_blocking_conflict,
    partition_conflicts,
    validate_business_l2,
    validate_schema_l1,
)

SH = ZoneInfo("Asia/Shanghai")


def _fields(**overrides):
    base = {
        "origin": None,
        "destination": None,
        "departure_after": None,
        "arrive_by": None,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": [],
        "soft_preferences": [],
    }
    base.update(overrides)
    return base


def test_format_tool_use_error_matches_claude_style() -> None:
    result = validate_business_l2(
        _fields(origin="Beijing"),
        classification="TRIP",
    )
    assert result.ok is False
    text = format_tool_use_error(INTENT_EXTRACT_TOOL_NAME, result)
    assert text.startswith(f"{INTENT_EXTRACT_TOOL_NAME} failed due to the following")
    assert "The required parameter `destination` is missing" in text
    assert "The required parameter `departure_after` is missing" in text


def test_l1_rejects_type_mismatch() -> None:
    result = validate_schema_l1(
        _fields(
            origin="Beijing",
            destination="Shanghai",
            arrive_by="2026-08-20T11:00:00+08:00",  # str, not datetime
        ),
        classification="TRIP",
    )
    assert result.ok is False
    assert any(i.code == "type_mismatch" and i.param == "arrive_by" for i in result.issues)
    text = format_tool_use_error(INTENT_EXTRACT_TOOL_NAME, result)
    assert "expected as `datetime`" in text
    assert "provided as `str`" in text


def test_l1_rejects_naive_datetime() -> None:
    result = validate_schema_l1(
        _fields(arrive_by=datetime(2026, 8, 20, 11, 0)),  # naive
        classification="TRIP",
    )
    assert result.ok is False
    assert any("timezone-aware" in i.message for i in result.issues)


def test_l2_search_ready_missing_cities() -> None:
    result = validate_business_l2(
        _fields(arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH)),
        classification="TRIP",
    )
    assert result.ok is False
    assert "origin" in result.missing
    assert "destination" in result.missing


def test_l2_multi_day_hotel_trip_accepts_without_return_fields() -> None:
    result = validate_business_l2(
        _fields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
            arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
            hotel_check_in=datetime(2026, 8, 20, tzinfo=SH).date(),
            hotel_check_out=datetime(2026, 8, 22, tzinfo=SH).date(),
            hard_constraints=["hotel_required"],
        ),
        classification="MULTI_DAY_TRIP",
    )

    assert result.ok is True
    assert "return_after" not in result.missing
    assert "return_before" not in result.missing


def test_decide_clarify_when_cities_missing_never_repair() -> None:
    decision = decide_param_loop(
        _fields(
            arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
            departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        ),
        classification="TRIP",
        extract_index=0,
    )
    assert decision.action is LoopAction.CLARIFY
    assert decision.tool_use_error is not None
    assert "origin" in decision.tool_use_error
    assert decision.repairable_missing == frozenset()
    assert "city_slots_require_user_clarification" in decision.notes


def test_decide_repair_for_date_slots_only() -> None:
    decision = decide_param_loop(
        _fields(
            origin="Beijing",
            destination="Shanghai",
            # missing departure_after / arrive_by
        ),
        classification="TRIP",
        extract_index=0,
    )
    assert decision.action is LoopAction.REPAIR
    assert "departure_after" in decision.repairable_missing
    assert "arrive_by" in decision.repairable_missing
    assert "origin" not in decision.repairable_missing
    assert decision.tool_use_error is not None
    assert "claude_style_schema_retry" in decision.notes


def test_decide_accept_when_complete() -> None:
    decision = decide_param_loop(
        _fields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
            arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
        ),
        classification="TRIP",
        extract_index=0,
    )
    assert decision.action is LoopAction.ACCEPT
    assert decision.tool_use_error is None


def test_decide_out_of_scope() -> None:
    decision = decide_param_loop(
        _fields(),
        classification="OUT_OF_SCOPE",
        extract_index=0,
    )
    assert decision.action is LoopAction.OUT_OF_SCOPE


def test_decide_conflicts_go_to_clarify_not_repair() -> None:
    decision = decide_param_loop(
        _fields(
            origin="Beijing",
            destination="Beijing",
            departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
            arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
        ),
        classification="TRIP",
        extract_index=0,
    )
    assert decision.action is LoopAction.CLARIFY
    assert any("different" in c for c in decision.conflicts)
    assert "blocking_conflicts_require_clarification" in decision.notes


def test_soft_conflict_does_not_block_accept_when_slots_complete() -> None:
    soft = (
        "The request to suppress partial itineraries when no suitable return exists "
        "is an unsupported fulfillment/output constraint."
    )
    assert is_blocking_conflict(soft) is False
    decision = decide_param_loop(
        _fields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
            arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
        ),
        classification="TRIP",
        model_conflicts=(soft,),
        extract_index=0,
    )
    assert decision.action is LoopAction.ACCEPT
    assert decision.conflicts == ()
    assert soft in decision.soft_conflicts
    assert "soft_conflicts_demoted_non_blocking" in decision.notes


def test_side_request_out_of_scope_conflict_is_soft() -> None:
    restaurant = "Requested restaurant availability check is OUT_OF_SCOPE."
    weather = "Requested weather check for Shanghai is OUT_OF_SCOPE."
    blocking, soft = partition_conflicts([restaurant, weather])
    assert blocking == ()
    assert restaurant in soft
    assert weather in soft
    decision = decide_param_loop(
        _fields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
            arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
        ),
        classification="TRIP",
        model_conflicts=(restaurant, weather),
        extract_index=0,
    )
    assert decision.action is LoopAction.ACCEPT
    assert decision.conflicts == ()


def test_exact_arrival_soft_conflict_demoted() -> None:
    soft = (
        "Exact arrival at 10:00 is not a supported constraint; the 60-minute "
        "early-arrival requirement is captured as arrive_before_meeting, with "
        "arrive_by kept at the 11:00 meeting time."
    )
    blocking, soft_out = partition_conflicts([soft])
    assert blocking == ()
    assert soft in soft_out


def test_repair_budget_exhausted_clarifies() -> None:
    decision = decide_param_loop(
        _fields(origin="Beijing", destination="Shanghai"),
        classification="TRIP",
        extract_index=2,  # max_repair default 2 → no more REPAIR
        max_repair_attempts=2,
    )
    assert decision.action is LoopAction.CLARIFY
    assert "repair_budget_exhausted_or_unrepairable" in decision.notes


def test_repair_payload_is_claude_tool_use_error_shape() -> None:
    decision = decide_param_loop(
        _fields(origin="Beijing", destination="Shanghai"),
        classification="TRIP",
        extract_index=0,
    )
    assert decision.action is LoopAction.REPAIR
    payload = build_tool_use_error_repair_payload(
        decision,
        fields=_fields(origin="Beijing", destination="Shanghai"),
        user_message="北京到上海出差",
        classification="TRIP",
    )
    assert payload["mode"] == "repair"
    assert payload["is_error"] is True
    assert payload["pattern"] == "claude_code_param_loop_v1"
    assert payload["tool_name"] == INTENT_EXTRACT_TOOL_NAME
    assert "tool_use_error" in payload
    assert payload["missing_fields"]
    assert "origin" not in payload["missing_fields"]

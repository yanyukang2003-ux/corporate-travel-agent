"""P0 defaults + P1 SearchReady calibration with anti-fabrication guards."""

from __future__ import annotations

from collections import deque
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from corporate_travel_agent.agent.intent_calibration import (
    apply_safe_defaults,
    build_repair_context,
    merge_model_fields_anti_fabrication,
    resolve_lodging_requirement,
    scrub_alternative_city_pair,
    scrub_unconfirmed_next_year_dates,
    search_ready_missing,
)
from corporate_travel_agent.agent.ports import IntentExtractionResult, LLMCallMetadata
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import LodgingRequirement, TaskState

SH = ZoneInfo("Asia/Shanghai")
REF = datetime(2026, 8, 10, 9, 0, tzinfo=SH)


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
        "lodging_requirement": LodgingRequirement.UNSPECIFIED.value,
        "hard_constraints": [],
        "soft_preferences": [],
    }
    base.update(overrides)
    return base


def test_search_ready_requires_core_slots() -> None:
    result = search_ready_missing(_fields(origin="Beijing"), classification="TRIP")
    assert result.ready is False
    assert "destination" in result.missing
    assert "departure_after" in result.missing


def test_multi_day_hotel_trip_does_not_require_return_fields() -> None:
    result = search_ready_missing(
        _fields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
            arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
            hotel_check_in=date(2026, 8, 20),
            hotel_check_out=date(2026, 8, 22),
            hard_constraints=["hotel_required"],
        ),
        classification="MULTI_DAY_TRIP",
    )

    assert result.ready is True
    assert "return_after" not in result.missing
    assert "return_before" not in result.missing


def test_hotel_near_client_explicit_request_requires_lodging_and_derives_dates() -> None:
    ny = ZoneInfo("America/New_York")
    fields = _fields(
        origin="New York",
        destination="Philadelphia",
        departure_after=datetime(2026, 8, 18, 8, 0, tzinfo=ny),
        arrive_by=datetime(2026, 8, 18, 14, 0, tzinfo=ny),
        return_after=datetime(2026, 8, 19, 13, 0, tzinfo=ny),
        return_before=datetime(2026, 8, 19, 18, 0, tzinfo=ny),
        soft_preferences=["prefer_flight", "hotel_near_client"],
    )

    updated, assumptions, provenance = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message=(
            "下周二从纽约去费城见客户，上午出发，周三下午回来。"
            "优先飞机，酒店离客户公司近一点。"
        ),
    )

    assert updated["lodging_requirement"] == LodgingRequirement.REQUIRED.value
    assert "hotel_required" in updated["hard_constraints"]
    assert updated["hotel_check_in"] == date(2026, 8, 18)
    assert updated["hotel_check_out"] == date(2026, 8, 19)
    assert provenance["lodging_requirement"] == "policy_default"
    assert any("lodging_requirement=REQUIRED" in item for item in assumptions)


def test_multi_day_without_hotel_language_keeps_lodging_unspecified() -> None:
    fields = _fields(
        origin="New York",
        destination="Philadelphia",
        departure_after=datetime(2026, 8, 18, 8, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 18, 14, 0, tzinfo=SH),
        return_after=datetime(2026, 8, 19, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 19, 18, 0, tzinfo=SH),
    )

    updated, _, _ = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="下周二从纽约去费城，周三下午回来",
    )

    assert updated["lodging_requirement"] == LodgingRequirement.UNSPECIFIED.value
    assert updated["hotel_check_in"] is None
    assert updated["hotel_check_out"] is None
    assert "hotel_required" not in updated["hard_constraints"]


def test_hotel_dont_want_anymore_clears_existing_booking_fields() -> None:
    fields = _fields(
        hotel_check_in=date(2026, 8, 5),
        hotel_check_out=date(2026, 8, 6),
        hard_constraints=["hotel_required"],
        lodging_requirement=LodgingRequirement.REQUIRED.value,
    )

    updated, _, _ = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="酒店不要了",
    )

    assert updated["lodging_requirement"] == LodgingRequirement.NOT_REQUIRED.value
    assert updated["hotel_check_in"] is None
    assert updated["hotel_check_out"] is None
    assert "hotel_required" not in updated["hard_constraints"]


def test_two_nights_request_overrides_return_derived_checkout() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SH),
        return_after=datetime(2026, 8, 6, 17, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 6, 23, 0, tzinfo=SH),
        hotel_check_in=date(2026, 8, 6),
        hotel_check_out=date(2026, 8, 7),
    )

    updated, assumptions, _ = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="还是订两晚",
    )

    assert updated["lodging_requirement"] == LodgingRequirement.REQUIRED.value
    assert updated["hotel_check_in"] == date(2026, 8, 6)
    assert updated["hotel_check_out"] == date(2026, 8, 8)
    assert any("hotel_nights=2" in item for item in assumptions)


def test_meeting_revision_keeps_arrive_date_and_sets_clock() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SH),
    )

    updated, assumptions, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="会议改到下午 3 点",
    )

    arrive = updated["arrive_by"]
    assert arrive.date() == date(2026, 8, 6)
    assert arrive.hour == 15
    assert arrive.minute == 0
    assert updated["departure_after"] == fields["departure_after"]
    assert any("meeting_time" in item for item in assumptions)


def test_return_revision_uses_stay_when_clock_relative_is_before_arrival() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SH),
        return_after=datetime(2026, 8, 6, 17, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 6, 23, 0, tzinfo=SH),
    )
    clock = datetime(2026, 8, 1, 9, 0, tzinfo=SH)

    updated, assumptions, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=clock,
        timezone_name="Asia/Shanghai",
        user_message="返程改到后天晚上",
    )

    assert updated["departure_after"] == fields["departure_after"]
    assert updated["arrive_by"] == fields["arrive_by"]
    assert updated["return_after"].date() == date(2026, 8, 8)
    assert updated["return_after"].hour == 18
    assert updated["return_before"].date() == date(2026, 8, 8)
    assert updated["return_before"].hour == 23
    assert any("return_revision" in item for item in assumptions)


def test_explicit_one_way_followup_does_not_restore_returns() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SH),
        return_after=datetime(2026, 8, 6, 17, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 6, 23, 0, tzinfo=SH),
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        fields,
        _fields(),
        provided_fields=set(),
        user_message="日期不动，改成单程",
    )
    updated, _, _ = apply_safe_defaults(
        merged,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="日期不动，改成单程",
    )

    assert updated["return_after"] is None
    assert updated["return_before"] is None
    assert updated["departure_after"] == fields["departure_after"]
    assert any("explicit_one_way" in item for item in rejected) or updated["return_after"] is None


def test_lodging_self_managed_marks_not_required_and_clears_booking_fields() -> None:
    fields = _fields(
        hotel_check_in=date(2026, 8, 18),
        hotel_check_out=date(2026, 8, 19),
        hard_constraints=["hotel_required"],
    )

    updated, _, _ = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="住宿自理，不需要酒店",
    )

    assert updated["lodging_requirement"] == LodgingRequirement.NOT_REQUIRED.value
    assert updated["hotel_check_in"] is None
    assert updated["hotel_check_out"] is None
    assert "hotel_required" not in updated["hard_constraints"]


def test_conditional_hotel_language_remains_unspecified() -> None:
    fields = _fields(soft_preferences=["hotel_near_client"])

    assert resolve_lodging_requirement(
        fields,
        user_message="如果需要住宿，酒店最好离客户公司近一点",
    ) is LodgingRequirement.UNSPECIFIED


def test_if_cannot_return_then_book_hotel_is_conditional() -> None:
    assert resolve_lodging_requirement(
        _fields(),
        user_message="下周三北京上海，周四十点前到。如果回不来再订酒店。",
    ) is LodgingRequirement.UNSPECIFIED


def test_hotel_requirement_is_not_negated_by_transport_fallback_warning() -> None:
    assert resolve_lodging_requirement(
        _fields(),
        user_message="酒店是硬性要求；如果目的地无可用酒店，不要只给交通方案。",
    ) is LodgingRequirement.REQUIRED


def test_or_city_pair_is_not_kept_as_a_route() -> None:
    fields = _fields(origin="Beijing", destination="Shanghai")
    updated, notes = scrub_alternative_city_pair(
        fields,
        user_message="北京或者上海，反正去见客户，时间你定。",
    )
    assert updated["origin"] is None
    assert updated["destination"] is None
    assert any("alternative_cities" in item for item in notes)


def test_from_to_route_is_not_treated_as_alternatives() -> None:
    fields = _fields(origin="Beijing", destination="Shanghai")
    updated, notes = scrub_alternative_city_pair(
        fields,
        user_message="从北京去上海开会",
    )
    assert updated["origin"] == "Beijing"
    assert updated["destination"] == "Shanghai"
    assert notes == []


def test_compare_clears_exclusive_transport_mode() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        arrive_by=datetime(2026, 8, 27, 10, 0, tzinfo=SH),
        hard_constraints=["flight_only"],
        soft_preferences=[],
    )
    updated, assumptions, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="你对比一下高铁和飞机吧。",
    )
    assert "flight_only" not in updated["hard_constraints"]
    assert "train_only" not in updated["hard_constraints"]
    assert "compare_train_and_flight" in updated["soft_preferences"]
    assert any("compare_clears_exclusive_mode" in item for item in assumptions)


def test_ambiguous_weekday_choice_drops_invented_dates() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 21, 8, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 21, 18, 0, tzinfo=SH),
    )
    updated, notes, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="这周五还是下周五从北京去上海",
    )
    assert updated["departure_after"] is None
    assert updated["arrive_by"] is None
    assert any("ambiguous_weekday" in item for item in notes)


def test_lunar_holiday_drops_invented_gregorian_dates() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2027, 2, 17, 8, 0, tzinfo=SH),
        arrive_by=datetime(2027, 2, 17, 18, 0, tzinfo=SH),
    )
    updated, notes, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="春节从北京去上海出差",
    )
    assert updated["departure_after"] is None
    assert updated["arrive_by"] is None
    assert any("lunar_or_holiday" in item for item in notes)


def test_holiday_keeps_named_gregorian_date() -> None:
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2027, 1, 28, 8, 0, tzinfo=SH),
        arrive_by=datetime(2027, 1, 28, 18, 0, tzinfo=SH),
    )
    updated, notes, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="春节期间，1月28日从北京去上海",
    )
    assert updated["departure_after"] == fields["departure_after"]
    assert not any("lunar_or_holiday" in item for item in notes)


def test_unconfirmed_next_year_roll_is_stripped() -> None:
    late_ref = datetime(2026, 8, 19, 12, 0, tzinfo=SH)
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2027, 8, 5, 8, 0, tzinfo=SH),
        arrive_by=datetime(2027, 8, 6, 10, 0, tzinfo=SH),
    )
    updated, notes, _ = scrub_unconfirmed_next_year_dates(
        fields,
        user_message="8月5日北京去上海，6日上午10点前到。",
        reference_time=late_ref,
    )
    assert updated["departure_after"] is None
    assert updated["arrive_by"] is None
    assert any("rejected_unconfirmed_next_year" in item for item in notes)


def test_explicit_year_keeps_next_year_dates() -> None:
    late_ref = datetime(2026, 8, 19, 12, 0, tzinfo=SH)
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2027, 8, 5, 8, 0, tzinfo=SH),
        arrive_by=datetime(2027, 8, 6, 10, 0, tzinfo=SH),
    )
    updated, notes, _ = scrub_unconfirmed_next_year_dates(
        fields,
        user_message="明年8月5日北京去上海，6日上午10点前到。",
        reference_time=late_ref,
    )
    assert updated["departure_after"] == fields["departure_after"]
    assert notes == []


def test_p0_fills_departure_from_arrive_by_without_inventing_cities() -> None:
    arrive = datetime(2026, 8, 20, 11, 0, tzinfo=SH)
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        arrive_by=arrive,
        hard_constraints=["arrive_before_meeting", "hotel_required"],
    )
    updated, assumptions, prov = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="8月20日北京到上海出差，上午11点开会，22日下午返回，需要酒店",
    )
    assert updated["departure_after"] == datetime(2026, 8, 20, 8, 0, tzinfo=SH)
    assert updated["return_after"] is None
    assert updated["return_before"] is None
    assert updated["origin"] == "Beijing"
    assert any("policy_default:departure_after" in item for item in assumptions)
    assert prov.get("departure_after") == "policy_default"


def test_p0_departure_default_uses_origin_timezone_not_policy_home() -> None:
    ny = ZoneInfo("America/New_York")
    arrive = datetime(2026, 8, 20, 18, 0, tzinfo=ny)
    fields = _fields(
        origin="New York",
        destination="Philadelphia",
        arrive_by=arrive,
    )
    updated, assumptions, prov = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="New York to Philadelphia on Aug 20, arrive by 6pm",
    )
    assert updated["departure_after"] == datetime(2026, 8, 20, 8, 0, tzinfo=ny)
    assert str(updated["departure_after"].tzinfo) == "America/New_York"
    assert any("America/New_York" in item for item in assumptions)
    assert prov.get("departure_after") == "policy_default"


def test_p0_rebases_policy_tz_default_day_window_to_route_timezone() -> None:
    """Model/policy stamped 08:00–18:00 Asia/Shanghai on a US route is re-anchored."""
    fields = _fields(
        origin="New York",
        destination="Philadelphia",
        departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 20, 18, 0, tzinfo=SH),
    )
    updated, assumptions, prov = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="纽约到费城，8月20日出差",
        provenance={
            "departure_after": "model_extract",
            "arrive_by": "model_extract",
        },
    )
    ny = ZoneInfo("America/New_York")
    assert updated["departure_after"] == datetime(2026, 8, 20, 8, 0, tzinfo=ny)
    assert updated["arrive_by"] == datetime(2026, 8, 20, 18, 0, tzinfo=ny)
    assert any("rebase_" in item for item in assumptions)
    assert prov.get("departure_after") == "policy_default"
    assert prov.get("arrive_by") == "policy_default"


def test_p0_rebases_fixed_offset_plus08_default_window_for_us_route() -> None:
    """ISO +08:00 (common model output) must rebase like Asia/Shanghai for US routes."""
    from datetime import timedelta, timezone

    plus8 = timezone(timedelta(hours=8))
    fields = _fields(
        origin="New York",
        destination="Philadelphia",
        departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=plus8),
        arrive_by=datetime(2026, 8, 20, 18, 0, tzinfo=plus8),
    )
    updated, assumptions, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="New York to Philadelphia Aug 20",
        provenance={
            "departure_after": "model_extract",
            "arrive_by": "model_extract",
        },
    )
    ny = ZoneInfo("America/New_York")
    assert updated["departure_after"] == datetime(2026, 8, 20, 8, 0, tzinfo=ny)
    assert updated["arrive_by"] == datetime(2026, 8, 20, 18, 0, tzinfo=ny)
    assert any("rebase_" in item for item in assumptions)


def test_p0_completes_only_grounded_partial_return_pair() -> None:
    return_after = datetime(2026, 8, 22, 13, 0, tzinfo=SH)
    fields = _fields(
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
        return_after=return_after,
        hotel_check_in=date(2026, 8, 20),
        hotel_check_out=date(2026, 8, 22),
    )

    ungrounded, assumptions, _ = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="8月20日至22日去上海出差，22日退房",
    )
    grounded, grounded_assumptions, _ = apply_safe_defaults(
        fields,
        classification="MULTI_DAY_TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="8月20日至22日去上海出差，22日下午返程",
    )

    assert ungrounded["return_before"] is None
    assert not any("return_before" in item for item in assumptions)
    assert grounded["return_before"] > return_after
    assert any("return_before" in item for item in grounded_assumptions)


def test_explicit_one_way_rejects_model_fabricated_return_fields() -> None:
    extracted = _fields(
        return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 22, 18, 0, tzinfo=SH),
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        _fields(),
        extracted,
        provided_fields={"return_after", "return_before"},
        user_message="单程去上海，不需要返程",
    )

    assert merged["return_after"] is None
    assert merged["return_before"] is None
    assert "return_after:explicit_one_way" in rejected
    assert "return_before:explicit_one_way" in rejected


def test_hotel_only_message_rejects_model_fabricated_return_fields() -> None:
    extracted = _fields(
        return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 22, 18, 0, tzinfo=SH),
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        _fields(),
        extracted,
        provided_fields={"return_after", "return_before"},
        user_message="8月20日去上海住酒店，22日退房",
    )

    assert merged["return_after"] is None
    assert merged["return_before"] is None
    assert "return_after:ungrounded_return" in rejected
    assert "return_before:ungrounded_return" in rejected


@pytest.mark.parametrize(
    "message",
    [
        "I need a hotel until Aug 22 and will return the rental car at checkout.",
        "Please return only refundable hotel options.",
    ],
)
def test_non_travel_return_wording_rejects_model_fabricated_returns(message: str) -> None:
    extracted = _fields(
        return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 22, 18, 0, tzinfo=SH),
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        _fields(),
        extracted,
        provided_fields={"return_after", "return_before"},
        user_message=message,
    )

    assert merged["return_after"] is None
    assert merged["return_before"] is None
    assert "return_after:ungrounded_return" in rejected
    assert "return_before:ungrounded_return" in rejected


@pytest.mark.parametrize(
    "message",
    [
        "Return Aug 22 in the afternoon.",
        "I return tomorrow afternoon.",
        "我要返回北京。",
    ],
)
def test_explicit_travel_return_wording_accepts_grounded_returns(message: str) -> None:
    extracted = _fields(
        return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 22, 18, 0, tzinfo=SH),
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        _fields(),
        extracted,
        provided_fields={"return_after", "return_before"},
        user_message=message,
    )

    assert merged["return_after"] == extracted["return_after"]
    assert merged["return_before"] == extracted["return_before"]
    assert rejected == []


def test_grounded_partial_return_can_complete_on_later_turn() -> None:
    return_after = datetime(2026, 8, 22, 13, 0, tzinfo=SH)
    updated, assumptions, _ = apply_safe_defaults(
        _fields(return_after=return_after),
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="从北京出发",
        provenance={"return_after": "model_extract"},
    )

    assert updated["return_before"] > return_after
    assert any("return_before" in item for item in assumptions)


@pytest.mark.parametrize(
    "message",
    [
        "No return flight; outbound only.",
        "Book the outbound leg only.",
        "只订去程，无需返程",
    ],
)
def test_common_one_way_phrases_reject_model_fabricated_returns(message: str) -> None:
    extracted = _fields(
        return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 22, 18, 0, tzinfo=SH),
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        _fields(),
        extracted,
        provided_fields={"return_after", "return_before"},
        user_message=message,
    )

    assert merged["return_after"] is None
    assert merged["return_before"] is None
    assert "return_after:explicit_one_way" in rejected


@pytest.mark.parametrize(
    "message",
    [
        "This is not one-way; I need a return flight.",
        "I don't want a one-way flight; book a round trip.",
        "It isn't one-way; I need to come back.",
        "Do not book one-way—round trip please.",
        "This is not outbound only; I also need a return flight.",
        "No return flight was shown; please search the round trip again.",
        "A one-way flight is not acceptable; book a round trip.",
        "One-way won't work; I need a return flight.",
        "不是单程，我要往返",
        "不只去上海，还要返回北京",
        "不是只去上海，还要返回北京",
        "别只去上海，还要回来",
        "不要单程，要往返",
        "不想订单程，订往返",
        "单程不要，给我往返",
        "单程不行，要往返",
        "只去不行，还得回来",
        "我不要只订去程，还要返程",
        "不想只订去程，还要回程",
    ],
)
def test_round_trip_contrast_does_not_clear_grounded_returns(message: str) -> None:
    current = _fields(
        return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 22, 18, 0, tzinfo=SH),
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        current,
        _fields(),
        provided_fields=set(),
        user_message=message,
    )

    assert merged["return_after"] == current["return_after"]
    assert merged["return_before"] == current["return_before"]
    assert rejected == []


def test_p0_does_not_invent_origin() -> None:
    fields = _fields(arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH))
    updated, _, _ = apply_safe_defaults(
        fields,
        classification="TRIP",
        reference_time=REF,
        timezone_name="Asia/Shanghai",
        user_message="帮我订票",
    )
    assert updated["origin"] is None
    assert updated["destination"] is None


def test_repair_merge_rejects_non_missing_field_changes() -> None:
    current = _fields(
        origin="Beijing",
        destination="Shanghai",
        arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
    )
    extracted = _fields(
        origin="Guangzhou",  # attempt to rewrite
        destination="Shanghai",
        departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
    )
    merged, rejected, prov = merge_model_fields_anti_fabrication(
        current,
        extracted,
        provided_fields={"origin", "departure_after"},
        repair_only_missing=frozenset({"departure_after"}),
    )
    assert merged["origin"] == "Beijing"
    assert "origin" in rejected
    assert merged["departure_after"] == datetime(2026, 8, 20, 8, 0, tzinfo=SH)
    assert prov.get("departure_after") == "model_repair"


def test_initial_merge_rejects_hotel_fields_not_grounded_in_user_message() -> None:
    current = _fields(hard_constraints=["arrive_before_meeting"])
    extracted = _fields(
        hotel_check_in=date(2026, 8, 20),
        hotel_check_out=date(2026, 8, 22),
        hard_constraints=["arrive_before_meeting", "hotel_required"],
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        current,
        extracted,
        provided_fields={"hotel_check_in", "hotel_check_out", "hard_constraints"},
        user_message=("安排20日至22日北京上海往返出差；如果没有符合时间的返程，不要返回残缺行程。"),
    )

    assert merged["hotel_check_in"] is None
    assert merged["hotel_check_out"] is None
    assert merged["hard_constraints"] == ["arrive_before_meeting"]
    assert "hotel_check_in:ungrounded_hotel" in rejected
    assert "hotel_required:ungrounded_hotel" in rejected


def test_follow_up_preserves_previously_grounded_hotel_requirement() -> None:
    current = _fields(
        hotel_check_in=date(2026, 8, 20),
        hotel_check_out=date(2026, 8, 22),
        hard_constraints=["arrive_before_meeting", "hotel_required"],
    )

    merged, rejected, _ = merge_model_fields_anti_fabrication(
        current,
        current,
        provided_fields={"hotel_check_in", "hotel_check_out", "hard_constraints"},
        user_message="从北京出发",
    )

    assert merged["hotel_check_in"] == date(2026, 8, 20)
    assert merged["hotel_check_out"] == date(2026, 8, 22)
    assert "hotel_required" in merged["hard_constraints"]
    assert rejected == []


def test_build_repair_context_lists_anti_fabrication_rules() -> None:
    ctx = build_repair_context(
        missing=("departure_after",),
        fields=_fields(origin="Beijing"),
        user_message="北京到上海",
        classification="TRIP",
    )
    assert ctx["mode"] == "repair"
    assert "departure_after" in ctx["missing_fields"]
    assert any("ONLY fill" in rule for rule in ctx["rules"])


class _ScriptedRepairModel:
    prompt_version = "scripted-repair-v1"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # First extract: cities + arrive_by only (missing departure/return).
        first = IntentExtractionSchema(
            classification="MULTI_DAY_TRIP",
            fields=TripIntentFields(
                origin="Beijing",
                destination="Shanghai",
                departure_after=None,
                arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
                return_after=None,
                return_before=None,
                hotel_check_in=None,
                hotel_check_out=None,
                client_location=None,
                hard_constraints=["arrive_before_meeting", "hotel_required"],
                soft_preferences=[],
            ),
            provided_fields=[
                "origin",
                "destination",
                "arrive_by",
                "hard_constraints",
            ],
            missing_required_fields=["departure_after"],
            conflicts=[],
            assumptions=[],
            confidence=0.9,
            manipulation_detected=False,
        )
        # Repair: fill the still-missing checkout from the user's explicit return day.
        second = IntentExtractionSchema(
            classification="MULTI_DAY_TRIP",
            fields=TripIntentFields(
                origin="Beijing",
                destination="Shanghai",
                departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
                arrive_by=datetime(2026, 8, 20, 11, 0, tzinfo=SH),
                return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
                return_before=datetime(2026, 8, 22, 18, 0, tzinfo=SH),
                hotel_check_in=None,
                hotel_check_out=date(2026, 8, 22),
                client_location=None,
                hard_constraints=["arrive_before_meeting", "hotel_required"],
                soft_preferences=[],
            ),
            provided_fields=["return_after", "return_before", "hotel_check_out"],
            missing_required_fields=[],
            conflicts=[],
            assumptions=["checkout grounded in the explicit return day"],
            confidence=0.92,
            manipulation_detected=False,
        )
        self._outputs = deque([first, second])

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        self.calls.append({"message": message, "context": context})
        payload = self._outputs.popleft()
        return IntentExtractionResult(
            payload=payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted",
                duration_ms=1,
                input_tokens=10,
                output_tokens=10,
            ),
        )

    def propose_search_adjustment(self, *args, **kwargs):
        return None

    def explain_verified_options(self, options):
        return {}


def test_orchestrator_p1_repair_reaches_search_without_fabricating_cities() -> None:
    model = _ScriptedRepairModel()
    workflow, _ = build_demo_system(
        language_model=model,
        clock=lambda: REF,
        max_llm_attempts=1,
    )
    task = workflow.create_task_from_message(
        "安排8月20日北京到上海出差，上午11点开会，22日下午返回，需要酒店；职级还没进政策。",
        traveler_id="E1001",
        task_id="calib-repair-001",
    )
    assert len(model.calls) >= 1
    # After P0/P1 should not be stuck missing core slots for a full trip message.
    assert task.metadata.get("intent_classification") == "MULTI_DAY_TRIP"
    calib = task.metadata.get("intent_calibration") or {}
    assert "field_provenance" in calib
    # Claude Code-style param loop audit is attached.
    loop = task.metadata.get("param_loop") or calib.get("param_loop") or {}
    assert loop.get("pattern") == "claude_code_param_loop_v1"
    assert loop.get("rounds")
    # Either ready for planning or at least applied defaults / repair.
    assert task.state is not TaskState.DRAFT
    if task.intent_fields.get("origin"):
        assert task.intent_fields["origin"] in {"Beijing", "北京"}

    # If a repair turn happened, model must see tool_use_error-shaped repair context.
    repair_calls = [
        call for call in model.calls if isinstance((call.get("context") or {}).get("repair"), dict)
    ]
    for call in repair_calls:
        repair = call["context"]["repair"]
        assert repair.get("mode") == "repair"
        assert repair.get("is_error") is True
        assert "tool_use_error" in repair


class _SoftHotelOnlyModel:
    prompt_version = "soft-hotel-only-v1"

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        _ = (message, task_id, traveler_id, context)
        ny = ZoneInfo("America/New_York")
        payload = IntentExtractionSchema(
            classification="MULTI_DAY_TRIP",
            fields=TripIntentFields(
                origin="New York",
                destination="Philadelphia",
                departure_after=datetime(2026, 8, 18, 8, 0, tzinfo=ny),
                arrive_by=datetime(2026, 8, 18, 14, 0, tzinfo=ny),
                return_after=datetime(2026, 8, 19, 13, 0, tzinfo=ny),
                return_before=datetime(2026, 8, 19, 18, 0, tzinfo=ny),
                hotel_check_in=None,
                hotel_check_out=None,
                client_location=None,
                hard_constraints=[],
                soft_preferences=["prefer_flight", "hotel_near_client"],
            ),
            provided_fields=[
                "origin",
                "destination",
                "departure_after",
                "arrive_by",
                "return_after",
                "return_before",
                "hard_constraints",
                "soft_preferences",
            ],
            missing_required_fields=[],
            conflicts=[],
            assumptions=[],
            confidence=0.94,
            manipulation_detected=False,
        )
        return IntentExtractionResult(
            payload=payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted",
                duration_ms=1,
            ),
        )

    def propose_search_adjustment(self, *args, **kwargs):
        return None

    def explain_verified_options(self, options):
        return {}


def test_soft_hotel_preference_with_explicit_wording_triggers_hotel_search() -> None:
    workflow, _ = build_demo_system(
        language_model=_SoftHotelOnlyModel(),
        clock=lambda: REF,
        max_llm_attempts=1,
    )

    task = workflow.create_task_from_message(
        "下周二从纽约去费城见客户，上午出发，周三下午回来。"
        "优先飞机，酒店离客户公司近一点。",
        traveler_id="E1001",
        task_id="soft-hotel-upgrade-001",
    )

    assert task.intent_fields["lodging_requirement"] == LodgingRequirement.REQUIRED.value
    assert task.intent_fields["hotel_check_in"] == date(2026, 8, 18)
    assert task.intent_fields["hotel_check_out"] == date(2026, 8, 19)
    assert "hotel_required" in task.intent_fields["hard_constraints"]
    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert "client_location" in (task.metadata.get("uncertain_slots") or [])
    task = workflow.submit_message(task.task_id, "client:skip")
    assert any(call.tool_name == "provider.search_hotels" for call in task.tool_calls)

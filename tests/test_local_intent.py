from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.intent_calibration import iter_calendar_date_mentions
from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser
from corporate_travel_agent.services.locations import CityNormalizer

SH = ZoneInfo("Asia/Shanghai")
REF = datetime(2026, 8, 19, 15, 0, tzinfo=SH)


def _parser() -> GroundedLocalIntentParser:
    return GroundedLocalIntentParser(
        CityNormalizer(
            {
                "纽约": "New York",
                "紐約": "New York",
                "new york": "New York",
                "费城": "Philadelphia",
                "費城": "Philadelphia",
                "philadelphia": "Philadelphia",
                "北京": "Beijing",
                "上海": "Shanghai",
            }
        )
    )


def test_ny_phl_relative_week_and_preferences() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "下周二从纽约去费城见客户，上午出发，周三下午回来。优先飞机，酒店离客户公司近一点。",
        task_id="local-1",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    fields = result.payload.fields
    assert fields.origin == "New York"
    assert fields.destination == "Philadelphia"
    assert fields.departure_after is not None
    assert fields.departure_after.date().isoformat() == "2026-08-25"
    assert fields.return_after is not None
    assert fields.return_after.date().isoformat() == "2026-08-26"
    assert "prefer_flight" in fields.soft_preferences
    assert "hotel_near_client" in fields.soft_preferences


def test_does_not_invent_unknown_cities() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "下周去阿特兰蒂斯开会",
        task_id="local-2",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.origin is None
    assert result.payload.fields.destination is None


def test_recent_past_month_day_is_left_unresolved() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "8月5日北京去上海，6日上午10点前到。",
        task_id="local-past",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.departure_after is None
    assert result.payload.fields.arrive_by is None


def test_or_cities_are_not_a_route() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "北京或者上海，反正去见客户，时间你定。",
        task_id="local-or",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.origin is None
    assert result.payload.fields.destination is None


def test_return_revision_does_not_steal_outbound_day() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "返程改到后天晚上",
        task_id="local-return-rev",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    fields = result.payload.fields
    assert fields.departure_after is None
    assert fields.arrive_by is None
    assert fields.return_after is not None
    assert fields.return_after.date().isoformat() == "2026-08-21"
    assert fields.return_after.hour == 18
    assert fields.return_before is not None
    assert fields.return_before.hour == 23


def test_calendar_mentions_keep_exact_source_span() -> None:
    message = "返程改到8月25日晚上"
    mentions = iter_calendar_date_mentions(message)
    assert len(mentions) == 1
    mention = mentions[0]
    assert mention.raw == "8月25日"
    assert message[mention.start : mention.end] == mention.raw
    assert mention.as_dict() == {
        "year": None,
        "month": 8,
        "day": 25,
        "start": 4,
        "end": 9,
        "raw": "8月25日",
    }


def test_return_only_calendar_date_fills_the_real_direction_primary_leg() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "返程8月25日晚上从上海回北京",
        task_id="local-return-only-date",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    fields = result.payload.fields
    assert fields.origin == "Shanghai"
    assert fields.destination == "Beijing"
    assert fields.departure_after is not None
    assert fields.departure_after.date().isoformat() == "2026-08-25"
    assert fields.arrive_by is not None
    assert fields.return_after is None
    assert fields.return_before is None


def test_return_only_weekday_fills_the_single_primary_leg() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "下周四下午返程",
        task_id="local-return-only-weekday",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    fields = result.payload.fields
    assert fields.departure_after is not None
    assert fields.departure_after.date().isoformat() == "2026-08-27"
    assert fields.arrive_by is not None
    assert fields.return_after is None


def test_return_first_dates_bind_by_clause_semantics_not_mention_order() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "返程8月26日从上海回北京；去程8月25日从北京去上海",
        task_id="local-return-first-dates",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    fields = result.payload.fields
    assert fields.origin == "Beijing"
    assert fields.destination == "Shanghai"
    assert fields.departure_after is not None
    assert fields.departure_after.date().isoformat() == "2026-08-25"
    assert fields.return_after is not None
    assert fields.return_after.date().isoformat() == "2026-08-26"


def test_return_route_first_does_not_invert_canonical_route() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "返程从上海回北京；去程从北京去上海，8月25日出发",
        task_id="local-return-first-route",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.origin == "Beijing"
    assert result.payload.fields.destination == "Shanghai"


def test_single_date_same_day_round_trip_binds_both_legs() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "8月25日从北京去上海，当天往返",
        task_id="local-same-day-round-trip",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    fields = result.payload.fields
    assert fields.departure_after is not None
    assert fields.departure_after.date().isoformat() == "2026-08-25"
    assert fields.return_after is not None
    assert fields.return_after.date().isoformat() == "2026-08-25"


def test_origin_only_from_shanghai_does_not_invent_destination() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "改从上海走",
        task_id="local-origin",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.origin == "Shanghai"
    assert result.payload.fields.destination is None


def test_week_after_next_wednesday() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "下下周三从北京去上海开会",
        task_id="local-week-after-next",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    depart = result.payload.fields.departure_after
    assert depart is not None
    assert depart.date().isoformat() == "2026-09-02"


def test_this_or_next_friday_is_not_resolved() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "这周五还是下周五从北京去上海",
        task_id="local-friday-or",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.departure_after is None
    assert result.payload.fields.arrive_by is None


def test_numeric_slash_date_before_the_day() -> None:
    parser = _parser()
    early = datetime(2026, 8, 1, 9, 0, tzinfo=SH)
    result = parser.extract_trip_intent(
        "8/5从北京去上海开会",
        task_id="local-slash",
        traveler_id="E1001",
        context={"reference_time": early.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.departure_after is not None
    assert result.payload.fields.departure_after.date().isoformat() == "2026-08-05"


def test_dotted_month_day_before_the_day() -> None:
    parser = _parser()
    early = datetime(2026, 8, 1, 9, 0, tzinfo=SH)
    result = parser.extract_trip_intent(
        "8.5从北京去上海开会",
        task_id="local-dot",
        traveler_id="E1001",
        context={"reference_time": early.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.departure_after is not None
    assert result.payload.fields.departure_after.date().isoformat() == "2026-08-05"


def test_explicit_dotted_year_keeps_named_past_day() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "2026.8.5从北京去上海开会",
        task_id="local-ymd",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.departure_after is not None
    assert result.payload.fields.departure_after.date().isoformat() == "2026-08-05"


def test_yearless_numeric_recent_past_is_unresolved() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "8/5从北京去上海开会",
        task_id="local-slash-past",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.departure_after is None


def test_lunar_holiday_does_not_invent_a_gregorian_day() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "春节从北京去上海出差",
        task_id="local-spring",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.origin == "Beijing"
    assert result.payload.fields.destination == "Shanghai"
    assert result.payload.fields.departure_after is None
    assert result.payload.fields.arrive_by is None


def test_december_to_january_return_wraps_the_year() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "12月30日从北京去上海，1月2日回",
        task_id="local-cross-year",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    fields = result.payload.fields
    assert fields.departure_after is not None
    assert fields.departure_after.date().isoformat() == "2026-12-30"
    assert fields.return_after is not None
    assert fields.return_after.date().isoformat() == "2027-01-02"


def test_far_past_month_day_may_roll_to_next_year() -> None:
    parser = _parser()
    result = parser.extract_trip_intent(
        "1月5日从北京去上海开会",
        task_id="local-jan",
        traveler_id="E1001",
        context={"reference_time": REF.isoformat(), "timezone": "Asia/Shanghai"},
    )
    assert result.payload.fields.departure_after is not None
    assert result.payload.fields.departure_after.date().isoformat() == "2027-01-05"

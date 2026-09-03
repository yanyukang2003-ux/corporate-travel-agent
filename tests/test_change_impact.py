"""变更影响评估与观察节奏：确定性代码算"这条动态意味着什么"和"下次几点查"。

守住的东西：
- 取消一定改期；延误但仍早于"到场时限 − 安全缓冲"只通知；超过就改期；
- 只给新起飞时刻时按原飞行时长推到达；
- 和下一段接不上也改期；
- 动态源判不了就什么都不做；
- 离起飞越近查得越勤，全部落地就不查。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from corporate_travel_agent.demo import make_demo_request
from corporate_travel_agent.domain.enums import ChangeImpactVerdict, FlightStatusKind
from corporate_travel_agent.domain.models import FlightStatusReport, TripWatchLeg
from corporate_travel_agent.services.change_impact import (
    LANDED_GRACE,
    assess_flight_change,
    next_flight_check_at,
)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 4, 9, 0, tzinfo=TZ)

# 演示请求：第一段 08-06 10:00 前到（要留 60 分钟安全缓冲 → 最晚 09:00），返程 23:00 前到。
REQUEST = make_demo_request(task_id="impact")
OUTBOUND = TripWatchLeg(
    ref_id="MU-EARLY",
    provider="mock",
    origin="Beijing",
    destination="Shanghai",
    depart_at=datetime(2026, 8, 5, 6, 20, tzinfo=TZ),
    arrive_at=datetime(2026, 8, 5, 8, 35, tzinfo=TZ),
)
RETURN = TripWatchLeg(
    ref_id="MU-RETURN",
    provider="mock",
    origin="Shanghai",
    destination="Beijing",
    depart_at=datetime(2026, 8, 6, 18, 0, tzinfo=TZ),
    arrive_at=datetime(2026, 8, 6, 20, 20, tzinfo=TZ),
)


def _report(status: FlightStatusKind, *, depart=None, arrive=None) -> FlightStatusReport:
    return FlightStatusReport(
        ref_id="MU-EARLY",
        status=status,
        observed_at=NOW,
        source="test",
        estimated_depart_at=depart,
        estimated_arrive_at=arrive,
    )


def _assess(report, *, following=None, buffer=60, **kwargs):
    return assess_flight_change(
        OUTBOUND,
        report,
        request=REQUEST,
        leg_index=0,
        arrival_buffer_minutes=buffer,
        now=NOW,
        following=following,
        **kwargs,
    )


def test_cancellation_always_requires_rebooking() -> None:
    impact = _assess(_report(FlightStatusKind.CANCELLED))
    assert impact.verdict is ChangeImpactVerdict.REBOOK_REQUIRED
    assert "已取消" in impact.reasons[0]
    assert impact.new_arrive_at is None


def test_unknown_status_changes_nothing() -> None:
    impact = _assess(_report(FlightStatusKind.UNKNOWN))
    assert impact.verdict is ChangeImpactVerdict.NO_CHANGE
    assert "判不了" in impact.reasons[0]


def test_tolerable_delay_only_notifies() -> None:
    impact = _assess(
        _report(FlightStatusKind.DELAYED, arrive=OUTBOUND.arrive_at + timedelta(minutes=30))
    )
    assert impact.verdict is ChangeImpactVerdict.NOTIFY_ONLY
    assert impact.delay_minutes == 30
    # 到场时限 08-06 10:00 减 60 分钟缓冲。
    assert impact.latest_acceptable_arrival == datetime(2026, 8, 6, 9, 0, tzinfo=TZ)
    assert impact.buffer_minutes == 60
    assert "仍早于最晚可接受到达" in impact.reasons[0]


def test_small_delay_is_below_the_notice_threshold() -> None:
    impact = _assess(
        _report(FlightStatusKind.DELAYED, arrive=OUTBOUND.arrive_at + timedelta(minutes=5))
    )
    assert impact.verdict is ChangeImpactVerdict.NO_CHANGE
    assert impact.delay_minutes == 5


def test_delay_past_the_buffered_deadline_requires_rebooking() -> None:
    impact = _assess(
        _report(FlightStatusKind.DELAYED, arrive=datetime(2026, 8, 6, 9, 30, tzinfo=TZ))
    )
    assert impact.verdict is ChangeImpactVerdict.REBOOK_REQUIRED
    assert "晚于最晚可接受到达" in impact.reasons[0]
    assert "60 分钟安全缓冲" in impact.reasons[0]


def test_without_buffer_the_deadline_itself_is_the_line() -> None:
    at_deadline = _assess(
        _report(FlightStatusKind.DELAYED, arrive=datetime(2026, 8, 6, 9, 30, tzinfo=TZ)),
        buffer=0,
    )
    assert at_deadline.verdict is ChangeImpactVerdict.NOTIFY_ONLY
    assert at_deadline.buffer_minutes == 0


def test_only_a_new_departure_time_projects_arrival_by_the_original_duration() -> None:
    impact = _assess(
        _report(FlightStatusKind.DELAYED, depart=OUTBOUND.depart_at + timedelta(minutes=40))
    )
    assert impact.new_arrive_at == OUTBOUND.arrive_at + timedelta(minutes=40)
    assert impact.verdict is ChangeImpactVerdict.NOTIFY_ONLY


def test_missed_connection_requires_rebooking_even_when_the_deadline_holds() -> None:
    following = TripWatchLeg(
        ref_id="NEXT",
        provider="mock",
        origin="Shanghai",
        destination="Hangzhou",
        depart_at=datetime(2026, 8, 5, 9, 15, tzinfo=TZ),
        arrive_at=datetime(2026, 8, 5, 10, 15, tzinfo=TZ),
    )
    impact = _assess(
        _report(FlightStatusKind.DELAYED, arrive=OUTBOUND.arrive_at + timedelta(minutes=30)),
        following=following,
        min_connection_minutes=60,
    )
    assert impact.verdict is ChangeImpactVerdict.REBOOK_REQUIRED
    assert impact.connection_ok is False
    assert "接下一段 NEXT" in impact.reasons[0]

    relaxed = _assess(
        _report(FlightStatusKind.DELAYED, arrive=OUTBOUND.arrive_at + timedelta(minutes=30)),
        following=following,
        min_connection_minutes=5,
    )
    assert relaxed.verdict is ChangeImpactVerdict.NOTIFY_ONLY
    assert relaxed.connection_ok is True


def test_an_early_reschedule_is_reported_not_rebooked() -> None:
    impact = _assess(
        _report(FlightStatusKind.DELAYED, arrive=OUTBOUND.arrive_at - timedelta(minutes=25))
    )
    assert impact.verdict is ChangeImpactVerdict.NOTIFY_ONLY
    assert impact.delay_minutes == -25
    assert "提前 25 分钟" in impact.reasons[0]


def test_departed_and_landed_are_informational() -> None:
    assert _assess(_report(FlightStatusKind.DEPARTED)).verdict is ChangeImpactVerdict.NO_CHANGE
    assert _assess(_report(FlightStatusKind.LANDED)).verdict is ChangeImpactVerdict.NO_CHANGE


def test_return_leg_without_meeting_buffer_uses_its_own_window() -> None:
    late_return = FlightStatusReport(
        ref_id="MU-RETURN",
        status=FlightStatusKind.DELAYED,
        observed_at=NOW,
        source="test",
        estimated_arrive_at=datetime(2026, 8, 6, 23, 30, tzinfo=TZ),
    )
    impact = assess_flight_change(
        RETURN, late_return, request=REQUEST, leg_index=1, arrival_buffer_minutes=60, now=NOW
    )
    assert impact.verdict is ChangeImpactVerdict.REBOOK_REQUIRED
    assert impact.buffer_minutes == 0
    assert impact.latest_acceptable_arrival == datetime(2026, 8, 6, 23, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    ("hours_before", "expected_gap"),
    [
        (72, timedelta(hours=24)),  # 48 小时外：直接跳到 T-48h
        (30, timedelta(hours=6)),
        (10, timedelta(hours=1)),
        (2, timedelta(minutes=15)),
    ],
)
def test_check_cadence_tightens_towards_departure(hours_before, expected_gap) -> None:
    now = OUTBOUND.depart_at - timedelta(hours=hours_before)
    assert next_flight_check_at(now, (OUTBOUND, RETURN)) == now + expected_gap


def test_check_never_scheduled_past_departure_of_the_leg() -> None:
    now = OUTBOUND.depart_at - timedelta(minutes=5)
    assert next_flight_check_at(now, (OUTBOUND,)) == OUTBOUND.depart_at


def test_in_flight_checks_every_half_hour_then_moves_to_the_next_leg() -> None:
    airborne = OUTBOUND.depart_at + timedelta(minutes=30)
    assert next_flight_check_at(airborne, (OUTBOUND, RETURN)) == airborne + timedelta(minutes=30)
    after_landing = OUTBOUND.arrive_at + LANDED_GRACE + timedelta(minutes=1)
    # 第一段过了落地宽限，轮到返程：它还在 48 小时内，每 6 小时一次。
    assert next_flight_check_at(after_landing, (OUTBOUND, RETURN)) == after_landing + timedelta(
        hours=6
    )


def test_no_check_once_every_leg_has_landed() -> None:
    done = RETURN.arrive_at + LANDED_GRACE + timedelta(minutes=1)
    assert next_flight_check_at(done, (OUTBOUND, RETURN)) is None
    assert next_flight_check_at(NOW, ()) is None

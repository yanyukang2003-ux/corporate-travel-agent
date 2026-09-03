"""变更影响评估与观察节奏：一条航班动态对已订行程意味着什么，以及下一次该几点去查。

## 为什么是确定性代码

航司说"延误 40 分钟"，这句话本身不是结论。要不要改期，取决于三件只有系统知道的事：
这一段是不是送人去会面的那一段、政策要求留多少安全缓冲、后面还有没有接不上的下一段。
这些和政策判定是同一类问题——**输入是事实，输出是可复核的结论**——所以它和政策引擎一样
是代码，不交给模型。每条结论都带 `reasons`，"为什么要改期"或"为什么只通知"说得出口。

## 三档结论

| 结论 | 什么时候 |
|---|---|
| `REBOOK_REQUIRED` | 取消；新到达晚于"最晚可接受到达"（到场时限 − 安全缓冲）；和下一段接不上 |
| `NOTIFY_ONLY` | 时刻变了、超过通知阈值，但仍来得及：告诉旅行者，不动行程 |
| `NO_CHANGE` | 按计划、变化在阈值内、已起飞/落地，或动态源判不了 |

## 观察节奏

离起飞越近查得越勤：48 小时外不查（等到 T-48h）；48–12 小时每 6 小时；12–3 小时每小时；
3 小时内每 15 分钟；起飞后到落地前每 30 分钟（落地延误也要知道）；全部落地就不查了。
这张表是常量，不是配置——节奏本身不是业务决定，供应商配额才是，那个用 `lookahead_hours`
和 worker 的轮询间隔调。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from corporate_travel_agent.domain.enums import ChangeImpactVerdict, FlightStatusKind
from corporate_travel_agent.domain.models import (
    ChangeImpact,
    FlightStatusReport,
    TripRequestVersion,
    TripWatchLeg,
)
from corporate_travel_agent.planning.feasibility import needs_arrival_buffer

#: 起飞前多少小时开始盯。
DEFAULT_LOOKAHEAD_HOURS = 48
#: 延误多少分钟以内不打扰旅行者。
DEFAULT_DELAY_NOTICE_MINUTES = 15
#: 前一段落地到下一段起飞之间至少留多久，才算"接得上"。
DEFAULT_MIN_CONNECTION_MINUTES = 60
#: 落地后多久不再查这一段。
LANDED_GRACE = timedelta(hours=2)

#: （离起飞不到多少小时, 查一次的间隔）。按行从上到下第一条命中的生效。
_CADENCE: tuple[tuple[timedelta, timedelta], ...] = (
    (timedelta(hours=3), timedelta(minutes=15)),
    (timedelta(hours=12), timedelta(hours=1)),
    (timedelta(hours=DEFAULT_LOOKAHEAD_HOURS), timedelta(hours=6)),
)
_IN_FLIGHT_INTERVAL = timedelta(minutes=30)


def assess_flight_change(
    leg: TripWatchLeg,
    report: FlightStatusReport,
    *,
    request: TripRequestVersion,
    leg_index: int,
    arrival_buffer_minutes: int,
    now: datetime,
    following: TripWatchLeg | None = None,
    min_connection_minutes: int = DEFAULT_MIN_CONNECTION_MINUTES,
    delay_notice_minutes: int = DEFAULT_DELAY_NOTICE_MINUTES,
) -> ChangeImpact:
    """判一条航班动态对这一段的影响。

    `leg_index` 是这一段在请求 `transport_legs()` 里的下标：到场时限和"要不要留安全缓冲"
    都按那一段的约束读。`following` 是同一趟里的下一段（有的话），用来判接不接得上。
    """
    if report.status is FlightStatusKind.CANCELLED:
        return ChangeImpact(
            verdict=ChangeImpactVerdict.REBOOK_REQUIRED,
            reasons=(f"{leg.ref_id} {leg.origin}→{leg.destination} 已取消",),
            assessed_at=now,
        )
    if report.status in {FlightStatusKind.UNKNOWN, FlightStatusKind.LANDED}:
        return ChangeImpact(
            verdict=ChangeImpactVerdict.NO_CHANGE,
            reasons=(
                "动态源查不到这一段，判不了" if report.status is FlightStatusKind.UNKNOWN
                else f"{leg.ref_id} 已落地",
            ),
            assessed_at=now,
        )

    new_depart = report.estimated_depart_at or leg.depart_at
    new_arrive = report.estimated_arrive_at
    if new_arrive is None:
        # 只给了新起飞时刻：按原飞行时长推到达。
        new_arrive = new_depart + (leg.arrive_at - leg.depart_at)
    delay = new_arrive - leg.arrive_at
    delay_minutes = int(round(delay.total_seconds() / 60))

    latest_acceptable, buffer_minutes = _latest_acceptable_arrival(
        request, leg_index, arrival_buffer_minutes
    )
    reasons: list[str] = []
    connection_ok: bool | None = None
    verdict = ChangeImpactVerdict.NO_CHANGE

    if latest_acceptable is not None and new_arrive > latest_acceptable:
        verdict = ChangeImpactVerdict.REBOOK_REQUIRED
        reasons.append(
            f"{leg.ref_id} 新到达 {_clock(new_arrive)} 晚于最晚可接受到达 "
            f"{_clock(latest_acceptable)}"
            + (f"（到场时限减 {buffer_minutes} 分钟安全缓冲）" if buffer_minutes else "")
        )
    if following is not None:
        connection_ok = (
            new_arrive + timedelta(minutes=min_connection_minutes) <= following.depart_at
        )
        if not connection_ok:
            verdict = ChangeImpactVerdict.REBOOK_REQUIRED
            reasons.append(
                f"{leg.ref_id} 新到达 {_clock(new_arrive)} 后不足 {min_connection_minutes} 分钟"
                f"接下一段 {following.ref_id}（{_clock(following.depart_at)} 起飞）"
            )
    if verdict is ChangeImpactVerdict.NO_CHANGE:
        if report.status is FlightStatusKind.DEPARTED:
            reasons.append(f"{leg.ref_id} 已起飞")
        elif abs(delay_minutes) >= delay_notice_minutes:
            verdict = ChangeImpactVerdict.NOTIFY_ONLY
            reasons.append(
                (
                    f"{leg.ref_id} 延误 {delay_minutes} 分钟，预计 {_clock(new_arrive)} 到"
                    if delay_minutes > 0
                    else f"{leg.ref_id} 提前 {-delay_minutes} 分钟，预计 {_clock(new_arrive)} 到"
                )
                + (
                    f"，仍早于最晚可接受到达 {_clock(latest_acceptable)}"
                    if latest_acceptable is not None
                    else "，这一段没有到场时限"
                )
            )
        else:
            reasons.append(
                f"{leg.ref_id} 按计划"
                if delay_minutes == 0
                else f"{leg.ref_id} 时刻变化 {delay_minutes} 分钟，在 {delay_notice_minutes} 分钟内"
            )
    return ChangeImpact(
        verdict=verdict,
        reasons=tuple(reasons),
        assessed_at=now,
        delay_minutes=delay_minutes,
        new_arrive_at=new_arrive,
        latest_acceptable_arrival=latest_acceptable,
        buffer_minutes=buffer_minutes,
        connection_ok=connection_ok,
    )


def _latest_acceptable_arrival(
    request: TripRequestVersion, leg_index: int, arrival_buffer_minutes: int
) -> tuple[datetime | None, int]:
    """这一段最晚几点到还来得及，以及扣了多少分钟缓冲。没有到场时限就是 (None, 0)。"""
    legs = request.transport_legs()
    if leg_index >= len(legs):
        return None, 0
    spec = legs[leg_index]
    if spec.arrive_before is None:
        return None, 0
    buffer = arrival_buffer_minutes if needs_arrival_buffer(request, leg_index) else 0
    return spec.arrive_before - timedelta(minutes=buffer), buffer


def next_flight_check_at(
    now: datetime,
    legs: tuple[TripWatchLeg, ...],
    *,
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
) -> datetime | None:
    """下一次该去问动态源的时刻；所有段都落地了就是 None。

    只看**还没落地**的最近一段：它离起飞越近，间隔越短。它落地之后轮到下一段。
    """
    pending = sorted(
        (leg for leg in legs if leg.arrive_at + LANDED_GRACE > now), key=lambda leg: leg.depart_at
    )
    if not pending:
        return None
    leg = pending[0]
    if now >= leg.depart_at:
        # 起飞后到落地：查落地延误，落地宽限过了就轮到下一段。
        return min(now + _IN_FLIGHT_INTERVAL, leg.arrive_at + LANDED_GRACE)
    until_departure = leg.depart_at - now
    lookahead = timedelta(hours=lookahead_hours)
    if until_departure > lookahead:
        return leg.depart_at - lookahead
    for threshold, interval in _CADENCE:
        if until_departure <= threshold:
            return min(now + interval, leg.depart_at)
    return min(now + _CADENCE[-1][1], leg.depart_at)


def _clock(value: datetime) -> str:
    return value.strftime("%m-%d %H:%M")

"""行程可行性校验：对照请求硬约束检查交通与酒店是否可组成合法方案。

**每一条检查都只看一段自己的事**——报价和**请求窗口**比，从不和另一段的实际报价比。
这不是新性质，原来就是这样写的，只是被摊平成"去程一段、返程一段"两份重复代码。
把它收成 `leg_reasons` 一份实现之后，规划器才能在**不假设只有两段**的前提下
先按段筛掉不可行的报价——见 `planner.py` 为什么不再做笛卡尔积。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from corporate_travel_agent.domain.models import (
    FeasibilityResult,
    HotelOffer,
    TransportOffer,
    TripRequestVersion,
)


@dataclass(frozen=True, slots=True)
class LegSpec:
    """一段行程自己的判据：走哪条航线、什么时间窗、报错时管它叫什么。"""

    label: str
    origin: str
    destination: str
    depart_after: datetime | None
    arrive_before: datetime | None
    needs_arrival_buffer: bool

    @property
    def late_arrival_reason(self) -> str:
        """到得太晚时的说法。去程说的是"要求的到达时间"，其余段说的是"允许的窗口"。"""
        if self.label == "outbound":
            return "outbound arrives after the requested arrival time"
        return f"{self.label} arrives after the allowed window"


def _needs_arrival_buffer(request: TripRequestVersion, leg_index: int) -> bool:
    """这一段要不要留到场安全缓冲。

    到场时限管的是**把人送到会面地点的那一段**，不是整趟行程的每一段。所以
    `arrive_before_meeting` 说的是整趟时，只作用于第一段——多城行程里返程也要留
    半小时缓冲是没有道理的。但旅行者若明说了是**哪一段**要赶会
    （"第三段要赶上客户会议"），那一段就该留：此前这种情况会被无声丢掉。
    """
    for item in request.scoped_constraints():
        if item.name != "arrive_before_meeting":
            continue
        if item.leg_index == leg_index:
            return True
        if item.whole_journey and leg_index == 0:
            return True
    return False


def planned_leg_count(request: TripRequestVersion) -> int:
    """这次要执行几段交通。

    显式给了 ``journey`` 就数它；没给才按返程窗口推——和改动之前逐字一致。
    """
    legs = request.transport_legs()
    if len(legs) > 1:
        return len(legs)
    return 2 if request.return_after is not None else 1


def leg_spec(request: TripRequestVersion, leg_index: int) -> LegSpec:
    """第 ``leg_index`` 段的判据。

    去程与返程仍然读扁平字段，和此前逐字一致；第三段起才读 ``journey`` 自己的窗口。
    """
    if leg_index == 0:
        return LegSpec(
            label="outbound",
            origin=request.origin,
            destination=request.destination,
            depart_after=request.departure_after,
            arrive_before=request.arrive_by,
            needs_arrival_buffer=_needs_arrival_buffer(request, 0),
        )

    legs = request.transport_legs()
    # 只有两段时，第二段就是返程，沿用它原来的名字与扁平字段——
    # 前端、评测与既有报错文案都认这个词。三段以上第 1 段只是中间的一段，
    # 它的时间窗必须读**它自己的**，读 `return_*` 会把中途那一段当成返程。
    label = "return" if leg_index == 1 and len(legs) <= 2 else f"leg {leg_index}"

    if leg_index < len(legs):
        # 航线按**行程里声明的那一段**校验，而不是硬套"目的地→出发地"：
        # “去上海、从杭州回”是一条合法行程，杭州不该被当成路线错误。
        leg = legs[leg_index]
        return LegSpec(
            label=label,
            origin=leg.origin,
            destination=leg.destination,
            depart_after=leg.depart_after,
            arrive_before=leg.arrive_before,
            needs_arrival_buffer=_needs_arrival_buffer(request, leg_index),
        )

    # 只声明了 return_after、没声明 return_before 时 `transport_legs()` 拼不出第二段，
    # 但这次仍然要订返程——退回扁平字段，和改动之前逐字一致。
    return LegSpec(
        label=label,
        origin=request.destination,
        destination=request.origin,
        depart_after=request.return_after,
        arrive_before=request.return_before,
        needs_arrival_buffer=_needs_arrival_buffer(request, leg_index),
    )


class FeasibilityValidator:
    """对交通与酒店做硬约束校验，返回可行性结果与原因列表。"""

    def leg_reasons(
        self,
        request: TripRequestVersion,
        leg_index: int,
        offer: TransportOffer,
        arrival_buffer_minutes: int,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """这一段自己不可行的理由；空元组表示这一段成立。

        ``now`` 是必填的：请求窗口只说明旅行者能接受什么，不说明现在还赶不赶得上。
        下午两点搜"今天从北京去上海"，上午九点那班已经飞了——窗口检查看不出这件事，
        只有和当前时刻比才看得出。
        """
        spec = leg_spec(request, leg_index)
        reasons: list[str] = []

        if offer.depart_at <= now:
            reasons.append(f"{spec.label} has already departed")
        if not offer.available:
            reasons.append(f"{spec.label} inventory is unavailable")
        if offer.origin != spec.origin or offer.destination != spec.destination:
            reasons.append(f"{spec.label} route does not match the request")
        if spec.depart_after is not None and offer.depart_at < spec.depart_after:
            reasons.append(f"{spec.label} departs before the allowed window")
        if spec.arrive_before is not None:
            latest_arrival = spec.arrive_before
            if spec.needs_arrival_buffer:
                # 会面前到达约束会额外扣减安全缓冲时间。
                latest_arrival -= timedelta(minutes=arrival_buffer_minutes)
            if offer.arrive_at > latest_arrival:
                reasons.append(
                    f"{spec.label} cannot meet arrival and safety-buffer requirement"
                    if spec.needs_arrival_buffer
                    else spec.late_arrival_reason
                )
        return tuple(reasons)

    def hotel_reasons(
        self, request: TripRequestVersion, hotel: HotelOffer | None
    ) -> tuple[str, ...]:
        """住宿不可行的理由；这趟不需要住宿时永远为空。"""
        if request.hotel_check_in is None or request.hotel_check_out is None:
            return ()
        if hotel is None:
            return ("hotel is required",)
        reasons: list[str] = []
        if not hotel.available:
            reasons.append("hotel inventory is unavailable")
        if hotel.city != request.destination:
            reasons.append("hotel city does not match the destination")
        if (
            hotel.check_in > request.hotel_check_in
            or hotel.check_out < request.hotel_check_out
        ):
            reasons.append("hotel stay does not cover requested dates")
        return tuple(reasons)

    def validate(
        self,
        request: TripRequestVersion,
        transports: Sequence[TransportOffer],
        hotel: HotelOffer | None,
        arrival_buffer_minutes: int,
        *,
        now: datetime,
    ) -> FeasibilityResult:
        """校验一整组库存是否满足请求窗口、路由与到达缓冲；任一失败则不可行。

        此前这里收的是 ``outbound`` 和 ``inbound`` 两个参数——两个位置，
        放不下第三段。现在收整条航段列表，段数由请求自己说了算。
        """
        reasons: list[str] = []
        for index in range(planned_leg_count(request)):
            if index >= len(transports):
                reasons.append(f"{leg_spec(request, index).label} transport is required")
                continue
            reasons.extend(
                self.leg_reasons(
                    request, index, transports[index], arrival_buffer_minutes, now=now
                )
            )

        reasons.extend(self.hotel_reasons(request, hotel))
        return FeasibilityResult(feasible=not reasons, reasons=tuple(reasons))

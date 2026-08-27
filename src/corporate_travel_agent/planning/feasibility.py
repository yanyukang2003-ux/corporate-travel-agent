"""行程可行性校验：对照请求硬约束检查交通与酒店是否可组成合法方案。"""

from __future__ import annotations

from datetime import datetime, timedelta

from corporate_travel_agent.domain.models import (
    FeasibilityResult,
    HotelOffer,
    TransportOffer,
    TripRequestVersion,
)


class FeasibilityValidator:
    """对去程/返程交通与酒店做硬约束校验，返回可行性结果与原因列表。"""

    def validate(
        self,
        request: TripRequestVersion,
        outbound: TransportOffer,
        inbound: TransportOffer | None,
        hotel: HotelOffer | None,
        arrival_buffer_minutes: int,
        *,
        now: datetime,
    ) -> FeasibilityResult:
        """校验单组库存是否满足请求窗口、路由与到达缓冲；任一失败则不可行。

        ``now`` 是必填的：请求窗口只说明旅行者能接受什么，不说明现在还赶不赶得上。
        下午两点搜"今天从北京去上海"，上午九点那班已经飞了——窗口检查看不出这件事，
        只有和当前时刻比才看得出。
        """
        reasons: list[str] = []

        if outbound.depart_at <= now:
            reasons.append("outbound has already departed")
        if not outbound.available:
            reasons.append("outbound inventory is unavailable")
        if outbound.origin != request.origin or outbound.destination != request.destination:
            reasons.append("outbound route does not match the request")
        if outbound.depart_at < request.departure_after:
            reasons.append("outbound departs before the allowed window")
        latest_arrival = request.arrive_by
        # 会议前到达约束会额外扣减安全缓冲时间
        if "arrive_before_meeting" in request.hard_constraints:
            latest_arrival -= timedelta(minutes=arrival_buffer_minutes)
        if outbound.arrive_at > latest_arrival:
            reason = (
                "outbound cannot meet arrival and safety-buffer requirement"
                if "arrive_before_meeting" in request.hard_constraints
                else "outbound arrives after the requested arrival time"
            )
            reasons.append(reason)

        if request.return_after is not None:
            if inbound is None:
                reasons.append("return transport is required")
            else:
                if inbound.depart_at <= now:
                    reasons.append("return has already departed")
                if not inbound.available:
                    reasons.append("return inventory is unavailable")
                if inbound.origin != request.destination or inbound.destination != request.origin:
                    reasons.append("return route does not match the request")
                if inbound.depart_at < request.return_after:
                    reasons.append("return departs before the allowed window")
                if request.return_before and inbound.arrive_at > request.return_before:
                    reasons.append("return arrives after the allowed window")

        if request.hotel_check_in is not None and request.hotel_check_out is not None:
            if hotel is None:
                reasons.append("hotel is required")
            else:
                if not hotel.available:
                    reasons.append("hotel inventory is unavailable")
                if hotel.city != request.destination:
                    reasons.append("hotel city does not match the destination")
                if (
                    hotel.check_in > request.hotel_check_in
                    or hotel.check_out < request.hotel_check_out
                ):
                    reasons.append("hotel stay does not cover requested dates")

        return FeasibilityResult(feasible=not reasons, reasons=tuple(reasons))

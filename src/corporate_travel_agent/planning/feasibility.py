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
        # 会议前到达约束会额外扣减安全缓冲时间。它是**这一段**的要求：
        # 到场时限管的是把人送到会面地点的那一段，不是整趟行程的每一段。
        outbound_constraints = request.constraints_for_leg(0)
        needs_buffer = "arrive_before_meeting" in outbound_constraints
        if needs_buffer:
            latest_arrival -= timedelta(minutes=arrival_buffer_minutes)
        if outbound.arrive_at > latest_arrival:
            reason = (
                "outbound cannot meet arrival and safety-buffer requirement"
                if needs_buffer
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
                # 返程航线按**行程里声明的那一段**校验，而不是硬套"目的地→出发地"：
                # “去上海、从杭州回”是一条合法行程，杭州不该被当成路线错误。
                legs = request.transport_legs()
                return_leg = legs[1] if len(legs) > 1 else None
                expected_origin = (
                    return_leg.origin if return_leg is not None else request.destination
                )
                expected_destination = (
                    return_leg.destination if return_leg is not None else request.origin
                )
                if (
                    inbound.origin != expected_origin
                    or inbound.destination != expected_destination
                ):
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

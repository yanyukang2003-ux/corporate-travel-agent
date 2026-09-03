"""谁在哪（duty of care）：从已确认行程的观察对象推出每位旅行者此刻的位置。

只用系统里有的事实：员工回填了下单确认的那份方案的航段。没确认的行程不在这里——
系统不知道人到底订没订，就不假装知道人在哪。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from corporate_travel_agent.domain.enums import TripStatus
from corporate_travel_agent.domain.models import Trip, TripWatchLeg


class WhereaboutsStatus(StrEnum):
    UPCOMING = "UPCOMING"
    """还没出发：人在出发地。"""

    IN_TRANSIT = "IN_TRANSIT"
    """某一段起飞了还没落地。"""

    AT_DESTINATION = "AT_DESTINATION"
    """上一段落地了，下一段还没起飞（或没有下一段）。"""

    COMPLETED = "COMPLETED"
    """观察期已过。"""


@dataclass(frozen=True, slots=True)
class Whereabouts:
    trip_id: str
    task_id: str
    traveler_id: str
    requester_id: str
    status: WhereaboutsStatus
    #: 城市名，或在途时 "起点→终点"。
    location: str
    current_leg: TripWatchLeg | None
    next_leg: TripWatchLeg | None
    trip_status: TripStatus
    #: 有一条变更事件开了改期任务、还没订好——看板上要标出来。
    change_pending: bool
    watch_until: datetime


_ORDER = {
    WhereaboutsStatus.IN_TRANSIT: 0,
    WhereaboutsStatus.AT_DESTINATION: 1,
    WhereaboutsStatus.UPCOMING: 2,
    WhereaboutsStatus.COMPLETED: 3,
}


def whereabouts_of(trip: Trip, *, at: datetime) -> Whereabouts | None:
    """一趟差旅在 `at` 这一刻的位置；没有观察对象（没确认过）就是 None。"""
    watch = trip.watch
    if watch is None or not watch.legs or trip.status is TripStatus.CANCELLED:
        # 取消了的差旅：人不去了，观察对象只是历史。
        return None
    legs = sorted(watch.legs, key=lambda leg: leg.depart_at)
    change_pending = trip.status is TripStatus.CHANGE_REQUESTED
    common = {
        "trip_id": trip.trip_id,
        "task_id": watch.task_id,
        "traveler_id": trip.traveler_id,
        "requester_id": trip.requester_id,
        "trip_status": trip.status,
        "change_pending": change_pending,
        "watch_until": watch.watch_until,
    }
    if at > watch.watch_until:
        return Whereabouts(
            status=WhereaboutsStatus.COMPLETED,
            location=legs[-1].destination,
            current_leg=None,
            next_leg=None,
            **common,
        )
    if at < legs[0].depart_at:
        return Whereabouts(
            status=WhereaboutsStatus.UPCOMING,
            location=legs[0].origin,
            current_leg=None,
            next_leg=legs[0],
            **common,
        )
    for index, leg in enumerate(legs):
        following = legs[index + 1] if index + 1 < len(legs) else None
        if leg.depart_at <= at < leg.arrive_at:
            return Whereabouts(
                status=WhereaboutsStatus.IN_TRANSIT,
                location=f"{leg.origin}→{leg.destination}",
                current_leg=leg,
                next_leg=following,
                **common,
            )
        if following is None or at < following.depart_at:
            return Whereabouts(
                status=WhereaboutsStatus.AT_DESTINATION,
                location=leg.destination,
                current_leg=None,
                next_leg=following,
                **common,
            )
    return None  # pragma: no cover - 上面的循环必然返回


def whereabouts(
    trips: tuple[Trip, ...] | list[Trip], *, at: datetime, include_completed: bool = False
) -> tuple[Whereabouts, ...]:
    """所有有观察对象的差旅此刻的位置：在途的排最前，然后是在目的地的、还没出发的。"""
    rows = [item for item in (whereabouts_of(trip, at=at) for trip in trips) if item is not None]
    if not include_completed:
        rows = [item for item in rows if item.status is not WhereaboutsStatus.COMPLETED]
    rows.sort(key=lambda item: (_ORDER[item.status], item.traveler_id, item.trip_id))
    return tuple(rows)

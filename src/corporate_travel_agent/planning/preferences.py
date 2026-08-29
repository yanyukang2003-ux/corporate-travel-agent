"""软偏好怎么影响排序：每个偏好各自对应一个真实的评分维度。

这个模块存在的唯一理由是**堵住空转的偏好**。此前 `lowest_cost`、`shortest_duration`、
`compare_train_and_flight` 三个名字在词表里，模型也会照着填，但排序代码里根本没出现——
声明与不声明毫无区别。词表是承诺，承诺必须兑现：每个受支持的偏好在这里都要指明
它到底改变了什么，`tests/test_preferences.py` 会盯着这份对应表不许有空缺。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum

from corporate_travel_agent.domain.enums import TransportMode
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    HotelOffer,
    TransportOffer,
    TripRequestVersion,
)


class PreferenceEffect(StrEnum):
    """一个偏好用什么方式影响结果。"""

    LEG_PENALTY = "LEG_PENALTY"
    """按航段罚分：这一段不合口味就扣分。"""

    LODGING_PENALTY = "LODGING_PENALTY"
    """按住宿罚分。"""

    DURATION_WEIGHT = "DURATION_WEIGHT"
    """改变"时长"在总分里相对"价格"的分量。"""

    RESULT_DIVERSITY = "RESULT_DIVERSITY"
    """不改分数，改的是最终摆出来的这几个方案必须涵盖什么。"""


PREFERENCE_EFFECTS: dict[str, PreferenceEffect] = {
    "avoid_early_departure": PreferenceEffect.LEG_PENALTY,
    "prefer_train": PreferenceEffect.LEG_PENALTY,
    "prefer_flight": PreferenceEffect.LEG_PENALTY,
    "hotel_near_client": PreferenceEffect.LODGING_PENALTY,
    "lowest_cost": PreferenceEffect.DURATION_WEIGHT,
    "shortest_duration": PreferenceEffect.DURATION_WEIGHT,
    "compare_train_and_flight": PreferenceEffect.RESULT_DIVERSITY,
}
"""受支持软偏好 → 它实际改变的那个维度。缺一个就是有名字没实现。"""

# 价格和时长本来没有共同单位，把它们加成一个分数就必须先给出一个换算比例。
# 这个比例是一个写明的选择，不是自然常数，所以三档之间差 10 倍，好当着人说出口：
#: 基准：10 分钟抵一块钱。谁都没表态时就用它，行为与此前一致。
_BASE_MINUTES_PER_UNIT = Decimal("10")
#: "怎么便宜怎么来"：一小时才抵一块钱，时长基本不参与竞争。
_LOWEST_COST_MINUTES_PER_UNIT = Decimal("60")
#: "越快越好"：一分钟就抵一块钱，多花半天路程要贵得很明显才划得来。
_SHORTEST_DURATION_MINUTES_PER_UNIT = Decimal("1")

_EARLY_DEPARTURE_HOUR = 7
_EARLY_DEPARTURE_PENALTY = Decimal("200")
_WRONG_MODE_PENALTY = Decimal("80")
_ACCEPTABLE_COMMUTE_MINUTES = 30
_COMMUTE_PENALTY_PER_MINUTE = Decimal("2")


def duration_minutes_per_unit(request: TripRequestVersion) -> Decimal:
    """把行程总时长折算成分数时，多少分钟算一个单位。

    两个都说了等于"都重要"，那就是基准比例——这不是取巧，"又要便宜又要快"
    本来就是让两边平等参与，而不是让其中一边压倒另一边。
    """
    preferences = request.journey_wide_preferences()
    wants_cheap = "lowest_cost" in preferences
    wants_fast = "shortest_duration" in preferences
    if wants_cheap and not wants_fast:
        return _LOWEST_COST_MINUTES_PER_UNIT
    if wants_fast and not wants_cheap:
        return _SHORTEST_DURATION_MINUTES_PER_UNIT
    return _BASE_MINUTES_PER_UNIT


def leg_penalty(
    request: TripRequestVersion, leg_index: int, offer: TransportOffer
) -> Decimal:
    """第 ``leg_index`` 段的偏好罚分；只看管得到这一段的那些偏好。

    此前这个函数只收去程，往返里说"优先高铁"时返程根本没被评分，段数一多
    漏评的比例还会成比例放大。
    """
    preferences = request.preferences_for_leg(leg_index)
    penalty = Decimal("0")
    if (
        "avoid_early_departure" in preferences
        and offer.depart_at.hour < _EARLY_DEPARTURE_HOUR
    ):
        penalty += _EARLY_DEPARTURE_PENALTY
    if "prefer_train" in preferences and offer.mode is not TransportMode.TRAIN:
        penalty += _WRONG_MODE_PENALTY
    if "prefer_flight" in preferences and offer.mode is not TransportMode.FLIGHT:
        penalty += _WRONG_MODE_PENALTY
    return penalty


def lodging_penalty(request: TripRequestVersion, hotel: HotelOffer | None) -> Decimal:
    """住宿相关的偏好罚分。"""
    if hotel is None:
        return Decimal("0")
    preferences = request.journey_wide_preferences()
    if "hotel_near_client" not in preferences:
        return Decimal("0")
    if hotel.commute_minutes == COMMUTE_UNKNOWN_MINUTES:
        return Decimal("0")
    excess = hotel.commute_minutes - _ACCEPTABLE_COMMUTE_MINUTES
    if excess <= 0:
        return Decimal("0")
    return Decimal(excess) * _COMMUTE_PENALTY_PER_MINUTE


def preference_penalty(
    request: TripRequestVersion,
    transports: Sequence[TransportOffer],
    stays: Sequence[HotelOffer] | HotelOffer | None,
) -> Decimal:
    """整趟走法的偏好罚分：每一段都算，**每一处住宿也各算一遍**。

    ``stays`` 仍接受单个酒店或 ``None``，既有调用方一个字都不用改。
    """
    lodging: Sequence[HotelOffer]
    if stays is None:
        lodging = ()
    elif isinstance(stays, HotelOffer):
        lodging = (stays,)
    else:
        lodging = [item for item in stays if item is not None]
    total = sum(
        (leg_penalty(request, index, offer) for index, offer in enumerate(transports)),
        Decimal("0"),
    )
    return total + sum(
        (lodging_penalty(request, stay) for stay in lodging), Decimal("0")
    )


def wants_mode_comparison(request: TripRequestVersion) -> bool:
    """旅行者是否要求把高铁和飞机放在一起比。"""
    return "compare_train_and_flight" in request.journey_wide_preferences()

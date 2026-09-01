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

from corporate_travel_agent.domain.enums import PreferenceOrigin, TransportMode
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    EmployeeTravelProfileSnapshot,
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

#: 一条**推断出来的**偏好，罚分打几折。
#:
#: 这四个数是一个写明的选择，不是自然常数。定这个梯度的理由只有一条：
#: **证据强度不一样，说话的分量就不该一样。** 他刚才亲口说的和"同级同事一般这么选"
#: 摆在同一个天平上，等于系统在替他做决定。
#:
#: `STATED` 不在表里——亲口说的走的是请求本身那条路，全权重，不打折。
_ORIGIN_WEIGHTS: dict[PreferenceOrigin, Decimal] = {
    #: 员工自己填的档案：是他本人的意思，但不是针对这一趟。
    PreferenceOrigin.DECLARED: Decimal("0.6"),
    #: 从他自己的历史行程推的：是推断，不是他说的。
    PreferenceOrigin.OBSERVED: Decimal("0.4"),
    #: 同级同城同事的常见选择：**根本不是关于他本人的**，只够轻轻推一下。
    PreferenceOrigin.ORG_DEFAULT: Decimal("0.2"),
}

#: 互相排斥的偏好各成一族。**这一轮说了族里任意一个，整族的推断全部让位。**
#:
#: 他这次说"这趟要飞"，画像里的"平时坐高铁"就不该再往下压分——那是拿上个月的习惯
#: 去和这一趟的原话打架。让位是整族让，不是只让同名的那一个：说了 `prefer_flight`
#: 之后，推断出来的 `prefer_train` 必须一起消失，否则两边互相抵消，
#: 排出来的顺序谁也解释不了。
_CONFLICT_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"prefer_train", "prefer_flight", "compare_train_and_flight"}),
    frozenset({"lowest_cost", "shortest_duration"}),
    frozenset({"avoid_early_departure"}),
    frozenset({"hotel_near_client"}),
)

_EARLY_DEPARTURE_HOUR = 7
_EARLY_DEPARTURE_PENALTY = Decimal("200")
_WRONG_MODE_PENALTY = Decimal("80")
_ACCEPTABLE_COMMUTE_MINUTES = 30
#: 住过还想住的酒店减多少分。比早班罚分小一档——习惯是加分项，不该压过
#: 「这班太早了」这种实打实的不便。
_REPEAT_HOTEL_BONUS = Decimal("60")
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


def weighted_profile_preferences(
    stated: frozenset[str],
    profile: EmployeeTravelProfileSnapshot | None,
) -> dict[str, Decimal]:
    """画像里**还能说话**的那些偏好，各自打完折之后的分量。

    两步：先按族让位——这一轮说过的那一族，整族的推断全部作废；剩下的按来源打折。
    同一个名字有多个来源时取最强的那一个，**不累加**：同一件事被推断了两次，
    不等于更确定。
    """
    if profile is None:
        return {}
    silenced: frozenset[str] = frozenset()
    for family in _CONFLICT_FAMILIES:
        if family & stated:
            silenced |= family

    weights: dict[str, Decimal] = {}
    for item in profile.preferences:
        if item.name in silenced or item.name in stated:
            continue
        weight = _ORIGIN_WEIGHTS.get(item.origin)
        if weight is None:
            continue
        weights[item.name] = max(weights.get(item.name, Decimal("0")), weight)
    return weights


def _strength(
    name: str, stated: frozenset[str], inferred: dict[str, Decimal]
) -> Decimal:
    """这条偏好现在有多大分量：亲口说过就是 1，推断出来的按来源打折，都没有就是 0。"""
    if name in stated:
        return Decimal("1")
    return inferred.get(name, Decimal("0"))


def leg_penalty(
    request: TripRequestVersion,
    leg_index: int,
    offer: TransportOffer,
    profile: EmployeeTravelProfileSnapshot | None = None,
) -> Decimal:
    """第 ``leg_index`` 段的偏好罚分；只看管得到这一段的那些偏好。

    此前这个函数只收去程，往返里说「优先高铁」时返程根本没被评分，段数一多
    漏评的比例还会成比例放大。

    ``profile`` 是这位员工的习惯画像。**不传就和以前逐字一样**——这不是偷懒，
    是为了让既有评测基线还能直接对比：接上画像那一刻分数就变了，
    旧数字和新数字不能放在同一张图上。
    """
    stated = request.preferences_for_leg(leg_index)
    inferred = weighted_profile_preferences(stated, profile)
    penalty = Decimal("0")
    if offer.depart_at.hour < _EARLY_DEPARTURE_HOUR:
        penalty += _EARLY_DEPARTURE_PENALTY * _strength(
            "avoid_early_departure", stated, inferred
        )
    if offer.mode is not TransportMode.TRAIN:
        penalty += _WRONG_MODE_PENALTY * _strength("prefer_train", stated, inferred)
    if offer.mode is not TransportMode.FLIGHT:
        penalty += _WRONG_MODE_PENALTY * _strength("prefer_flight", stated, inferred)
    return penalty


def lodging_penalty(
    request: TripRequestVersion,
    hotel: HotelOffer | None,
    profile: EmployeeTravelProfileSnapshot | None = None,
) -> Decimal:
    """住宿相关的偏好罚分。

    画像里的 ``preferred_hotels`` 在这里**减分**：那是这位员工住过、而且又选了
    第二次的酒店，是他自己的选择留下的痕迹，不是系统的猜测。
    """
    if hotel is None:
        return Decimal("0")
    stated = request.journey_wide_preferences()
    inferred = weighted_profile_preferences(stated, profile)
    strength = _strength("hotel_near_client", stated, inferred)

    penalty = Decimal("0")
    if strength > 0 and hotel.commute_minutes != COMMUTE_UNKNOWN_MINUTES:
        excess = hotel.commute_minutes - _ACCEPTABLE_COMMUTE_MINUTES
        if excess > 0:
            penalty += Decimal(excess) * _COMMUTE_PENALTY_PER_MINUTE * strength

    if profile is not None and hotel.name in profile.preferred_hotels:
        penalty -= _REPEAT_HOTEL_BONUS
    return penalty


def preference_penalty(
    request: TripRequestVersion,
    transports: Sequence[TransportOffer],
    stays: Sequence[HotelOffer] | HotelOffer | None,
    profile: EmployeeTravelProfileSnapshot | None = None,
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
        (
            leg_penalty(request, index, offer, profile)
            for index, offer in enumerate(transports)
        ),
        Decimal("0"),
    )
    return total + sum(
        (lodging_penalty(request, stay, profile) for stay in lodging), Decimal("0")
    )


def wants_mode_comparison(request: TripRequestVersion) -> bool:
    """旅行者是否要求把高铁和飞机放在一起比。"""
    return "compare_train_and_flight" in request.journey_wide_preferences()

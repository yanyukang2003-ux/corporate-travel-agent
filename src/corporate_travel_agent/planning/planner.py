"""行程规划器：先枚举**走法**，再为每种走法挑报价，最后按类别与政策分档摆出来。

**走法**：这趟差旅**怎么走**——每一段坐飞机还是高铁、要不要住一晚。
14 点那班和 16 点那班不是两种走法，是同一种走法的两个价格。

此前这里是「拿到全部报价 → 笛卡尔积 → 过滤 → 按分数取前三」，于是推荐列表经常
是同一个走法的三个价格：看着有三个选择，其实只有一个。差旅本来就没有唯一的最好
——便宜的起得早，舒服的贵——把它们揉成一个分数再取前三，等于替旅行者做了他自己
该做的取舍。规划器该产出的是**几个真正不同的走法，每个都是它那一类里最好的**。

规模在开工前量过（`examples/measure_plan_shape_enumeration.py`）：6 段行程最坏 128
种走法，库存查询次数一次不多——一段查一次就把这段所有交通方式都拿回来了。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from itertools import product

from corporate_travel_agent.domain.enums import PolicyOutcome, TransportMode
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    EmployeeProfileSnapshot,
    HotelOffer,
    PolicySnapshot,
    TransportOffer,
    TravelOptionVersion,
    TripRequestVersion,
)
from corporate_travel_agent.policy.engine import PolicyEngine

from .feasibility import FeasibilityValidator
from .preferences import (
    duration_minutes_per_unit,
    preference_penalty,
    wants_mode_comparison,
)


@dataclass(frozen=True, slots=True)
class PlanShape:
    """一种走法：每段坐什么，住不住。"""

    modes: tuple[TransportMode, ...]
    with_lodging: bool

    def label(self) -> str:
        """给解释用的一句话标签，例如 ``FLIGHT+TRAIN+hotel``。"""
        parts = [mode.value for mode in self.modes]
        if self.with_lodging:
            parts.append("hotel")
        return "+".join(parts)


def _leg_satisfies_constraints(
    request: TripRequestVersion, leg_index: int, offer: TransportOffer
) -> bool:
    """这一段受哪几条硬要求管，就只用那几条判它。

    此前 `direct_only` 是一个全局字符串，"去程直飞就行、返程无所谓"只能被放大成
    全程直飞，于是旅行者明明接受的返程方案被无声筛掉了。
    """
    names = request.constraints_for_leg(leg_index)
    if "train_only" in names and offer.mode is not TransportMode.TRAIN:
        return False
    if "flight_only" in names and offer.mode is not TransportMode.FLIGHT:
        return False
    return not ("direct_only" in names and not offer.is_direct)


class ItineraryPlanner:
    """先枚举走法，再为每种走法挑报价，最后按类别与政策分档选出要摆的方案。"""

    def __init__(
        self,
        validator: FeasibilityValidator | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.validator = validator or FeasibilityValidator()
        self.policy_engine = policy_engine or PolicyEngine()

    def plan(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        outbound_offers: list[TransportOffer],
        inbound_offers: list[TransportOffer],
        hotel_offers: list[HotelOffer],
        limit: int = 3,
        *,
        now: datetime,
    ) -> list[TravelOptionVersion]:
        """产出最多 ``limit`` 条**互不相同的**方案，外加需要说明的被挡方案。"""
        pools = [
            [
                offer
                for offer in outbound_offers
                if _leg_satisfies_constraints(request, 0, offer)
            ]
        ]
        if request.return_after is not None:
            pools.append(
                [
                    offer
                    for offer in inbound_offers
                    if _leg_satisfies_constraints(request, 1, offer)
                ]
            )
        hotel_choices: list[HotelOffer | None] = (
            list(hotel_offers) if request.hotel_check_in is not None else [None]
        )

        minutes_per_unit = duration_minutes_per_unit(request)
        candidates: list[tuple[PlanShape, TravelOptionVersion]] = []
        for shape in _enumerate_shapes(pools, hotel_choices):
            shaped_pools = [
                [offer for offer in pool if offer.mode is shape.modes[index]]
                for index, pool in enumerate(pools)
            ]
            shaped_hotels: list[HotelOffer | None] = (
                [item for item in hotel_choices if item is not None]
                if shape.with_lodging
                else [None]
            )
            for combination in product(*shaped_pools, shaped_hotels):
                *transports, hotel = combination
                option = self._evaluate(
                    request,
                    employee,
                    policy,
                    list(transports),
                    hotel,
                    shape,
                    minutes_per_unit,
                    now=now,
                )
                if option is not None:
                    candidates.append((shape, option))

        return _select_options(
            candidates, limit, compare_modes=wants_mode_comparison(request)
        )

    def _evaluate(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        transports: list[TransportOffer],
        hotel: HotelOffer | None,
        shape: PlanShape,
        minutes_per_unit: Decimal,
        *,
        now: datetime,
    ) -> TravelOptionVersion | None:
        """把一种走法的一组具体报价评成一条方案；不可行则返回 None。"""
        outbound = transports[0]
        inbound = transports[1] if len(transports) > 1 else None
        feasibility = self.validator.validate(
            request, outbound, inbound, hotel, policy.arrival_buffer_minutes, now=now
        )
        if not feasibility.feasible:
            return None

        # 政策只做标注，不在这里过滤。被禁的方案也要带着理由留在结果里——
        # 否则旅行者只会看到一份莫名偏贵的列表，永远不知道最便宜那个是被政策禁的。
        # 要不要展示给用户，是展示层的决定，不是规划层的决定。
        decision = self.policy_engine.evaluate(employee, policy, transports, hotel)

        total_cost = sum((item.price for item in transports), Decimal("0"))
        if hotel:
            total_cost += hotel.total_price
        duration = sum(_minutes(item.depart_at, item.arrive_at) for item in transports)
        penalty = preference_penalty(request, transports, hotel)
        # 政策**不进分数**。它是三档分类结论，不是可以被价格投票推翻的权重：
        # 折算成罚分的话，一个需审批但足够便宜的方案会排到完全合规的前面。
        # 排序改为「先按政策分档，档内再按分数」，见 _select_options。
        score = total_cost + Decimal(duration) / minutes_per_unit + penalty
        snapshot_ids = tuple(
            dict.fromkeys(
                [item.snapshot_id for item in transports]
                + ([hotel.snapshot_id] if hotel else [])
            )
        )
        facts = [
            f"total_cost={total_cost}",
            f"currency={policy.currency}",
            f"outbound={outbound.ref_id}",
            f"policy={decision.outcome.value}",
            f"plan_shape={shape.label()}",
            f"inventory_snapshots={','.join(snapshot_ids)}",
        ]
        if inbound:
            facts.append(f"inbound={inbound.ref_id}")
        if hotel:
            commute_fact = (
                "commute_minutes=unknown"
                if hotel.commute_minutes == COMMUTE_UNKNOWN_MINUTES
                else f"commute_minutes={hotel.commute_minutes}"
            )
            facts.extend([f"hotel={hotel.ref_id}", commute_fact])
        option_key = "-".join(item.ref_id for item in transports)
        if hotel:
            option_key += f"-{hotel.ref_id}"
        return TravelOptionVersion(
            option_id=f"opt-{option_key}",
            version=1,
            trip_request_version=request.version,
            inventory_snapshot_ids=snapshot_ids,
            outbound=outbound,
            inbound=inbound,
            hotel=hotel,
            total_cost=total_cost,
            total_duration_minutes=duration,
            feasibility=feasibility,
            policy_decision=decision,
            preference_penalty=penalty,
            score=score,
            explanation_facts=tuple(facts),
            currency=policy.currency,
        )


def _enumerate_shapes(
    pools: list[list[TransportOffer]], hotel_choices: list[HotelOffer | None]
) -> list[PlanShape]:
    """先把走法数出来：每段有哪几种交通方式的货，住宿是不是一个真选择。

    只枚举**库存里真有的**交通方式——涵盖不了的走法不该靠编造来凑。
    """
    mode_sets = [
        sorted({offer.mode for offer in pool}, key=lambda item: item.value)
        for pool in pools
    ]
    if not mode_sets or any(not modes for modes in mode_sets):
        return []
    lodging_choices = [True] if any(item is not None for item in hotel_choices) else [False]
    return [
        PlanShape(modes=tuple(modes), with_lodging=lodging)
        for modes in product(*mode_sets)
        for lodging in lodging_choices
    ]


#: 政策分档：合规优先，需审批其次，不可选的排最后。政策不参与分数计算。
_POLICY_BANDS: dict[PolicyOutcome, int] = {
    PolicyOutcome.COMPLIANT: 0,
    PolicyOutcome.REQUIRES_APPROVAL: 1,
}
_BLOCKED_BAND = 2


def _policy_band(option: TravelOptionVersion) -> int:
    return _POLICY_BANDS.get(option.policy_decision.outcome, _BLOCKED_BAND)


def _rank_key(entry: tuple[PlanShape, TravelOptionVersion]) -> tuple[object, ...]:
    option = entry[1]
    return (_policy_band(option), option.score, option.option_id)


def _select_options(
    candidates: list[tuple[PlanShape, TravelOptionVersion]],
    limit: int,
    *,
    compare_modes: bool = False,
) -> list[TravelOptionVersion]:
    """选出要摆给旅行者的几条：先分档，档内按类别取，再按走法补齐。

    **类别**指的是"它是哪一类里最好的"——最便宜的、最快的、综合最合适的。
    差旅没有唯一的最好，把这些揉成一个分数再取前三，等于替旅行者做了他该做的取舍。

    ``compare_modes`` 对应偏好 `compare_train_and_flight`："我想比比高铁和飞机"。
    这句话要的不是罚分，而是**摆出来的这几个里两种都得有**。
    """
    ranked = sorted(candidates, key=_rank_key)
    eligible: list[tuple[PlanShape, TravelOptionVersion]] = []
    blocked: list[TravelOptionVersion] = []
    seen: set[tuple[object, ...]] = set()
    for shape, option in ranked:
        fingerprint = _display_fingerprint(option)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        if _policy_band(option) < _BLOCKED_BAND:
            eligible.append((shape, option))
        else:
            blocked.append(option)

    if not eligible:
        return []

    chosen = _choose_by_category(eligible, limit)
    if compare_modes:
        chosen = _cover_every_available_mode(chosen, eligible, limit)

    categories = _category_labels(eligible, chosen)
    selectable = [
        replace(
            option,
            explanation_facts=(
                *option.explanation_facts,
                f"category={categories[option.option_id]}",
            ),
        )
        for _, option in sorted(chosen, key=_rank_key)
    ]
    best_selectable = min(item.score for item in selectable)
    would_have_won = [item for item in blocked if item.score < best_selectable]
    # 被政策挡住的方案只在**本可胜出**时才保留：那正是"最便宜的那个被政策禁了"
    # 这句话需要说出口的情形。比可选方案还差的被挡方案只是噪音，不保留。
    return selectable + would_have_won[:1]


def _choose_by_category(
    eligible: list[tuple[PlanShape, TravelOptionVersion]], limit: int
) -> list[tuple[PlanShape, TravelOptionVersion]]:
    """每一类各取一个代表，再按"还没出现过的走法"优先补齐到 ``limit``。"""
    chosen: list[tuple[PlanShape, TravelOptionVersion]] = []

    def take(entry: tuple[PlanShape, TravelOptionVersion] | None) -> None:
        if entry is None or len(chosen) >= limit:
            return
        if any(item[1].option_id == entry[1].option_id for item in chosen):
            return
        chosen.append(entry)

    take(eligible[0])  # 综合最合适的：排名本身就是这一类。
    take(min(eligible, key=lambda item: (item[1].total_cost, _rank_key(item))))
    take(min(eligible, key=lambda item: (item[1].total_duration_minutes, _rank_key(item))))

    # 补齐时优先换一个还没出现过的走法，别让同一个走法的价格变体占满名单。
    for prefer_new_shape in (True, False):
        for entry in eligible:
            if len(chosen) >= limit:
                break
            if prefer_new_shape and any(item[0] == entry[0] for item in chosen):
                continue
            take(entry)
    return sorted(chosen, key=_rank_key)


def _category_labels(
    eligible: list[tuple[PlanShape, TravelOptionVersion]],
    chosen: list[tuple[PlanShape, TravelOptionVersion]],
) -> dict[str, str]:
    """给每条入选方案贴一句"它凭什么在这儿"，好让解释说得出彼此差在哪。"""
    cheapest = min(eligible, key=lambda item: (item[1].total_cost, _rank_key(item)))[1]
    fastest = min(
        eligible, key=lambda item: (item[1].total_duration_minutes, _rank_key(item))
    )[1]
    best_overall = eligible[0][1]
    labels: dict[str, str] = {}
    for _, option in chosen:
        tags = []
        if option.option_id == best_overall.option_id:
            tags.append("best_overall")
        if option.option_id == cheapest.option_id:
            tags.append("cheapest")
        if option.option_id == fastest.option_id:
            tags.append("fastest")
        labels[option.option_id] = "|".join(tags) if tags else "alternative"
    return labels


def _modes_used(option: TravelOptionVersion) -> frozenset[TransportMode]:
    modes = {option.outbound.mode}
    if option.inbound is not None:
        modes.add(option.inbound.mode)
    return frozenset(modes)


def _cover_every_available_mode(
    chosen: list[tuple[PlanShape, TravelOptionVersion]],
    eligible: list[tuple[PlanShape, TravelOptionVersion]],
    limit: int,
) -> list[tuple[PlanShape, TravelOptionVersion]]:
    """让入选名单涵盖库存里真实存在的每种交通方式，名额不变。

    库存里只有飞机时什么都不做——涵盖不了的东西不该靠编造来凑。
    """
    available = {
        mode
        for _, option in eligible
        for mode in _modes_used(option)
        if mode in {TransportMode.TRAIN, TransportMode.FLIGHT}
    }
    result = sorted(chosen, key=_rank_key)
    for mode in sorted(available, key=lambda item: item.value):
        if any(mode in _modes_used(option) for _, option in result):
            continue
        replacement = next(
            (
                entry
                for entry in eligible
                if mode in _modes_used(entry[1])
                and all(entry[1].option_id != item[1].option_id for item in result)
            ),
            None,
        )
        if replacement is None:
            continue
        if len(result) < limit:
            result.append(replacement)
        else:
            result[-1] = replacement
        result = sorted(result, key=_rank_key)
    return result


def _display_fingerprint(option: TravelOptionVersion) -> tuple[object, ...]:
    hotel = option.hotel
    hotel_key: tuple[object, ...]
    if hotel is None:
        hotel_key = ()
    else:
        hotel_key = (
            hotel.name.casefold(),
            hotel.city.casefold(),
            hotel.nightly_price,
            hotel.nights,
            hotel.check_in,
            hotel.check_out,
        )
    inbound_id = option.inbound.ref_id if option.inbound else None
    return (option.outbound.ref_id, inbound_id, hotel_key)


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)

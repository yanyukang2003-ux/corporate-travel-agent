"""行程规划器：枚举交通/酒店组合，经可行性与政策过滤后按分数排序输出。"""

from __future__ import annotations

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


def _legs_satisfy_constraints(
    request: TripRequestVersion, transports: list[TransportOffer]
) -> bool:
    """逐段核对硬要求：这一段受哪几条管，就只用那几条判它。

    此前 `direct_only` 是一个全局字符串，"去程直飞就行、返程无所谓"只能被放大成全程直飞，
    于是旅行者明明接受的返程方案被无声筛掉了。
    """
    for index, offer in enumerate(transports):
        names = request.constraints_for_leg(index)
        if "train_only" in names and offer.mode is not TransportMode.TRAIN:
            return False
        if "flight_only" in names and offer.mode is not TransportMode.FLIGHT:
            return False
        if "direct_only" in names and not offer.is_direct:
            return False
    return True


class ItineraryPlanner:
    """枚举完整行程组合，硬过滤后对幸存方案打分并排序。"""

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
        """笛卡尔积组合去程/返程/酒店，过滤后返回最多 ``limit`` 条去重排名方案。"""
        inbound_choices: list[TransportOffer | None] = (
            list(inbound_offers) if request.return_after is not None else [None]
        )
        hotel_choices: list[HotelOffer | None] = (
            list(hotel_offers) if request.hotel_check_in is not None else [None]
        )

        minutes_per_unit = duration_minutes_per_unit(request)
        options: list[TravelOptionVersion] = []
        for outbound, inbound, hotel in product(
            outbound_offers, inbound_choices, hotel_choices
        ):
            transports = [outbound, *([inbound] if inbound else [])]
            if not _legs_satisfy_constraints(request, transports):
                continue
            feasibility = self.validator.validate(
                request, outbound, inbound, hotel, policy.arrival_buffer_minutes, now=now
            )
            if not feasibility.feasible:
                continue

            # 政策只做标注，不在这里过滤。被禁的方案也要带着理由留在结果里——
            # 否则旅行者只会看到一份莫名偏贵的列表，永远不知道最便宜那个是被政策禁的。
            # 要不要展示给用户，是展示层的决定，不是规划层的决定。
            decision = self.policy_engine.evaluate(employee, policy, transports, hotel)

            total_cost = sum((item.price for item in transports), Decimal("0"))
            if hotel:
                total_cost += hotel.total_price
            duration = sum(self._minutes(item.depart_at, item.arrive_at) for item in transports)
            penalty = preference_penalty(request, transports, hotel)
            # 政策**不进分数**。它是三档分类结论，不是可以被价格投票推翻的权重：
            # 折算成罚分的话，一个需审批但足够便宜的方案会排到完全合规的前面。
            # 排序改为「先按政策分档，档内再按分数」，见 _select_ranked_options。
            score = total_cost + Decimal(duration) / minutes_per_unit + penalty
            snapshot_ids = tuple(
                dict.fromkeys(
                    [outbound.snapshot_id]
                    + ([inbound.snapshot_id] if inbound else [])
                    + ([hotel.snapshot_id] if hotel else [])
                )
            )
            facts = [
                f"total_cost={total_cost}",
                f"currency={policy.currency}",
                f"outbound={outbound.ref_id}",
                f"policy={decision.outcome.value}",
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
            options.append(
                TravelOptionVersion(
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
            )

        return self._select_ranked_options(
            options, limit, compare_modes=wants_mode_comparison(request)
        )

    #: 政策分档：合规优先，需审批其次，不可选的排最后。政策不参与分数计算。
    _POLICY_BANDS: dict[PolicyOutcome, int] = {
        PolicyOutcome.COMPLIANT: 0,
        PolicyOutcome.REQUIRES_APPROVAL: 1,
    }
    _BLOCKED_BAND = 2

    @classmethod
    def _policy_band(cls, option: TravelOptionVersion) -> int:
        return cls._POLICY_BANDS.get(option.policy_decision.outcome, cls._BLOCKED_BAND)

    @staticmethod
    def _select_ranked_options(
        options: list[TravelOptionVersion],
        limit: int,
        *,
        compare_modes: bool = False,
    ) -> list[TravelOptionVersion]:
        """先按政策分档、档内按分数排序；被政策挡住的方案只在本可胜出时才保留。

        保留的目的是**透明**，不是凑数：只有当一个被挡住的方案分数优于所有可选方案时，
        它才值得摆出来——那正是"最便宜的那个被政策禁了"这句话需要说出口的情形。
        比可选方案还差的被挡方案只是噪音，不保留。

        ``compare_modes`` 对应偏好 `compare_train_and_flight`："我想比比高铁和飞机"。
        这句话要的不是罚分而是**摆出来的这几个方案里两种都得有**，所以它在这里生效，
        不在打分里生效。
        """
        ranked = sorted(
            options,
            key=lambda item: (
                ItineraryPlanner._policy_band(item),
                item.score,
                item.option_id,
            ),
        )
        selectable: list[TravelOptionVersion] = []
        blocked: list[TravelOptionVersion] = []
        eligible: list[TravelOptionVersion] = []
        seen: set[tuple[object, ...]] = set()
        for option in ranked:
            fingerprint = ItineraryPlanner._display_fingerprint(option)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            if ItineraryPlanner._policy_band(option) < ItineraryPlanner._BLOCKED_BAND:
                eligible.append(option)
                if len(selectable) < limit:
                    selectable.append(option)
            else:
                blocked.append(option)

        if compare_modes:
            selectable = ItineraryPlanner._cover_both_modes(selectable, eligible, limit)

        if not selectable:
            return []
        best_selectable = min(item.score for item in selectable)
        would_have_won = [item for item in blocked if item.score < best_selectable]
        return selectable + would_have_won[:1]


    @staticmethod
    def _modes_used(option: TravelOptionVersion) -> frozenset[TransportMode]:
        modes = {option.outbound.mode}
        if option.inbound is not None:
            modes.add(option.inbound.mode)
        return frozenset(modes)

    @staticmethod
    def _cover_both_modes(
        selectable: list[TravelOptionVersion],
        eligible: list[TravelOptionVersion],
        limit: int,
    ) -> list[TravelOptionVersion]:
        """让入选名单涵盖库存里真实存在的每种交通方式，名额不变。

        库存里只有飞机时什么都不做——涵盖不了的东西不该靠编造来凑。
        """
        available = {
            mode
            for option in eligible
            for mode in ItineraryPlanner._modes_used(option)
            if mode in {TransportMode.TRAIN, TransportMode.FLIGHT}
        }
        chosen = list(selectable)
        for mode in sorted(available, key=lambda item: item.value):
            if any(mode in ItineraryPlanner._modes_used(item) for item in chosen):
                continue
            replacement = next(
                (
                    item
                    for item in eligible
                    if mode in ItineraryPlanner._modes_used(item) and item not in chosen
                ),
                None,
            )
            if replacement is None:
                continue
            if len(chosen) < limit:
                chosen.append(replacement)
            else:
                chosen[-1] = replacement
        return chosen

    @staticmethod
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

    @staticmethod
    def _minutes(start: datetime, end: datetime) -> int:
        return int((end - start).total_seconds() // 60)

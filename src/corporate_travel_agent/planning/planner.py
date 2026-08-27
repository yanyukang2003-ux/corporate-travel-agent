"""行程规划器：枚举交通/酒店组合，经可行性与政策过滤后按分数排序输出。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import product

from corporate_travel_agent.domain.enums import PolicyOutcome
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

        options: list[TravelOptionVersion] = []
        for outbound, inbound, hotel in product(
            outbound_offers, inbound_choices, hotel_choices
        ):
            transports = [outbound, *([inbound] if inbound else [])]
            hard_constraints = set(request.hard_constraints)
            if "train_only" in hard_constraints and any(
                item.mode.value != "TRAIN" for item in transports
            ):
                continue
            if "flight_only" in hard_constraints and any(
                item.mode.value != "FLIGHT" for item in transports
            ):
                continue
            if "direct_only" in hard_constraints and any(
                not item.is_direct for item in transports
            ):
                continue
            feasibility = self.validator.validate(
                request, outbound, inbound, hotel, policy.arrival_buffer_minutes, now=now
            )
            if not feasibility.feasible:
                continue

            decision = self.policy_engine.evaluate(employee, policy, transports, hotel)
            if decision.outcome in {
                PolicyOutcome.FORBIDDEN,
                PolicyOutcome.INSUFFICIENT_EVIDENCE,
            }:
                continue

            total_cost = sum((item.price for item in transports), Decimal("0"))
            if hotel:
                total_cost += hotel.total_price
            duration = sum(self._minutes(item.depart_at, item.arrive_at) for item in transports)
            preference_penalty = self._preference_penalty(request, outbound, hotel)
            # 需审批方案加重罚分，排序时靠后
            policy_penalty = (
                Decimal("1000")
                if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
                else Decimal("0")
            )
            score = (
                total_cost
                + Decimal(duration) / Decimal("10")
                + preference_penalty
                + policy_penalty
            )
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
                    preference_penalty=preference_penalty,
                    score=score,
                explanation_facts=tuple(facts),
                currency=policy.currency,
                )
            )

        return self._select_ranked_options(options, limit)

    @staticmethod
    def _select_ranked_options(
        options: list[TravelOptionVersion],
        limit: int,
    ) -> list[TravelOptionVersion]:
        """按分数排序后保留展示指纹唯一的方案，避免同酒店多价位重复。"""
        ranked = sorted(options, key=lambda item: (item.score, item.option_id))
        unique: list[TravelOptionVersion] = []
        seen: set[tuple[object, ...]] = set()
        for option in ranked:
            fingerprint = ItineraryPlanner._display_fingerprint(option)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append(option)
        return unique[:limit]

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

    @staticmethod
    def _preference_penalty(
        request: TripRequestVersion,
        outbound: TransportOffer,
        hotel: HotelOffer | None,
    ) -> Decimal:
        """根据软偏好（早班、通勤、交通方式）累加排序罚分。"""
        penalty = Decimal("0")
        preferences = set(request.soft_preferences)
        if "avoid_early_departure" in preferences and outbound.depart_at.hour < 7:
            penalty += Decimal("200")
        if (
            "hotel_near_client" in preferences
            and hotel
            and hotel.commute_minutes != COMMUTE_UNKNOWN_MINUTES
            and hotel.commute_minutes > 30
        ):
            penalty += Decimal(hotel.commute_minutes - 30) * Decimal("2")
        if "prefer_train" in preferences and outbound.mode.value != "TRAIN":
            penalty += Decimal("80")
        if "prefer_flight" in preferences and outbound.mode.value != "FLIGHT":
            penalty += Decimal("80")
        return penalty

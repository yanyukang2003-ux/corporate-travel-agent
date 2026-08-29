"""政策引擎：以代码执行差旅合规判定，不依赖语言模型。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from corporate_travel_agent.domain.enums import PolicyOutcome, TransportMode
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    PolicyDecision,
    PolicySnapshot,
    RuleEvidence,
    TransportOffer,
)


def _as_hotels(
    stays: Iterable[HotelOffer] | HotelOffer | None,
) -> tuple[HotelOffer, ...]:
    """把"一家酒店或 None"和"一串住宿"都收成同一种形状。"""
    if stays is None:
        return ()
    if isinstance(stays, HotelOffer):
        return (stays,)
    return tuple(item for item in stays if item is not None)


class PolicyEngine:
    """将政策快照落实为可审计的规则证据与最终 outcome；决策不含 LLM。"""

    def evaluate(
        self,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        transports: Iterable[TransportOffer],
        stays: Iterable[HotelOffer] | HotelOffer | None,
    ) -> PolicyDecision:
        """评估职级舱位、币种、酒店上限与政策生效窗口，聚合为 PolicyDecision。

        ``stays`` 收的是**整串住宿**：多城行程一站一处，每处各判自己那座城市的
        夜费上限。它仍接受单个酒店或 ``None``，既有调用方一个字都不用改。
        """
        transports = tuple(transports)
        hotels = _as_hotels(stays)
        level_rule = policy.level_rules.get(employee.level)
        if level_rule is None:
            evidence = RuleEvidence(
                rule_id="employee.level.known",
                actual=employee.level,
                threshold="configured level",
                policy_version=policy.policy_version,
                outcome=PolicyOutcome.INSUFFICIENT_EVIDENCE,
                message=f"No policy rule is configured for level {employee.level}.",
                exception_allowed=False,
            )
            return PolicyDecision(PolicyOutcome.INSUFFICIENT_EVIDENCE, (evidence,))

        evidence: list[RuleEvidence] = []
        window_evidence = self._policy_window_evidence(policy, transports, hotels)
        if window_evidence is not None:
            evidence.append(window_evidence)
        priced_items = (*transports, *hotels)
        currencies = sorted({item.currency for item in priced_items})
        currency_compliant = currencies == [policy.currency]
        evidence.append(
            RuleEvidence(
                rule_id="pricing.currency",
                actual=", ".join(currencies),
                threshold=policy.currency,
                policy_version=policy.policy_version,
                outcome=(
                    PolicyOutcome.COMPLIANT
                    if currency_compliant
                    else PolicyOutcome.INSUFFICIENT_EVIDENCE
                ),
                message=(
                    f"All prices use policy currency {policy.currency}."
                    if currency_compliant
                    else "Prices cannot be combined without an approved FX conversion snapshot."
                ),
                exception_allowed=False,
            )
        )
        for segment in transports:
            allowed = (
                level_rule.allowed_flight_classes
                if segment.mode is TransportMode.FLIGHT
                else level_rule.allowed_train_classes
            )
            rule_id = f"transport.{segment.mode.value.lower()}.seat_class"
            compliant = segment.seat_class in allowed
            outcome = PolicyOutcome.COMPLIANT
            if not compliant:
                outcome = self._exception_outcome(rule_id, policy)
            evidence.append(
                RuleEvidence(
                    rule_id=rule_id,
                    actual=segment.seat_class,
                    threshold=", ".join(allowed),
                    policy_version=policy.policy_version,
                    outcome=outcome,
                    message=(
                        f"{segment.ref_id}: {segment.seat_class} is allowed."
                        if compliant
                        else f"{segment.ref_id}: {segment.seat_class} exceeds the level rule."
                    ),
                    exception_allowed=outcome is PolicyOutcome.REQUIRES_APPROVAL,
                )
            )

        # 一站一条证据。规则名不变——同一个 rule_id 出现多次是既有做法
        # （每段交通各产一条 seat_class），聚合照旧取最差的一档。
        for hotel in hotels:
            if hotel.currency != policy.currency:
                continue
            cap = policy.hotel_city_caps.get(hotel.city)
            rule_id = "hotel.city.nightly_cap"
            if cap is None:
                evidence.append(
                    RuleEvidence(
                        rule_id=rule_id,
                        actual=hotel.city,
                        threshold="configured city cap",
                        policy_version=policy.policy_version,
                        outcome=PolicyOutcome.INSUFFICIENT_EVIDENCE,
                        message=f"No hotel cap is configured for {hotel.city}.",
                        exception_allowed=False,
                    )
                )
            else:
                compliant = hotel.nightly_price <= cap
                outcome = (
                    PolicyOutcome.COMPLIANT
                    if compliant
                    else self._exception_outcome(rule_id, policy)
                )
                evidence.append(
                    RuleEvidence(
                        rule_id=rule_id,
                        actual=str(hotel.nightly_price),
                        threshold=str(cap),
                        policy_version=policy.policy_version,
                        outcome=outcome,
                        message=(
                            f"{hotel.name}: nightly price {hotel.nightly_price} "
                            f"is within cap {cap}."
                            if compliant
                            else f"{hotel.name}: nightly price {hotel.nightly_price} "
                            f"exceeds cap {cap}."
                        ),
                        exception_allowed=outcome is PolicyOutcome.REQUIRES_APPROVAL,
                    )
                )

        return PolicyDecision(self._aggregate(item.outcome for item in evidence), tuple(evidence))

    @staticmethod
    def _travel_service_dates(
        transports: tuple[TransportOffer, ...],
        hotels: tuple[HotelOffer, ...],
    ) -> tuple[date, ...]:
        dates: set[date] = {segment.depart_at.date() for segment in transports}
        for hotel in hotels:
            dates.add(hotel.check_in)
            # 退房日为排他；最后入住夜为退房日前一天
            last_night = hotel.check_out - timedelta(days=1)
            if last_night >= hotel.check_in:
                dates.add(last_night)
            else:
                dates.add(hotel.check_out)
        return tuple(sorted(dates))

    @classmethod
    def _policy_window_evidence(
        cls,
        policy: PolicySnapshot,
        transports: tuple[TransportOffer, ...],
        hotels: tuple[HotelOffer, ...],
    ) -> RuleEvidence | None:
        """检查行程服务日是否落在政策生效窗口内。"""
        service_dates = cls._travel_service_dates(transports, hotels)
        if not service_dates:
            return None

        threshold = (
            f"{policy.effective_from.isoformat()}..{policy.effective_to.isoformat()}"
            if policy.effective_to is not None
            else f"{policy.effective_from.isoformat()}.."
        )
        outside = [
            day
            for day in service_dates
            if day < policy.effective_from
            or (policy.effective_to is not None and day > policy.effective_to)
        ]
        if not outside:
            return RuleEvidence(
                rule_id="policy.effective_window",
                actual=",".join(day.isoformat() for day in service_dates),
                threshold=threshold,
                policy_version=policy.policy_version,
                outcome=PolicyOutcome.COMPLIANT,
                message="Travel service dates fall within the policy effective window.",
                exception_allowed=False,
            )
        return RuleEvidence(
            rule_id="policy.effective_window",
            actual=",".join(day.isoformat() for day in outside),
            threshold=threshold,
            policy_version=policy.policy_version,
            outcome=PolicyOutcome.INSUFFICIENT_EVIDENCE,
            message=(
                "Policy is not effective for travel service dates "
                f"{', '.join(day.isoformat() for day in outside)}."
            ),
            exception_allowed=False,
        )

    @staticmethod
    def _exception_outcome(rule_id: str, policy: PolicySnapshot) -> PolicyOutcome:
        if rule_id in policy.exception_allowed_rule_ids:
            return PolicyOutcome.REQUIRES_APPROVAL
        return PolicyOutcome.FORBIDDEN

    @staticmethod
    def _aggregate(outcomes: Iterable[PolicyOutcome]) -> PolicyOutcome:
        """按严重度优先聚合：证据不足 > 禁止 > 需审批 > 合规。"""
        values = set(outcomes)
        for outcome in (
            PolicyOutcome.INSUFFICIENT_EVIDENCE,
            PolicyOutcome.FORBIDDEN,
            PolicyOutcome.REQUIRES_APPROVAL,
        ):
            if outcome in values:
                return outcome
        return PolicyOutcome.COMPLIANT

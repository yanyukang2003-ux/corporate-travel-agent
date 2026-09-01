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


#: 这几条规则判不了时，**同一份判定里其余的"判过了"也不算数**。
#:
#: - `pricing.currency`：总价把 100 USD 和 500 CNY 直接相加得到 600，
#:   那不是任何一种货币下的价格。方案自身的数字不成立。
#: - `policy.effective_window`：这几天不归这一版政策管。那么同一份判定里的
#:   "舱位合规""夜费合规"是拿一份**不适用的政策**判出来的，同样不算数。
#:
#: 和"成都缺一条夜费上限"的区别就在这里：后者的缺口有名有姓、范围清楚，
#: 其余规则都在有效政策下真判过了，人补得上；这两条一破，整份判定都悬空。
EVIDENCE_INVALIDATING_RULE_IDS = frozenset({"pricing.currency", "policy.effective_window"})

#: "一条规则都没真正判过"这个理由的代号。审批材料里和真实 rule_id 区分得开。
NO_JUDGED_RULE = "policy.no_judged_rule"


def unreviewable_reasons(decision: PolicyDecision) -> tuple[str, ...]:
    """把这条方案摆给审批人看**也没用**的理由；空元组表示可以交给人定。

    "证据不足"现在是可选的一档：系统判不了，就把方案摆出来、请人来定
    （见 `ItineraryPlanner` 的分档与 `select_option`）。但要请人定，
    得先有**可定的东西**。两种情况没有：

    - **其余证据也跟着不成立。** 见 `EVIDENCE_INVALIDATING_RULE_IDS`：
      总价算不出来（混币种），或者这几天根本不归这版政策管。摆出来的
      "已经判过的规则"是假材料，审批人照着批等于什么都没批。
    - **一条规则都没真正判过。** 职级不在政策表里时，引擎直接返回，
      舱位、夜费一条都没查——审批人手里只有一句"什么都没判"，无从批起。

    共同点是"批的人看不出自己在批什么"。这和"成都缺一条夜费上限、其余都判过了"
    是两回事：后者的缺口有名有姓、范围清楚，正是人能补的那种。
    """
    reasons = [
        rule_id
        for rule_id in dict.fromkeys(decision.unjudged_rule_ids)
        if rule_id in EVIDENCE_INVALIDATING_RULE_IDS
    ]
    if not any(
        item.outcome is not PolicyOutcome.INSUFFICIENT_EVIDENCE
        for item in decision.evidence
    ):
        reasons.append(NO_JUDGED_RULE)
    return tuple(reasons)


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
                        # 夜费上限是今天唯一一条阈值本来就是数字的规则，所以它
                        # 额外给出带类型的值：**超出差标多少钱**由确定性代码算，
                        # 不让别人去 parse 上面那两个展示字符串。
                        # 币种到这里已经和政策一致——上面 `continue` 挡掉了不一致的。
                        actual_amount=hotel.nightly_price,
                        threshold_amount=cap,
                        amount_currency=policy.currency,
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

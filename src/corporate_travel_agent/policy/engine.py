"""政策引擎：以代码执行差旅合规判定，不依赖语言模型。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from corporate_travel_agent.domain.enums import PolicyOutcome, TransportMode
from corporate_travel_agent.domain.models import (
    BudgetSnapshot,
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
        *,
        now: datetime | None = None,
        budget: BudgetSnapshot | None = None,
        assess_budget: bool = False,
    ) -> PolicyDecision:
        """评估职级舱位、币种、酒店上限、生效窗口、提前预订、淡旺季与预算，聚合为 PolicyDecision。

        ``stays`` 收的是**整串住宿**：多城行程一站一处，每处各判自己那座城市的
        夜费上限。它仍接受单个酒店或 ``None``，既有调用方一个字都不用改。

        三条后加的规则各自有"没有暴露面就不判"的口径，**不判和判不了是两回事**：

        - 提前预订天数（``booking.advance_days``）需要 ``now``。调用方没给（只想看
          一张报价本身合不合规的诊断调用）就不产这条证据；政策没配这条规则也不产。
        - 淡旺季上限（``hotel.city.seasonal_cap``）在入住日落进某个窗口时**替代**基础上限。
        - 预算（``budget.cost_center.remaining``）判的是**整趟**的总价，逐张报价的调用
          谈不上它，所以要调用方用 ``assess_budget=True`` 明确要求。要求了却没有
          ``budget``（账本没接上）才是"判不了"。政策没给这位员工的成本中心配预算，
          同样不产证据。
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

        advance_evidence = self._advance_days_evidence(policy, transports, now)
        if advance_evidence is not None:
            evidence.append(advance_evidence)

        # 一站一条证据。规则名不变——同一个 rule_id 出现多次是既有做法
        # （每段交通各产一条 seat_class），聚合照旧取最差的一档。
        for hotel in hotels:
            if hotel.currency != policy.currency:
                continue
            season = policy.seasonal_cap_for(hotel.city, hotel.check_in)
            if season is not None:
                evidence.append(self._seasonal_cap_evidence(policy, hotel, season))
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

        if assess_budget:
            budget_evidence = self._budget_evidence(
                employee, policy, transports, hotels, budget
            )
            if budget_evidence is not None:
                evidence.append(budget_evidence)

        return PolicyDecision(self._aggregate(item.outcome for item in evidence), tuple(evidence))

    @classmethod
    def _advance_days_evidence(
        cls,
        policy: PolicySnapshot,
        transports: tuple[TransportOffer, ...],
        now: datetime | None,
    ) -> RuleEvidence | None:
        """至少提前几天订：按**出发地当地的日历日**数，不按 24 小时整段数。

        "8 月 1 日晚上订 8 月 4 日早上的票"是提前 3 天，哪怕不足 72 小时——
        差标写的是"天"，人也是这么理解的。
        """
        minimum = policy.min_advance_booking_days
        if minimum is None or now is None or not transports:
            return None
        first = min(transports, key=lambda item: item.depart_at)
        # 报价的出发时刻带着出发地时区（Provider 契约）；万一是裸时间就按 UTC 数，不猜。
        zone = first.depart_at.tzinfo or UTC
        booked_day = now.astimezone(zone).date()
        days = (first.depart_at.date() - booked_day).days
        rule_id = "booking.advance_days"
        compliant = days >= minimum
        outcome = PolicyOutcome.COMPLIANT if compliant else cls._exception_outcome(rule_id, policy)
        return RuleEvidence(
            rule_id=rule_id,
            actual=f"{days} days",
            threshold=f">= {minimum} days",
            policy_version=policy.policy_version,
            outcome=outcome,
            message=(
                f"{first.ref_id}: booked {days} days ahead, at least {minimum} required."
                if compliant
                else f"{first.ref_id}: booked only {days} days ahead; policy requires "
                f"at least {minimum}."
            ),
            exception_allowed=outcome is PolicyOutcome.REQUIRES_APPROVAL,
        )

    @classmethod
    def _seasonal_cap_evidence(
        cls, policy: PolicySnapshot, hotel: HotelOffer, season
    ) -> RuleEvidence:
        rule_id = "hotel.city.seasonal_cap"
        compliant = hotel.nightly_price <= season.nightly_cap
        outcome = PolicyOutcome.COMPLIANT if compliant else cls._exception_outcome(rule_id, policy)
        window = f"{season.season_from.isoformat()}..{season.season_to.isoformat()}"
        return RuleEvidence(
            rule_id=rule_id,
            actual=str(hotel.nightly_price),
            threshold=f"{season.nightly_cap} ({season.label}, {window})",
            policy_version=policy.policy_version,
            outcome=outcome,
            message=(
                f"{hotel.name}: nightly price {hotel.nightly_price} is within the "
                f"{season.label} cap {season.nightly_cap}."
                if compliant
                else f"{hotel.name}: nightly price {hotel.nightly_price} exceeds the "
                f"{season.label} cap {season.nightly_cap} ({window})."
            ),
            exception_allowed=outcome is PolicyOutcome.REQUIRES_APPROVAL,
            actual_amount=hotel.nightly_price,
            threshold_amount=season.nightly_cap,
            amount_currency=policy.currency,
        )

    @classmethod
    def _budget_evidence(
        cls,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        transports: tuple[TransportOffer, ...],
        hotels: tuple[HotelOffer, ...],
        budget: BudgetSnapshot | None,
    ) -> RuleEvidence | None:
        """整趟总价对成本中心剩余预算。

        只在政策给这位员工的成本中心配了预算时才判。配了却拿不到账本快照，是
        "判不了"，摆出来请人定——不是当作没超。
        """
        cost_center = employee.cost_center
        if cost_center is None:
            return None
        configured = policy.cost_center_budgets.get(cost_center)
        if configured is None:
            return None
        rule_id = "budget.cost_center.remaining"
        if budget is None or budget.cost_center != cost_center:
            return RuleEvidence(
                rule_id=rule_id,
                actual="budget ledger unavailable",
                threshold=f"{configured.amount} {configured.currency} per period",
                policy_version=policy.policy_version,
                outcome=PolicyOutcome.INSUFFICIENT_EVIDENCE,
                message=(
                    f"A budget is configured for cost center {cost_center} but no budget "
                    "snapshot was available at planning time."
                ),
                exception_allowed=False,
            )
        priced = (*transports, *hotels)
        if any(item.currency != budget.currency for item in priced):
            # 币种不一致已经由 pricing.currency 判成"判不了"，这里不再叠一条。
            return None
        total = sum((item.price for item in transports), Decimal("0"))
        total += sum((hotel.total_price for hotel in hotels), Decimal("0"))
        remaining = budget.remaining
        compliant = total <= remaining
        outcome = PolicyOutcome.COMPLIANT if compliant else cls._exception_outcome(rule_id, policy)
        return RuleEvidence(
            rule_id=rule_id,
            actual=str(total),
            threshold=f"{remaining} remaining of {budget.limit}",
            policy_version=policy.policy_version,
            outcome=outcome,
            message=(
                f"Trip total {total} fits the remaining budget {remaining} of "
                f"cost center {cost_center}."
                if compliant
                else f"Trip total {total} exceeds the remaining budget {remaining} of "
                f"cost center {cost_center} (limit {budget.limit}, spent {budget.spent})."
            ),
            exception_allowed=outcome is PolicyOutcome.REQUIRES_APPROVAL,
            actual_amount=total,
            threshold_amount=max(remaining, Decimal("0")),
            amount_currency=budget.currency,
        )

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

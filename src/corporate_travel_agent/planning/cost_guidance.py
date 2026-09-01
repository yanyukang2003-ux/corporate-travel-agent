"""把代价摊到选择那一刻：贵多少、超标多少、该谁批、换一条能省多少。

## 为什么这一层要存在

政策引擎已经算出了"这条需要审批"，但员工在选的时候看到的只是一个结论。
产品设计思路里那句话说得清楚：**让员工在做选择时就知道代价，比事后驳回有效得多，
也不招恨**。要做到这点，结论不够，得有数：

- 超出差标 **300**（不是"超标了"）；
- 需要 **M2001** 批（不是"需要审批"）；
- 换第二条 **晚走 2 小时、省 800**（不是让他自己在三张卡片之间做减法）。

## 三条硬规矩

1. **只在这一批已经摆出来的方案里比，不额外查库存。** 所以它一次工具调用都不花，
   也不会让"给个建议"变成一次新的供应商请求。代价是：建议的上限就是这几条方案，
   摆出来的方案里没有更便宜的，这里就一句建议都没有——那是**真的没有**，不是没算。
2. **省钱必须连着代价一起说。** 只说"省 800"不说"晚走两小时"是诱导，不是引导。
   所以 `Tradeoff` 里省多少和多花多少时间是同一个对象上的两个字段，读的人拆不开。
3. **不建议政策上更差的方案。** 一条更便宜但需要审批的方案不是"建议"，是麻烦：
   员工照着做会多走一轮审批。只在**政策档位不比当前差**的方案里找。

## 混币种直接不出提示

两条方案币种不同就没法说"省 800"——省的是 800 什么？这种情况一条建议都不给，
而不是把数字硬凑出来。和政策引擎在 `pricing.currency` 上的做法一致：
算不出来就说算不出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from corporate_travel_agent.domain.enums import PolicyOutcome
from corporate_travel_agent.domain.models import RuleEvidence, TravelOptionVersion

#: 政策档位：数字越小越好。用来判断"换过去会不会更麻烦"。
#: 顺序和规划器摆方案的分档一致：合规 < 需审批 < 判不了 < 禁止。
_POLICY_BANDS: dict[PolicyOutcome, int] = {
    PolicyOutcome.COMPLIANT: 0,
    PolicyOutcome.REQUIRES_APPROVAL: 1,
    PolicyOutcome.INSUFFICIENT_EVIDENCE: 2,
    PolicyOutcome.FORBIDDEN: 3,
}


@dataclass(frozen=True, slots=True)
class PolicyOverage:
    """一条规则超出了多少。只有阈值本来就是数字的规则才会出现在这里。"""

    rule_id: str
    #: 超出多少。单位见 `unit`——夜费上限是**每晚**，不是整趟。
    amount: Decimal
    unit: str
    currency: str
    #: 这条超标能不能走例外审批。不能的话它是硬禁止，摆出来也没用。
    exception_allowed: bool


@dataclass(frozen=True, slots=True)
class Tradeoff:
    """换成另一条方案会省多少、代价是什么。

    省钱和代价放在同一个对象上，是为了让读的人拆不开——"省 800"单独拿出去展示
    就是诱导。
    """

    option_id: str
    #: 省多少钱，恒为正数。不省钱的方案不会出现在建议里。
    saves: Decimal
    #: 出发晚多少分钟。正数是更晚走，负数是更早走。
    departure_delta_minutes: int
    #: 路上多花多少分钟。正数是更久。
    duration_delta_minutes: int
    currency: str


@dataclass(frozen=True, slots=True)
class CostGuidance:
    """一条方案在"选它要付出什么"这件事上的全部确定性事实。"""

    option_id: str
    currency: str
    #: 比这批方案里最便宜的**合规**方案贵多少。
    #: 这批里没有合规方案，或者币种对不上时是 `None`——没有基准就没有溢价可言。
    premium_over_cheapest_compliant: Decimal | None
    cheapest_compliant_option_id: str | None
    #: 逐条超标金额。空元组表示没有数值型超标，**不表示没有超标**——
    #: 舱位超标没有数值阈值，它只会出现在政策证据里。
    policy_overages: tuple[PolicyOverage, ...]
    #: 选它要谁批。只有这条方案需要审批时才有值。
    approver_id: str | None
    #: 换哪条能更省，以及代价。按省得最多排前面。
    tradeoffs: tuple[Tradeoff, ...]


def build_cost_guidance(
    options: Sequence[TravelOptionVersion],
    *,
    approver_id: str | None,
    max_tradeoffs: int = 2,
) -> tuple[CostGuidance, ...]:
    """按 `options` 的顺序算出每条方案的代价说明。

    `approver_id` 是这位员工的审批人（今天就是直属经理）。传 `None` 表示不知道
    该谁批——那就不写，而不是编一个。
    """
    if not options:
        return ()

    currencies = {item.currency for item in options}
    # 币种不一致时，跨方案的减法一律不做：溢价和建议都算不出来。
    # 逐条方案自己的超标金额不受影响——那是方案内部和政策比，没有跨方案相加。
    comparable = len(currencies) == 1
    currency = next(iter(currencies)) if comparable else ""

    cheapest_compliant = _cheapest_compliant(options) if comparable else None

    return tuple(
        CostGuidance(
            option_id=option.option_id,
            currency=option.currency,
            premium_over_cheapest_compliant=(
                None
                if cheapest_compliant is None
                else max(option.total_cost - cheapest_compliant.total_cost, Decimal("0"))
            ),
            cheapest_compliant_option_id=(
                None if cheapest_compliant is None else cheapest_compliant.option_id
            ),
            policy_overages=_policy_overages(option),
            approver_id=(
                approver_id
                if option.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
                else None
            ),
            tradeoffs=(
                _tradeoffs(option, options, currency, max_tradeoffs)
                if comparable
                else ()
            ),
        )
        for option in options
    )


def _cheapest_compliant(
    options: Sequence[TravelOptionVersion],
) -> TravelOptionVersion | None:
    """这批里最便宜的**完全合规**方案；没有就是 None。

    基准只认 `COMPLIANT`。拿一条"需审批"的方案当基准，算出来的溢价会把
    "你已经超标了"这件事藏起来。
    """
    compliant = [
        item
        for item in options
        if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    ]
    if not compliant:
        return None
    # 同价时取 option_id 小的那个，保证同一批方案每次算出来的基准一样。
    return min(compliant, key=lambda item: (item.total_cost, item.option_id))


def _policy_overages(option: TravelOptionVersion) -> tuple[PolicyOverage, ...]:
    """逐条读证据，把有数值阈值且真的超了的挑出来。"""
    return tuple(
        overage
        for evidence in option.policy_decision.evidence
        if (overage := _overage_from(evidence)) is not None
    )


def _overage_from(evidence: RuleEvidence) -> PolicyOverage | None:
    amount = evidence.overage_amount
    if amount is None or amount <= 0 or evidence.amount_currency is None:
        return None
    return PolicyOverage(
        rule_id=evidence.rule_id,
        amount=amount,
        # 今天只有夜费上限一条数值规则，它判的是每晚。多一条规则就在这里多一个分支，
        # 而不是让读的人默认所有金额都是同一个单位。
        unit="per_night" if evidence.rule_id == "hotel.city.nightly_cap" else "total",
        currency=evidence.amount_currency,
        exception_allowed=evidence.exception_allowed,
    )


def _tradeoffs(
    option: TravelOptionVersion,
    options: Sequence[TravelOptionVersion],
    currency: str,
    limit: int,
) -> tuple[Tradeoff, ...]:
    """在同一批方案里找"更省而且不更麻烦"的换法。"""
    band = _POLICY_BANDS[option.policy_decision.outcome]
    candidates: list[Tradeoff] = []
    for other in options:
        if other.option_id == option.option_id:
            continue
        saves = option.total_cost - other.total_cost
        if saves <= 0:
            continue
        if _POLICY_BANDS[other.policy_decision.outcome] > band:
            # 更便宜但政策上更差：换过去要多走一轮审批，这不是省，是麻烦。
            continue
        candidates.append(
            Tradeoff(
                option_id=other.option_id,
                saves=saves,
                departure_delta_minutes=_departure_delta(option, other),
                duration_delta_minutes=(
                    other.total_duration_minutes - option.total_duration_minutes
                ),
                currency=currency,
            )
        )
    candidates.sort(key=lambda item: (-item.saves, item.option_id))
    return tuple(candidates[:limit])


def _departure_delta(
    option: TravelOptionVersion, other: TravelOptionVersion
) -> int:
    """换过去之后第一段出发晚了多少分钟。

    比的是**第一段**：员工感觉得到的"要几点出门"就是这一段。多城行程后面几段
    的时刻由前面决定，拿来比没有意义。任一方没有航段时按 0 处理——
    说不出差别，就不要编一个差别出来。
    """
    if not option.legs or not other.legs:
        return 0
    delta = other.legs[0].depart_at - option.legs[0].depart_at
    return round(delta.total_seconds() / 60)

"""把"这条政策我判不了"翻成一句旅行者读得懂的话。

分开成一个模块，是因为它是**呈现**，不是判定：`PolicyEngine` 只负责得出
`INSUFFICIENT_EVIDENCE` 和它的规则证据，怎么把这件事说给人听是另一层的事。

写这一层的直接原因是 §41.3（五）：用户看到过
`no hotel nightly cap is configured for cities: Chengdu (hotel.city.nightly_cap
→ INSUFFICIENT_EVIDENCE)` 这种给运维看的英文内部串。`RuleEvidence.message`
是给日志和审计看的，不是给旅行者看的。
"""

from __future__ import annotations

from collections.abc import Iterable

from corporate_travel_agent.domain.enums import PolicyOutcome
from corporate_travel_agent.domain.models import PolicyDecision, RuleEvidence


def _joined(values: tuple[str, ...]) -> str:
    """城市名、职级、货币码这些值现在是英文的（`Shanghai`、`L3`、`CNY`）。

    两边留空格，中文句子里才不会挤成"还没有Shanghai的"。
    """
    return " " + "、".join(values) + " "


def _cap_sentence(values: tuple[str, ...]) -> str:
    return (
        f"公司政策里还没有{_joined(values)}的酒店夜费上限，"
        "这几晚是否超标我判不了——票和房都是真的，只是缺一条公司自己定的数字。"
    )


def _level_sentence(values: tuple[str, ...]) -> str:
    return (
        f"公司政策里没有{_joined(values)}这个职级的差旅规定，"
        "舱位和住宿标准我判不了。"
    )


def _currency_sentence(values: tuple[str, ...]) -> str:
    return (
        f"这条方案里混了{_joined(values)}几种货币，"
        "没有经过批准的汇率快照，我没法把它们合并起来和上限核对。"
    )


def _window_sentence(values: tuple[str, ...]) -> str:
    return (
        f"当前政策版本在{_joined(values)}这几天不生效，"
        "这段时间该按哪一版判，我这边没有依据。"
    )


#: 每个会产生"证据不足"的规则各配一句话。**加规则就要加文案**，
#: `test_policy_gaps` 会逐个 rule_id 检查这张表有没有漏。
_SENTENCES = {
    "hotel.city.nightly_cap": _cap_sentence,
    "employee.level.known": _level_sentence,
    "pricing.currency": _currency_sentence,
    "policy.effective_window": _window_sentence,
}


def unjudged_gap_sentences(decision: PolicyDecision) -> tuple[str, ...]:
    """这条方案里"判不了"的部分，一条规则一句话。

    同一条规则会出现多次（多城行程一站一条夜费上限证据），所以按 rule_id 归并，
    把各自的实际值收成一句，而不是同一句话重复三遍。
    """
    return _sentences_for(
        item
        for item in decision.evidence
        if item.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE
    )


def _sentences_for(evidence: Iterable[RuleEvidence]) -> tuple[str, ...]:
    grouped: dict[str, list[str]] = {}
    for item in evidence:
        values = grouped.setdefault(item.rule_id, [])
        if item.actual not in values:
            values.append(item.actual)
    return tuple(
        _SENTENCES.get(rule_id, _fallback(rule_id))(tuple(values))
        for rule_id, values in grouped.items()
    )


def _fallback(rule_id: str):
    """没配文案的规则也要说人话，不能把 rule_id 直接甩给用户。"""

    def sentence(values: tuple[str, ...]) -> str:
        detail = "、".join(values)
        return f"这条方案有一项公司标准我查不到（{rule_id}：{detail}），因此判不了。"

    return sentence

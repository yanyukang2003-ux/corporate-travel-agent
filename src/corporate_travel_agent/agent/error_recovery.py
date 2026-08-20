"""error_recovery：错误恢复分类（SideEffectClass + RecoveryAction）。

原则（与 ProviderError 文档及 Claude Code 恢复环对齐）：

1. 先判断失败尝试是否可能产生副作用。
2. 仅当上次尝试已知无副作用（或任务本地且可同参重发）时才自动重试。
3. 多段搜索部分成功时，先 recon 已成功腿，再决定 replan / revalidate / safe-degrade。
4. 外部写路径（未来 Order/Payment）须先 recon 远端状态，禁止盲目重建。

本模块不执行恢复，只分类并记录决策，供 Orchestrator、轨迹与评测共用词汇。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class SideEffectClass(StrEnum):
    """失败尝试可能对外部世界造成的副作用程度。"""

    NONE = "none"
    """无可观察写入：纯传输失败、无响应、尚未改任务。"""

    TASK_LOCAL = "task_local"
    """仅内存/任务库变更（槽位、方案、工具记录）。"""

    EXTERNAL_READ = "external_read"
    """Provider 搜索/再校验/deep_link 类读（或类读）。适配器标为可重试且无副作用时可重发。"""

    EXTERNAL_WRITE_POSSIBLE = "external_write_possible"
    """可能已发生写入（发送后超时、STARTED 中断）。必须 recon。"""

    EXTERNAL_WRITE_CONFIRMED = "external_write_confirmed"
    """写入已知完成。应补偿/查询，禁止盲目重试创建。"""


class RecoveryAction(StrEnum):
    """分类后的下一步恢复动作（Orchestrator / 用户）。"""

    RETRY_SAME = "retry_same"
    """在有界次数内重发相同请求。"""

    RETRY_AFTER_RECON = "retry_after_recon"
    """先检查已完成腿/远端状态，再可能重试子集。"""

    CLARIFY = "clarify"
    """询问用户；不编造参数、不重打 Provider。"""

    REPLAN = "replan"
    """用户或系统改请求后重新搜索。"""

    REVALIDATE = "revalidate"
    """库存可能过期；在 Booking Intent 前再校验。"""

    SAFE_DEGRADE = "safe_degrade"
    """以明确失败状态与 next_action 收束，不假装成功。"""

    ABORT = "abort"
    """硬停止（预算耗尽等真正终态）。"""


# 当前 V1 面上的外部读工具名。
_PROVIDER_READ_TOOLS = frozenset(
    {
        "provider.search_transport.outbound",
        "provider.search_transport.inbound",
        "provider.search_hotels",
        "provider.revalidate",
        "provider.create_deep_link",
    }
)

# 未来写工具——在具备远端 recon 前一律按 write_possible 分类。
_PROVIDER_WRITE_TOOLS = frozenset(
    {
        "provider.create_order",
        "provider.create_payment",
        "provider.cancel_order",
    }
)

_SEARCH_LEG_TOOLS = (
    "provider.search_transport.outbound",
    "provider.search_transport.inbound",
    "provider.search_hotels",
)


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """挂到工具失败与任务元数据上的脱敏恢复决策。"""

    side_effect_class: SideEffectClass
    recovery_action: RecoveryAction
    retryable: bool
    reason: str
    successful_legs: tuple[str, ...] = ()
    failed_leg: str | None = None
    will_auto_retry: bool = False

    def as_dict(self) -> dict[str, Any]:
        """序列化为可落库字典。"""
        return asdict(self)


def classify_tool_failure(
    *,
    tool_name: str,
    tool_kind: str,
    retryable: bool,
    response_received: bool | None,
    attempt: int,
    max_attempts: int,
    will_auto_retry: bool,
    interrupted: bool = False,
) -> RecoveryDecision:
    """对一次失败的工具调用做分类，供轨迹与恢复策略使用。"""

    if interrupted or (
        tool_name in _PROVIDER_WRITE_TOOLS and response_received is not True
    ):
        side = (
            SideEffectClass.EXTERNAL_WRITE_CONFIRMED
            if response_received is True and tool_name in _PROVIDER_WRITE_TOOLS
            else SideEffectClass.EXTERNAL_WRITE_POSSIBLE
        )
        return RecoveryDecision(
            side_effect_class=side,
            recovery_action=RecoveryAction.RETRY_AFTER_RECON,
            retryable=False,
            reason=(
                "interrupted_or_write_uncertain: recon remote/task state before any retry"
            ),
            failed_leg=tool_name if tool_name in _PROVIDER_READ_TOOLS else tool_name,
            will_auto_retry=False,
        )

    if tool_name in _PROVIDER_WRITE_TOOLS and response_received is True:
        return RecoveryDecision(
            side_effect_class=SideEffectClass.EXTERNAL_WRITE_CONFIRMED,
            recovery_action=RecoveryAction.RETRY_AFTER_RECON,
            retryable=False,
            reason="external_write_confirmed: query/compensate; do not re-create",
            failed_leg=tool_name,
            will_auto_retry=False,
        )

    if tool_kind == "LLM":
        if will_auto_retry and retryable:
            return RecoveryDecision(
                side_effect_class=SideEffectClass.NONE,
                recovery_action=RecoveryAction.RETRY_SAME,
                retryable=True,
                reason="llm_transport_transient: same extract is side-effect free",
                will_auto_retry=True,
            )
        # Exhausted or non-retryable: conversation can continue (clarify / re-chat).
        return RecoveryDecision(
            side_effect_class=SideEffectClass.TASK_LOCAL,
            recovery_action=RecoveryAction.CLARIFY,
            retryable=False,
            reason=(
                "llm_failed_after_policy: prefer clarify/re-chat over silent invent; "
                f"attempt={attempt}/{max_attempts}"
            ),
            will_auto_retry=False,
        )

    if tool_name in _PROVIDER_READ_TOOLS or tool_kind == "PROVIDER":
        if will_auto_retry and retryable:
            return RecoveryDecision(
                side_effect_class=SideEffectClass.EXTERNAL_READ,
                recovery_action=RecoveryAction.RETRY_SAME,
                retryable=True,
                reason="provider_transient_read: identical request is safe to re-issue",
                failed_leg=tool_name,
                will_auto_retry=True,
            )
        return RecoveryDecision(
            side_effect_class=SideEffectClass.EXTERNAL_READ,
            recovery_action=RecoveryAction.SAFE_DEGRADE,
            retryable=False,
            reason=(
                "provider_failed_closed: no empty-inventory reinterpretation; "
                f"attempt={attempt}/{max_attempts}"
            ),
            failed_leg=tool_name,
            will_auto_retry=False,
        )

    return RecoveryDecision(
        side_effect_class=SideEffectClass.TASK_LOCAL,
        recovery_action=RecoveryAction.SAFE_DEGRADE,
        retryable=bool(retryable),
        reason=f"unclassified_tool_failure:{tool_name}",
        failed_leg=tool_name,
        will_auto_retry=will_auto_retry,
    )


def recon_search_legs(tool_calls: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    """查看本任务多段搜索中哪些腿已成功。

    后续腿失败时避免把整次搜索当成空操作，也不为缺失腿编造库存。
    """

    succeeded: list[str] = []
    failed: list[str] = []
    started: list[str] = []
    for record in tool_calls:
        name = getattr(record, "tool_name", None)
        if name not in _SEARCH_LEG_TOOLS:
            continue
        status = getattr(record, "status", None)
        status_value = status.value if hasattr(status, "value") else str(status)
        if status_value == "SUCCEEDED":
            if name not in succeeded:
                succeeded.append(name)
        elif status_value == "FAILED":
            if name not in failed:
                failed.append(name)
        elif status_value == "STARTED":
            if name not in started:
                started.append(name)

    partial = bool(succeeded) and bool(failed or started)
    if started:
        action = RecoveryAction.RETRY_AFTER_RECON
        side = SideEffectClass.EXTERNAL_WRITE_POSSIBLE
        reason = "search_interrupted_mid_leg: mark failed then replan or recon"
    elif partial:
        action = RecoveryAction.RETRY_AFTER_RECON
        side = SideEffectClass.TASK_LOCAL
        reason = (
            "partial_search_success: keep audit of succeeded legs; do not claim "
            "full inventory; user replan or retry failed legs only"
        )
    elif failed and not succeeded:
        action = RecoveryAction.SAFE_DEGRADE
        side = SideEffectClass.EXTERNAL_READ
        reason = "search_failed_before_any_leg_success"
    else:
        action = RecoveryAction.REPLAN
        side = SideEffectClass.NONE
        reason = "no_search_leg_activity"

    decision = RecoveryDecision(
        side_effect_class=side,
        recovery_action=action,
        retryable=False,
        reason=reason,
        successful_legs=tuple(succeeded),
        failed_leg=failed[-1] if failed else (started[-1] if started else None),
        will_auto_retry=False,
    )
    return {
        "successful_legs": list(succeeded),
        "failed_legs": list(failed),
        "started_legs": list(started),
        "partial": partial,
        "recovery": decision.as_dict(),
    }


def decision_from_mapping(payload: Mapping[str, Any] | None) -> RecoveryDecision | None:
    """从字典还原 RecoveryDecision；字段非法则返回 None。"""
    if not payload:
        return None
    try:
        return RecoveryDecision(
            side_effect_class=SideEffectClass(str(payload["side_effect_class"])),
            recovery_action=RecoveryAction(str(payload["recovery_action"])),
            retryable=bool(payload.get("retryable", False)),
            reason=str(payload.get("reason") or ""),
            successful_legs=tuple(payload.get("successful_legs") or ()),
            failed_leg=payload.get("failed_leg"),
            will_auto_retry=bool(payload.get("will_auto_retry", False)),
        )
    except (KeyError, TypeError, ValueError):
        return None

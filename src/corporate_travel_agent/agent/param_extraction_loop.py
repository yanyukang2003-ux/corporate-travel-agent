"""param_extraction_loop：Claude Code 风格的参数抽取环，落地到行程意图。

对齐 Anthropic Claude Code（``toolExecution.ts``）标准环：

  模型产出参数
    → L1 Schema 校验（Zod ``safeParse`` 类比）
    → L2 业务校验（``tool.validateInput`` 类比）
    → 失败：结构化 ``tool_use_error`` 反馈（禁止静默编造）
    → 成功：允许副作用（搜索/规划）

领域防编造（超出 Claude Code）：

  - 仅 ``provided_fields`` 可写槽位（白名单合并在外层）。
  - 城市（origin/destination）从不由 LLM 修复，只走用户澄清。
  - 日期/酒店槽在用户文本可支撑时，最多 ``MAX_INTENT_REPAIR_ATTEMPTS`` 次模型修复，否则澄清。
  - 确定性 P0 默认（白名单推导）在 L2 之前运行且不编造城市——见 ``intent_calibration.apply_safe_defaults``。

错误文案形状对齐 Claude Code ``formatZodValidationError``，便于修复提示与审计。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from corporate_travel_agent.agent.intent_calibration import (
    MAX_INTENT_REPAIR_ATTEMPTS,
    search_ready_missing,
)
from corporate_travel_agent.domain.validation import validate_trip_request_values

# Claude 风格错误串与审计中的工具名。
INTENT_EXTRACT_TOOL_NAME = "llm.extract_trip_intent"

# 对齐 Claude Code ValidationResult.errorCode 风格的错误码。
ERROR_OK = 0
ERROR_MISSING_PARAM = 1
ERROR_TYPE_MISMATCH = 2
ERROR_BUSINESS_RULE = 3
ERROR_CONFLICT = 4
ERROR_ANTI_FABRICATION = 5

IssueCode = Literal[
    "missing_param",
    "type_mismatch",
    "business_rule",
    "conflict",
    "anti_fabrication",
]

ValidationLayer = Literal["schema", "business"]


class LoopAction(StrEnum):
    """校验后的下一步（Claude：重提示工具 vs 继续执行）。"""

    ACCEPT = "accept"  # 参数有效 → 执行搜索/规划
    REPAIR = "repair"  # 把 tool_use_error 反馈给模型（有界）
    CLARIFY = "clarify"  # 问用户；不编造
    OUT_OF_SCOPE = "out_of_scope"


# REPAIR 时模型可重填的槽（永不含城市）。
REPAIRABLE_SLOTS: frozenset[str] = frozenset(
    {
        "departure_after",
        "arrive_by",
        "return_after",
        "return_before",
        "hotel_check_in",
        "hotel_check_out",
    }
)

CITY_SLOTS: frozenset[str] = frozenset({"origin", "destination"})

_SLOT_TYPES: dict[str, type | tuple[type, ...]] = {
    "origin": str,
    "destination": str,
    "departure_after": datetime,
    "arrive_by": datetime,
    "return_after": datetime,
    "return_before": datetime,
    "hotel_check_in": date,
    "hotel_check_out": date,
    "client_location": str,
    "hard_constraints": (list, tuple),
    "soft_preferences": (list, tuple),
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条离散参数问题（Claude Zod issue 类比）。"""

    code: IssueCode
    message: str
    param: str | None = None
    expected: str | None = None
    received: str | None = None
    error_code: int = ERROR_BUSINESS_RULE

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "param": self.param,
            "expected": self.expected,
            "received": self.received,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """L1 或 L2 校验结果。

    ``ok=True`` 表示本层通过。Claude Code 失败时早退并带 tool_result ``is_error``；
    这里对应 ``format_tool_use_error`` + ``LoopAction``。

    ``conflicts`` 仅含阻断项；软性模型备注进 ``soft_conflicts``，不导致本层失败。
    """

    ok: bool
    layer: ValidationLayer
    issues: tuple[ValidationIssue, ...] = ()
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    soft_conflicts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "layer": self.layer,
            "issues": [item.as_dict() for item in self.issues],
            "missing": list(self.missing),
            "conflicts": list(self.conflicts),
            "soft_conflicts": list(self.soft_conflicts),
        }


@dataclass
class ParamLoopDecision:
    """L1+L2（及可选修复预算）后的完整决策。"""

    action: LoopAction
    schema: ValidationResult
    business: ValidationResult
    missing: tuple[str, ...]
    conflicts: tuple[str, ...]
    repairable_missing: frozenset[str]
    tool_use_error: str | None = None
    notes: tuple[str, ...] = ()
    soft_conflicts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "schema": self.schema.as_dict(),
            "business": self.business.as_dict(),
            "missing": list(self.missing),
            "conflicts": list(self.conflicts),
            "soft_conflicts": list(self.soft_conflicts),
            "repairable_missing": sorted(self.repairable_missing),
            "tool_use_error": self.tool_use_error,
            "notes": list(self.notes),
        }


@dataclass
class ParamLoopTrace:
    """跨抽取/修复轮次的 Claude 风格环审计轨迹。"""

    rounds: list[dict[str, Any]] = field(default_factory=list)
    final_action: str | None = None
    final_tool_use_error: str | None = None

    def record_round(
        self,
        *,
        extract_index: int,
        decision: ParamLoopDecision,
        rejected_fields: list[str] | None = None,
    ) -> None:
        self.rounds.append(
            {
                "extract_index": extract_index,
                "decision": decision.as_dict(),
                "rejected_fields": list(rejected_fields or ()),
            }
        )
        self.final_action = decision.action.value
        self.final_tool_use_error = decision.tool_use_error

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern": "claude_code_param_loop_v1",
            "rounds": list(self.rounds),
            "final_action": self.final_action,
            "final_tool_use_error": self.final_tool_use_error,
            "max_repair_attempts": MAX_INTENT_REPAIR_ATTEMPTS,
        }


# ---------------------------------------------------------------------------
# L1 — Schema/类型校验（Zod safeParse 类比）
# ---------------------------------------------------------------------------


def validate_schema_l1(
    fields: dict[str, Any],
    *,
    classification: str,
) -> ValidationResult:
    """仅做类型与形状检查，不编造值。

    Claude Code: ``tool.inputSchema.safeParse(input)``。
    """
    if classification == "OUT_OF_SCOPE":
        return ValidationResult(ok=True, layer="schema")

    issues: list[ValidationIssue] = []
    missing: list[str] = []
    conflicts: list[str] = []

    for name, expected_type in _SLOT_TYPES.items():
        value = fields.get(name)
        if value is None or value == "":
            continue
        # 酒店日期：datetime 是 date 子类——拒绝裸 datetime
        if name in {"hotel_check_in", "hotel_check_out"}:
            if not isinstance(value, date) or isinstance(value, datetime):
                issues.append(
                    ValidationIssue(
                        code="type_mismatch",
                        param=name,
                        expected="date",
                        received=_type_name(value),
                        message=(
                            f"The parameter `{name}` type is expected as `date` "
                            f"but provided as `{_type_name(value)}`"
                        ),
                        error_code=ERROR_TYPE_MISMATCH,
                    )
                )
                conflicts.append(f"{name} must be a date")
            continue
        if name in {"departure_after", "arrive_by", "return_after", "return_before"}:
            if not isinstance(value, datetime):
                issues.append(
                    ValidationIssue(
                        code="type_mismatch",
                        param=name,
                        expected="datetime",
                        received=_type_name(value),
                        message=(
                            f"The parameter `{name}` type is expected as `datetime` "
                            f"but provided as `{_type_name(value)}`"
                        ),
                        error_code=ERROR_TYPE_MISMATCH,
                    )
                )
                conflicts.append(f"{name} must be a datetime")
            elif value.tzinfo is None or value.utcoffset() is None:
                issues.append(
                    ValidationIssue(
                        code="type_mismatch",
                        param=name,
                        expected="timezone-aware datetime",
                        received="naive datetime",
                        message=(
                            f"The parameter `{name}` type is expected as "
                            "`timezone-aware datetime` but provided as `naive datetime`"
                        ),
                        error_code=ERROR_TYPE_MISMATCH,
                    )
                )
                conflicts.append(f"{name} must include a timezone")
            continue
        if name in {"origin", "destination"}:
            if not isinstance(value, str):
                issues.append(
                    ValidationIssue(
                        code="type_mismatch",
                        param=name,
                        expected="string",
                        received=_type_name(value),
                        message=(
                            f"The parameter `{name}` type is expected as `string` "
                            f"but provided as `{_type_name(value)}`"
                        ),
                        error_code=ERROR_TYPE_MISMATCH,
                    )
                )
                conflicts.append(f"{name} must be a string")
            continue
        if not isinstance(value, expected_type):
            expected_label = "list" if expected_type == (list, tuple) else str(expected_type)
            issues.append(
                ValidationIssue(
                    code="type_mismatch",
                    param=name,
                    expected=expected_label,
                    received=_type_name(value),
                    message=(
                        f"The parameter `{name}` type is expected as `{expected_label}` "
                        f"but provided as `{_type_name(value)}`"
                    ),
                    error_code=ERROR_TYPE_MISMATCH,
                )
            )
            conflicts.append(f"{name} has invalid type")

    ok = not issues
    return ValidationResult(
        ok=ok,
        layer="schema",
        issues=tuple(issues),
        missing=tuple(missing),
        conflicts=tuple(dict.fromkeys(conflicts)),
    )


# ---------------------------------------------------------------------------
# 冲突分级（阻断 vs 非阻断）
# ---------------------------------------------------------------------------

# 结构性领域失败一律阻断搜索/修复。
_BLOCKING_CONFLICT_RE = re.compile(
    r"(must be different|must be later|must include a timezone|must be a datetime|"
    r"must be a date|must be a string|unsupported hard constraint|"
    r"unsupported soft preference|train_only and flight_only|invalid type|"
    r"origin and destination)",
    re.I,
)

# 模型自由文本中的元说明/输出策略/他处已处理项——不因此冻结工作流（记为软假设）。
_SOFT_CONFLICT_RE = re.compile(
    r"(partial itinerar|incomplete itinerar|残缺|"
    r"fulfillment|output constraint|suppress partial|"
    r"exact arrival|exactly \d|not a supported constraint|"
    r"outside the supported|outside of the supported|outside supported|"
    r"cannot be represented within this single-trip|"
    r"separate concurrent route|cross-timezone instant|instant comparison|"
    r"compare instants|captured as arrive_before_meeting|"
    r"60-minute early|one-hour-early|提前.?60|"
    r"not support(ed)? (by )?this (single-trip|intent|schema|model)|"
    r"out[_ ]of[_ ]scope|outside the corporate travel|"
    r"restaurant|weather|日料|下雨|订位|"
    r"unrelated local|sightseeing)",
    re.I,
)


def is_blocking_conflict(text: str) -> bool:
    """冲突是否必须阻断 ACCEPT（澄清或硬失败）。

    领域结构问题一律阻断。关于不支持输出策略、精确到达评论、多路线元措辞等，
    在行程槽位本身已完整时可降为软冲突。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    from corporate_travel_agent.agent.capability_boundaries import is_capability_conflict

    if is_capability_conflict(raw):
        return True
    if _BLOCKING_CONFLICT_RE.search(raw):
        return True
    if _SOFT_CONFLICT_RE.search(raw):
        return False
    # 无结构标记的「unsupported / outside」自由文本 → 软。
    lowered = raw.lower()
    if any(
        token in lowered
        for token in (
            "unsupported",
            "not supported",
            "outside the",
            "outside of",
            "cannot be represented",
            "not a supported",
        )
    ):
        return False
    # 保守默认：未知模型文本阻断（优于静默接受）。
    return True


def partition_conflicts(
    conflicts: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """将冲突拆成 (阻断, 软/非阻断)。"""
    blocking: list[str] = []
    soft: list[str] = []
    for item in conflicts:
        text = str(item).strip()
        if not text:
            continue
        if is_blocking_conflict(text):
            blocking.append(text)
        else:
            soft.append(text)
    return tuple(dict.fromkeys(blocking)), tuple(dict.fromkeys(soft))


# ---------------------------------------------------------------------------
# L2 — 业务 validateInput（Claude tool.validateInput 类比）
# ---------------------------------------------------------------------------


def validate_business_l2(
    fields: dict[str, Any],
    *,
    classification: str,
    model_conflicts: tuple[str, ...] = (),
) -> ValidationResult:
    """语义完备性与行程领域规则；从不填槽。

    Claude Code: ``await tool.validateInput?.(parsedInput.data, context)``。
    组合领域 ``validate_trip_request_values`` + SearchReady 清单。

    模型 ``conflicts`` 分级：仅阻断项使本层失败；软项进 ``soft_conflicts`` 记假设。
    """
    if classification == "OUT_OF_SCOPE":
        return ValidationResult(ok=True, layer="business")

    issues: list[ValidationIssue] = []
    trip = validate_trip_request_values(fields)
    ready = search_ready_missing(fields, classification=classification)

    missing = list(dict.fromkeys((*trip.missing, *ready.missing)))
    missing = list(dict.fromkeys(missing))

    # 领域行程冲突一律结构性 → 阻断（永不降为软）。
    model_blocking, model_soft = partition_conflicts(model_conflicts)
    blocking = tuple(dict.fromkeys((*trip.conflicts, *model_blocking)))
    soft = model_soft

    for name in missing:
        issues.append(
            ValidationIssue(
                code="missing_param",
                param=name,
                message=f"The required parameter `{name}` is missing",
                expected="non-null grounded value",
                received="null",
                error_code=ERROR_MISSING_PARAM,
            )
        )

    for conflict in blocking:
        param = _guess_conflict_param(conflict)
        issues.append(
            ValidationIssue(
                code="conflict",
                param=param,
                message=conflict,
                error_code=ERROR_CONFLICT,
            )
        )

    for soft_item in soft:
        issues.append(
            ValidationIssue(
                code="business_rule",
                message=f"non_blocking_conflict: {soft_item}",
                error_code=ERROR_BUSINESS_RULE,
            )
        )

    for note in ready.notes:
        issues.append(
            ValidationIssue(
                code="business_rule",
                message=note,
                error_code=ERROR_BUSINESS_RULE,
            )
        )

    # 仅有软冲突不使本层失败。
    hard_fail = bool(missing or blocking)
    return ValidationResult(
        ok=not hard_fail,
        layer="business",
        issues=tuple(issues),
        missing=tuple(missing),
        conflicts=tuple(blocking),
        soft_conflicts=tuple(soft),
    )


# ---------------------------------------------------------------------------
# 决策 + Claude 风格错误格式化
# ---------------------------------------------------------------------------


def format_tool_use_error(
    tool_name: str,
    result: ValidationResult,
    *,
    extra_issues: tuple[ValidationIssue, ...] = (),
) -> str:
    """人类可读错误，对齐 Claude Code ``formatZodValidationError``。

    示例::

        llm.extract_trip_intent failed due to the following issues:
        The required parameter `origin` is missing
        The parameter `arrive_by` type is expected as `datetime` but provided as `str`
    """
    issues = list(result.issues) + list(extra_issues)
    if not issues:
        return f"{tool_name} failed due to validation error"

    # 优先 missing / type 类消息（Claude 顺序）。
    parts: list[str] = []
    for issue in issues:
        if (
            issue.code == "business_rule"
            and issue.param is None
            and not issue.message.startswith("The ")
        ):
            # 软备注不是给模型的主失败行。
            continue
        parts.append(issue.message)

    if not parts:
        parts = [issue.message for issue in issues]

    noun = "issues" if len(parts) > 1 else "issue"
    return f"{tool_name} failed due to the following {noun}:\n" + "\n".join(parts)


def format_combined_tool_use_error(
    tool_name: str,
    *results: ValidationResult,
) -> str:
    """合并 L1+L2 问题为一段 Claude 风格错误块。"""
    combined_issues: list[ValidationIssue] = []
    for result in results:
        if not result.ok:
            combined_issues.extend(result.issues)
    if not combined_issues:
        return f"{tool_name} failed due to validation error"
    synthetic = ValidationResult(
        ok=False,
        layer="business",
        issues=tuple(combined_issues),
    )
    return format_tool_use_error(tool_name, synthetic)


def decide_param_loop(
    fields: dict[str, Any],
    *,
    classification: str,
    model_conflicts: tuple[str, ...] = (),
    extract_index: int = 0,
    max_repair_attempts: int = MAX_INTENT_REPAIR_ATTEMPTS,
) -> ParamLoopDecision:
    """跑 L1 → L2，并选择 ACCEPT / REPAIR / CLARIFY / OUT_OF_SCOPE。

    修复预算：``extract_index`` 从 0 起；仅当 ``extract_index < max_repair_attempts``
    才允许 REPAIR（与历史 P1 上限一致）。
    """
    if classification == "OUT_OF_SCOPE":
        schema = ValidationResult(ok=True, layer="schema")
        business = ValidationResult(ok=True, layer="business")
        return ParamLoopDecision(
            action=LoopAction.OUT_OF_SCOPE,
            schema=schema,
            business=business,
            missing=(),
            conflicts=(),
            repairable_missing=frozenset(),
            notes=("classification OUT_OF_SCOPE",),
        )

    schema = validate_schema_l1(fields, classification=classification)
    business = validate_business_l2(
        fields,
        classification=classification,
        model_conflicts=model_conflicts,
    )

    missing = tuple(dict.fromkeys((*schema.missing, *business.missing)))
    # 仅阻断冲突（软冲突已在 L2 降级）。
    conflicts = tuple(dict.fromkeys((*schema.conflicts, *business.conflicts)))
    soft_conflicts = tuple(business.soft_conflicts)

    if schema.ok and business.ok:
        notes: tuple[str, ...] = ()
        if soft_conflicts:
            notes = ("soft_conflicts_demoted_non_blocking",)
        return ParamLoopDecision(
            action=LoopAction.ACCEPT,
            schema=schema,
            business=business,
            missing=(),
            conflicts=(),
            repairable_missing=frozenset(),
            soft_conflicts=soft_conflicts,
            notes=notes,
        )

    tool_use_error = format_combined_tool_use_error(INTENT_EXTRACT_TOOL_NAME, schema, business)

    # 阻断冲突与类型不匹配不静默改写——走澄清。
    if conflicts:
        return ParamLoopDecision(
            action=LoopAction.CLARIFY,
            schema=schema,
            business=business,
            missing=missing,
            conflicts=conflicts,
            repairable_missing=frozenset(),
            tool_use_error=tool_use_error,
            soft_conflicts=soft_conflicts,
            notes=("blocking_conflicts_require_clarification",),
        )

    city_missing = frozenset(name for name in missing if name in CITY_SLOTS)
    if city_missing:
        # 防编造：从不让 LLM 修复城市。
        return ParamLoopDecision(
            action=LoopAction.CLARIFY,
            schema=schema,
            business=business,
            missing=missing,
            conflicts=conflicts,
            repairable_missing=frozenset(),
            tool_use_error=tool_use_error,
            soft_conflicts=soft_conflicts,
            notes=("city_slots_require_user_clarification",),
        )

    repairable = frozenset(name for name in missing if name in REPAIRABLE_SLOTS)
    if repairable and extract_index < max_repair_attempts:
        return ParamLoopDecision(
            action=LoopAction.REPAIR,
            schema=schema,
            business=business,
            missing=missing,
            conflicts=conflicts,
            repairable_missing=repairable,
            tool_use_error=tool_use_error,
            soft_conflicts=soft_conflicts,
            notes=("claude_style_schema_retry",),
        )

    return ParamLoopDecision(
        action=LoopAction.CLARIFY,
        schema=schema,
        business=business,
        missing=missing,
        conflicts=conflicts,
        repairable_missing=repairable,
        tool_use_error=tool_use_error,
        soft_conflicts=soft_conflicts,
        notes=("repair_budget_exhausted_or_unrepairable",),
    )


def build_tool_use_error_repair_payload(
    decision: ParamLoopDecision,
    *,
    fields: dict[str, Any],
    user_message: str,
    classification: str,
) -> dict[str, Any]:
    """注入下一轮模型调用的载荷（Claude tool_result is_error）。

    模型只能填 ``repairable_missing``；城市与冲突在 REPAIR 构造时已排除。
    """
    from corporate_travel_agent.agent.intent_calibration import _json_safe

    return {
        "mode": "repair",
        "pattern": "claude_code_param_loop_v1",
        "is_error": True,
        "tool_name": INTENT_EXTRACT_TOOL_NAME,
        "tool_use_error": decision.tool_use_error
        or format_combined_tool_use_error(
            INTENT_EXTRACT_TOOL_NAME, decision.schema, decision.business
        ),
        "missing_fields": sorted(decision.repairable_missing),
        "all_missing": list(decision.missing),
        "conflicts": list(decision.conflicts),
        "issues": [
            item.as_dict()
            for item in (*decision.schema.issues, *decision.business.issues)
            if item.param in decision.repairable_missing
            or item.code in {"type_mismatch", "missing_param"}
        ],
        "current_fields": _json_safe(fields),
        "classification": classification,
        "user_message": user_message,
        "rules": [
            "This is a tool_use_error recovery turn (Claude Code pattern).",
            "ONLY fill fields listed in missing_fields.",
            "Every filled value MUST be grounded in the user_message or current_fields.",
            "If the user message does not support a missing field, leave it null "
            "and do not list it in provided_fields.",
            "Do NOT invent cities, dates, seat classes, prices, policy outcomes, "
            "or employee levels.",
            "Do NOT change fields that are already filled unless they are in missing_fields.",
            "Identity/level/approval chatter must not clear trip slots.",
        ],
    }


def repair_system_instructions_from_loop(repair: dict[str, Any]) -> str:
    """REPAIR 轮次的系统提示片段（含 Claude 风格错误块）。"""
    import json

    error_block = repair.get("tool_use_error") or ""
    missing = ", ".join(repair.get("missing_fields") or [])
    return (
        "REPAIR MODE (Claude Code parameter loop): The previous extract_trip_intent "
        "call failed validation. Treat the following as a tool_use_error result and "
        "correct ONLY the listed parameters.\n\n"
        f"<tool_use_error>\n{error_block}\n</tool_use_error>\n\n"
        f"Fill ONLY these missing_fields when clearly grounded: [{missing}]. "
        "If unsupported, leave null — never invent. "
        "Do not invent inventory, prices, approvals, or policy outcomes. "
        "provided_fields must list only fields you are newly grounding in this repair. "
        "Repair context JSON: "
        + json.dumps(repair, ensure_ascii=False, default=str, sort_keys=True)
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    return type(value).__name__


def _guess_conflict_param(conflict: str) -> str | None:
    for name in (
        "origin",
        "destination",
        "departure_after",
        "arrive_by",
        "return_after",
        "return_before",
        "hotel_check_in",
        "hotel_check_out",
        "hard_constraints",
        "soft_preferences",
    ):
        if name in conflict:
            return name
    return None

"""capability_contract：行程搜索的能力完备性契约（L2，类 Claude/grok-build）。

Anthropic Claude Code 与 grok-build **不**枚举用户措辞，而是：

    模型填工具参数（任意措辞 → 结构化槽位）
      → L1 Schema 校验
      → L2 业务/能力校验
      → 不完整：结构化错误或 AskUserQuestion
      → 完整：允许副作用

宿主决定「工具需要什么」；模型决定「用户怎么说的」。

本模块是行程搜索的 L2：若状态里已有偏好/约束，其支撑槽位须已填或显式跳过。
新增「必须追问」规则只需加表项，而不是再写用户文本正则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SupportingSlot:
    """已抽出偏好时，为落实该偏好所需的额外输入槽。"""

    name: str
    header: str
    question: str
    skip_value: str | None = None
    skip_label: str | None = None
    skip_description: str | None = None


# 活跃偏好/约束 → 搜索/规划真正可用的支撑槽。
# 检测基于状态：抽取模型（或 L0）须按语义写入偏好；本表不看原始用户文本。
PREFERENCE_SUPPORTING_SLOTS: dict[str, tuple[SupportingSlot, ...]] = {
    "hotel_near_client": (
        SupportingSlot(
            name="client_location",
            header="客户位置",
            question=(
                "酒店要靠近客户时需要公司位置；没有位置就无法按通勤筛选。"
                "请填写地址/商圈，或先按目的地城市搜索。"
            ),
            skip_value="client:skip",
            skip_label="先按目的地城市搜",
            skip_description="不按通勤排序；酒店仍是城市报价",
        ),
    ),
}

CONSTRAINT_SUPPORTING_SLOTS: dict[str, tuple[SupportingSlot, ...]] = {}

SKIP_SENTINELS = frozenset({"SKIPPED", "skip", "n/a", "none"})


def slot_is_filled(fields: dict[str, Any], name: str) -> bool:
    """槽位是否已填或显式跳过（SKIP 哨兵视为已处理）。"""
    value = fields.get(name)
    if value is None or value == "":
        return False
    if isinstance(value, str) and value.strip().casefold() in SKIP_SENTINELS:
        return True
    return True


def supporting_slots_for_state(fields: dict[str, Any]) -> tuple[SupportingSlot, ...]:
    """由当前活跃偏好/约束推导出的全部支撑槽。"""
    found: list[SupportingSlot] = []
    seen: set[str] = set()
    for pref in fields.get("soft_preferences") or ():
        for slot in PREFERENCE_SUPPORTING_SLOTS.get(str(pref), ()):
            if slot.name not in seen:
                seen.add(slot.name)
                found.append(slot)
    for constraint in fields.get("hard_constraints") or ():
        for slot in CONSTRAINT_SUPPORTING_SLOTS.get(str(constraint), ()):
            if slot.name not in seen:
                seen.add(slot.name)
                found.append(slot)
    return tuple(found)


def _lodging_search_active(fields: dict[str, Any]) -> bool:
    """本次是否会实际跑酒店搜索。"""
    if fields.get("lodging_requirement") == "REQUIRED":
        return True
    if "hotel_required" in (fields.get("hard_constraints") or ()):
        return True
    return fields.get("hotel_check_in") is not None or fields.get("hotel_check_out") is not None


def completeness_gaps(fields: dict[str, Any]) -> tuple[str, ...]:
    """仍为空的支撑槽（AskUserQuestion 候选）。

    不是 SearchReady 阻断项；显式跳过后可继续搜索。偏好活跃时应至少问一次。
    通勤类槽位仅在实际会搜酒店时生效。
    """
    lodging_active = _lodging_search_active(fields)
    gaps: list[str] = []
    for slot in supporting_slots_for_state(fields):
        if slot.name == "client_location" and not lodging_active:
            continue
        if not slot_is_filled(fields, slot.name):
            gaps.append(slot.name)
    return tuple(dict.fromkeys(gaps))


def slot_spec(name: str) -> SupportingSlot | None:
    """按槽位名查找 SupportingSlot 规格。"""
    for slots in (*PREFERENCE_SUPPORTING_SLOTS.values(), *CONSTRAINT_SUPPORTING_SLOTS.values()):
        for slot in slots:
            if slot.name == name:
                return slot
    return None

"""capability_boundaries：V1 能力边界——披露不支持需求，且不假装已满足。

职责划分：
- 模型判断用户「是否提出」了不支持需求（措辞层面）。
- 宿主拥有目录与后果：披露且不搜索。
- 用户文本正则仅在无模型判断时作 fail-closed 回退（LLM 跳过或失败）。
  成功抽取且返回空列表视为「模型判定无此类需求」。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CapabilityBoundary:
    """一条不支持能力：代码 + 面向用户的披露文案。"""

    code: str
    disclosure: str

    @property
    def conflict_text(self) -> str:
        """写入 conflicts 的机器可读标记串。"""
        return f"unsupported_capability:{self.code}:{self.disclosure}"

    def as_dict(self) -> dict[str, str]:
        """序列化为 {code, disclosure}。"""
        return {"code": self.code, "disclosure": self.disclosure}


_DISCLOSURES = {
    "children": "当前不支持儿童/婴儿票，只规划单人成人差旅，不会按带儿童去搜索。",
    "visa": "当前不支持签证或护照办理，不会把它当成已安排的行程条件。",
    "seat_mileage": "当前不支持选座或里程卡/常旅客积分，搜索结果不含指定座位或积分兑换。",
    "open_jaw": "当前不支持开口票、缺口程或多城市串访，不会改成单段往返来凑合。",
    "ticket_change": "当前不支持已出票改签或退票，不会用新库存搜索代替退改。",
    "accessibility_pet": "当前不支持无障碍座位或宠物随行预订，不会按普通成人票假装已满足。",
}

_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "children",
        re.compile(
            r"儿童票|婴儿票|带.{0,8}(?:小孩|孩子|儿童|婴儿|幼儿)|"
            r"\binfant\b|\bchildren\b|\bchild(?:ren)? tickets?\b",
            re.I,
        ),
    ),
    (
        "visa",
        re.compile(r"签证|护照|\bvisa\b|\bpassport\b", re.I),
    ),
    (
        "seat_mileage",
        re.compile(
            r"选座|指定座位|靠过道|靠窗座位|里程卡|常旅客卡|常旅客积分|"
            r"seat selection|frequent flyer|\bmileage\b",
            re.I,
        ),
    ),
    (
        "open_jaw",
        re.compile(r"开口票|缺口程|open[\s-]?jaw", re.I),
    ),
    (
        "ticket_change",
        re.compile(r"已出票|已经出票|退票|退改签|这张票|票号", re.I),
    ),
    (
        "accessibility_pet",
        re.compile(
            r"无障碍|轮椅座位|导盲|宠物随行|带宠物|带狗|带猫|"
            r"wheelchair|\bpet(?:s)?\b|service animal",
            re.I,
        ),
    ),
)

_RETURN_FROM_CITY_RE = re.compile(
    r"去(?P<dest>.{1,12}?)[，,].{0,16}从(?P<ret>.{1,12}?)回"
)
_MULTI_STOP_RE = re.compile(r"再去|然后去|再到|接着去|最后(?:去|到|回)")


def detect_capability_boundaries(
    message: str,
    city_normalizer: Any | None = None,
) -> tuple[CapabilityBoundary, ...]:
    """用户文本正则回退；已有模型抽取时不作主判定。"""
    text = message or ""
    found: list[CapabilityBoundary] = []
    seen: set[str] = set()
    for code, pattern in _DETECTORS:
        if pattern.search(text) and code not in seen:
            seen.add(code)
            found.append(CapabilityBoundary(code, _DISCLOSURES[code]))
    if "open_jaw" not in seen and _message_is_open_jaw_or_multicity(text, city_normalizer):
        found.append(CapabilityBoundary("open_jaw", _DISCLOSURES["open_jaw"]))
    return tuple(found)


def boundaries_from_model(
    codes: Sequence[str] | None,
    conflicts: Sequence[str] | None = None,
) -> tuple[CapabilityBoundary, ...]:
    """解读模型结构化判断，而非原始用户句子。"""
    found: list[CapabilityBoundary] = []
    seen: set[str] = set()
    for raw in codes or ():
        code = str(raw).strip()
        if code in _DISCLOSURES and code not in seen:
            seen.add(code)
            found.append(CapabilityBoundary(code, _DISCLOSURES[code]))
    for text in conflicts or ():
        for code in _codes_from_model_conflict(str(text)):
            if code not in seen:
                seen.add(code)
                found.append(CapabilityBoundary(code, _DISCLOSURES[code]))
    return tuple(found)


def resolve_capability_boundaries(
    *,
    user_message: str,
    model_codes: Sequence[str] | None = None,
    model_conflicts: Sequence[str] | None = None,
    llm_judged: bool = False,
    city_normalizer: Any | None = None,
) -> tuple[CapabilityBoundary, ...]:
    """优先模型判断；无抽取结果时才回退用户文本正则。"""
    modeled = boundaries_from_model(model_codes, model_conflicts)
    if modeled:
        return modeled
    if llm_judged:
        return ()
    return detect_capability_boundaries(user_message, city_normalizer)


def capability_boundary_conflicts(
    message: str,
    city_normalizer: Any | None = None,
) -> tuple[str, ...]:
    """仅基于正则检测，返回 conflict_text 元组。"""
    return tuple(
        item.conflict_text
        for item in detect_capability_boundaries(message, city_normalizer)
    )


def _codes_from_model_conflict(text: str) -> tuple[str, ...]:
    """从模型 conflict 文案解析能力代码。"""
    raw = (text or "").strip()
    if is_capability_conflict(raw):
        code = raw.split(":", 2)[1] if raw.count(":") >= 1 else ""
        return (code,) if code in _DISCLOSURES else ()
    if not re.search(r"不支持|unsupported|not supported", raw, re.I):
        return ()
    return tuple(code for code, pattern in _DETECTORS if pattern.search(raw))


def format_capability_conflict(text: str) -> str | None:
    """把 unsupported_capability:code:disclosure 转成用户可读披露。"""
    raw = (text or "").strip()
    if not raw.startswith("unsupported_capability:"):
        return None
    parts = raw.split(":", 2)
    if len(parts) != 3 or not parts[2].strip():
        return None
    return parts[2].strip()


def is_capability_conflict(text: str) -> bool:
    """是否为能力边界冲突标记串。"""
    return (text or "").startswith("unsupported_capability:")


def _message_is_open_jaw_or_multicity(message: str, city_normalizer: Any | None) -> bool:
    """启发式：多城串访或去/回城市不一致。"""
    unique = _unique_catalog_cities(message, city_normalizer)
    if len(unique) >= 3:
        return True
    if _MULTI_STOP_RE.search(message or ""):
        return True
    match = _RETURN_FROM_CITY_RE.search(message or "")
    if match is None:
        return False
    dest_raw = (match.group("dest") or "").strip()
    ret_raw = (match.group("ret") or "").strip()
    dest = _lookup_city(dest_raw, city_normalizer) or dest_raw
    ret_origin = _lookup_city(ret_raw, city_normalizer) or ret_raw
    if dest and ret_origin and dest.casefold() != ret_origin.casefold():
        return True
    return False


def _unique_catalog_cities(message: str, city_normalizer: Any | None) -> list[str]:
    from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

    parser = GroundedLocalIntentParser(city_normalizer)
    return list(dict.fromkeys(canonical for _index, canonical in parser._city_mentions(message)))


def _lookup_city(span: str, city_normalizer: Any | None) -> str | None:
    from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

    parser = GroundedLocalIntentParser(city_normalizer)
    return parser._lookup_span(span)

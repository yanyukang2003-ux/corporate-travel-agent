"""Source-grounded evidence for semantic travel slots.

The language model owns semantic slot assignment. This module owns the other
half of the contract: every accepted location/date value must point back to an
exact span in the current user message, a preserved prior field, or a documented
deterministic derivation from such a span.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from corporate_travel_agent.domain.enums import BookingScope

from .intent_calibration import iter_calendar_date_mentions, resolve_calendar_mention

_DELIMITER_RE = re.compile(r"[，,。\.；;！？!?\n]")
_RETURN_RE = re.compile(
    r"返程|返回|回来|回程|\breturn\b|get back|coming home|come home|back by",
    re.I,
)
_OUTBOUND_RE = re.compile(
    r"去程|出发|启程|抵达|到达|前到|最晚|开会|会议|从.+(?:去|到|至)|"
    r"\bfrom\b.+\bto\b|\boutbound\b|\bdepart|\barriv|\bmeeting",
    re.I,
)
_ROUND_TRIP_RE = re.compile(
    r"同日往返|当天往返|当日往返|当天回来|当天返回|same[- ]day round trip",
    re.I,
)
_RELATIVE_DAY_RE = re.compile(r"后天|明天|今天|tomorrow|today", re.I)
_SAME_DAY_REF_RE = re.compile(r"当天|当日|当晚")
_RETURN_REFERENCE_RE = re.compile(
    r"返程|回程|返回|回来|\breturn\b|coming home|come home|back by",
    re.I,
)
_WEEKDAY_RE = re.compile(r"(?P<prefix>下下|下|本|这)?周(?P<day>[一二三四五六日天])")
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_CITY_FIELDS = frozenset({"origin", "destination"})
_DATE_FIELDS = frozenset(
    {
        "departure_after",
        "arrive_by",
        "return_after",
        "return_before",
        "hotel_check_in",
        "hotel_check_out",
    }
)


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    raw: str
    start: int
    end: int
    kind: str
    scope: str
    normalized: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "scope": self.scope,
            "normalized": self.normalized,
        }


@dataclass(frozen=True, slots=True)
class IntentEvidence:
    message: str
    dates: tuple[SourceEvidence, ...]
    cities: tuple[SourceEvidence, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dates": [item.as_dict() for item in self.dates],
            "cities": [item.as_dict() for item in self.cities],
        }


@dataclass(frozen=True, slots=True)
class FieldEvidenceValidation:
    accepted_fields: frozenset[str]
    rejected_fields: tuple[str, ...]
    field_evidence: dict[str, dict[str, Any]]


def iter_clause_spans(message: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _DELIMITER_RE.finditer(message or ""):
        if start < match.start():
            spans.append((start, match.start()))
        start = match.end()
    if start < len(message):
        spans.append((start, len(message)))
    return tuple(spans or [(0, len(message))])


def clause_scope(clause: str) -> str:
    if _ROUND_TRIP_RE.search(clause):
        return "round_trip"
    if _RETURN_RE.search(clause):
        return "return"
    if _OUTBOUND_RE.search(clause):
        return "outbound"
    return "ambiguous"


def span_scope(message: str, start: int, end: int) -> str:
    for clause_start, clause_end in iter_clause_spans(message):
        if clause_start <= start and end <= clause_end:
            return clause_scope(message[clause_start:clause_end])
    return "ambiguous"


def build_intent_evidence(
    message: str,
    *,
    reference_time: datetime,
    city_mentions: Iterable[tuple[int, int, str, str]] = (),
) -> IntentEvidence:
    """Extract host-owned spans; this function never assigns canonical slots."""
    date_items: list[SourceEvidence] = []
    for mention in iter_calendar_date_mentions(message):
        resolved = resolve_calendar_mention(mention, reference_time=reference_time)
        date_items.append(
            SourceEvidence(
                raw=mention.raw,
                start=mention.start,
                end=mention.end,
                kind="calendar_date",
                scope=span_scope(message, mention.start, mention.end),
                normalized=resolved.isoformat() if resolved is not None else None,
            )
        )

    for match in _RELATIVE_DAY_RE.finditer(message or ""):
        raw = match.group(0)
        lowered = raw.casefold()
        delta = 2 if raw == "后天" else 1 if raw == "明天" or lowered == "tomorrow" else 0
        resolved = reference_time.date() + timedelta(days=delta)
        date_items.append(
            SourceEvidence(
                raw=raw,
                start=match.start(),
                end=match.end(),
                kind="relative_date",
                scope=span_scope(message, match.start(), match.end()),
                normalized=resolved.isoformat(),
            )
        )

    for match in _WEEKDAY_RE.finditer(message or ""):
        resolved = _resolve_weekday(
            reference_time.date(), match.group("prefix"), _WEEKDAY_INDEX[match.group("day")]
        )
        earlier_days = [
            date.fromisoformat(item.normalized)
            for item in date_items
            if item.start < match.start() and item.normalized is not None
        ]
        if match.group("prefix") is None and earlier_days:
            anchor = max(earlier_days)
            resolved = anchor + timedelta(
                days=(_WEEKDAY_INDEX[match.group("day")] - anchor.weekday()) % 7
            )
        date_items.append(
            SourceEvidence(
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
                kind="weekday",
                scope=span_scope(message, match.start(), match.end()),
                normalized=resolved.isoformat(),
            )
        )

    for match in _SAME_DAY_REF_RE.finditer(message or ""):
        anchors = [
            item
            for item in date_items
            if item.start < match.start() and item.normalized is not None
        ]
        if not anchors:
            continue
        anchor = max(anchors, key=lambda item: item.start)
        date_items.append(
            SourceEvidence(
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
                kind="same_day_reference",
                scope=span_scope(message, match.start(), match.end()),
                normalized=anchor.normalized,
            )
        )

    for match in _RETURN_REFERENCE_RE.finditer(message or ""):
        anchors = [
            item
            for item in date_items
            if item.start < match.start() and item.normalized is not None
        ]
        if not anchors:
            continue
        anchor = max(anchors, key=lambda item: item.start)
        date_items.append(
            SourceEvidence(
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
                kind="return_date_reference",
                scope="return",
                normalized=anchor.normalized,
            )
        )

    date_items.sort(key=lambda item: item.start)
    date_items = _apply_positional_fallback_scopes(message, date_items)
    city_items = tuple(
        SourceEvidence(
            raw=raw,
            start=start,
            end=end,
            kind="city",
            scope=span_scope(message, start, end),
            normalized=canonical,
        )
        for start, end, raw, canonical in city_mentions
    )
    return IntentEvidence(message=message, dates=tuple(date_items), cities=city_items)


def validate_model_field_evidence(
    *,
    extracted_fields: dict[str, Any],
    provided_fields: set[str],
    prior_fields: dict[str, Any],
    evidence: IntentEvidence,
    reject_untraceable: bool = True,
) -> FieldEvidenceValidation:
    """Validate newly supplied location/date slots against exact source spans."""
    accepted: set[str] = set()
    rejected: list[str] = []
    traces: dict[str, dict[str, Any]] = {}
    return_only = prior_fields.get("booking_scope") == BookingScope.RETURN_ONLY.value
    for name in provided_fields:
        value = extracted_fields.get(name)
        if value in (None, "", []):
            accepted.add(name)
            continue
        if prior_fields.get(name) == value and prior_fields.get(name) not in (None, "", []):
            accepted.add(name)
            traces[name] = {"kind": "prior_field", "source": "previous_turn"}
            continue
        if name in _CITY_FIELDS:
            match = _city_evidence(value, evidence.cities, evidence.message)
            if match is None:
                if reject_untraceable:
                    rejected.append(f"{name}:untraceable_source_span")
                else:
                    accepted.add(name)
                    traces[name] = _unverified_trace("untraceable_source_span")
                continue
            accepted.add(name)
            traces[name] = match.as_dict()
            continue
        if name in _DATE_FIELDS:
            match, reason = _date_evidence(
                name,
                value,
                evidence.dates,
                return_as_primary=return_only,
            )
            if match is None:
                if reason == "untraceable_source_span" and not reject_untraceable:
                    accepted.add(name)
                    traces[name] = _unverified_trace(reason)
                else:
                    rejected.append(f"{name}:{reason}")
                continue
            accepted.add(name)
            traces[name] = match.as_dict() | {
                "derivation": "date_from_span;clock_from_user_or_policy"
            }
            continue
        if name == "client_location":
            raw = str(value)
            start = evidence.message.casefold().find(raw.casefold())
            if start < 0:
                if reject_untraceable:
                    rejected.append(f"{name}:untraceable_source_span")
                else:
                    accepted.add(name)
                    traces[name] = _unverified_trace("untraceable_source_span")
                continue
            accepted.add(name)
            traces[name] = SourceEvidence(
                raw=evidence.message[start : start + len(raw)],
                start=start,
                end=start + len(raw),
                kind="client_location",
                scope="ambiguous",
                normalized=raw,
            ).as_dict()
            continue
        # Constraint/preference enums are semantic labels, not copied scalar slots.
        accepted.add(name)
        traces[name] = {"kind": "semantic_label", "source": "current_message"}
    return FieldEvidenceValidation(frozenset(accepted), tuple(rejected), traces)


def _unverified_trace(reason: str) -> dict[str, Any]:
    return {
        "kind": "unverified",
        "validation": "failed",
        "reason": reason,
        "source": "model_semantics_compatibility",
    }


def _city_evidence(
    value: Any,
    mentions: tuple[SourceEvidence, ...],
    message: str,
) -> SourceEvidence | None:
    needle = str(value).casefold()
    for item in mentions:
        if needle in {item.raw.casefold(), str(item.normalized or "").casefold()}:
            return item
    start = message.casefold().find(needle)
    if start >= 0:
        return SourceEvidence(
            raw=message[start : start + len(str(value))],
            start=start,
            end=start + len(str(value)),
            kind="city",
            scope=span_scope(message, start, start + len(str(value))),
            normalized=str(value),
        )
    return None


def _date_evidence(
    field_name: str,
    value: Any,
    mentions: tuple[SourceEvidence, ...],
    *,
    return_as_primary: bool = False,
) -> tuple[SourceEvidence | None, str]:
    value_day = value.date() if isinstance(value, datetime) else value
    if not isinstance(value_day, date):
        return None, "invalid_date_value"
    same_day = [item for item in mentions if item.normalized == value_day.isoformat()]
    if not same_day:
        if (
            field_name in {"departure_after", "arrive_by"}
            and mentions
            and all(item.scope == "return" for item in mentions)
            and not return_as_primary
        ):
            return None, "source_scope_mismatch"
        return None, "untraceable_source_span"
    if field_name in {"departure_after", "arrive_by"}:
        compatible = {"outbound", "round_trip", "ambiguous"}
        if return_as_primary:
            compatible.add("return")
    elif field_name in {"return_after", "return_before"}:
        compatible = {"return", "round_trip"}
    else:
        compatible = {"outbound", "return", "round_trip", "ambiguous"}
    for item in same_day:
        if item.scope in compatible:
            return item, ""
    return None, "source_scope_mismatch"


def _apply_positional_fallback_scopes(
    message: str, items: list[SourceEvidence]
) -> list[SourceEvidence]:
    if len(items) == 1 and _ROUND_TRIP_RE.search(message):
        item = items[0]
        return [
            SourceEvidence(
                item.raw,
                item.start,
                item.end,
                item.kind,
                "round_trip",
                item.normalized,
            )
        ]
    ambiguous_indexes = [i for i, item in enumerate(items) if item.scope == "ambiguous"]
    explicit_scopes = {item.scope for item in items if item.scope != "ambiguous"}
    if not explicit_scopes and len(ambiguous_indexes) >= 2:
        updated = list(items)
        for position, index in enumerate(ambiguous_indexes):
            item = updated[index]
            scope = "outbound" if position == 0 else "return"
            updated[index] = SourceEvidence(
                item.raw, item.start, item.end, item.kind, scope, item.normalized
            )
        return updated
    if len(items) == 1 and items[0].scope == "ambiguous":
        item = items[0]
        return [
            SourceEvidence(
                item.raw,
                item.start,
                item.end,
                item.kind,
                "outbound",
                item.normalized,
            )
        ]
    return items


def _resolve_weekday(today: date, prefix: str | None, weekday: int) -> date:
    if prefix in {"下", "下下"}:
        days_to_monday = (7 - today.weekday()) % 7 or 7
        extra = 7 if prefix == "下下" else 0
        return today + timedelta(days=days_to_monday + weekday + extra)
    return today + timedelta(days=(weekday - today.weekday()) % 7)

"""领域对象与持久化 JSON 载荷之间的序列化/反序列化（含 schema 版本校验）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast

from pydantic import TypeAdapter

from corporate_travel_agent.domain.models import AuditEvent, InventorySnapshot, Trip, TripTask

SCHEMA_VERSION = 1

_TASK_ADAPTER = TypeAdapter(TripTask)
_AUDIT_ADAPTER = TypeAdapter(AuditEvent)
_SNAPSHOT_ADAPTER = TypeAdapter(InventorySnapshot)
_TRIP_ADAPTER = TypeAdapter(Trip)

_INTENT_DATETIME_FIELDS = (
    "departure_after",
    "arrive_by",
    "return_after",
    "return_before",
)
_INTENT_DATE_FIELDS = ("hotel_check_in", "hotel_check_out")


class UnsupportedPayloadVersion(ValueError):
    """持久化载荷的 schema_version 不受当前代码支持。"""


def serialize_task(task: TripTask) -> dict[str, Any]:
    """将 TripTask 序列化为带 schema_version 的 JSON 兼容字典。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "task": cast(dict[str, Any], _TASK_ADAPTER.dump_python(task, mode="json")),
    }


def deserialize_task(payload: dict[str, Any]) -> TripTask:
    """从持久化载荷还原 TripTask，并恢复意图字段中的日期时间类型。"""
    _require_supported_version(payload)
    task = _TASK_ADAPTER.validate_python(payload["task"])
    task.intent_fields = _restore_intent_types(task.intent_fields)
    reasons = task.metadata.get("no_feasible_reasons")
    if isinstance(reasons, list):
        task.metadata["no_feasible_reasons"] = tuple(reasons)
    return task


def serialize_audit_event(event: AuditEvent) -> dict[str, Any]:
    """将 AuditEvent 序列化为带 schema_version 的 JSON 兼容字典。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "event": cast(dict[str, Any], _AUDIT_ADAPTER.dump_python(event, mode="json")),
    }


def deserialize_audit_event(payload: dict[str, Any]) -> AuditEvent:
    """从持久化载荷还原 AuditEvent。"""
    _require_supported_version(payload)
    return _AUDIT_ADAPTER.validate_python(payload["event"])


def serialize_snapshot(snapshot: InventorySnapshot) -> dict[str, Any]:
    """将 InventorySnapshot 序列化为带 schema_version 的 JSON 兼容字典。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot": cast(
            dict[str, Any],
            _SNAPSHOT_ADAPTER.dump_python(snapshot, mode="json"),
        ),
    }


def deserialize_snapshot(payload: dict[str, Any]) -> InventorySnapshot:
    """从持久化载荷还原 InventorySnapshot。"""
    _require_supported_version(payload)
    return _SNAPSHOT_ADAPTER.validate_python(payload["snapshot"])


def _require_supported_version(payload: dict[str, Any]) -> None:
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise UnsupportedPayloadVersion(
            f"Unsupported persistence payload version: {version!r}"
        )


def _restore_intent_types(fields: dict[str, Any]) -> dict[str, Any]:
    restored = dict(fields)
    for field_name in _INTENT_DATETIME_FIELDS:
        value = restored.get(field_name)
        if isinstance(value, str):
            restored[field_name] = datetime.fromisoformat(value.replace("Z", "+00:00"))
    for field_name in _INTENT_DATE_FIELDS:
        value = restored.get(field_name)
        if isinstance(value, str):
            restored[field_name] = date.fromisoformat(value)
    return restored


def serialize_trip(trip: Trip) -> dict[str, Any]:
    """将 Trip 序列化为带 schema_version 的 JSON 兼容字典。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "trip": cast(dict[str, Any], _TRIP_ADAPTER.dump_python(trip, mode="json")),
    }


def deserialize_trip(payload: dict[str, Any]) -> Trip:
    """从持久化载荷还原 Trip。"""
    _require_supported_version(payload)
    return _TRIP_ADAPTER.validate_python(payload["trip"])

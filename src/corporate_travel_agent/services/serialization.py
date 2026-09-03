"""领域对象与持久化 JSON 载荷之间的序列化/反序列化（含 schema 版本与升级链）。

## 载荷版本

每条载荷带 `schema_version`。**模型改了形状，就在这里加一级升级函数**，读取时把旧载荷逐级
升到当前版本再交给 pydantic；写入永远是当前版本。此前从头到尾只有版本 1，2026-08-29 方案
从 `outbound/inbound/hotel` 改成 `legs/stays` 之后，旧行再也读不出来——开发库里 20 条任务
只有 12 条能加载，列表接口碰到旧行直接 500。

| 版本 | 变化 | 升级 |
|---|---|---|
| 1 → 2 | `TravelOptionVersion.outbound/inbound/hotel` → `legs/stays` | `_upgrade_v1_to_v2` |
| 2 → 3 | 请求的扁平字段并入 `journey/stays/scoped_*`（ADR-0010） | `_upgrade_v2_to_v3` |

升级只改形状，不补业务事实；升不上去的载荷抛 `PayloadIncompatible`，API 层给明确的错误而
不是 500，`examples/upgrade_task_payloads.py` 把库里的旧行批量改写成当前版本。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from corporate_travel_agent.domain.models import AuditEvent, InventorySnapshot, Trip, TripTask

SCHEMA_VERSION = 3

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
    """持久化载荷的 schema_version 不受当前代码支持（比当前代码还新，或者缺失）。"""


class PayloadIncompatible(ValueError):
    """载荷升到当前版本之后仍然不符合当前模型：这一行需要人看。"""

    def __init__(self, kind: str, version: object, cause: Exception) -> None:
        super().__init__(
            f"stored {kind} payload (schema_version {version!r}) does not match the current "
            f"model even after upgrade: {str(cause)[:300]}"
        )
        self.kind = kind
        self.version = version
        self.cause = cause


def _upgrade_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """1 → 2：方案的 `outbound/inbound/hotel` 三个槽变成有序的 `legs/stays`。

    只动任务载荷；审计、快照、差旅在这两版之间形状没变，只升版本号。
    """
    task = payload.get("task")
    if isinstance(task, dict):
        upgraded_options = []
        for option in task.get("options") or []:
            if not isinstance(option, dict) or "legs" in option:
                upgraded_options.append(option)
                continue
            option = dict(option)
            outbound = option.pop("outbound", None)
            inbound = option.pop("inbound", None)
            hotel = option.pop("hotel", None)
            option["legs"] = [leg for leg in (outbound, inbound) if leg is not None]
            option["stays"] = [hotel] if hotel is not None else []
            upgraded_options.append(option)
        task = dict(task)
        task["options"] = upgraded_options
        payload = dict(payload)
        payload["task"] = task
    payload = dict(payload)
    payload["schema_version"] = 2
    return payload


_FLAT_REQUEST_FIELDS = (
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
)


def _upgrade_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """2 → 3：请求的扁平字段并入 `journey / stays / scoped_*`，然后删掉（ADR-0010）。

    推导规则和旧的 `transport_legs()` / `lodging_stays()` 一字不差：单程一段；预订范围是往返
    （显式，或没写但有返程窗口）且给了返程窗口才有第二段；住宿只在目的地住一次；要求按管全程。
    只动任务载荷里的 `request`；审计、快照、差旅在这两版之间形状没变，只升版本号。
    """
    task = payload.get("task")
    if isinstance(task, dict) and isinstance(task.get("request"), dict):
        request = dict(task["request"])
        scope = request.get("booking_scope")
        if not request.get("journey"):
            if scope is None:
                scope = (
                    "ROUND_TRIP"
                    if request.get("return_after") is not None
                    or request.get("return_before") is not None
                    else "OUTBOUND_ONLY"
                )
            legs = [
                {
                    "role": "RETURN" if scope == "RETURN_ONLY" else "OUTBOUND",
                    "origin": request.get("origin"),
                    "destination": request.get("destination"),
                    "depart_after": request.get("departure_after"),
                    "arrive_before": request.get("arrive_by"),
                }
            ]
            if (
                scope == "ROUND_TRIP"
                and request.get("return_after") is not None
                and request.get("return_before") is not None
            ):
                legs.append(
                    {
                        "role": "RETURN",
                        "origin": request.get("destination"),
                        "destination": request.get("origin"),
                        "depart_after": request.get("return_after"),
                        "arrive_before": request.get("return_before"),
                    }
                )
            request["journey"] = legs
        if not request.get("stays"):
            request["stays"] = (
                [
                    {
                        "city": request.get("destination"),
                        "check_in": request.get("hotel_check_in"),
                        "check_out": request.get("hotel_check_out"),
                    }
                ]
                if request.get("hotel_check_in") is not None
                and request.get("hotel_check_out") is not None
                else []
            )
        for flat, scoped in (
            ("hard_constraints", "scoped_hard_constraints"),
            ("soft_preferences", "scoped_soft_preferences"),
        ):
            if not request.get(scoped):
                request[scoped] = [
                    {"name": name, "leg_index": None}
                    for name in dict.fromkeys(request.get(flat) or [])
                ]
        for key in _FLAT_REQUEST_FIELDS:
            request.pop(key, None)
        task = dict(task)
        task["request"] = request
        payload = dict(payload)
        payload["task"] = task
    payload = dict(payload)
    payload["schema_version"] = 3
    return payload


#: 从版本 N 升到 N+1 的函数；读取时按顺序走到 `SCHEMA_VERSION`。
_UPGRADES: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _upgrade_v1_to_v2,
    2: _upgrade_v2_to_v3,
}


def upgrade_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """把载荷升到当前版本；返回 (载荷, 是否动过)。版本缺失或比当前新就拒绝。"""
    version = payload.get("schema_version")
    if not isinstance(version, int) or version < 1 or version > SCHEMA_VERSION:
        raise UnsupportedPayloadVersion(f"Unsupported persistence payload version: {version!r}")
    changed = False
    while version < SCHEMA_VERSION:
        step = _UPGRADES.get(version)
        if step is None:
            raise UnsupportedPayloadVersion(f"No upgrade path from payload version {version}")
        payload = step(payload)
        version = int(payload["schema_version"])
        changed = True
    return payload, changed


def _validate(kind: str, adapter: TypeAdapter, payload: dict[str, Any]) -> Any:
    original_version = payload.get("schema_version")
    upgraded, _ = upgrade_payload(payload)
    try:
        return adapter.validate_python(upgraded[kind])
    except (ValidationError, KeyError, TypeError) as exc:
        raise PayloadIncompatible(kind, original_version, exc) from exc


def serialize_task(task: TripTask) -> dict[str, Any]:
    """将 TripTask 序列化为带 schema_version 的 JSON 兼容字典。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "task": cast(dict[str, Any], _TASK_ADAPTER.dump_python(task, mode="json")),
    }


def deserialize_task(payload: dict[str, Any]) -> TripTask:
    """从持久化载荷还原 TripTask（旧版本先升级），并恢复意图字段中的日期时间类型。"""
    task = _validate("task", _TASK_ADAPTER, payload)
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
    return _validate("event", _AUDIT_ADAPTER, payload)


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
    return _validate("snapshot", _SNAPSHOT_ADAPTER, payload)


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
    return _validate("trip", _TRIP_ADAPTER, payload)

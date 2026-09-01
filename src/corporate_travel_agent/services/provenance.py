"""方案溯源：一条方案的每一步，各自的依据是什么。

**这份记录是推导出来的，不是另存一份。** 输入全是已经落库的不可变对象——
库存快照、规则证据、审计事件、搜索出处——这里只负责把它们按"哪一步 → 凭什么"
串成一条链。

为什么不新写一张表：另存一份叙述就等于多一个事实源，而**第二个事实源迟早会和
第一个分叉**。分叉之后没人知道该信哪个，溯源反而变成了负资产。推导出来的链条
不会分叉，代价是每次都要重算——那点算力换"永远和事实一致"，划算。

"存下来"是另一件事，用哈希做：交接那一刻把这条链算一个哈希钉住
（`provenance_hash`），事后任何时候重算比对，就能证明它没被改过。这和审批主体
哈希（`_approval_subject_hash`）是同一个套路。

**链条断掉的地方要说出来，不要静默省略。** 记录里的 `gaps` 就是干这个的：
结构化入口没有用户原话可抄、原始响应没留存、快照找不回来——每一条都写明。
一份不承认自己有缺口的溯源记录，比没有还危险。
"""

from __future__ import annotations

from typing import Any

from corporate_travel_agent.domain.models import (
    AuditEvent,
    HotelOffer,
    InventorySnapshot,
    PolicySnapshot,
    SearchProvenance,
    TransportOffer,
    TravelOptionVersion,
    TripTask,
)
from corporate_travel_agent.services.audit import stable_hash

SCHEMA_VERSION = 1


def option_provenance(
    *,
    task: TripTask,
    option: TravelOptionVersion,
    snapshots: tuple[InventorySnapshot, ...] | list[InventorySnapshot],
    events: tuple[AuditEvent, ...] | list[AuditEvent] = (),
    policy: PolicySnapshot | None = None,
) -> dict[str, Any]:
    """一条方案的完整依据链，JSON 安全。

    ``policy`` 只用来补政策版本号；拿不到也能出记录，缺的部分会进 `gaps`。
    """
    by_snapshot = {item.snapshot_id: item for item in snapshots}
    searches = {item.snapshot_id: item for item in task.searches}
    gaps: list[str] = []

    items = [
        _leg_record(index, leg, by_snapshot, searches, task, gaps)
        for index, leg in enumerate(option.legs)
    ]
    items.extend(
        _stay_record(index, stay, by_snapshot, searches, task, gaps)
        for index, stay in enumerate(option.stays)
    )

    decision = option.policy_decision
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task.task_id,
        "option_id": option.option_id,
        "option_version": option.version,
        "trip_request_version": option.trip_request_version,
        "traveler": {
            "employee_id": task.employee.employee_id,
            "employee_snapshot_id": task.employee.snapshot_id,
            "level": task.employee.level,
        },
        "policy": {
            "snapshot_id": task.policy_snapshot_id,
            "content_hash": task.metadata.get("policy_content_hash"),
            "policy_version": policy.policy_version if policy is not None else None,
        },
        "items": items,
        "policy_decision": {
            "outcome": decision.outcome.value,
            "unjudged_rule_ids": list(decision.unjudged_rule_ids),
            "forbidden_rule_ids": list(decision.forbidden_rule_ids),
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "outcome": rule.outcome.value,
                    "actual": rule.actual,
                    "threshold": rule.threshold,
                    "policy_version": rule.policy_version,
                    "exception_allowed": rule.exception_allowed,
                }
                for rule in decision.evidence
            ],
        },
        "state_events": [
            {
                "event_type": event.event_type,
                "actor_type": event.actor_type,
                "created_at": event.created_at.isoformat(),
                "input_hash": event.input_hash,
                "output_hash": event.output_hash,
                "evidence_refs": list(event.evidence_refs),
            }
            for event in events
        ],
        "approval": _approval_record(task),
        "handoff": _handoff_record(task, option),
    }
    if policy is None:
        gaps.append("政策快照没有随记录取回，规则里的版本号来自各条证据自身")
    if not events:
        gaps.append("没有取到状态事件，这条链只覆盖到方案本身")
    record["gaps"] = gaps
    return record


#: 进指纹的字段。**只有"依据"进，"日志"不进。**
#:
#: 审计事件是追加日志，它按定义就会一直变长；把它算进指纹，指纹第二天必然对不上，
#: 于是"对不上"这件事就再也说明不了任何问题。同理，审批状态和交接单状态在钉住
#: 之后仍会变（交接完成会改 `BookingIntent.status`），它们的**身份**进指纹，
#: **状态**不进。
_SEALED_FIELDS = (
    "schema_version",
    "task_id",
    "option_id",
    "option_version",
    "trip_request_version",
    "traveler",
    "policy",
    "items",
    "policy_decision",
    "gaps",
)


def sealed_view(record: dict[str, Any]) -> dict[str, Any]:
    """记录里**不该再变**的那一部分。指纹只盖在这上面。"""
    sealed = {name: record[name] for name in _SEALED_FIELDS if name in record}
    approval = record.get("approval")
    if approval is not None:
        # 审批主体哈希本身已经把"批的是哪条方案、哪几条规则"焊死了，
        # 所以这里只认身份，不认状态。
        sealed["approval"] = {
            "approval_id": approval["approval_id"],
            "subject_hash": approval["subject_hash"],
        }
    handoff = record.get("handoff")
    if handoff is not None:
        sealed["handoff"] = {
            "intent_id": handoff["intent_id"],
            "idempotency_key": handoff["idempotency_key"],
            "selected_option_version": handoff["selected_option_version"],
        }
    return sealed


def provenance_hash(record: dict[str, Any]) -> str:
    """这条链的指纹。``gaps`` 一并计入——**当时说不清的地方，事后也不许悄悄补上**。"""
    return stable_hash(sealed_view(record))


def _leg_record(
    index: int,
    leg: TransportOffer,
    by_snapshot: dict[str, InventorySnapshot],
    searches: dict[str, SearchProvenance],
    task: TripTask,
    gaps: list[str],
) -> dict[str, Any]:
    return {
        "role": "leg",
        "index": index,
        "ref_id": leg.ref_id,
        "what": {
            "mode": leg.mode.value,
            "origin": leg.origin,
            "destination": leg.destination,
            "depart_at": leg.depart_at.isoformat(),
            "arrive_at": leg.arrive_at.isoformat(),
            "seat_class": leg.seat_class,
            "price": str(leg.price),
            "currency": leg.currency,
        },
        "because": _because(
            f"第 {index + 1} 段", leg.snapshot_id, by_snapshot, searches, task, gaps
        ),
    }


def _stay_record(
    index: int,
    stay: HotelOffer,
    by_snapshot: dict[str, InventorySnapshot],
    searches: dict[str, SearchProvenance],
    task: TripTask,
    gaps: list[str],
) -> dict[str, Any]:
    return {
        "role": "stay",
        "index": index,
        "ref_id": stay.ref_id,
        "what": {
            "name": stay.name,
            "city": stay.city,
            "check_in": stay.check_in.isoformat(),
            "check_out": stay.check_out.isoformat(),
            "nights": stay.nights,
            "nightly_price": str(stay.nightly_price),
            "currency": stay.currency,
        },
        "because": _because(
            f"第 {index + 1} 处住宿", stay.snapshot_id, by_snapshot, searches, task, gaps
        ),
    }


def _because(
    label: str,
    snapshot_id: str,
    by_snapshot: dict[str, InventorySnapshot],
    searches: dict[str, SearchProvenance],
    task: TripTask,
    gaps: list[str],
) -> dict[str, Any]:
    """这一项凭什么在方案里：来自哪个快照，那次搜索又凭什么发生。"""
    snapshot = by_snapshot.get(snapshot_id)
    if snapshot is None:
        gaps.append(f"{label}的库存快照 {snapshot_id} 取不回来，无法验证它的原始响应")
        return {"snapshot_id": snapshot_id, "snapshot": None, "search": None}

    raw = snapshot.raw_response
    if raw is None:
        gaps.append(f"{label}的原始供应商响应没有留存，只能验到快照哈希这一层")

    search = searches.get(snapshot_id)
    if search is None:
        gaps.append(f"{label}没有搜索出处记录，说不出这次搜索用了什么参数")
    elif search.date_evidence is None:
        gaps.append(f"{label}的日期没有对话出处——它来自已校验的行程请求，不是某句原话")

    return {
        "snapshot_id": snapshot_id,
        "snapshot": {
            "provider": snapshot.provider,
            "source_type": snapshot.source_type.value,
            "captured_at": snapshot.captured_at.isoformat(),
            "valid_until": snapshot.valid_until.isoformat(),
            "query_hash": snapshot.query_hash,
            "raw_payload_hash": snapshot.raw_payload_hash,
            "raw_response": None
            if raw is None
            else {
                "object_key": raw.object_key,
                "sha256": raw.sha256,
                "size_bytes": raw.size_bytes,
                "stored_at": raw.stored_at.isoformat(),
                "retention_until": raw.retention_until.isoformat(),
                "access_policy": raw.access_policy.value,
            },
        },
        "search": None
        if search is None
        else {
            "kind": search.kind,
            "parameters": [list(pair) for pair in search.parameters],
            "date_evidence": search.date_evidence,
            "assumption": search.assumption,
            "quoted_from_message": _message_index(task, search.date_evidence),
        },
    }


def _message_index(task: TripTask, quote: str | None) -> int | None:
    """这句原话是对话里的第几条。

    找不到就是 None——**不猜**。工具循环的出处关卡本来就要求逐字引用，
    找不到说明消息被改过或者引用跨了多条，那正是该露出来的异常。
    """
    if not quote:
        return None
    for index, message in enumerate(task.messages):
        if quote in message.content:
            return index
    return None


def _approval_record(task: TripTask) -> dict[str, Any] | None:
    approval = task.approval
    if approval is None:
        return None
    return {
        "approval_id": approval.approval_id,
        "subject_hash": approval.subject_hash,
        "rules": list(approval.violations),
        "business_reason": approval.business_reason,
        "approver_id": approval.approver_id,
        "status": approval.status.value,
        "created_at": approval.created_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "decision_reason": approval.decision_reason,
    }


def _handoff_record(task: TripTask, option: TravelOptionVersion) -> dict[str, Any] | None:
    intent = task.booking_intent
    if intent is None or intent.selected_option_id != option.option_id:
        return None
    return {
        "intent_id": intent.intent_id,
        "idempotency_key": intent.idempotency_key,
        "selected_option_version": intent.selected_option_version,
        "revalidated_at": intent.revalidated_at.isoformat(),
        "status": intent.status,
    }

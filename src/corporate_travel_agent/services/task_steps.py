"""把一条任务从建到现在的每一步收集起来，按先后整理成一条时间线。

数据全部来自已经落库的东西，这个模块**只读不写**：

- 对话（`task.messages`）——旅行者说了什么、助手问了什么；
- 工具调用（`task.tool_calls`）——模型每一步选了什么工具、成没成、重试没有；
- 搜索出处（`task.searches` + 库存快照）——为什么搜这一天（用户原话）、系统补了什么
  假设、搜回来多少条、样例长什么样；
- 方案、审批、交接单、下单确认、费控对账——各自一步，带各自的内容；
- 审计事件——上面这些盖不到的里程碑（澄清、改口、失败、恢复……）只取时间和名目。

审计事件本身只存哈希不存内容（设计如此），所以**内容一律读任务上的字段**，
事件只负责"什么时候发生的"。排序按时间，同一时刻按来源稳定排。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from corporate_travel_agent.domain.models import (
    AuditEvent,
    HotelOffer,
    InventorySnapshot,
    TransportOffer,
    TripTask,
)

#: 工具名 → 人话标题。没列到的工具原名照给，不猜。
_TOOL_TITLES = {
    "llm.next_tool_call": "模型决定下一步",
    "llm.extract_intent": "模型解析需求",
    "tool.search_transport": "执行交通搜索",
    "tool.search_hotels": "执行酒店搜索",
    "tool.lookup_city": "查询城市名",
    "tool.ask_traveler": "向旅行者提问",
    "tool.propose_options": "交出方案",
    "provider.search_transport.outbound": "供应商搜索：去程",
    "provider.search_transport.return": "供应商搜索：返程",
    "provider.search_hotels": "供应商搜索：酒店",
    "provider.search_journey": "供应商搜索：整票",
    "provider.revalidate": "报价重验",
    "provider.create_deep_link": "生成交接链接",
}

#: 审计事件 → 里程碑标题。TOOL_CALL_* 和 STATE_TRANSITION 故意不在：
#: 前者已由工具调用覆盖，后者只有哈希、说不出从哪到哪。
_MILESTONE_TITLES = {
    "TASK_CREATED": "任务创建（结构化请求）",
    "AGENTIC_TASK_CREATED_FROM_MESSAGE": "任务创建（自然语言）",
    "AGENTIC_CLARIFICATION_REQUESTED": "助手停下来提问",
    "AGENTIC_CLARIFICATION_RECEIVED": "旅行者补充了信息",
    "AGENTIC_REVISION_RECEIVED": "旅行者修改了要求",
    "AGENTIC_CLARIFICATION_EXHAUSTED": "澄清轮次用尽",
    "AGENTIC_OUT_OF_SCOPE": "判定超出能力范围",
    "AGENTIC_LOOP_ABORTED": "工具循环没有收敛",
    "AGENTIC_PROVIDER_FAILED": "供应商故障",
    "AGENTIC_PROPOSAL_WITHOUT_SEARCH": "方案没有搜索支撑，被拒绝",
    "NO_FEASIBLE_OPTION": "没有可行方案",
    "STRUCTURED_FALLBACK_SUBMITTED": "提交了结构化表单",
    "REQUEST_REVISED": "需求已修订",
    "OPTION_SELECTED": "旅行者选中方案",
    "INVENTORY_REVALIDATED": "选中方案报价重验",
    "INVENTORY_SNAPSHOT_REJECTED": "过期库存被拒收",
    "APPROVAL_INVALIDATED": "审批失效",
    "BOOKING_INTENT_CREATED": "生成交接单",
    "PROVENANCE_PINNED": "依据链钉上指纹",
    "HANDOFF_COMPLETED": "旅行者确认已去官方渠道下单",
    "PROVIDER_COVERAGE_INCOMPLETE": "供应商声明覆盖不完整",
    "PROVIDER_DELAYED_RETRY_STARTED": "延迟重试开始",
    "INTERRUPTED_TASK_RECOVERED": "进程重启后恢复了中断任务",
    "TRIP_CHANGE_REQUESTED": "收到行程变更事件",
}

#: 排序用的来源优先级：同一时刻，先对话、再工具、再搜索、再里程碑、再汇总性的步骤。
_SOURCE_ORDER = {
    "message": 0,
    "tool_call": 1,
    "search": 2,
    "milestone": 3,
    "plan": 4,
    "approval": 5,
    "handoff": 6,
    "confirmation": 7,
    "reconciliation": 8,
}


def collect_task_steps(
    task: TripTask,
    *,
    events: Sequence[AuditEvent] = (),
    snapshots: Sequence[InventorySnapshot] = (),
) -> list[dict[str, Any]]:
    """把任务的每一步按先后整理成一条列表。只读，不改任务。"""
    snapshot_by_id = {item.snapshot_id: item for item in snapshots}
    raw: list[tuple[datetime | None, int, int, dict[str, Any]]] = []

    def add(kind: str, at: datetime | None, title: str, detail: dict[str, Any], *,
            status: str | None = None) -> None:
        raw.append(
            (
                _aware(at),
                _SOURCE_ORDER.get(kind, 9),
                len(raw),
                {"kind": kind, "at": _iso(at), "title": title, "status": status,
                 "detail": _json_safe(detail)},
            )
        )

    for message in task.messages:
        role = "旅行者" if message.role == "user" else "助手"
        add(
            "message",
            message.created_at,
            f"{role}说",
            {"role": message.role, "content": message.content},
        )

    for call in task.tool_calls:
        detail: dict[str, Any] = {
            "sequence": call.sequence,
            "tool_name": call.tool_name,
            "tool_kind": call.tool_kind,
        }
        if call.completed_at is not None and call.started_at is not None:
            detail["duration_ms"] = round(
                (_aware(call.completed_at) - _aware(call.started_at)).total_seconds() * 1000, 1
            )
        for field in ("retry_of", "reason_code", "error_code", "error_type"):
            value = getattr(call, field, None)
            if value is not None:
                detail[field] = value
        add(
            "tool_call",
            call.started_at,
            _TOOL_TITLES.get(call.tool_name, call.tool_name),
            detail,
            status=call.status.value if isinstance(call.status, Enum) else str(call.status),
        )

    last_search_at: datetime | None = None
    for search in task.searches:
        params = dict(search.parameters)
        snapshot = snapshot_by_id.get(search.snapshot_id)
        samples, count = _snapshot_samples(snapshot)
        if search.kind == "transport":
            title = f"搜索交通：{params.get('origin', '?')} → {params.get('destination', '?')}"
        else:
            title = f"搜索酒店：{params.get('city', '?')}"
        add(
            "search",
            search.captured_at,
            title,
            {
                "parameters": params,
                # 人说的和机器推的分开：出处是用户原话，假设是系统自己算的那一步。
                "date_evidence": search.date_evidence,
                "assumption": search.assumption,
                "snapshot_id": search.snapshot_id,
                "result_count": count,
                "samples": samples,
            },
            status="success",
        )
        at = _aware(search.captured_at)
        if at is not None and (last_search_at is None or at > last_search_at):
            last_search_at = at

    if task.options:
        add(
            "plan",
            last_search_at or _latest_tool_time(task),
            f"方案生成：{len(task.options)} 条",
            {
                "options": [
                    {
                        "option_id": option.option_id,
                        "route": " → ".join(
                            [option.legs[0].origin, *[leg.destination for leg in option.legs]]
                        )
                        if option.legs
                        else "",
                        "total_cost": option.total_cost,
                        "currency": option.currency,
                        "policy_outcome": option.policy_decision.outcome,
                        "hotel": option.stays[0].name if option.stays else None,
                    }
                    for option in task.options
                ],
                "open_questions": list(
                    (task.metadata.get("agentic_proposal") or {}).get("open_questions") or ()
                ),
            },
        )

    if task.approval is not None:
        approval = task.approval
        add(
            "approval",
            approval.created_at,
            "创建审批请求",
            {
                "approver_id": approval.approver_id,
                "status": approval.status,
                "business_reason": approval.business_reason,
                "violations": list(approval.violations),
                "approved_price": approval.approved_price,
                "expires_at": approval.expires_at,
                "decision_reason": approval.decision_reason,
            },
            status=str(getattr(approval.status, "value", approval.status)),
        )

    if task.booking_intent is not None:
        intent = task.booking_intent
        add(
            "handoff",
            getattr(intent, "created_at", None),
            "交接单就绪（不下单、不付款）",
            {
                "intent_id": getattr(intent, "intent_id", None),
                "status": getattr(intent, "status", None),
                "instructions": getattr(intent, "instructions", None),
            },
        )

    if task.booking_confirmation is not None:
        confirmation = task.booking_confirmation
        add(
            "confirmation",
            getattr(confirmation, "reported_at", None)
            or getattr(confirmation, "created_at", None),
            "旅行者回填订单（自述，不是回执）",
            {
                "order_references": list(getattr(confirmation, "order_references", ()) or ()),
                "total_amount": getattr(confirmation, "total_amount", None),
                "currency": getattr(confirmation, "currency", None),
                "reported_by": getattr(confirmation, "reported_by", None),
            },
        )

    if task.expense_reconciliation is not None:
        recon = task.expense_reconciliation
        detail = asdict(recon) if is_dataclass(recon) else {"value": recon}
        add(
            "reconciliation",
            getattr(recon, "reconciled_at", None) or getattr(recon, "created_at", None),
            "费控对账",
            detail,
            status=str(
                getattr(getattr(recon, "status", None), "value", getattr(recon, "status", None))
            ),
        )

    for event in events:
        title = _MILESTONE_TITLES.get(event.event_type)
        if title is None:
            continue
        add(
            "milestone",
            event.created_at,
            title,
            {
                "event_type": event.event_type,
                "actor_type": event.actor_type,
                "evidence_refs": list(event.evidence_refs),
            },
        )

    raw.sort(key=lambda item: (item[0] or datetime.max.replace(tzinfo=UTC), item[1], item[2]))
    steps = []
    for index, (_, _, _, step) in enumerate(raw, start=1):
        step["sequence"] = index
        steps.append(step)
    return steps


def _snapshot_samples(
    snapshot: InventorySnapshot | None,
) -> tuple[list[dict[str, Any]], int | None]:
    """从快照里取最多 3 条样例：让"搜到了什么"看得见，又不把整页塞满。"""
    if snapshot is None:
        return [], None
    items = list(snapshot.items)
    samples: list[dict[str, Any]] = []
    for item in items[:3]:
        if isinstance(item, TransportOffer):
            samples.append(
                {
                    "ref_id": item.ref_id,
                    "label": f"{item.origin} → {item.destination} · "
                    f"{item.depart_at:%m-%d %H:%M} 出发 {item.arrive_at:%H:%M} 到",
                    "price": item.price,
                    "currency": item.currency,
                }
            )
        elif isinstance(item, HotelOffer):
            samples.append(
                {
                    "ref_id": item.ref_id,
                    "label": f"{item.name} · 每晚",
                    "price": item.nightly_price,
                    "currency": item.currency,
                }
            )
    return samples, len(items)


def _latest_tool_time(task: TripTask) -> datetime | None:
    times = [
        _aware(call.completed_at or call.started_at)
        for call in task.tool_calls
        if call.completed_at is not None or call.started_at is not None
    ]
    return max(times) if times else None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware is not None else None


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        return list(value)
    return str(value)

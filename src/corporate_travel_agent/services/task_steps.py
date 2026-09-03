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
    "FLIGHT_STATUS_OBSERVED": "收到航班动态",
    "TRIP_CANCELLED": "差旅已取消",
    "CHANGE_MESSAGE_RECEIVED": "旅行者在已订行程上提出变更",
    "CHANGE_INTENT_QUESTION": "助手追问变更内容",
    "CHANGE_INTENT_RESOLVED": "变更请求已读出并落地",
    "CHANGE_MESSAGE_CARRIED": "改期任务带上了旅行者的原话",
}

#: 每种步骤由哪条函数链产生。这不是猜测，是代码里的真实调用路径；
#: 改了调用路径要同步改这张表，否则过程记录会说谎。
_STEP_FUNCTIONS = {
    "message.user.first": (
        "api.main.create_agentic_trip_task → "
        "TripWorkflowOrchestrator.create_task_from_agentic_message"
    ),
    "message.user.followup": (
        "api.main.submit_agentic_trip_message → TripWorkflowOrchestrator.submit_agentic_message"
    ),
    "message.assistant": (
        "TripWorkflowOrchestrator._pause_for_agentic_question（把模型的问题写进对话）"
    ),
    "loop_run": "ConversationLedger.render → ToolLoopRunner.run",
    "llm": (
        "ToolLoopRunner._model_turn → OpenAIToolCallingLanguageModel.next_turn"
        " → POST /chat/completions"
    ),
    "tool": "ToolLoopRunner._dispatch → ToolExecutor.{name}",
    "provider": "TripWorkflowOrchestrator._search_and_plan → TravelInventoryProvider.{name}",
    "search": "ToolExecutor.search_* → TravelInventoryProvider.search_* → InventorySnapshot",
    "plan": (
        "TripWorkflowOrchestrator._plan_from_tool_loop → ItineraryPlanner.plan"
        " → PolicyEngine.evaluate"
    ),
    "approval": "PlanningMixin.select_option → ApprovalMixin._new_approval",
    "handoff": "PlanningMixin._revalidate_selected → BookingIntent",
    "confirmation": "ConfirmationMixin.confirm_booking",
    "reconciliation": "ConfirmationMixin.reconcile_expense",
    "milestone": "RecordsMixin._audit（审计事件，只有时间和名目；内容在任务字段上）",
}

#: 排序用的来源优先级：同一时刻，先对话、再循环装配、再工具、再搜索、再里程碑、再汇总性的步骤。
_SOURCE_ORDER = {
    "message": 0,
    "loop_run": 1,
    "tool_call": 2,
    "search": 3,
    "milestone": 4,
    "plan": 5,
    "approval": 6,
    "handoff": 7,
    "confirmation": 8,
    "reconciliation": 9,
}


def collect_task_steps(
    task: TripTask,
    *,
    events: Sequence[AuditEvent] = (),
    snapshots: Sequence[InventorySnapshot] = (),
) -> list[dict[str, Any]]:
    """把任务的每一步按先后整理成一条列表。只读，不改任务。"""
    snapshot_by_id = {item.snapshot_id: item for item in snapshots}
    process_log: list[dict[str, Any]] = list(task.metadata.get("process_log") or ())
    llm_exchanges: list[dict[str, Any]] = [
        exchange for run in process_log for exchange in (run.get("llm_exchanges") or ())
    ]
    tool_exchanges: list[dict[str, Any]] = [
        exchange for run in process_log for exchange in (run.get("tool_exchanges") or ())
    ]
    raw: list[tuple[datetime | None, int, int, dict[str, Any]]] = []

    def add(kind: str, at: datetime | None, title: str, detail: dict[str, Any], *,
            status: str | None = None, function: str | None = None) -> None:
        raw.append(
            (
                _aware(at),
                _SOURCE_ORDER.get(kind, 10),
                len(raw),
                {"kind": kind, "at": _iso(at), "title": title, "status": status,
                 "function": function or _STEP_FUNCTIONS.get(kind),
                 "detail": _json_safe(detail)},
            )
        )

    user_seen = 0
    for message in task.messages:
        role = "旅行者" if message.role == "user" else "助手"
        if message.role == "user":
            user_seen += 1
            function: str | None = _STEP_FUNCTIONS[
                "message.user.first" if user_seen == 1 else "message.user.followup"
            ]
        else:
            function = _STEP_FUNCTIONS["message.assistant"]
        add(
            "message",
            message.created_at,
            f"{role}说",
            {"role": message.role, "content": message.content},
            function=function,
        )

    # 每一轮工具循环：对话怎么装配、上下文是什么、终局是什么；
    # 终局工具调用和被拒的调用（不进 ToolCallRecord）也完整躺在这里。
    for run in process_log:
        exchanges = run.get("tool_exchanges")
        # 成功的终局工具不进 transcript——它直接变成 outcome（kind/summary/refs 就是它的
        # 参数）。留在 transcript 里的终局调用都是**被拒的**（引用编造、缺字段……），
        # 和其它被拒调用一起单独列出来：模型试过什么、宿主拦了什么，都要看得见。
        refused = [
            exchange for exchange in (exchanges or ()) if not exchange.get("ok", True)
        ]
        add(
            "loop_run",
            datetime.fromisoformat(run["started_at"]) if run.get("started_at") else None,
            f"第 {run.get('run')} 轮工具循环：对话装配与终局",
            {
                "entry_function": run.get("entry_function"),
                "conversation_function": run.get("conversation_function"),
                "conversation": run.get("conversation"),
                "context": run.get("context"),
                "prompt_version": run.get("prompt_version"),
                "llm_call_count": len(run.get("llm_exchanges") or ()),
                "tool_exchange_count": None if exchanges is None else len(exchanges),
                "tool_exchanges_note": run.get("tool_exchanges_note"),
                "refused_exchanges": refused,
                "outcome": run.get("outcome"),
                "error": run.get("error"),
            },
            status="error" if run.get("error") else "success",
        )

    llm_cursor = 0
    tool_cursor = 0
    for call in task.tool_calls:
        detail: dict[str, Any] = {
            "sequence": call.sequence,
            "tool_name": call.tool_name,
            "tool_kind": call.tool_kind,
        }
        if call.completed_at is not None and call.started_at is not None:
            detail["duration_ms"] = round(
                (_as_aware(call.completed_at) - _as_aware(call.started_at)).total_seconds() * 1000,
                1,
            )
        for field in ("retry_of", "reason_code", "error_code", "error_type"):
            value = getattr(call, field, None)
            if value is not None:
                detail[field] = value
        function = None
        if call.tool_name == "llm.next_tool_call":
            # 按发生顺序一一对应：第 k 次模型调用记录 ↔ 适配器记下的第 k 笔完整往返。
            # 重试也各占一笔（失败那笔带 error），所以顺序对齐是成立的。
            if llm_cursor < len(llm_exchanges):
                exchange = llm_exchanges[llm_cursor]
                llm_cursor += 1
                detail["function"] = exchange.get("function")
                detail["api"] = exchange.get("api")
                detail["llm_request"] = exchange.get("request")
                if "response" in exchange:
                    detail["llm_response"] = exchange["response"]
                if "error" in exchange:
                    detail["llm_error"] = exchange["error"]
            function = _STEP_FUNCTIONS["llm"]
        elif call.tool_name.startswith("tool."):
            short = call.tool_name.removeprefix("tool.")
            # 只有非重试的记录消费一笔工具往返；往返里存的是参数原文和完整结果。
            if getattr(call, "retry_of", None) is None:
                probe = tool_cursor
                while probe < len(tool_exchanges) and tool_exchanges[probe].get("tool") != short:
                    probe += 1
                if probe < len(tool_exchanges):
                    exchange = tool_exchanges[probe]
                    tool_cursor = probe + 1
                    detail["arguments"] = exchange.get("arguments")
                    detail["ok"] = exchange.get("ok")
                    detail["result"] = exchange.get("result")
            function = _STEP_FUNCTIONS["tool"].replace("{name}", short)
        elif call.tool_name.startswith("provider."):
            function = _STEP_FUNCTIONS["provider"].replace(
                "{name}", call.tool_name.removeprefix("provider.")
            )
        add(
            "tool_call",
            call.started_at,
            _TOOL_TITLES.get(call.tool_name, call.tool_name),
            detail,
            status=call.status.value if isinstance(call.status, Enum) else str(call.status),
            function=function,
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
        milestone_title = _MILESTONE_TITLES.get(event.event_type)
        if milestone_title is None:
            continue
        add(
            "milestone",
            event.created_at,
            milestone_title,
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
    times: list[datetime] = []
    for call in task.tool_calls:
        stamp = call.completed_at or call.started_at
        if stamp is not None:
            times.append(_as_aware(stamp))
    return max(times) if times else None


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else _as_aware(value)


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

"""领域对象 → 公开 JSON 形状。每个函数产出的字典键集合由 `schemas.py` 里对应的响应模型钉住。

这里只做投影，不做判断：金额是 Decimal 就原样交出去（FastAPI 按响应模型序列化成数字），
枚举原样交出去（序列化成它的值）。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from corporate_travel_agent.agent.orchestrator import (
    PARTIAL_COVERAGE_METADATA_KEY,
    TripWorkflowOrchestrator,
)
from corporate_travel_agent.api.schemas import TripCreate
from corporate_travel_agent.domain.models import (
    AuditEvent,
    HotelOffer,
    InventorySnapshot,
    TransportOffer,
    Trip,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.planning.cost_guidance import CostGuidance, build_cost_guidance
from corporate_travel_agent.planning.preferences import duration_minutes_per_unit
from corporate_travel_agent.services.task_projections import TaskSummary


def to_request(payload: TripCreate, *, task_id: str, version: int) -> TripRequestVersion:
    """把 TripCreate 载荷转为 TripRequestVersion。"""
    return TripRequestVersion(
        task_id=task_id,
        version=version,
        traveler_id=payload.traveler_id,
        origin=payload.origin,
        destination=payload.destination,
        departure_after=payload.departure_after,
        arrive_by=payload.arrive_by,
        return_after=payload.return_after,
        return_before=payload.return_before,
        hotel_check_in=payload.hotel_check_in,
        hotel_check_out=payload.hotel_check_out,
        hard_constraints=tuple(payload.hard_constraints),
        soft_preferences=tuple(payload.soft_preferences),
        booking_scope=payload.booking_scope,
    )


def public_task_summary(item: TaskSummary) -> dict[str, Any]:
    """任务列表项的公开摘要。"""
    return {
        "task_id": item.task_id,
        "state": item.state,
        "employee_id": item.employee_id,
        "manager_id": item.manager_id,
        "policy_snapshot_id": item.policy_snapshot_id,
        "request_version": item.request_version,
        "selected_option_id": item.selected_option_id,
        "clarification_rounds": item.clarification_rounds,
        "option_count": item.option_count,
        "failure": item.failure,
        "provider_retry": {
            "next_retry_at": item.next_retry_at,
            "delayed_retry_count": item.delayed_retry_count,
        },
        "updated_at": item.updated_at,
        "pending_approver_id": item.pending_approver_id,
        "requester_id": item.requester_id,
        "summary": True,
    }


def public_task(workflow: TripWorkflowOrchestrator, task: TripTask) -> dict[str, Any]:
    """任务详情的公开视图（含方案、澄清题等）。"""
    return {
        "task_id": task.task_id,
        "intent_entrypoint": task.metadata.get("intent_entrypoint", "legacy"),
        "state": task.state,
        "traveler_id": task.employee.employee_id,
        # 谁发起的；代订时和旅行者不是同一个人。差标、审批、预算全看旅行者。
        "requester_id": task.requested_by,
        "is_delegated": task.is_delegated,
        # 属于哪趟差旅；改期任务还记它改的是哪个任务、因为哪条事件。
        "trip_id": task.trip_id,
        "parent_task_id": task.parent_task_id,
        "change_event_id": task.change_event_id,
        "is_change_task": task.is_change_task,
        "change_event": task.metadata.get("change_event"),
        "request_version": task.request.version if task.request else None,
        "booking_scope": (
            task.request.resolved_booking_scope
            if task.request is not None
            else task.intent_fields.get("booking_scope")
        ),
        "client_location": task.request.client_location if task.request else None,
        "commitments": (
            [asdict(item) for item in task.request.commitments]
            if task.request is not None
            else []
        ),
        "transport_legs": (
            [asdict(leg) for leg in task.request.transport_legs()]
            if task.request is not None
            else []
        ),
        "failure": task.failure,
        "failure_details": task.metadata.get("no_feasible_reasons", ()),
        "provider_retry": workflow.provider_retry_status(task),
        "coverage_notices": task.metadata.get(PARTIAL_COVERAGE_METADATA_KEY, ()),
        "intent_fields": task.intent_fields,
        "missing_required_fields": task.missing_required_fields,
        "conflicts": task.intent_conflicts,
        "assumptions": task.assumptions,
        "clarification_question": task.clarification_question,
        "clarification_questions": task.metadata.get("clarification_questions") or [],
        "uncertain_slots": task.metadata.get("uncertain_slots") or [],
        "clarification_rounds": task.clarification_rounds,
        "manipulation_detected": bool(task.metadata.get("manipulation_detected")),
        "tool_budget": {
            "limit": task.tool_call_limit,
            "used": task.tool_calls_used,
            "remaining": task.tool_calls_remaining,
            "blocked": task.state.value == "TOOL_BUDGET_EXHAUSTED",
            "calls": [asdict(item) for item in task.tool_calls],
        },
        "messages": [
            {"role": item.role, "content": item.content, "created_at": item.created_at}
            for item in task.messages
        ],
        "original_instruction": next(
            (item.content for item in task.messages if item.role == "user"),
            None,
        ),
        # 排序是怎么算出来的：把价格和时长折算到同一个分数上的那个比例，
        # 以及此刻真正生效的整程偏好。分数本身每条方案上已经有了，缺的是"分数怎么来的"。
        "scoring": (
            {
                "minutes_per_unit": duration_minutes_per_unit(task.request),
                "journey_preferences": sorted(task.request.journey_wide_preferences()),
            }
            if task.request is not None
            else None
        ),
        # 这趟任务用的习惯画像（每条习惯凭什么成立）；没接历史来源时是 null。
        "travel_profile": task.metadata.get("travel_profile"),
        # 规划那一刻钉住的预算余额；没接账本、没成本中心或政策没配预算时是 null。
        "budget_snapshot": task.metadata.get("budget_snapshot"),
        # 工具循环写给旅行者的那段话：推荐理由 + 还没定的事。它不进 task.messages。
        "agentic_proposal": task.metadata.get("agentic_proposal"),
        "extract_failure": task.metadata.get("extract_failure"),
        "model_fallback": task.metadata.get("model_fallback"),
        "options": [
            _public_option(item, guidance)
            for item, guidance in zip(task.options, _option_cost_guidance(task), strict=True)
        ],
        "selected_option_id": task.selected_option_id,
        "approval": asdict(task.approval) if task.approval else None,
        "booking_intent": asdict(task.booking_intent) if task.booking_intent else None,
        "booking_confirmation": public_booking_confirmation(task),
        # 费控对账结果；没对过账是 null。对上了，自述才算"核实过"。
        "expense_reconciliation": (
            {
                **asdict(task.expense_reconciliation),
                "amount_variance": (
                    task.expense_reconciliation.amount_variance(task.booking_confirmation)
                    if task.booking_confirmation is not None
                    else None
                ),
            }
            if task.expense_reconciliation is not None
            else None
        ),
        "summary": False,
    }


def _public_option(item: Any, guidance: CostGuidance) -> dict[str, Any]:
    return {
        "option_id": item.option_id,
        "version": item.version,
        "trip_request_version": item.trip_request_version,
        "inventory_snapshot_ids": item.inventory_snapshot_ids,
        "inventory_refs": item.inventory_refs,
        # 完整的有序航段列表。三段以上只有这里读得到——outbound / inbound 仍然照旧给。
        "legs": [public_transport_offer(leg) for leg in item.legs],
        # 这条方案要买几张票、各多少钱。**展示价格读这里**——整票只有一个价，记在第一段上。
        "fares": [{"fare_ref": ref, "total": total} for ref, total in item.fares],
        "outbound": public_transport_offer(item.outbound),
        "inbound": public_transport_offer(item.inbound) if item.inbound else None,
        # 完整的有序住宿列表，一站一条；hotel 仍然照旧给第一处。
        "stays": [public_hotel_offer(stay) for stay in item.stays],
        "hotel": public_hotel_offer(item.hotel) if item.hotel else None,
        "total_cost": item.total_cost,
        "total_duration_minutes": item.total_duration_minutes,
        "currency": item.currency,
        "feasibility": asdict(item.feasibility),
        "preference_penalty": item.preference_penalty,
        "score": item.score,
        "policy_outcome": item.policy_decision.outcome,
        "rule_evidence": [
            # `overage_amount` 是属性不是字段，`asdict` 带不出来；前端要的正是这个数。
            {**asdict(rule), "overage_amount": rule.overage_amount}
            for rule in item.policy_decision.evidence
        ],
        # 选它要付出什么：贵多少、超标多少、该谁批、换哪条能省。读时现算，不进持久化。
        "cost_guidance": asdict(guidance),
        "facts": item.explanation_facts,
    }


def public_booking_confirmation(task: TripTask) -> dict[str, Any] | None:
    """回填记录，外加"方案价多少 / 实付多少 / 差多少"。币种对不上时差额是 null，不替他换算。"""
    confirmation = task.booking_confirmation
    if confirmation is None:
        return None
    option = task.confirmed_option()
    return {
        **asdict(confirmation),
        "planned_total": option.total_cost if option is not None else None,
        "planned_currency": option.currency if option is not None else None,
        "cost_variance": task.booking_cost_variance(),
    }


def _option_cost_guidance(task: TripTask) -> tuple[CostGuidance, ...]:
    """审批人取员工快照上的直属经理——和真正创建审批单时同一个来源。"""
    return build_cost_guidance(task.options, approver_id=task.employee.manager_id)


def public_transport_offer(offer: TransportOffer) -> dict[str, Any]:
    """客户端要的行程字段，不带供应商内部字段。"""
    return {
        "ref_id": offer.ref_id,
        "snapshot_id": offer.snapshot_id,
        "provider": offer.provider,
        "mode": offer.mode,
        "origin": offer.origin,
        "destination": offer.destination,
        "depart_at": offer.depart_at,
        "arrive_at": offer.arrive_at,
        "price": offer.price,
        "seat_class": offer.seat_class,
        "available": offer.available,
        "is_direct": offer.is_direct,
        "currency": offer.currency,
        # 这一段属于哪张票。null = 它自己就是一张票；同一个 fare_ref 的几段是一张整票。
        "fare_ref": offer.fare_ref,
    }


def public_hotel_offer(offer: HotelOffer) -> dict[str, Any]:
    return {
        "ref_id": offer.ref_id,
        "snapshot_id": offer.snapshot_id,
        "provider": offer.provider,
        "name": offer.name,
        "city": offer.city,
        "check_in": offer.check_in,
        "check_out": offer.check_out,
        "nightly_price": offer.nightly_price,
        "nights": offer.nights,
        "total_price": offer.total_price,
        "commute_minutes": offer.commute_minutes,
        "commute_known": offer.commute_known,
        "available": offer.available,
        "currency": offer.currency,
    }


def public_snapshot(snapshot: InventorySnapshot) -> dict[str, Any]:
    """库存快照的对外脱敏序列化：原文只公开哈希、大小、保留期和访问策略。"""
    result = asdict(snapshot)
    raw_response = snapshot.raw_response
    result["raw_response"] = (
        {
            "archived": True,
            "sha256": raw_response.sha256,
            "size_bytes": raw_response.size_bytes,
            "content_type": raw_response.content_type,
            "stored_at": raw_response.stored_at,
            "retention_until": raw_response.retention_until,
            "access_policy": raw_response.access_policy,
        }
        if raw_response
        else {"archived": False}
    )
    return result


def public_trip(trip: Trip) -> dict[str, Any]:
    return {
        "trip_id": trip.trip_id,
        "traveler_id": trip.traveler_id,
        "requester_id": trip.requester_id,
        "status": trip.status.value,
        "task_ids": list(trip.task_ids),
        "created_at": trip.created_at,
        "watch": asdict(trip.watch) if trip.watch is not None else None,
        "events": [asdict(item) for item in trip.events],
    }


def public_audit_event(event: AuditEvent) -> dict[str, Any]:
    return asdict(event)

"""一趟差旅：聚合视图、变更事件、航班动态（手工 / 推送）、取消、观察 worker 的手动一轮。"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.api.deps import (
    CurrentIdentity,
    Runtime,
    bounded_limit,
    requester_id,
    require_admin,
    run,
    visible_trip,
)
from corporate_travel_agent.api.runtime import ApiRuntime
from corporate_travel_agent.api.schemas import (
    FlightStatusIn,
    FlightStatusObservationResponse,
    FlightStatusPush,
    FlightStatusWebhookResponse,
    TaskResponse,
    TripAggregateResponse,
    TripCancelRequest,
    TripEventRequest,
    TripWatchRunResponse,
)
from corporate_travel_agent.api.serializers import public_trip
from corporate_travel_agent.domain.enums import TripEventType
from corporate_travel_agent.domain.models import FlightStatusReport
from corporate_travel_agent.services.auth import Role
from corporate_travel_agent.services.repositories import ConcurrentUpdateError, NotFoundError

router = APIRouter(tags=["trips"])


@router.get("/trips", response_model=list[TripAggregateResponse])
def list_trip_aggregates(
    identity: CurrentIdentity, runtime: Runtime, limit: int = 100
) -> list[dict[str, Any]]:
    """我的差旅（我是旅行者或发起人）；管理员看全部。"""
    bounded_limit(limit)
    trips = runtime.workflow.trips
    if identity.has_role(Role.ADMIN):
        items = trips.list_all(limit=limit)
    elif identity.has_role(Role.EMPLOYEE) and identity.employee_id:
        items = trips.list_involving(identity.employee_id, limit=limit)
    else:
        items = ()
    return [public_trip(item) for item in items]


@router.get("/trips/{trip_id}", response_model=TripAggregateResponse)
def get_trip_aggregate(trip_id: str, identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    return public_trip(visible_trip(runtime, trip_id, identity))


@router.post("/trips/{trip_id}/events", response_model=TaskResponse)
def report_trip_event(
    trip_id: str, payload: TripEventRequest, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """报一条变更事件；系统开一个改期任务挂在这趟差旅下，原任务一个字不动。

    航变由管理员（代表航司/供应商推送）报；会议改期旅行者或发起人自己也能报。
    """
    trip = visible_trip(runtime, trip_id, identity)
    if payload.event_type is TripEventType.FLIGHT_CHANGED and not identity.has_role(Role.ADMIN):
        raise HTTPException(
            status_code=403, detail="Flight changes are reported by the carrier feed"
        )
    reported_by = requester_id(identity) or identity.user_id
    return run(
        runtime,
        lambda: runtime.workflow.report_trip_event(
            trip.trip_id,
            event_type=payload.event_type,
            ref_id=payload.ref_id,
            new_depart_at=payload.new_depart_at,
            new_arrive_by=payload.new_arrive_by,
            note=payload.note,
            reported_by=reported_by,
            leg_index=payload.leg_index,
        ),
    )


def _observe_flight_status(
    runtime: ApiRuntime, trip_id: str, payload: FlightStatusIn, *, reported_by: str
) -> dict[str, Any]:
    workflow = runtime.workflow
    report = FlightStatusReport(
        ref_id=payload.ref_id,
        status=payload.status,
        observed_at=payload.observed_at or workflow.clock(),
        source=payload.source,
        estimated_depart_at=payload.estimated_depart_at,
        estimated_arrive_at=payload.estimated_arrive_at,
        note=payload.note,
    )
    try:
        observation = workflow.observe_flight_status(trip_id, report, reported_by=reported_by)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ConcurrentUpdateError, WorkflowError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"observation": asdict(observation), "trip": public_trip(workflow.trips.get(trip_id))}


@router.post("/trips/{trip_id}/flight-status", response_model=FlightStatusObservationResponse)
def report_flight_status(
    trip_id: str, payload: FlightStatusIn, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """报一条航班动态（管理员，代表航司/供应商推送）。

    和 `POST /trips/{id}/events` 的区别：那边是"我要开一个改期任务"；这边只是"这张票现在
    这样"——先过确定性影响评估，取消 / 赶不上 / 接不上才开改期任务，延误但来得及只通知。
    """
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Flight status is reported by the carrier feed")
    trip = visible_trip(runtime, trip_id, identity)
    return _observe_flight_status(
        runtime, trip.trip_id, payload, reported_by=f"flight-status:{payload.source}"
    )


@router.post("/flight-status/webhook", response_model=FlightStatusWebhookResponse)
async def flight_status_webhook(request: Request, runtime: Runtime) -> dict[str, Any]:
    """企业侧 / TMC 推送航班动态：HMAC-SHA256 签名验身份，不走 Bearer。

    请求头 `X-Flight-Status-Signature` 是对原始请求体的 HMAC-SHA256 十六进制摘要。
    没配密钥就 503——不接受没法验身份的推送。
    """
    secret = runtime.flight_status_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Flight status webhook is not configured")
    body = await request.body()
    signature = request.headers.get("X-Flight-Status-Signature", "")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid flight status signature")
    try:
        payload = FlightStatusPush.model_validate_json(body)
    except ValidationError as exc:
        # 不带 input：坏 JSON 的 input 是 bytes，塞进 detail 会让 422 变成 500。
        raise HTTPException(
            status_code=422, detail=exc.errors(include_input=False, include_url=False)
        ) from exc
    status_in = FlightStatusIn(**payload.model_dump(exclude={"trip_id"}))
    reported_by = f"flight-status:{payload.source}"
    if payload.trip_id is not None:
        result = _observe_flight_status(
            runtime, payload.trip_id, status_in, reported_by=reported_by
        )
        return {"results": [result], "trip_count": 1}
    # 一班航班上可能坐着好几位旅行者：只带票号的推送对每一趟盯着它的差旅都算数。
    trips = runtime.workflow.trips.list_watching(payload.ref_id)
    if not trips:
        raise HTTPException(
            status_code=404, detail=f"No watched trip holds ticket {payload.ref_id}"
        )
    results = [
        _observe_flight_status(runtime, trip.trip_id, status_in, reported_by=reported_by)
        for trip in trips
    ]
    return {"results": results, "trip_count": len(results)}


@router.post("/trips/{trip_id}/cancel", response_model=TripAggregateResponse)
def cancel_trip(
    trip_id: str, payload: TripCancelRequest, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """旅行者、发起人或管理员取消整趟差旅：观察停止、看板不再显示，票由人去退改。"""
    trip = visible_trip(runtime, trip_id, identity)
    reported_by = requester_id(identity) or identity.user_id
    try:
        cancelled = runtime.workflow.cancel_trip(
            trip.trip_id, reported_by=reported_by, reason=payload.reason
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ConcurrentUpdateError, WorkflowError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return public_trip(cancelled)


@router.post("/trip-watch/run", response_model=TripWatchRunResponse)
def run_trip_watch(identity: CurrentIdentity, runtime: Runtime, limit: int = 20) -> dict[str, Any]:
    """管理员手动跑一轮差旅观察。生产里由 `examples/run_trip_watch_worker.py` 循环做同一件事。"""
    require_admin(identity)
    bounded_limit(limit, high=200)
    workflow = runtime.workflow
    processed = workflow.process_due_flight_checks(limit=limit)
    return {
        "processed": list(processed),
        "source": workflow.flight_status_source.name,
        "metrics": workflow.trip_watch_metrics(),
    }

"""当前生效政策快照的只读视图。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from corporate_travel_agent.api.deps import CurrentIdentity, Runtime
from corporate_travel_agent.api.schemas import PolicyResponse
from corporate_travel_agent.services.repositories import NotFoundError

router = APIRouter(tags=["policy"])


@router.get("/policy", response_model=PolicyResponse)
def active_policy(identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    """政策是公司规则而不是个人数据，对所有已认证身份可读；内容直接来自 PolicySnapshot。"""
    snapshot = runtime.workflow.policies.current()
    employee_level: str | None = None
    if identity.employee_id:
        try:
            employee_level = runtime.workflow.employees.snapshot(identity.employee_id).level
        except NotFoundError:
            employee_level = None
    return {
        "snapshot_id": snapshot.snapshot_id,
        "policy_version": snapshot.policy_version,
        "content_hash": snapshot.content_hash,
        "currency": snapshot.currency,
        "effective_from": snapshot.effective_from.isoformat(),
        "effective_to": snapshot.effective_to.isoformat() if snapshot.effective_to else None,
        "arrival_buffer_minutes": snapshot.arrival_buffer_minutes,
        "viewer_level": employee_level,
        "level_rules": [
            {
                "level": level,
                "allowed_flight_classes": sorted(rule.allowed_flight_classes),
                "allowed_train_classes": sorted(rule.allowed_train_classes),
            }
            for level, rule in sorted(snapshot.level_rules.items())
        ],
        "hotel_city_caps": [
            {"city": city, "nightly_cap": str(cap)}
            for city, cap in sorted(snapshot.hotel_city_caps.items())
        ],
        "exception_allowed_rule_ids": sorted(snapshot.exception_allowed_rule_ids),
        # 后加的三个维度。None / 空列表就是"这版政策没有这条规则"。
        "min_advance_booking_days": snapshot.min_advance_booking_days,
        "hotel_seasonal_caps": [
            {
                "city": item.city,
                "label": item.label,
                "season_from": item.season_from.isoformat(),
                "season_to": item.season_to.isoformat(),
                "nightly_cap": str(item.nightly_cap),
            }
            for item in snapshot.hotel_seasonal_caps
        ],
        "cost_center_budgets": [
            {
                "cost_center": item.cost_center,
                "amount": str(item.amount),
                "currency": item.currency,
                "period_from": item.period_from.isoformat(),
                "period_to": item.period_to.isoformat(),
            }
            for item in sorted(snapshot.cost_center_budgets.values(), key=lambda b: b.cost_center)
        ],
    }

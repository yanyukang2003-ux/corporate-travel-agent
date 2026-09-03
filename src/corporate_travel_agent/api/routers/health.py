"""健康检查：服务、模型、仓库、供应商重试与差旅观察的状态。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from corporate_travel_agent.api.deps import Runtime
from corporate_travel_agent.api.schemas import HealthResponse
from corporate_travel_agent.providers.base import ModeReporting

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(runtime: Runtime) -> dict[str, Any]:
    """健康检查必须一直可用：任何一块读不到都只降级，不抛错。"""
    workflow = runtime.workflow
    tasks = runtime.task_repository
    persistence_details: dict[str, Any] = {"backend": tasks.backend_name}
    status = "ok"
    from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository

    if isinstance(tasks, SQLAlchemyTaskRepository):
        try:
            persistence_details.update(tasks.operational_status())
        except Exception as exc:  # noqa: BLE001 - health must stay available
            persistence_details["error"] = str(exc)
            status = "degraded"
    outbox_status: dict[str, Any] = {"backend": runtime.outbox_store.backend_name}
    try:
        outbox_status["unpublished_count"] = runtime.outbox_store.unpublished_count()
    except Exception as exc:  # noqa: BLE001
        outbox_status["error"] = str(exc)
        status = "degraded"
    model = workflow.tool_calling_language_model
    provider = workflow.provider
    retry_policy = workflow.delayed_provider_retry_policy
    return {
        "status": status,
        "booking_capability": "disabled",
        # 只剩一条自然语言入口（工具循环）；`language_model` 这个键保留给前端和旧脚本读。
        "language_model": "configured" if model else "not_configured",
        "tool_calling_language_model": "configured" if model else "not_configured",
        "intent_entrypoints": {"structured": "/trip-tasks", "agentic": "/agentic/trip-tasks"},
        "language_model_status": workflow.llm_runtime_status if model else "not_configured",
        "language_model_ready": bool(
            model and workflow.llm_runtime_status not in {"billing_blocked", "auth_failed"}
        ),
        "language_model_fallback": workflow.fallback_model,
        "travel_provider": provider.name,
        "travel_provider_mode": (
            provider.provider_mode if isinstance(provider, ModeReporting) else "deterministic"
        ),
        "persistence": tasks.backend_name,
        "persistence_details": persistence_details,
        "outbox": outbox_status,
        "raw_response_store": runtime.raw_response_store.backend_name,
        "authentication": "enabled" if runtime.auth_service.enabled else "disabled",
        "policy_config": runtime.policy_configuration.source_type,
        "policy_config_version": runtime.policy_configuration.config.config_version,
        "active_policy_snapshot": runtime.policy_configuration.active_policy.snapshot_id,
        "policy_config_sha256": runtime.policy_configuration.sha256,
        "provider_resilience": {
            "immediate_attempts": workflow.max_provider_attempts,
            "circuit": workflow.provider_circuit_breaker.snapshot(),
            "max_delayed_retries": retry_policy.max_attempts,
            "delayed_retry_seconds": list(
                retry_policy.delays_seconds[: retry_policy.max_attempts]
            ),
            "process_role": runtime.process_role,
            "retry_worker_enabled": runtime.provider_retry_scheduler is not None,
            "retry_lease_seconds": workflow.provider_retry_lease_duration.total_seconds(),
            "retry_metrics": workflow.provider_retry_metrics(),
        },
        "trip_watch": {
            "source": workflow.flight_status_source.name,
            # 动态源可以自报"配好了没有"（真实适配器缺凭证时为 False）；没这个属性就当配好了。
            "source_configured": bool(getattr(workflow.flight_status_source, "configured", True)),
            "worker_enabled": runtime.trip_watch_scheduler is not None,
            "poll_seconds": runtime.settings.trip_watch_poll_seconds,
            "lease_seconds": workflow.trip_watch_lease_duration.total_seconds(),
            "lookahead_hours": workflow.trip_watch_lookahead_hours,
            "min_connection_minutes": workflow.min_connection_minutes,
            "delay_notice_minutes": workflow.delay_notice_minutes,
            "webhook_configured": bool(runtime.flight_status_webhook_secret),
            "metrics": workflow.trip_watch_metrics(),
        },
    }

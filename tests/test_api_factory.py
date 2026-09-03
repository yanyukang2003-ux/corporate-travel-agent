"""应用工厂：同一个进程里装配两套互不相干的应用，不经环境变量。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from corporate_travel_agent.api.app import create_app
from corporate_travel_agent.api.runtime import build_runtime
from corporate_travel_agent.api.settings import ApiSettings
from corporate_travel_agent.demo import DEMO_CLOCK, make_demo_request


def _app(settings: ApiSettings) -> TestClient:
    runtime = build_runtime(settings, clock=lambda: DEMO_CLOCK)
    return TestClient(create_app(runtime))


def test_two_apps_in_one_process_do_not_share_state() -> None:
    first = _app(ApiSettings())
    second = _app(ApiSettings(process_role="all"))

    created = first.app.state.runtime.workflow.create_task(make_demo_request(task_id="factory-1"))
    assert first.get(f"/trip-tasks/{created.task_id}").status_code == 200
    assert second.get(f"/trip-tasks/{created.task_id}").status_code == 404

    assert first.get("/health").json()["provider_resilience"]["process_role"] == "api"
    assert second.get("/health").json()["provider_resilience"]["process_role"] == "all"
    # worker 角色装了调度器，但 lifespan 没跑之前不会起线程。
    assert first.app.state.runtime.provider_retry_scheduler is None
    assert second.app.state.runtime.provider_retry_scheduler is not None


def test_settings_reject_the_same_misconfigurations_as_before() -> None:
    import pytest

    with pytest.raises(RuntimeError, match="PROCESS_ROLE"):
        ApiSettings(process_role="cron")
    with pytest.raises(RuntimeError, match="DATABASE_AUTO_CREATE"):
        ApiSettings(database_auto_create=True, environment="production")
    with pytest.raises(RuntimeError, match="PROVIDER_DELAYED_RETRY_SECONDS"):
        ApiSettings(max_delayed_provider_attempts=3, delayed_provider_retry_seconds=(60.0,))


def test_settings_come_from_a_mapping_not_only_the_process_environment() -> None:
    settings = ApiSettings.from_environment(
        {
            "PROCESS_ROLE": "worker",
            "TRIP_WATCH_LOOKAHEAD_HOURS": "72",
            "FLIGHT_STATUS_WEBHOOK_SECRET": "s3cret",
            "CORS_ALLOW_ORIGINS": "https://a.example, https://b.example",
        }
    )
    assert settings.process_role == "worker"
    assert settings.runs_workers
    assert settings.trip_watch_lookahead_hours == 72
    assert settings.flight_status_webhook_secret == "s3cret"
    assert settings.cors_allow_origins == ("https://a.example", "https://b.example")

"""API 层的配置：从环境变量读一次，之后整个进程只认这个对象。

`ApiSettings` 只覆盖 API 装配自己的旋钮（数据库、进程角色、重试与观察节奏、发件箱投递、CORS、
模型接入、入站推送密钥）。供应商（`TRAVEL_PROVIDER`）、政策配置（`POLICY_CONFIG_*`）、鉴权
（`AUTH_*`）、航班动态源（`FLIGHT_STATUS_SOURCE`）各自有 `*_from_environment()` 工厂，变量表在
各自模块里；`runtime.build_runtime` 按同一份环境把它们装起来。

测试和脚本可以不经环境变量直接构造 `ApiSettings(...)`，再交给 `create_app`：同一个进程里可以
装配两套互不相干的应用。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from corporate_travel_agent.services.runtime_config import validate_deployment_environment
from corporate_travel_agent.services.trip_watch import (
    DEFAULT_TRIP_WATCH_LEASE_SECONDS,
    DEFAULT_TRIP_WATCH_POLL_SECONDS,
)

PROCESS_ROLES = frozenset({"api", "worker", "all"})
PRODUCTION_LIKE_ENVIRONMENTS = frozenset({"production", "prod", "staging"})


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """API 进程的全部旋钮。字段默认值就是零配置本机演示的取值。"""

    database_url: str | None = None
    database_auto_create: bool = False
    environment: str = "development"
    #: `api` 只开 HTTP；`worker` 只跑延迟重试和差旅观察；`all` 两者都在一个进程里。
    process_role: str = "api"
    cors_allow_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    #: 有没有配模型 API Key。没配就不装模型端口，自然语言入口返回 503。
    openai_api_key_configured: bool = False
    openai_model: str = "gpt-5.6"
    openai_timeout_seconds: float = 60.0
    raw_response_store_dir: str | None = None
    raw_response_retention_days: int = 90
    provider_circuit_open_seconds: float = 60.0
    max_delayed_provider_attempts: int = 3
    delayed_provider_retry_seconds: tuple[float, ...] = (60.0, 180.0, 600.0)
    provider_retry_poll_seconds: float = 5.0
    provider_retry_worker_id: str | None = None
    provider_retry_lease_seconds: float = 900.0
    max_concurrent_llm_calls: int = 8
    max_concurrent_provider_calls: int = 16
    tool_acquire_timeout_seconds: float = 5.0
    interrupted_task_stale_seconds: float = 30.0
    trip_watch_poll_seconds: float = DEFAULT_TRIP_WATCH_POLL_SECONDS
    trip_watch_lease_seconds: float = DEFAULT_TRIP_WATCH_LEASE_SECONDS
    trip_watch_lookahead_hours: int = 48
    trip_change_min_connection_minutes: int = 60
    flight_delay_notice_minutes: int = 15
    trip_watch_worker_id: str | None = None
    #: 入站航班动态推送的 HMAC 密钥；没配就拒收推送（503）。
    flight_status_webhook_secret: str | None = None
    outbox_webhook_url: str | None = None
    outbox_webhook_secret: str | None = None
    outbox_max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.process_role not in PROCESS_ROLES:
            raise RuntimeError("PROCESS_ROLE must be api, worker, or all")
        production_like = self.environment.casefold() in PRODUCTION_LIKE_ENVIRONMENTS
        if self.database_auto_create and production_like:
            raise RuntimeError(
                "DATABASE_AUTO_CREATE is forbidden when ENVIRONMENT is production/staging; "
                "run 'alembic upgrade head' instead"
            )
        if not 1 <= self.raw_response_retention_days <= 3650:
            raise RuntimeError("RAW_RESPONSE_RETENTION_DAYS must be between 1 and 3650")
        if not 0 <= self.provider_circuit_open_seconds <= 3600:
            raise RuntimeError("PROVIDER_CIRCUIT_OPEN_SECONDS must be between 0 and 3600")
        if not 0 <= self.max_delayed_provider_attempts <= 3:
            raise RuntimeError("MAX_DELAYED_PROVIDER_RETRIES must be between 0 and 3")
        if len(self.delayed_provider_retry_seconds) < self.max_delayed_provider_attempts or any(
            value < 0 for value in self.delayed_provider_retry_seconds
        ):
            raise RuntimeError("PROVIDER_DELAYED_RETRY_SECONDS must cover every delayed retry")
        if self.provider_retry_poll_seconds <= 0:
            raise RuntimeError("PROVIDER_RETRY_POLL_SECONDS must be greater than zero")
        if self.trip_watch_poll_seconds <= 0:
            raise RuntimeError("TRIP_WATCH_POLL_SECONDS must be greater than zero")
        if not 1 <= self.trip_watch_lease_seconds <= 3600:
            raise RuntimeError("TRIP_WATCH_LEASE_SECONDS must be between 1 and 3600")
        if not 1 <= self.trip_watch_lookahead_hours <= 336:
            raise RuntimeError("TRIP_WATCH_LOOKAHEAD_HOURS must be between 1 and 336")
        if not 0 <= self.trip_change_min_connection_minutes <= 1440:
            raise RuntimeError("TRIP_CHANGE_MIN_CONNECTION_MINUTES must be between 0 and 1440")
        if not 0 <= self.flight_delay_notice_minutes <= 1440:
            raise RuntimeError("FLIGHT_DELAY_NOTICE_MINUTES must be between 0 and 1440")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ApiSettings:
        """按环境变量构造；变量名和取值范围与 `.env.example` 一致。

        先跑部署环境校验（production / staging 缺数据库、鉴权、原响应存储时直接拒绝启动），
        再逐个读值。读不出数字的变量按 `ValueError` 抛出，不做静默回退。
        """
        env = os.environ if environ is None else environ
        validate_deployment_environment(env)

        def text(name: str) -> str | None:
            value = env.get(name)
            return value.strip() if value and value.strip() else None

        def flag(name: str) -> bool:
            return (env.get(name) or "false").strip().casefold() == "true"

        origins = tuple(
            item.strip()
            for item in (
                env.get("CORS_ALLOW_ORIGINS") or "http://127.0.0.1:5173,http://localhost:5173"
            ).split(",")
            if item.strip()
        )
        schedule = tuple(
            float(value.strip())
            for value in (env.get("PROVIDER_DELAYED_RETRY_SECONDS") or "60,180,600").split(",")
            if value.strip()
        )
        return cls(
            database_url=text("DATABASE_URL"),
            database_auto_create=flag("DATABASE_AUTO_CREATE"),
            environment=(env.get("ENVIRONMENT") or "development").strip() or "development",
            process_role=(env.get("PROCESS_ROLE") or "api").strip().casefold(),
            cors_allow_origins=origins,
            openai_api_key_configured=bool(text("OPENAI_API_KEY")),
            openai_model=text("OPENAI_MODEL") or "gpt-5.6",
            openai_timeout_seconds=float(env.get("OPENAI_TIMEOUT_SECONDS") or "60"),
            raw_response_store_dir=text("RAW_RESPONSE_STORE_DIR"),
            raw_response_retention_days=int(env.get("RAW_RESPONSE_RETENTION_DAYS") or "90"),
            provider_circuit_open_seconds=float(env.get("PROVIDER_CIRCUIT_OPEN_SECONDS") or "60"),
            max_delayed_provider_attempts=int(env.get("MAX_DELAYED_PROVIDER_RETRIES") or "3"),
            delayed_provider_retry_seconds=schedule,
            provider_retry_poll_seconds=float(env.get("PROVIDER_RETRY_POLL_SECONDS") or "5"),
            provider_retry_worker_id=text("PROVIDER_RETRY_WORKER_ID"),
            provider_retry_lease_seconds=float(env.get("PROVIDER_RETRY_LEASE_SECONDS") or "900"),
            max_concurrent_llm_calls=int(env.get("MAX_CONCURRENT_LLM_CALLS") or "8"),
            max_concurrent_provider_calls=int(env.get("MAX_CONCURRENT_PROVIDER_CALLS") or "16"),
            tool_acquire_timeout_seconds=float(env.get("TOOL_ACQUIRE_TIMEOUT_SECONDS") or "5"),
            interrupted_task_stale_seconds=float(
                env.get("INTERRUPTED_TASK_STALE_SECONDS") or "30"
            ),
            trip_watch_poll_seconds=float(
                env.get("TRIP_WATCH_POLL_SECONDS") or str(DEFAULT_TRIP_WATCH_POLL_SECONDS)
            ),
            trip_watch_lease_seconds=float(
                env.get("TRIP_WATCH_LEASE_SECONDS") or str(DEFAULT_TRIP_WATCH_LEASE_SECONDS)
            ),
            trip_watch_lookahead_hours=int(env.get("TRIP_WATCH_LOOKAHEAD_HOURS") or "48"),
            trip_change_min_connection_minutes=int(
                env.get("TRIP_CHANGE_MIN_CONNECTION_MINUTES") or "60"
            ),
            flight_delay_notice_minutes=int(env.get("FLIGHT_DELAY_NOTICE_MINUTES") or "15"),
            trip_watch_worker_id=text("TRIP_WATCH_WORKER_ID"),
            flight_status_webhook_secret=text("FLIGHT_STATUS_WEBHOOK_SECRET"),
            outbox_webhook_url=text("OUTBOX_WEBHOOK_URL"),
            outbox_webhook_secret=text("OUTBOX_WEBHOOK_SECRET"),
            outbox_max_attempts=int(env.get("OUTBOX_MAX_ATTEMPTS") or "5"),
        )

    @property
    def runs_workers(self) -> bool:
        return self.process_role in {"worker", "all"}

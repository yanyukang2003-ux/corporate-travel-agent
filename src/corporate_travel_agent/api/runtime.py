"""API 进程的装配：一份 `ApiSettings` 进来，一个 `ApiRuntime` 出去。

以前这些逻辑散在 `api/main.py` 的模块顶层——import 那一刻就读环境变量、连数据库、建编排器，
同一个进程装不了第二套配置，测试只能先设环境变量再 import。现在装配是一个函数，返回值是一个
普通对象，路由通过 `app.state.runtime` 拿到它。

**演示库存只在这里的一处出现**：没配外部供应商（`TRAVEL_PROVIDER=mock`）时，从 `demo` 模块
借那份冻结库存；编排器本身在这里直接构造，不再经 `build_demo_system`。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine

from corporate_travel_agent.agent.orchestrator import TripWorkflowOrchestrator
from corporate_travel_agent.agent.ports import LanguageModelError
from corporate_travel_agent.agent.tool_loop_adapter import OpenAIToolCallingLanguageModel
from corporate_travel_agent.api.settings import ApiSettings
from corporate_travel_agent.providers.base import Closeable, TravelInventoryProvider
from corporate_travel_agent.providers.factory import travel_provider_from_environment
from corporate_travel_agent.providers.flight_status import (
    FlightStatusPort,
    flight_status_source_from_environment,
)
from corporate_travel_agent.services.auth import AuthService
from corporate_travel_agent.services.budget_ledger import RepositoryTripBudgetLedger
from corporate_travel_agent.services.db_engine import EngineBound
from corporate_travel_agent.services.expense_reconciliation import (
    ExpenseRecordStore,
    InMemoryExpenseRecordStore,
)
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.object_storage import (
    InMemoryRawResponseObjectStore,
    LocalRawResponseObjectStore,
    RawResponseObjectStore,
)
from corporate_travel_agent.services.outbox_dispatch import (
    LoggingChannel,
    OutboxChannel,
    OutboxDispatcher,
    WebhookChannel,
)
from corporate_travel_agent.services.outbox_events import OutboxStore
from corporate_travel_agent.services.policy_config import (
    LoadedPolicyConfiguration,
    load_policy_configuration_from_environment,
)
from corporate_travel_agent.services.provider_quote_context import (
    InMemoryProviderQuoteContextStore,
    ProviderQuoteContextStore,
)
from corporate_travel_agent.services.provider_resilience import (
    ProviderCircuitStateStore,
    ProviderRetryScheduler,
)
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    InMemoryTaskRepository,
    TaskRepository,
)
from corporate_travel_agent.services.trip_watch import TripWatchScheduler
from corporate_travel_agent.services.trips import (
    InMemoryTripRepository,
    SQLAlchemyTripRepository,
    TripRepository,
)

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ApiRuntime:
    """一个 API 进程装配好的全部协作对象。路由只认它，不认模块全局。"""

    settings: ApiSettings
    workflow: TripWorkflowOrchestrator
    task_repository: TaskRepository
    auth_service: AuthService
    policy_configuration: LoadedPolicyConfiguration
    raw_response_store: RawResponseObjectStore
    quote_context_store: ProviderQuoteContextStore
    outbox_store: OutboxStore
    outbox_dispatcher: OutboxDispatcher
    expense_store: ExpenseRecordStore
    provider_retry_scheduler: ProviderRetryScheduler | None
    trip_watch_scheduler: TripWatchScheduler | None
    #: 入站航班动态推送的密钥。可变：测试按需装上、拆掉，不必重装整个进程。
    flight_status_webhook_secret: str | None

    @property
    def process_role(self) -> str:
        return self.settings.process_role

    def start(self) -> None:
        """启动后台调度器（只有 worker / all 角色有）。"""
        if self.provider_retry_scheduler is not None:
            self.provider_retry_scheduler.start()
        if self.trip_watch_scheduler is not None:
            self.trip_watch_scheduler.start()

    def stop(self) -> None:
        """停调度器、等在途调用 drain，然后释放供应商连接和数据库连接池。"""
        retry_worker_drained = True
        if self.trip_watch_scheduler is not None:
            self.trip_watch_scheduler.stop()
        if self.provider_retry_scheduler is not None:
            retry_worker_drained = self.provider_retry_scheduler.stop()
        if not retry_worker_drained:
            return
        provider = self.workflow.provider
        if isinstance(provider, Closeable):
            provider.close()
        tasks = self.task_repository
        if isinstance(tasks, EngineBound):
            from corporate_travel_agent.services.sqlalchemy_repository import (
                SQLAlchemyTaskRepository,
            )

            if isinstance(tasks, SQLAlchemyTaskRepository):
                tasks.dispose()


def build_runtime(
    settings: ApiSettings,
    *,
    task_repository: TaskRepository | None = None,
    provider: TravelInventoryProvider | None = None,
    tool_calling_language_model: Any | None = None,
    auth_service: AuthService | None = None,
    policy_configuration: LoadedPolicyConfiguration | None = None,
    flight_status_source: FlightStatusPort | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ApiRuntime:
    """按配置装配整套运行时。关键字参数是给测试和脚本替换真实依赖用的。

    `tool_calling_language_model` 传 None 且配置里有 API Key 时，按配置装 OpenAI 兼容适配器；
    要显式"不装模型"就把 `settings.openai_api_key_configured` 设成 False。
    """
    effective_clock = clock or (lambda: datetime.now(UTC))
    raw_response_store = _raw_response_store(settings)
    auth = auth_service or AuthService.from_environment()
    policy = policy_configuration or load_policy_configuration_from_environment()
    tasks = task_repository or _task_repository(settings)
    engine = tasks.engine if isinstance(tasks, EngineBound) else None
    quote_context_store = _quote_context_store(engine)
    active_policy = next(
        (
            item
            for item in policy.policy_snapshots
            if item.snapshot_id == policy.config.active_policy_snapshot_id
        ),
        policy.policy_snapshots[0] if policy.policy_snapshots else None,
    )
    inventory_provider = provider or travel_provider_from_environment(
        raw_response_store=raw_response_store,
        policy_currency=active_policy.currency if active_policy is not None else None,
        quote_context_store=quote_context_store,
    )
    if inventory_provider is None:
        # 没配外部供应商：借演示模块那份冻结库存。演示装配只剩这一个入口。
        from corporate_travel_agent.demo import build_demo_provider

        inventory_provider = build_demo_provider(
            clock=effective_clock,
            raw_response_store=raw_response_store,
            raw_response_retention_days=settings.raw_response_retention_days,
        )
    model = (
        tool_calling_language_model
        if tool_calling_language_model is not None
        else _tool_calling_language_model(settings)
    )
    trips: TripRepository = (
        SQLAlchemyTripRepository(engine) if engine is not None else InMemoryTripRepository()
    )
    workflow = TripWorkflowOrchestrator(
        tasks=tasks,
        employees=InMemoryEmployeeDirectory(list(policy.employee_snapshots)),
        policies=InMemoryPolicyRepository(
            policy.policy_snapshots,
            current_snapshot_id=policy.config.active_policy_snapshot_id,
        ),
        provider=inventory_provider,
        budget_ledger=RepositoryTripBudgetLedger(tasks),
        trips=trips,
        tool_calling_language_model=model,
        clock=effective_clock,
        provider_circuit_open_seconds=settings.provider_circuit_open_seconds,
        provider_circuit_store=_provider_circuit_store(engine),
        max_delayed_provider_attempts=settings.max_delayed_provider_attempts,
        delayed_provider_retry_seconds=settings.delayed_provider_retry_seconds,
        max_concurrent_llm_calls=settings.max_concurrent_llm_calls,
        max_concurrent_provider_calls=settings.max_concurrent_provider_calls,
        tool_acquire_timeout_seconds=settings.tool_acquire_timeout_seconds,
        interrupted_task_stale_seconds=settings.interrupted_task_stale_seconds,
        provider_retry_worker_id=settings.provider_retry_worker_id,
        provider_retry_lease_seconds=settings.provider_retry_lease_seconds,
        timezone_name=policy.config.timezone_name,
        city_normalizer=CityNormalizer(policy.city_aliases),
        flight_status_source=flight_status_source or flight_status_source_from_environment(),
        trip_watch_worker_id=settings.trip_watch_worker_id,
        trip_watch_lease_seconds=settings.trip_watch_lease_seconds,
        trip_watch_lookahead_hours=settings.trip_watch_lookahead_hours,
        min_connection_minutes=settings.trip_change_min_connection_minutes,
        delay_notice_minutes=settings.flight_delay_notice_minutes,
    )
    outbox_store = _outbox_store(tasks, engine)
    return ApiRuntime(
        settings=settings,
        workflow=workflow,
        task_repository=tasks,
        auth_service=auth,
        policy_configuration=policy,
        raw_response_store=raw_response_store,
        quote_context_store=quote_context_store,
        outbox_store=outbox_store,
        outbox_dispatcher=_outbox_dispatcher(settings, outbox_store),
        expense_store=_expense_store(engine),
        # 差旅观察 worker 和延迟重试一样只在 worker / all 角色里起；没接动态源时它每轮空转。
        provider_retry_scheduler=(
            ProviderRetryScheduler(workflow, poll_seconds=settings.provider_retry_poll_seconds)
            if settings.runs_workers
            else None
        ),
        trip_watch_scheduler=(
            TripWatchScheduler(workflow, poll_seconds=settings.trip_watch_poll_seconds)
            if settings.runs_workers
            else None
        ),
        flight_status_webhook_secret=settings.flight_status_webhook_secret,
    )


# -- 各部件 ---------------------------------------------------------------------


def _task_repository(settings: ApiSettings) -> TaskRepository:
    """按 DATABASE_URL 选择内存或 SQLAlchemy 任务仓库。"""
    if not settings.database_url:
        return InMemoryTaskRepository()
    from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository

    repository = SQLAlchemyTaskRepository(settings.database_url)
    if settings.database_auto_create:
        repository.create_schema()
    repository.check_connection()
    repository.check_schema()
    return repository


def _tool_calling_language_model(settings: ApiSettings) -> OpenAIToolCallingLanguageModel | None:
    """工具循环的模型端口：每轮只让模型挑一个工具，不让它一次交出整个结构体。"""
    if not settings.openai_api_key_configured:
        return None
    try:
        return OpenAIToolCallingLanguageModel(
            model=settings.openai_model,
            request_timeout_seconds=settings.openai_timeout_seconds,
        )
    except LanguageModelError:
        return None


def _raw_response_store(settings: ApiSettings) -> RawResponseObjectStore:
    if not settings.raw_response_store_dir:
        return InMemoryRawResponseObjectStore()
    return LocalRawResponseObjectStore(settings.raw_response_store_dir)


def _quote_context_store(engine: Engine | None) -> ProviderQuoteContextStore:
    if engine is None:
        return InMemoryProviderQuoteContextStore()
    from corporate_travel_agent.services.sqlalchemy_repository import (
        SQLAlchemyProviderQuoteContextStore,
    )

    return SQLAlchemyProviderQuoteContextStore(engine)


def _provider_circuit_store(engine: Engine | None) -> ProviderCircuitStateStore | None:
    """熔断状态存储：和任务仓储同一个引擎，多实例共用；内存仓储时每个进程一份。"""
    if engine is None:
        return None
    from corporate_travel_agent.services.sqlalchemy_repository import (
        SQLAlchemyProviderCircuitStore,
    )

    return SQLAlchemyProviderCircuitStore(engine)


def _outbox_store(tasks: TaskRepository, engine: Engine | None) -> OutboxStore:
    """发件箱和任务仓储同一个地方：内存仓储自带的那份，或同一个 SQL 引擎。"""
    if isinstance(tasks, InMemoryTaskRepository):
        return tasks.outbox
    if engine is None:
        raise RuntimeError("task repository has neither an in-memory outbox nor a SQL engine")
    from corporate_travel_agent.services.outbox import SQLAlchemyOutboxStore

    return SQLAlchemyOutboxStore(engine)


def _expense_store(engine: Engine | None) -> ExpenseRecordStore:
    if engine is None:
        return InMemoryExpenseRecordStore()
    from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyExpenseRecordStore

    return SQLAlchemyExpenseRecordStore(engine)


def _outbox_dispatcher(settings: ApiSettings, store: OutboxStore) -> OutboxDispatcher:
    """配了 OUTBOX_WEBHOOK_URL 就 POST 给企业侧，否则记日志。至少一次投递，对面按 event_id 幂等。"""
    channel: OutboxChannel = (
        WebhookChannel(settings.outbox_webhook_url, secret=settings.outbox_webhook_secret)
        if settings.outbox_webhook_url
        else LoggingChannel()
    )
    return OutboxDispatcher(
        store, default_channel=channel, max_attempts=settings.outbox_max_attempts
    )

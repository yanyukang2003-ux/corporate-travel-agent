"""demo：组装可运行的演示用 Orchestrator + Mock Provider（库存与策略已预置）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.orchestrator import (
    MAX_PROVIDER_ATTEMPTS,
    TripWorkflowOrchestrator,
)
from corporate_travel_agent.agent.ports import (
    WorkflowTraceObserverPort,
)
from corporate_travel_agent.domain.enums import TransportMode
from corporate_travel_agent.domain.models import HotelOffer, TransportOffer, TripRequestVersion
from corporate_travel_agent.providers.base import TravelInventoryProvider
from corporate_travel_agent.providers.flight_status import FlightStatusPort
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.budget_ledger import (
    BudgetLedgerPort,
    RepositoryTripBudgetLedger,
)
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.object_storage import RawResponseObjectStore
from corporate_travel_agent.services.policy_config import (
    LoadedPolicyConfiguration,
    load_policy_configuration,
)
from corporate_travel_agent.services.provider_resilience import (
    DEFAULT_CIRCUIT_OPEN_SECONDS,
    DEFAULT_DELAYED_PROVIDER_RETRY_SECONDS,
    DEFAULT_MAX_DELAYED_PROVIDER_ATTEMPTS,
    ProviderCircuitStateStore,
)
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    InMemoryTaskRepository,
    TaskRepository,
)
from corporate_travel_agent.services.travel_profile import TripHistoryPort
from corporate_travel_agent.services.trips import InMemoryTripRepository, TripRepository

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


# 演示库存是冻结的：航班/列车都在 2026-08-05。可行性校验现在会拒绝"已经起飞"的
# 班次，所以任何**使用这份冻结库存**的调用方都必须把时钟设在它之前，否则真实时间
# 一旦越过 2026-08-05，整份演示数据就全部不可行了。
# 接真实 Provider 的调用方不受影响：它们不用这份库存，应当继续用真实时钟。
DEMO_CLOCK = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)


def build_demo_provider(
    *,
    clock: Callable[[], datetime] | None = None,
    raw_response_store: RawResponseObjectStore | None = None,
    raw_response_retention_days: int = 90,
) -> MockProvider:
    """演示库存：北京—上海 2026-08-05 前后的几班机票 / 高铁和两家酒店，冻结不变。

    没配外部供应商时，API 运行时（`api/runtime.py`）也从这里借这份库存；这是演示装配
    留给产品代码的唯一入口，编排器本身不再经 `build_demo_system` 装。
    """
    effective_clock = clock or (lambda: datetime.now(UTC))
    transports = [
        _transport(
            "CA-EVE",
            TransportMode.FLIGHT,
            "Beijing",
            "Shanghai",
            datetime(2026, 8, 4, 19, 30, tzinfo=SHANGHAI_TZ),
            datetime(2026, 8, 4, 21, 50, tzinfo=SHANGHAI_TZ),
            "980",
            "ECONOMY",
        ),
        _transport(
            "MU-EARLY",
            TransportMode.FLIGHT,
            "Beijing",
            "Shanghai",
            datetime(2026, 8, 5, 6, 20, tzinfo=SHANGHAI_TZ),
            datetime(2026, 8, 5, 8, 35, tzinfo=SHANGHAI_TZ),
            "950",
            "ECONOMY",
        ),
        _transport(
            "MU-COMFORT",
            TransportMode.FLIGHT,
            "Beijing",
            "Shanghai",
            datetime(2026, 8, 5, 10, 0, tzinfo=SHANGHAI_TZ),
            datetime(2026, 8, 5, 12, 15, tzinfo=SHANGHAI_TZ),
            "1350",
            "BUSINESS",
        ),
        _transport(
            "G-BUFFER-FAIL",
            TransportMode.TRAIN,
            "Beijing",
            "Shanghai",
            datetime(2026, 8, 6, 4, 0, tzinfo=SHANGHAI_TZ),
            datetime(2026, 8, 6, 9, 30, tzinfo=SHANGHAI_TZ),
            "553",
            "SECOND_CLASS",
        ),
        _transport(
            "G-LATE",
            TransportMode.TRAIN,
            "Beijing",
            "Shanghai",
            datetime(2026, 8, 6, 6, 0, tzinfo=SHANGHAI_TZ),
            datetime(2026, 8, 6, 11, 0, tzinfo=SHANGHAI_TZ),
            "553",
            "SECOND_CLASS",
        ),
        _transport(
            "G-RETURN-AFTERNOON",
            TransportMode.TRAIN,
            "Shanghai",
            "Beijing",
            datetime(2026, 8, 6, 12, 30, tzinfo=SHANGHAI_TZ),
            datetime(2026, 8, 6, 17, 45, tzinfo=SHANGHAI_TZ),
            "553",
            "SECOND_CLASS",
        ),
        _transport(
            "MU-RETURN",
            TransportMode.FLIGHT,
            "Shanghai",
            "Beijing",
            datetime(2026, 8, 6, 18, 0, tzinfo=SHANGHAI_TZ),
            datetime(2026, 8, 6, 20, 20, tzinfo=SHANGHAI_TZ),
            "880",
            "ECONOMY",
        ),
    ]
    hotels = [
        HotelOffer(
            ref_id="HT-NEAR",
            snapshot_id="catalog",
            provider="mock",
            name="Near Client Hotel",
            city="Shanghai",
            check_in=date(2026, 8, 5),
            check_out=date(2026, 8, 6),
            nightly_price=Decimal("720"),
            commute_minutes=8,
        ),
        HotelOffer(
            ref_id="HT-COMPLIANT",
            snapshot_id="catalog",
            provider="mock",
            name="Policy Hotel",
            city="Shanghai",
            check_in=date(2026, 8, 5),
            check_out=date(2026, 8, 6),
            nightly_price=Decimal("520"),
            commute_minutes=42,
        ),
    ]
    return MockProvider(
        transports,
        hotels,
        clock=effective_clock,
        raw_response_store=raw_response_store,
        raw_response_retention_days=raw_response_retention_days,
    )


def build_demo_system(
    *,
    tool_calling_language_model: object | None = None,
    clock: Callable[[], datetime] | None = None,
    max_tool_calls: int = 12,
    agentic_tool_call_limit: int = 20,
    max_provider_attempts: int = MAX_PROVIDER_ATTEMPTS,
    max_llm_attempts: int = 2,
    retry_backoff_base_seconds: float = 0.5,
    retry_sleep: Callable[[float], None] | None = None,
    retry_jitter: Callable[[], float] | None = None,
    provider_circuit_open_seconds: float = DEFAULT_CIRCUIT_OPEN_SECONDS,
    #: 多实例共享的熔断状态存储；API 按 DATABASE_URL 换成 SQL，默认每个进程一份。
    provider_circuit_store: ProviderCircuitStateStore | None = None,
    max_delayed_provider_attempts: int = DEFAULT_MAX_DELAYED_PROVIDER_ATTEMPTS,
    delayed_provider_retry_seconds: tuple[float, ...] = (DEFAULT_DELAYED_PROVIDER_RETRY_SECONDS),
    max_concurrent_llm_calls: int = 8,
    max_concurrent_provider_calls: int = 16,
    tool_acquire_timeout_seconds: float = 5.0,
    interrupted_task_stale_seconds: float = 30.0,
    provider_retry_worker_id: str | None = None,
    provider_retry_lease_seconds: float = 900.0,
    task_repository: TaskRepository | None = None,
    raw_response_store: RawResponseObjectStore | None = None,
    raw_response_retention_days: int = 90,
    policy_configuration: LoadedPolicyConfiguration | None = None,
    trace_observer: WorkflowTraceObserverPort | None = None,
    provider: TravelInventoryProvider | None = None,
    #: 员工习惯画像的历史来源。默认 None——这一层默认关着，理由见
    #: `TripWorkflowOrchestrator.__init__` 上的说明。
    trip_history: TripHistoryPort | None = None,
    #: 预算账本。默认读同一个任务仓储里回填过的下单确认；政策没配预算就不会有规则。
    budget_ledger: BudgetLedgerPort | None = None,
    #: 差旅聚合仓储。默认内存；API 按 DATABASE_URL 换成 SQL。
    trip_repository: TripRepository | None = None,
    #: 航班动态源。默认没接；API 按 FLIGHT_STATUS_SOURCE 装配。
    flight_status_source: FlightStatusPort | None = None,
    trip_watch_worker_id: str | None = None,
    trip_watch_lease_seconds: float = 300.0,
    trip_watch_lookahead_hours: int = 48,
    min_connection_minutes: int = 60,
    delay_notice_minutes: int = 15,
) -> tuple[TripWorkflowOrchestrator, TravelInventoryProvider]:
    """构建演示系统：返回 (Orchestrator, Provider)，便于本地/API 冒烟。"""
    effective_clock = clock or (lambda: datetime.now(UTC))
    policy_configuration = policy_configuration or load_policy_configuration()
    if provider is None:
        provider = build_demo_provider(
            clock=effective_clock,
            raw_response_store=raw_response_store,
            raw_response_retention_days=raw_response_retention_days,
        )
    tasks = task_repository or InMemoryTaskRepository()
    workflow = TripWorkflowOrchestrator(
        tasks=tasks,
        employees=InMemoryEmployeeDirectory(list(policy_configuration.employee_snapshots)),
        policies=InMemoryPolicyRepository(
            policy_configuration.policy_snapshots,
            current_snapshot_id=policy_configuration.config.active_policy_snapshot_id,
        ),
        provider=provider,
        trip_history=trip_history,
        budget_ledger=budget_ledger or RepositoryTripBudgetLedger(tasks),
        trips=trip_repository or InMemoryTripRepository(),
        tool_calling_language_model=tool_calling_language_model,
        agentic_tool_call_limit=agentic_tool_call_limit,
        clock=effective_clock,
        max_tool_calls=max_tool_calls,
        max_provider_attempts=max_provider_attempts,
        max_llm_attempts=max_llm_attempts,
        retry_backoff_base_seconds=retry_backoff_base_seconds,
        retry_sleep=retry_sleep,
        retry_jitter=retry_jitter,
        provider_circuit_open_seconds=provider_circuit_open_seconds,
        provider_circuit_store=provider_circuit_store,
        max_delayed_provider_attempts=max_delayed_provider_attempts,
        delayed_provider_retry_seconds=delayed_provider_retry_seconds,
        max_concurrent_llm_calls=max_concurrent_llm_calls,
        max_concurrent_provider_calls=max_concurrent_provider_calls,
        tool_acquire_timeout_seconds=tool_acquire_timeout_seconds,
        interrupted_task_stale_seconds=interrupted_task_stale_seconds,
        provider_retry_worker_id=provider_retry_worker_id,
        provider_retry_lease_seconds=provider_retry_lease_seconds,
        timezone_name=policy_configuration.config.timezone_name,
        city_normalizer=CityNormalizer(policy_configuration.city_aliases),
        trace_observer=trace_observer,
        flight_status_source=flight_status_source,
        trip_watch_worker_id=trip_watch_worker_id,
        trip_watch_lease_seconds=trip_watch_lease_seconds,
        trip_watch_lookahead_hours=trip_watch_lookahead_hours,
        min_connection_minutes=min_connection_minutes,
        delay_notice_minutes=delay_notice_minutes,
    )
    return workflow, provider


def make_demo_request(*, task_id: str | None = None, version: int = 1) -> TripRequestVersion:
    """构造与演示库存对齐的示例 TripRequestVersion（北京→上海）。"""
    return TripRequestVersion(
        task_id=task_id or str(uuid4()),
        version=version,
        traveler_id="E1001",
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI_TZ),
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI_TZ),
        return_after=datetime(2026, 8, 6, 17, 0, tzinfo=SHANGHAI_TZ),
        return_before=datetime(2026, 8, 6, 23, 0, tzinfo=SHANGHAI_TZ),
        hotel_check_in=date(2026, 8, 5),
        hotel_check_out=date(2026, 8, 6),
        hard_constraints=("arrive_before_meeting",),
        soft_preferences=("avoid_early_departure", "hotel_near_client", "prefer_train"),
    )


def _transport(
    ref_id: str,
    mode: TransportMode,
    origin: str,
    destination: str,
    depart_at: datetime,
    arrive_at: datetime,
    price: str,
    seat_class: str,
) -> TransportOffer:
    """内部辅助：生成一条 Mock 交通报价。"""
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="catalog",
        provider="mock",
        mode=mode,
        origin=origin,
        destination=destination,
        depart_at=depart_at,
        arrive_at=arrive_at,
        price=Decimal(price),
        seat_class=seat_class,
    )

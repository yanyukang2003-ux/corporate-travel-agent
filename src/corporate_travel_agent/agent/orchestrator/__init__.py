"""orchestrator：差旅工作流编排器（TripWorkflowOrchestrator）。

有界 Agent 循环的确定性控制器：按 TaskState 观察/决策，调用 Provider 与 LLM 端口，
再经确定性规划与政策校验后持久化并暂停。V1 不代付、不改签、不出票。

2026-09-01 起这是一个包：编排器按职责拆成 `core` / `intake` / `planning` / `approval` /
`confirmation` / `resilience` / `records` 七个模块，`TripWorkflowOrchestrator` 在这里由
六个 mixin 组装。**行为没有变，公开名字也没有变**——从这个包导入的一切和拆分前相同。
拆分依据见 HANDOFF §48。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from random import random
from threading import BoundedSemaphore, RLock
from time import sleep
from typing import Any
from uuid import uuid4

from corporate_travel_agent.agent.orchestrator.approval import ApprovalMixin
from corporate_travel_agent.agent.orchestrator.confirmation import ConfirmationMixin
from corporate_travel_agent.agent.orchestrator.core import (
    MAX_LLM_ATTEMPTS,
    MAX_PROVIDER_ATTEMPTS,
    PARTIAL_COVERAGE_METADATA_KEY,
    TRANSIENT_LLM_RETRY_REASON,
    TRANSIENT_RETRY_REASON,
    LanguageModelUnavailable,
    ToolBudgetExceeded,
    ToolResult,
    WorkflowError,
)
from corporate_travel_agent.agent.orchestrator.intake import IntakeMixin
from corporate_travel_agent.agent.orchestrator.planning import PlanningMixin
from corporate_travel_agent.agent.orchestrator.records import (
    RecordsMixin,
)
from corporate_travel_agent.agent.orchestrator.records import (
    _budget_snapshot_from_metadata as _budget_snapshot_from_metadata,
)
from corporate_travel_agent.agent.orchestrator.records import (
    _travel_profile_from_metadata as _travel_profile_from_metadata,
)
from corporate_travel_agent.agent.orchestrator.resilience import ResilienceMixin
from corporate_travel_agent.agent.orchestrator.watch import TripWatchMixin
from corporate_travel_agent.agent.ports import WorkflowTraceObserverPort
from corporate_travel_agent.planning.planner import ItineraryPlanner
from corporate_travel_agent.providers.base import TravelInventoryProvider
from corporate_travel_agent.providers.flight_status import FlightStatusPort, NullFlightStatusSource
from corporate_travel_agent.services.budget_ledger import BudgetLedgerPort
from corporate_travel_agent.services.change_impact import (
    DEFAULT_DELAY_NOTICE_MINUTES,
    DEFAULT_LOOKAHEAD_HOURS,
    DEFAULT_MIN_CONNECTION_MINUTES,
)
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.provider_resilience import (
    DEFAULT_CIRCUIT_OPEN_SECONDS,
    DEFAULT_DELAYED_PROVIDER_RETRY_SECONDS,
    DEFAULT_MAX_DELAYED_PROVIDER_ATTEMPTS,
    ProviderCircuitBreaker,
    ProviderCircuitStateStore,
    ProviderDelayedRetryPolicy,
)
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    TaskRepository,
)
from corporate_travel_agent.services.travel_profile import TripHistoryPort
from corporate_travel_agent.services.trips import InMemoryTripRepository, TripRepository
from corporate_travel_agent.workflow.state_machine import StateMachine

__all__ = [
    "TripWorkflowOrchestrator",
    "WorkflowError",
    "LanguageModelUnavailable",
    "ToolBudgetExceeded",
    "MAX_PROVIDER_ATTEMPTS",
    "MAX_LLM_ATTEMPTS",
    "TRANSIENT_RETRY_REASON",
    "TRANSIENT_LLM_RETRY_REASON",
    "PARTIAL_COVERAGE_METADATA_KEY",
    "ToolResult",
]


class TripWorkflowOrchestrator(
    IntakeMixin,
    PlanningMixin,
    ApprovalMixin,
    ConfirmationMixin,
    TripWatchMixin,
    ResilienceMixin,
    RecordsMixin,
):
    """有界 Agent 循环的确定性控制器（Orchestrator）。

    观察/决策由 TaskState 编码；Act 调用类型化 Provider 端口；
    Verify 跑确定性规划与政策；每次有意义状态迁移后持久化并暂停。
    """

    def __init__(
        self,
        *,
        tasks: TaskRepository,
        employees: InMemoryEmployeeDirectory,
        policies: InMemoryPolicyRepository,
        provider: TravelInventoryProvider,
        tool_calling_language_model: Any | None = None,
        planner: ItineraryPlanner | None = None,
        #: 员工习惯画像的来源。**默认 None，也就是这一层默认关着。**
        #:
        #: 关着不是没做完，是刻意的：接上画像的那一刻排序分数就变了，既有的评测
        #: 基线（`reports/evaluation-runs/`）和新数字就不能放在同一张图上比。
        #: 要打开就传一个 `TripHistoryPort`（`services/travel_profile.py` 里有
        #: 读任务仓储的现成适配器），并且新开一个报告目录重跑基线。
        trip_history: TripHistoryPort | None = None,
        #: 预算账本。None 表示没接：政策给员工的成本中心配了预算时，规划会把预算规则
        #: 判成"判不了"（请人定），而不是当作没超。演示系统和 API 默认接仓储账本。
        budget_ledger: BudgetLedgerPort | None = None,
        #: 差旅聚合仓储：一趟差旅跨越规划任务和改期任务。None 用内存实现。
        trips: TripRepository | None = None,
        state_machine: StateMachine | None = None,
        clock: Callable[[], datetime] | None = None,
        timezone_name: str = "Asia/Shanghai",
        max_clarification_rounds: int = 5,
        #: 几段起才额外问一次"整票"。
        #:
        #: **默认 3（只有多城走整票），是一个有意的保守选择，不是技术限制。**
        #: 实测整票连普通往返都便宜 18%–23%
        #: （`reports/evaluation-runs/multicity-pricing-*/`），但把它对往返也打开
        #: 意味着**每一趟差旅都多发一次供应商请求**——那是项目所有者的取舍
        #: （HANDOFF §1.5 第 3 条一直挂着这一条），不是规划层该替他定的。
        #: 要打开就把它设成 2。
        journey_fare_min_legs: int = 3,
        max_tool_calls: int = 12,
        #: 工具循环入口的预算上限，**默认比另外两条高**。
        #:
        #: 这不是放松限制，是算术：循环一轮要花两次预算（选工具一次、执行工具
        #: 一次），旧链路一个动作花一次。12 次只够循环走 6 轮，而三城行程至少要
        #: 四次搜索加一次交付，一点余量都没有——实测多城在 12 下跑不完，20 下能
        #: 收敛（`reports/evaluation-runs/toolloop-*`）。
        #:
        #: 另外两条入口保持 12 不动：它们有冻结的评测基线，改了预算旧数字就不能
        #: 直接对比了。
        agentic_tool_call_limit: int = 20,
        max_provider_attempts: int = MAX_PROVIDER_ATTEMPTS,
        max_llm_attempts: int = MAX_LLM_ATTEMPTS,
        retry_backoff_base_seconds: float = 0.5,
        retry_sleep: Callable[[float], None] | None = None,
        retry_jitter: Callable[[], float] | None = None,
        provider_circuit_open_seconds: float = DEFAULT_CIRCUIT_OPEN_SECONDS,
        #: 多实例共享的熔断状态存储。None 就是每个进程一份（单进程、单测、演示）。
        provider_circuit_store: ProviderCircuitStateStore | None = None,
        max_delayed_provider_attempts: int = DEFAULT_MAX_DELAYED_PROVIDER_ATTEMPTS,
        delayed_provider_retry_seconds: tuple[
            float, ...
        ] = DEFAULT_DELAYED_PROVIDER_RETRY_SECONDS,
        max_concurrent_llm_calls: int = 8,
        max_concurrent_provider_calls: int = 16,
        tool_acquire_timeout_seconds: float = 5.0,
        interrupted_task_stale_seconds: float = 30.0,
        provider_retry_worker_id: str | None = None,
        provider_retry_lease_seconds: float = 900.0,
        city_normalizer: CityNormalizer | None = None,
        trace_observer: WorkflowTraceObserverPort | None = None,
        #: 航班动态源。默认没接（`NullFlightStatusSource`）：watch worker 一趟都不领。
        flight_status_source: FlightStatusPort | None = None,
        trip_watch_worker_id: str | None = None,
        trip_watch_lease_seconds: float = 300.0,
        #: 起飞前多少小时开始盯。
        trip_watch_lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
        #: 前一段落地到下一段起飞至少留多久才算接得上。
        min_connection_minutes: int = DEFAULT_MIN_CONNECTION_MINUTES,
        #: 延误多少分钟以内不打扰旅行者。
        delay_notice_minutes: int = DEFAULT_DELAY_NOTICE_MINUTES,
    ) -> None:
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least 1")
        if not 1 <= max_provider_attempts <= 3:
            raise ValueError("max_provider_attempts must be between 1 and 3")
        if not 1 <= max_llm_attempts <= 2:
            raise ValueError("max_llm_attempts must be between 1 and 2")
        if not 0 <= retry_backoff_base_seconds <= 5:
            raise ValueError("retry_backoff_base_seconds must be between 0 and 5")
        if not 1 <= max_concurrent_llm_calls <= 128:
            raise ValueError("max_concurrent_llm_calls must be between 1 and 128")
        if not 1 <= max_concurrent_provider_calls <= 128:
            raise ValueError("max_concurrent_provider_calls must be between 1 and 128")
        if not 0 <= tool_acquire_timeout_seconds <= 60:
            raise ValueError("tool_acquire_timeout_seconds must be between 0 and 60")
        if not 0 <= interrupted_task_stale_seconds <= 3600:
            raise ValueError("interrupted_task_stale_seconds must be between 0 and 3600")
        if not 1 <= provider_retry_lease_seconds <= 3600:
            raise ValueError("provider_retry_lease_seconds must be between 1 and 3600")
        self.tasks = tasks
        self.employees = employees
        self.policies = policies
        self.provider = provider
        self.tool_calling_language_model = tool_calling_language_model
        self.fallback_model = getattr(tool_calling_language_model, "fallback_model", None)
        self.llm_runtime_status = "unknown"
        self.planner = planner or ItineraryPlanner()
        self.trip_history = trip_history
        self.budget_ledger = budget_ledger
        self.trips: TripRepository = trips or InMemoryTripRepository()
        self.state_machine = state_machine or StateMachine()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timezone_name = timezone_name
        self.max_clarification_rounds = max_clarification_rounds
        if journey_fare_min_legs < 2:
            raise ValueError("journey_fare_min_legs must be at least 2")
        self.journey_fare_min_legs = journey_fare_min_legs
        self.max_tool_calls = max_tool_calls
        if agentic_tool_call_limit < 1:
            raise ValueError("agentic_tool_call_limit must be at least 1")
        self.agentic_tool_call_limit = agentic_tool_call_limit
        self.max_provider_attempts = max_provider_attempts
        self.max_llm_attempts = max_llm_attempts
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.retry_sleep = retry_sleep or sleep
        self.retry_jitter = retry_jitter or random
        self.tool_acquire_timeout_seconds = tool_acquire_timeout_seconds
        self.interrupted_task_stale_seconds = interrupted_task_stale_seconds
        self.provider_retry_worker_id = provider_retry_worker_id or f"worker-{uuid4()}"
        self.provider_retry_lease_duration = timedelta(
            seconds=provider_retry_lease_seconds
        )
        self._provider_retry_metrics = {
            "claimed": 0,
            "lease_conflict": 0,
            "reclaimed": 0,
            "completed": 0,
            "exhausted": 0,
            "oldest_due_lag_seconds": 0.0,
        }
        self._llm_slots = BoundedSemaphore(max_concurrent_llm_calls)
        self._provider_slots = BoundedSemaphore(max_concurrent_provider_calls)
        self.delayed_provider_retry_policy = ProviderDelayedRetryPolicy(
            max_attempts=max_delayed_provider_attempts,
            delays_seconds=delayed_provider_retry_seconds,
        )
        self.provider_circuit_breaker = ProviderCircuitBreaker(
            clock=self.clock,
            open_seconds=provider_circuit_open_seconds,
            store=provider_circuit_store,
        )
        self.city_normalizer = city_normalizer or CityNormalizer()
        self.trace_observer = trace_observer
        if not 1 <= trip_watch_lease_seconds <= 3600:
            raise ValueError("trip_watch_lease_seconds must be between 1 and 3600")
        if not 1 <= trip_watch_lookahead_hours <= 24 * 14:
            raise ValueError("trip_watch_lookahead_hours must be between 1 and 336")
        if not 0 <= min_connection_minutes <= 24 * 60:
            raise ValueError("min_connection_minutes must be between 0 and 1440")
        if not 0 <= delay_notice_minutes <= 24 * 60:
            raise ValueError("delay_notice_minutes must be between 0 and 1440")
        self.flight_status_source: FlightStatusPort = (
            flight_status_source or NullFlightStatusSource()
        )
        self.trip_watch_worker_id = trip_watch_worker_id or f"watch-{uuid4()}"
        self.trip_watch_lease_duration = timedelta(seconds=trip_watch_lease_seconds)
        self.trip_watch_lookahead_hours = trip_watch_lookahead_hours
        self.min_connection_minutes = min_connection_minutes
        self.delay_notice_minutes = delay_notice_minutes
        self._trip_watch_metrics = {
            "claimed": 0,
            "checked": 0,
            "notices": 0,
            "changes_opened": 0,
            "source_errors": 0,
        }
        self._tool_budget_lock = RLock()
        self._delayed_retry_lock = RLock()
        self.recover_interrupted_tasks()
        self._restore_provider_circuit()

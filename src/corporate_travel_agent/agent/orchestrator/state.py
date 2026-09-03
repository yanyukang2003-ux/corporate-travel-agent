"""编排器的状态契约：每个 mixin 能读写的实例属性，在这里一次声明清楚。

ADR-0004 把编排器拆成 mixin 时定了一条规矩："状态全部在实例上、只在 `__init__` 里定义"。
但那条规矩以前只写在文档里——每个 mixin 的 `self.tasks`、`self.clock` 在类型检查器眼里都是
"未定义属性"，所以它们内部的一切都查不到。这个基类只有类型标注、没有任何值，运行时不改变
行为；有了它，mypy 才能核对 mixin 之间的每一次 `self.` 访问。

**新增实例属性必须先在这里声明，再在 `__init__` 里赋值。** 声明了没赋值，或者赋了值没声明，
类型检查都会报出来。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from threading import BoundedSemaphore, RLock
from typing import Any

from corporate_travel_agent.agent.orchestrator.core import ToolResult
from corporate_travel_agent.agent.ports import WorkflowTraceEvent, WorkflowTraceObserverPort
from corporate_travel_agent.domain.enums import TaskState, TripEventType
from corporate_travel_agent.domain.models import (
    ApprovalRequest,
    AuditEvent,
    BudgetSnapshot,
    ChangeImpact,
    EmployeeTravelProfileSnapshot,
    HotelOffer,
    InventorySnapshot,
    PolicySnapshot,
    SearchProvenance,
    TransportOffer,
    TravelOptionVersion,
    Trip,
    TripEvent,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.planning.planner import ItineraryPlanner
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    RetryableProviderError,
    TransportSearchQuery,
    TravelInventoryProvider,
)
from corporate_travel_agent.providers.flight_status import FlightStatusPort
from corporate_travel_agent.services.budget_ledger import BudgetLedgerPort
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.outbox_events import OutboxEventDraft
from corporate_travel_agent.services.provider_resilience import (
    ProviderCircuitBreaker,
    ProviderDelayedRetryPolicy,
)
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    TaskRepository,
)
from corporate_travel_agent.services.travel_profile import TripHistoryPort
from corporate_travel_agent.services.trips import TripRepository
from corporate_travel_agent.workflow.state_machine import StateMachine


class OrchestratorState:
    """`TripWorkflowOrchestrator.__init__` 赋值的全部实例属性，以及 mixin 之间互相调用的方法。

    属性部分只有标注、没有值。方法部分是**跨模块契约**：一个 mixin 调用另一个 mixin 的方法，
    那个方法就必须列在这里（签名逐字相同，正文只抛 NotImplementedError，运行时永远被真正
    的实现覆盖）。这张表就是 mixin 之间的耦合面——它越短，模块越独立。
    """

    # -- 端口与协作对象 -------------------------------------------------------
    tasks: TaskRepository
    employees: InMemoryEmployeeDirectory
    policies: InMemoryPolicyRepository
    provider: TravelInventoryProvider
    #: 工具循环的模型端口；None 表示没接模型。不同评测替身实现的方法集合不完全一样，
    #: 所以这里仍是 Any——见 `ToolLoopRunner._model_turn` 的兼容分支。
    tool_calling_language_model: Any | None
    fallback_model: Any | None
    planner: ItineraryPlanner
    trip_history: TripHistoryPort | None
    budget_ledger: BudgetLedgerPort | None
    trips: TripRepository
    state_machine: StateMachine
    city_normalizer: CityNormalizer
    trace_observer: WorkflowTraceObserverPort | None
    flight_status_source: FlightStatusPort
    delayed_provider_retry_policy: ProviderDelayedRetryPolicy
    provider_circuit_breaker: ProviderCircuitBreaker

    # -- 时钟与参数 ------------------------------------------------------------
    clock: Callable[[], datetime]
    timezone_name: str
    max_clarification_rounds: int
    journey_fare_min_legs: int
    max_tool_calls: int
    agentic_tool_call_limit: int
    max_provider_attempts: int
    max_llm_attempts: int
    retry_backoff_base_seconds: float
    retry_sleep: Callable[[float], None]
    retry_jitter: Callable[[], float]
    tool_acquire_timeout_seconds: float
    interrupted_task_stale_seconds: float
    provider_retry_worker_id: str
    provider_retry_lease_duration: timedelta
    trip_watch_worker_id: str
    trip_watch_lease_duration: timedelta
    trip_watch_lookahead_hours: int
    min_connection_minutes: int
    delay_notice_minutes: int

    # -- 运行时状态 ------------------------------------------------------------
    llm_runtime_status: str
    _provider_retry_metrics: dict[str, int | float]
    _trip_watch_metrics: dict[str, int]
    _llm_slots: BoundedSemaphore
    _provider_slots: BoundedSemaphore
    _tool_budget_lock: RLock
    _delayed_retry_lock: RLock

    # -- 跨 mixin 方法契约（由实现的签名生成；改签名两边都要改） -------------

    # confirmation.py — used by intake
    def _add_task_with_trip(self, task: TripTask, *, change_event: TripEvent | None = None) -> None:
        raise NotImplementedError

    # approval.py — used by planning
    def _approval_event(self, task: TripTask, event_type: str) -> OutboxEventDraft:
        raise NotImplementedError

    # records.py — used by approval, confirmation, intake, planning, resilience, watch
    def _audit(
        self,
        task: TripTask,
        event_type: str,
        input_value: object,
        output_value: object,
        evidence_refs: tuple[str, ...] = (),
        *,
        outbox: Sequence[OutboxEventDraft] = (),
        trip: Trip | None = None,
        preceding: Sequence[AuditEvent] = (),
    ) -> None:
        raise NotImplementedError

    # planning.py — used by intake
    def _audit_snapshots(self, task: TripTask, snapshots: list[InventorySnapshot]) -> None:
        raise NotImplementedError

    # records.py — used by intake, planning
    def _budget_snapshot(self, task: TripTask) -> BudgetSnapshot | None:
        raise NotImplementedError

    # resilience.py — used by planning
    def _complete_provider_retry(self, task: TripTask) -> None:
        raise NotImplementedError

    # records.py — used by planning
    @staticmethod
    def _hotel_provenance(
        query: HotelSearchQuery, snapshot: InventorySnapshot
    ) -> SearchProvenance:
        raise NotImplementedError

    # planning.py — used by intake
    @staticmethod
    def _hotels(snapshot: InventorySnapshot | None) -> list[HotelOffer]:
        raise NotImplementedError

    # planning.py — used by intake
    def _invalid_snapshot_ids(self, snapshots: list[InventorySnapshot]) -> list[str]:
        raise NotImplementedError

    # resilience.py — used by intake, planning, watch
    def _invoke_tool(
        self,
        task: TripTask,
        *,
        tool_name: str,
        tool_kind: str,
        input_value: object,
        operation: Callable[[], ToolResult],
        counts_toward_budget: bool = True,
    ) -> ToolResult:
        raise NotImplementedError

    # resilience.py — used by planning
    def _mark_provider_retry_terminal(self, task: TripTask, *, status: str) -> None:
        raise NotImplementedError

    # approval.py — used by planning
    def _new_approval(self, task: TripTask, business_reason: str) -> ApprovalRequest:
        raise NotImplementedError

    # planning.py — used by intake
    def _no_feasible_reasons(
        self,
        request: TripRequestVersion,
        leg_snapshots: Sequence[InventorySnapshot],
        hotel_snapshots: Sequence[InventorySnapshot],
        *,
        policy: PolicySnapshot | None = None,
    ) -> tuple[str, ...]:
        raise NotImplementedError

    # resilience.py — used by intake, watch
    def _note_llm_success(self) -> None:
        raise NotImplementedError

    # records.py — used by planning
    def _pin_provenance(self, task: TripTask, option: TravelOptionVersion) -> None:
        raise NotImplementedError

    # records.py — used by approval, intake, planning, resilience, watch
    def _policy_for(self, task: TripTask) -> PolicySnapshot:
        raise NotImplementedError

    # resilience.py — used by planning
    def _prepare_provider_operation(self, task: TripTask, *, resume_operation: str) -> bool:
        raise NotImplementedError

    # resilience.py — used by planning
    @staticmethod
    def _provider_retry_metadata(task: TripTask) -> dict[str, Any] | None:
        raise NotImplementedError

    # resilience.py — used by planning
    @staticmethod
    def _public_provider_retry_metadata(metadata: dict[str, Any]) -> dict[str, object]:
        raise NotImplementedError

    # planning.py — used by intake
    def _record_coverage_notices(self, task: TripTask, snapshots: list[InventorySnapshot]) -> None:
        raise NotImplementedError

    # records.py — used by intake, planning
    def _record_searches(
        self, task: TripTask, searches: Sequence[SearchProvenance]
    ) -> None:
        raise NotImplementedError

    # records.py — used by resilience
    def _record_trace(self, event: WorkflowTraceEvent) -> None:
        raise NotImplementedError

    # records.py — used by approval, intake, planning
    @staticmethod
    def _request(task: TripTask) -> TripRequestVersion:
        raise NotImplementedError

    # planning.py — used by approval, resilience
    def _revalidate_selected(self, task: TripTask) -> TripTask:
        raise NotImplementedError

    # resilience.py — used by planning
    def _schedule_provider_retry(
        self,
        task: TripTask,
        *,
        resume_operation: str,
        error: RetryableProviderError | None = None,
    ) -> None:
        raise NotImplementedError

    # planning.py — used by intake, resilience
    def _search_and_plan(self, task: TripTask, policy: PolicySnapshot) -> TripTask:
        raise NotImplementedError

    # resilience.py — used by intake, planning
    def _stop_for_tool_budget(
        self,
        task: TripTask,
        operation: str,
        *,
        required_calls: int = 1,
    ) -> TripTask:
        raise NotImplementedError

    # intake.py — used by planning
    @staticmethod
    def _timezone_aware(value: datetime) -> bool:
        raise NotImplementedError

    # records.py — used by resilience
    @staticmethod
    def _trace_evidence_refs(result: object, input_value: object) -> tuple[str, ...]:
        raise NotImplementedError

    # records.py — used by approval, confirmation, intake, planning, resilience
    def _transition(self, task: TripTask, target: TaskState) -> None:
        raise NotImplementedError

    # records.py — used by confirmation
    def _transition_pending(self, task: TripTask, target: TaskState) -> AuditEvent:
        raise NotImplementedError

    # records.py — used by planning
    @staticmethod
    def _transport_provenance(
        query: TransportSearchQuery, snapshot: InventorySnapshot
    ) -> SearchProvenance:
        raise NotImplementedError

    # planning.py — used by intake
    @staticmethod
    def _transports(snapshot: InventorySnapshot | None) -> list[TransportOffer]:
        raise NotImplementedError

    # records.py — used by intake, planning
    def _travel_profile(self, task: TripTask) -> EmployeeTravelProfileSnapshot | None:
        raise NotImplementedError

    # intake.py — used by watch
    @staticmethod
    def _validate_message(message: str) -> str:
        raise NotImplementedError

    # planning.py — used by intake
    @staticmethod
    def _without_excluded(task: TripTask, offers: list[TransportOffer]) -> list[TransportOffer]:
        raise NotImplementedError

    # intake.py — used by confirmation
    def create_task(
        self,
        request: TripRequestVersion,
        *,
        requester_id: str | None = None,
        trip_id: str | None = None,
        parent_task_id: str | None = None,
        change_event: TripEvent | None = None,
    ) -> TripTask:
        raise NotImplementedError

    # confirmation.py — used by watch
    def report_trip_event(
        self,
        trip_id: str,
        *,
        event_type: TripEventType,
        reported_by: str,
        ref_id: str | None = None,
        new_depart_at: datetime | None = None,
        new_arrive_by: datetime | None = None,
        note: str | None = None,
        leg_index: int | None = None,
        impact: ChangeImpact | None = None,
    ) -> TripTask:
        raise NotImplementedError

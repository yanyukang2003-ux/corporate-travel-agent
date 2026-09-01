"""orchestrator：差旅工作流编排器（TripWorkflowOrchestrator）。

有界 Agent 循环的确定性控制器：按 TaskState 观察/决策，调用 Provider 与 LLM 端口，
再经确定性规划与政策校验后持久化并暂停。V1 不代付、不改签、不出票。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from random import random
from threading import BoundedSemaphore, RLock
from time import perf_counter_ns, sleep
from typing import Any, TypeVar
from uuid import uuid4

from corporate_travel_agent.agent.error_recovery import (
    RecoveryAction,
    SideEffectClass,
    classify_tool_failure,
    hotel_search_tool_name,
    recon_search_legs,
    transport_search_tool_name,
)
from corporate_travel_agent.agent.ports import (
    LanguageModelError,
    WorkflowTraceEvent,
    WorkflowTraceObserverPort,
)
from corporate_travel_agent.domain.enums import (
    ApprovalStatus,
    BookingConfirmationSource,
    BookingScope,
    IntentEntrypoint,
    LodgingRequirement,
    PolicyOutcome,
    PreferenceOrigin,
    RevalidationStatus,
    TaskState,
    ToolCallStatus,
    TripLegRole,
)
from corporate_travel_agent.domain.models import (
    ApprovalRequest,
    ApprovalStep,
    BookingConfirmation,
    BookingIntent,
    BudgetSnapshot,
    ConversationMessage,
    EmployeeProfileSnapshot,
    EmployeeTravelProfileSnapshot,
    HotelOffer,
    InventorySnapshot,
    PolicyDecision,
    PolicySnapshot,
    ProfilePreference,
    ScopedRequirement,
    SearchProvenance,
    ToolCallRecord,
    TransportOffer,
    TravelOptionVersion,
    TripLeg,
    TripRequestVersion,
    TripStay,
    TripTask,
)
from corporate_travel_agent.domain.validation import (
    BookingConfirmationValidationError,
    validate_booking_confirmation_values,
    validate_trip_request,
)
from corporate_travel_agent.planning.feasibility import leg_spec, planned_leg_count
from corporate_travel_agent.planning.planner import ItineraryPlanner
from corporate_travel_agent.policy.engine import unreviewable_reasons
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    JourneySearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
    TravelInventoryProvider,
)
from corporate_travel_agent.services.audit import new_audit_event, stable_hash
from corporate_travel_agent.services.budget_ledger import (
    BudgetLedgerPort,
    derive_budget_snapshot,
)
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.outbox_events import OutboxEventDraft
from corporate_travel_agent.services.provenance import (
    option_provenance,
    provenance_hash,
)
from corporate_travel_agent.services.provider_resilience import (
    DEFAULT_CIRCUIT_OPEN_SECONDS,
    DEFAULT_DELAYED_PROVIDER_RETRY_SECONDS,
    DEFAULT_MAX_DELAYED_PROVIDER_ATTEMPTS,
    PROVIDER_RETRY_METADATA_KEY,
    ProviderCircuitBreaker,
    ProviderDelayedRetryPolicy,
)
from corporate_travel_agent.services.repositories import (
    ConcurrentUpdateError,
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    TaskRepository,
)
from corporate_travel_agent.services.travel_profile import (
    TripHistoryPort,
    derive_travel_profile,
)
from corporate_travel_agent.workflow.state_machine import StateMachine


class WorkflowError(RuntimeError):
    """工作流层可预期错误基类。"""

    pass


class LanguageModelUnavailable(WorkflowError):
    """未配置或不可用语言模型。"""

    pass


class ToolBudgetExceeded(WorkflowError):
    """任务工具调用预算耗尽。"""

    pass


ToolResult = TypeVar("ToolResult")

MAX_PROVIDER_ATTEMPTS = 3
"""每次 Provider 调用的总尝试次数（含首次）；有意设上限。"""

# 默认两次，使单次 429/5xx 可在澄清前恢复（HANDOFF §13 Step2）。
MAX_LLM_ATTEMPTS = 2
"""默认 LLM 尝试次数；生产/评测可改为显式单次重试。"""

TRANSIENT_RETRY_REASON = "TRANSIENT_PROVIDER_FAULT"
TRANSIENT_LLM_RETRY_REASON = "TRANSIENT_LLM_TRANSPORT_FAULT"
PARTIAL_COVERAGE_METADATA_KEY = "provider_coverage_notices"



def _fares_from(offers: list[TransportOffer]) -> list[list[TransportOffer]]:
    """把一次整票搜索的结果按 ``fare_ref`` 分回一张张票。

    一张票的几段在快照里是并排放着的；分组之后每一组就是**一个不可拆的候选**。
    没有 ``fare_ref`` 的（理论上不会出现在整票快照里）各自成一组，不会被静默丢掉。
    """
    grouped: dict[str, list[TransportOffer]] = {}
    order: list[str] = []
    for offer in offers:
        key = offer.fare_ref or offer.ref_id
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(offer)
    return [grouped[key] for key in order]


def _leg_phrase(request: TripRequestVersion, leg_index: int) -> str:
    """报错文案里怎么称呼这一段。

    沿用旧名字的那两段（outbound、以及往返里的 return）说法一个字不变——
    既有文案、评测断言与前端都认这两个词。**其余段一律带上自己的航线**：
    多城行程里光说"第 1 段没搜到"看的人根本不知道是哪一程，
    而这正是中间段搜不到时唯一能给出的线索。

    按**标签**判而不是按下标判：三段行程的第 1 段是中途那一段，不是返程，
    `leg_spec` 已经叫它 "leg 1" 了——那它就该带航线。
    """
    spec = leg_spec(request, leg_index)
    if spec.label in {"outbound", "return"}:
        return spec.label
    return f"{spec.label} ({spec.origin} → {spec.destination})"


def _approval_subject_rules(decision: PolicyDecision) -> tuple[str, ...]:
    """这次审批到底在批哪几条规则。

    违规规则原样列出；系统判不了的加 `unjudged:` 前缀。两者都进审批主体哈希——
    "批的是一条违规"和"批的是一条查不到标准"不该哈希成同一件事。
    没有判不了的规则时，返回值和以前逐字相同，既有审批对象不受影响。
    """
    return (
        *decision.violation_ids,
        *(f"unjudged:{rule_id}" for rule_id in dict.fromkeys(decision.unjudged_rule_ids)),
    )


def _empty_corridor_question(query: TransportSearchQuery) -> str:
    """一段搜空时对旅行者说的话：不编火车票，也不把整趟行程停掉。"""
    return (
        f"{query.origin}→{query.destination} 这段没有可用机票。"
        "短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，"
        "或者告诉我换一个日期再搜。"
    )


class TripWorkflowOrchestrator:
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
        )
        self.city_normalizer = city_normalizer or CityNormalizer()
        self.trace_observer = trace_observer
        self._tool_budget_lock = RLock()
        self._delayed_retry_lock = RLock()
        self.recover_interrupted_tasks()
        self._restore_provider_circuit()

    def create_task(
        self, request: TripRequestVersion, *, requester_id: str | None = None
    ) -> TripTask:
        """用结构化 TripRequest 建任务并立即搜索规划。"""
        request = self._canonicalize_request_cities(request)
        validate_trip_request(request).require_valid()
        employee = self.employees.snapshot(request.traveler_id)
        requester = self._requester_for(employee, requester_id)
        policy = self.policies.current()
        task = TripTask(
            task_id=request.task_id,
            state=TaskState.DRAFT,
            request=request,
            employee=employee,
            requester_id=requester,
            policy_snapshot_id=policy.snapshot_id,
            tool_call_limit=self.max_tool_calls,
            metadata={
                "policy_content_hash": policy.content_hash,
                "intent_entrypoint": IntentEntrypoint.STRUCTURED.value,
            },
        )
        self.tasks.add(task)
        self._audit(
            task,
            "TASK_CREATED",
            request,
            {"state": task.state.value, "requester_id": task.requested_by},
        )
        return self._search_and_plan(task, policy)

    # ------------------------------------------------------------------
    # 工具循环入口（ADR-0002 的并行迁移方式：新入口另起一条，旧的一个字不动）
    # ------------------------------------------------------------------

    def create_task_from_agentic_message(
        self,
        message: str,
        *,
        traveler_id: str,
        task_id: str | None = None,
        requester_id: str | None = None,
    ) -> TripTask:
        """用工具循环入口创建任务。

        和语义入口的差别只有一条，但那一条是结构性的：**没有全局必填表。**
        模型每轮挑一个带类型的工具，够不够往下走由那个工具自己的签名决定。
        """
        message = self._validate_message(message)
        if self.tool_calling_language_model is None:
            raise LanguageModelUnavailable("No tool-calling language model adapter is configured")
        employee = self.employees.snapshot(traveler_id)
        requester = self._requester_for(employee, requester_id)
        policy = self.policies.current()
        task = TripTask(
            task_id=task_id or str(uuid4()),
            state=TaskState.DRAFT,
            request=None,
            employee=employee,
            requester_id=requester,
            policy_snapshot_id=policy.snapshot_id,
            intent_fields=self._empty_intent_fields(),
            messages=[ConversationMessage(role="user", content=message)],
            tool_call_limit=self.agentic_tool_call_limit,
            metadata={
                "policy_content_hash": policy.content_hash,
                "intent_entrypoint": IntentEntrypoint.AGENTIC.value,
            },
        )
        self.tasks.add(task)
        self._audit(
            task,
            "AGENTIC_TASK_CREATED_FROM_MESSAGE",
            message,
            {"state": task.state.value, "requester_id": task.requested_by},
        )
        return self._run_tool_loop(task)

    def submit_agentic_message(self, task_id: str, message: str) -> TripTask:
        """向工具循环任务追加消息；整段对话重跑一次循环。

        不做"补一格字段"那种增量修补——旧链路那样做会把上一轮的错误一起带下来。
        """
        message = self._validate_message(message)
        task = self.tasks.get(task_id)
        self._require_intent_entrypoint(task, IntentEntrypoint.AGENTIC)
        if self.tool_calling_language_model is None:
            raise LanguageModelUnavailable("No tool-calling language model adapter is configured")
        allowed = {
            TaskState.NEEDS_CLARIFICATION,
            TaskState.NEEDS_STRUCTURED_INPUT,
            TaskState.WAITING_FOR_USER,
            TaskState.NO_FEASIBLE_OPTION,
            TaskState.PROVIDER_FAILED,
            TaskState.WAITING_FOR_PROVIDER,
            TaskState.OUT_OF_SCOPE,
        }
        if task.state not in allowed:
            raise WorkflowError(f"Cannot submit agentic message in {task.state.value}")
        prior_state = task.state
        task.failure = None
        task.clarification_question = None
        if prior_state is TaskState.NEEDS_CLARIFICATION:
            self._audit(task, "AGENTIC_CLARIFICATION_RECEIVED", message, task.clarification_rounds)
        else:
            task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
            self._audit(
                task,
                "AGENTIC_REVISION_RECEIVED",
                message,
                {"from_state": prior_state.value},
            )
        self._transition(task, TaskState.DRAFT)
        task.messages.append(ConversationMessage(role="user", content=message))
        return self._run_tool_loop(task)

    def _run_tool_loop(self, task: TripTask) -> TripTask:
        """跑一轮有界工具循环，把它的终局动作落成任务状态。

        循环里的**每一次**调用（含每轮选工具的模型调用）都走 `_invoke_tool`：
        工具预算、审计轨迹、有界重试、并发闸和其余两条入口共用同一套，不另起炉灶。
        """
        from corporate_travel_agent.agent.semantic_intent import ConversationLedger
        from corporate_travel_agent.agent.tool_loop import (
            ToolExecutor,
            ToolLoopAborted,
            ToolLoopRunner,
        )

        policy = self._policy_for(task)
        ledger = ConversationLedger.from_messages(task.messages)
        executor = ToolExecutor(
            provider=self.provider,
            # 和规划器**同一个**政策引擎实例：政策结论只有一个来源。
            policy_engine=self.planner.policy_engine,
            employee=task.employee,
            policy=policy,
            city_normalizer=self.city_normalizer,
            now=self.clock(),
            fallback_timezone=self.timezone_name,
            # 每一段的日期都要能在这段原文里逐字找到出处，编的对不上。
            conversation=ledger.render(),
        )
        runner = ToolLoopRunner(
            model=self.tool_calling_language_model,
            executor=executor,
            # 一轮最多花 2 次预算（选工具 1 次 + 执行工具 1 次），所以轮数上限要按
            # 剩余预算的一半算。此前直接用 max_tool_calls，循环永远是被预算掐断的，
            # "最后一轮只给终局工具"那道保险根本轮不到生效。
            max_iterations=max(1, task.tool_calls_remaining // 2),
            invoke=lambda *, tool_name, tool_kind, operation: self._invoke_tool(
                task,
                tool_name=tool_name,
                tool_kind=tool_kind,
                input_value={"task_id": task.task_id, "turns": len(ledger.turns)},
                operation=operation,
            ),
        )
        context = {
            "reference_time": self.clock().isoformat(),
            "timezone": self.timezone_name,
            "clarification_round": task.clarification_rounds,
            "max_clarification_rounds": self.max_clarification_rounds,
        }
        # 循环期间**留在 DRAFT**。它把"理解"和"搜索"交织在一起做，现有状态机里
        # 没有一个格子正好对应这件事；进 SEARCHING 会让"最后决定提问"变成非法迁移。
        # 状态标签在这里比循环实际做的事粗——真实经过在 tool_calls 和审计里。
        try:
            outcome = runner.run(ledger.render(), context=context)
        except ToolBudgetExceeded:
            # 预算耗尽时**把已经查到的事实带出来**。真模型实测会在一条没货的航线上
            # 反复搜到预算见底；只回一句"预算用完了"，用户根本不知道发生过什么。
            #
            # 状态仍然是 TOOL_BUDGET_EXHAUSTED——预算确实用光了，这是运维要看见的
            # 事实，不该被包装成"没有方案"。但话要说清楚。
            findings = self._loop_findings(executor)
            task.metadata["agentic_partial_findings"] = findings
            task = self._stop_for_tool_budget(task, "agentic.tool_loop")
            empty = [
                f"{item['origin']}→{item['destination']}"
                for item in findings["searched_legs"]
                if item["option_count"] == 0
            ]
            if empty:
                task.failure = (
                    f"{task.failure}。已经查明：{'、'.join(dict.fromkeys(empty))} "
                    "在试过的时间窗里都没有库存。"
                )
            return task
        except ToolLoopAborted as exc:
            # 循环没收敛不是"没方案"，是这条链路没走完——别把它伪装成结论。
            task.metadata["agentic_partial_findings"] = self._loop_findings(executor)
            task.metadata["agentic_transcript"] = [
                {
                    "tool": exchange.invocation.name,
                    "arguments": dict(exchange.invocation.arguments),
                    "ok": exchange.ok,
                    "error": None if exchange.ok else str(exchange.result.get("error", ""))[:300],
                }
                for exchange in exc.transcript
            ]
            task.failure = str(exc)
            task.clarification_question = None
            self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self._audit(task, "AGENTIC_LOOP_ABORTED", {"turns": len(ledger.turns)}, task.failure)
            return task
        except LanguageModelError as exc:
            task.failure = str(exc)
            task.metadata["agentic_loop_failure"] = exc.trace_details()
            task.clarification_question = None
            self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self._audit(
                task,
                "AGENTIC_LOOP_FAILED",
                {"turns": len(ledger.turns)},
                task.metadata["agentic_loop_failure"],
            )
            return task
        except ProviderError as exc:
            task.failure = str(exc)
            task.options = []
            # 先记 SEARCHING 再记失败：库存确实去要过了，状态机也只从这里通往 PROVIDER_FAILED。
            self._transition(task, TaskState.SEARCHING)
            self._transition(task, TaskState.PROVIDER_FAILED)
            self._audit(task, "AGENTIC_PROVIDER_FAILED", {"turns": len(ledger.turns)}, task.failure)
            return task

        self._note_llm_success()
        # 宿主替旅行者定下来的事必须当面说出口。循环里唯一这样的推导就是
        # "只说了几点前到，搜索窗口从时限往前扩，覆盖前一晚出发"。
        task.assumptions = tuple(dict.fromkeys(executor.assumptions))
        task.metadata["agentic_transcript"] = [
            {
                "tool": exchange.invocation.name,
                "arguments": dict(exchange.invocation.arguments),
                "ok": exchange.ok,
            }
            for exchange in outcome.transcript
        ]
        if outcome.kind == "ask_traveler":
            if outcome.out_of_scope and not executor.leg_searches and not executor.stay_searches:
                return self._stop_for_out_of_scope(task, outcome.question)
            if executor.leg_searches and not executor.seen_transport:
                # 真的去搜了、每一段都是空的、模型于是开口问：这不是"还没问清"，
                # 是"没有可行方案"。状态机本来就有这个格子，用它——前端会摆出原因和
                # "重新规划"，也不占澄清轮数。问题原文留着，那是给旅行者看的解释。
                return self._stop_for_empty_inventory(task, executor, outcome.question)
            return self._pause_for_agentic_question(task, outcome.question)
        return self._plan_from_tool_loop(task, executor, outcome, policy)

    @staticmethod
    def _loop_findings(executor: Any) -> dict[str, Any]:
        """循环没走完时，把它已经查到的事实留下来，别让证据跟着失败一起消失。"""
        return {
            "searched_legs": [
                {
                    "origin": query.origin,
                    "destination": query.destination,
                    "depart_after": query.depart_after.isoformat(),
                    "arrive_before": (
                        query.arrive_before.isoformat() if query.arrive_before else None
                    ),
                    "option_count": len(snapshot.items),
                }
                for query, snapshot in executor.leg_searches
            ],
            "searched_stays": [
                {
                    "city": query.city,
                    "check_in": query.check_in.isoformat(),
                    "check_out": query.check_out.isoformat(),
                    "option_count": len(snapshot.items),
                }
                for query, snapshot in executor.stay_searches
            ],
            "assumptions": list(dict.fromkeys(executor.assumptions)),
        }

    def _stop_for_out_of_scope(self, task: TripTask, question: str | None) -> TripTask:
        """模型说这根本不是差旅请求：和另外两条入口一样落成 OUT_OF_SCOPE。

        这条状态是可以重开的（`submit_agentic_message` 接受它）——判错了，旅行者
        再说一句话就回到 DRAFT。它不占澄清轮数：越界不是"没问清"。
        """
        task.request = None
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.failure = "The request is outside the corporate travel planning scope"
        task.clarification_question = question
        if question:
            task.messages.append(ConversationMessage(role="assistant", content=question))
        self._transition(task, TaskState.OUT_OF_SCOPE)
        self._audit(task, "AGENTIC_OUT_OF_SCOPE", None, question)
        return task

    def _stop_for_empty_inventory(
        self, task: TripTask, executor: Any, question: str | None
    ) -> TripTask:
        """循环搜过、每一段都空、模型开口问：落成 NO_FEASIBLE_OPTION，而不是一轮澄清。

        搜索的出处和快照照记——"我们搜过这条航线、结果是空的"本身就是证据。
        """
        empty = [
            f"{query.origin}→{query.destination}" for query, _ in executor.leg_searches
        ]
        reasons = tuple(
            dict.fromkeys(
                [
                    *(
                        f"no inventory matched {route} in the requested time window"
                        for route in empty
                    ),
                    *([question] if question else []),
                ]
            )
        )
        self._record_searches(task, executor.searches)
        self._transition(task, TaskState.SEARCHING)
        self._audit_snapshots(task, list(executor.captured_snapshots))
        self._transition(task, TaskState.PLANNING)
        task.request = None
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.metadata["no_feasible_reasons"] = reasons
        task.failure = "; ".join(reasons)
        task.clarification_question = question
        if question:
            task.messages.append(ConversationMessage(role="assistant", content=question))
        self._transition(task, TaskState.NO_FEASIBLE_OPTION)
        self._audit(task, "NO_FEASIBLE_OPTION", {"searched_legs": empty}, reasons)
        return task

    def _pause_for_agentic_question(self, task: TripTask, question: str | None) -> TripTask:
        """模型主动调用 `ask_traveler` 收的场。

        **和旧链路的追问不是同一种东西。** 旧的是编译失败之后按字段名查表拼出来的；
        这里提问是一个模型可以主动选的动作，所以它能引用已经搜到的真实选项。
        """
        task.request = None
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.failure = None
        task.clarification_rounds += 1
        if task.clarification_rounds > self.max_clarification_rounds:
            task.clarification_question = None
            task.failure = (
                "Agentic clarification limit reached; use the structured form"
            )
            self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self._audit(task, "AGENTIC_CLARIFICATION_EXHAUSTED", None, task.failure)
            return task
        task.clarification_question = question or "请确认我对这次出行的理解。"
        task.messages.append(
            ConversationMessage(role="assistant", content=task.clarification_question)
        )
        self._transition(task, TaskState.NEEDS_CLARIFICATION)
        self._audit(task, "AGENTIC_CLARIFICATION_REQUESTED", None, task.clarification_question)
        return task

    def _plan_from_tool_loop(
        self,
        task: TripTask,
        executor: Any,
        outcome: Any,
        policy: PolicySnapshot,
    ) -> TripTask:
        """把循环真实搜到的库存交给现成的规划器，**不再搜一遍**。

        这里构造的 `TripRequestVersion` 是**事后记录**，不是事前关卡：模型已经决定
        并且已经搜过了，这个结构体只是把"它实际做了什么"写下来，好让规划器、政策
        引擎和后续的预订/审批链路照常工作。旧链路那张表的问题从来不是"存在一个结构
        体"，而是"填不满就不许往下走"。
        """
        settled, empty_queries = self._settled_leg_searches(executor)
        if not settled:
            task.failure = "the tool loop proposed options without searching any transport leg"
            self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self._audit(task, "AGENTIC_PROPOSAL_WITHOUT_SEARCH", None, task.failure)
            return task

        leg_snapshots = [snapshot for _, snapshot in settled]
        hotel_snapshots = [snapshot for _, snapshot in executor.stay_searches]
        # 搜索出处从执行器抄到任务上。**抄全部，不只抄用上的那几次**：
        # "我们还搜过这条航线、结果是空的"本身就是溯源的一部分。
        self._record_searches(task, executor.searches)
        self._transition(task, TaskState.SEARCHING)
        invalid = self._invalid_snapshot_ids([*leg_snapshots, *hotel_snapshots])
        if invalid:
            task.failure = "provider returned expired or invalid inventory snapshots: " + ", ".join(
                invalid
            )
            self._transition(task, TaskState.PROVIDER_FAILED)
            self._audit(task, "INVENTORY_SNAPSHOT_REJECTED", tuple(invalid), task.failure)
            return task

        version = task.request.version + 1 if task.request is not None else 1
        task.request = self._request_from_tool_loop(
            task,
            settled,
            executor.stay_searches,
            version=version,
            hard_constraints=outcome.hard_constraints,
            soft_preferences=outcome.soft_preferences,
        )
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.clarification_question = None
        task.metadata.pop("agentic_open_questions", None)
        task.failure = None
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        self._audit(task, "SEARCH_COMMAND_COMPILED", outcome.summary, task.request)
        self._record_coverage_notices(task, [*leg_snapshots, *hotel_snapshots])
        # 存档认**每一次真实搜索**，不认跨日合并出来的那个：合并快照的原始响应
        # 只覆盖第一天，却装着后面几天的报价，按哈希核对不上。见 `search_transport`。
        self._audit_snapshots(task, list(executor.captured_snapshots))
        self._transition(task, TaskState.PLANNING)

        task.options = self.planner.plan(
            request=task.request,
            employee=task.employee,
            policy=policy,
            leg_offers=[self._transports(item) for item in leg_snapshots],
            hotel_offers=[self._hotels(item) for item in hotel_snapshots],
            profile=self._travel_profile(task),
            budget=self._budget_snapshot(task),
            now=self.clock(),
        )
        if not task.options:
            # **模型说"这几条可以"不算数。** 政策与可行性由确定性代码判，判不过就是没有。
            reasons = self._no_feasible_reasons(
                task.request, leg_snapshots, hotel_snapshots, policy=policy
            )
            task.metadata["no_feasible_reasons"] = reasons
            task.failure = "; ".join(reasons)
            self._transition(task, TaskState.NO_FEASIBLE_OPTION)
            self._audit(task, "NO_FEASIBLE_OPTION", task.request.version, reasons)
            return task

        gap_questions = tuple(
            _empty_corridor_question(query) for query in empty_queries
        )
        open_questions = tuple(
            dict.fromkeys((*outcome.open_questions, *gap_questions))
        )
        if gap_questions:
            # 空段说明写进每张方案的摘要。只放在追问里的话，看方案的人
            # 会以为北京→上海这几张票就是全程，上海→杭州被静默丢掉了。
            task.options = [
                replace(
                    option,
                    explanation_facts=(*gap_questions, *option.explanation_facts),
                )
                for option in task.options
            ]
        task.metadata["agentic_proposal"] = {
            "summary": outcome.summary,
            "transport_refs": list(outcome.transport_refs),
            "hotel_refs": list(outcome.hotel_refs),
            "open_questions": list(open_questions),
        }
        # **方案和未决问题可以同时存在。** 三段行程里两段的日期写在原话里、一段没写，
        # 正确做法是把能定的两段排出来、只问剩下那一段——不是整轮停住什么都不给。
        # 任务因此停在 WAITING_FOR_USER：方案可看可选，问题也摆在那里等回答。
        if open_questions:
            task.clarification_question = "\n".join(open_questions)
            task.messages.append(
                ConversationMessage(role="assistant", content=task.clarification_question)
            )
            task.metadata["agentic_open_questions"] = list(open_questions)
            self._audit(
                task,
                "AGENTIC_PARTIAL_ITINERARY",
                {"legs_settled": len(settled)},
                list(open_questions),
            )
        self._transition(task, TaskState.OPTIONS_READY)
        self._audit(
            task,
            "OPTIONS_VERIFIED",
            task.request.version,
            [item.option_id for item in task.options],
            tuple(ref for item in task.options for ref in item.inventory_refs),
        )
        self._transition(task, TaskState.WAITING_FOR_USER)
        return task

    @staticmethod
    def _settled_leg_searches(executor: Any) -> tuple[list[Any], list[Any]]:
        """有货的段拿去规划；没货的段变成未决问题，不让整单一起死。"""
        collapsed: dict[tuple[str, str], Any] = {}
        order: list[tuple[str, str]] = []
        for query, snapshot in executor.leg_searches:
            key = (query.origin, query.destination)
            if key not in collapsed:
                order.append(key)
                collapsed[key] = (query, snapshot)
                continue
            previous_query, previous_snapshot = collapsed[key]
            previous_count = sum(
                1 for item in previous_snapshot.items if isinstance(item, TransportOffer)
            )
            current_count = sum(
                1 for item in snapshot.items if isinstance(item, TransportOffer)
            )
            if current_count >= previous_count:
                collapsed[key] = (query, snapshot)
        settled: list[Any] = []
        empty: list[Any] = []
        for key in order:
            query, snapshot = collapsed[key]
            if any(isinstance(item, TransportOffer) for item in snapshot.items):
                settled.append((query, snapshot))
            else:
                empty.append(query)
        return settled, empty

    def _request_from_tool_loop(
        self,
        task: TripTask,
        settled: Sequence[Any],
        stay_searches: Sequence[Any],
        *,
        version: int,
        hard_constraints: Sequence[str] = (),
        soft_preferences: Sequence[str] = (),
    ) -> TripRequestVersion:
        """把循环实际搜到货的段与站写成一个请求版本。

        **段数是数出来的**：数有库存的段，空搜不进规划器。

        硬要求和偏好来自交付时的声明（`propose_options.hard_constraints /
        soft_preferences`）。此前这里一个都不带——"只要直飞"到了规划器就没了，
        "优先高铁"也不参与排序，模型在提示词里被告知的那两张词表根本没有出口。
        声明按"管全程"落到请求上；分段作用域工具循环今天表达不了，不假装。
        """
        legs = tuple(
            TripLeg(
                # 第一段是去程，其余按顺序算返程/续程；角色只影响文案，不影响搜索。
                role=TripLegRole.OUTBOUND if index == 0 else TripLegRole.RETURN,
                origin=query.origin,
                destination=query.destination,
                depart_after=query.depart_after,
                arrive_before=query.arrive_before,
            )
            for index, (query, _) in enumerate(settled)
        )
        stays = tuple(
            TripStay(city=query.city, check_in=query.check_in, check_out=query.check_out)
            for query, _ in stay_searches
        )
        first = legs[0]
        last = legs[-1]
        hard = tuple(dict.fromkeys(hard_constraints))
        soft = tuple(dict.fromkeys(soft_preferences))
        return TripRequestVersion(
            task_id=task.task_id,
            version=version,
            traveler_id=task.employee.employee_id,
            origin=first.origin,
            destination=first.destination,
            departure_after=first.depart_after,
            arrive_by=first.arrive_before,
            return_after=last.depart_after if len(legs) > 1 else None,
            return_before=last.arrive_before if len(legs) > 1 else None,
            hotel_check_in=stays[0].check_in if stays else None,
            hotel_check_out=stays[0].check_out if stays else None,
            hard_constraints=hard,
            soft_preferences=soft,
            scoped_hard_constraints=tuple(ScopedRequirement(name=item) for item in hard),
            scoped_soft_preferences=tuple(ScopedRequirement(name=item) for item in soft),
            booking_scope=(
                BookingScope.ROUND_TRIP if len(legs) > 1 else BookingScope.OUTBOUND_ONLY
            ),
            journey=legs,
            stays=stays,
            created_at=self.clock(),
        )

    def _requester_for(
        self, traveler: EmployeeProfileSnapshot, requester_id: str | None
    ) -> str:
        """定下这趟任务的发起人，并把"能不能替他订"挡在这里。

        - 没给发起人（旧调用方、内部脚本）：就是旅行者本人。
        - 发起人是本系统认识的员工：要么是本人，要么在旅行者的委托名单上，否则拒绝。
          这是防御纵深——API 层已经按身份查过一次，编排器不信它。
        - 发起人不是员工（管理员、系统身份）：照记不拦。他们能不能建任务由角色管，
          不由委托名单管。
        """
        if requester_id is None:
            return traveler.employee_id
        knows = getattr(self.employees, "knows", None)
        if callable(knows) and knows(requester_id):
            may_book_for = getattr(self.employees, "may_book_for", None)
            if callable(may_book_for) and not may_book_for(requester_id, traveler.employee_id):
                raise WorkflowError(
                    f"{requester_id} is not allowed to book on behalf of {traveler.employee_id}"
                )
        return requester_id

    def complete_with_structured_request(
        self, task_id: str, request: TripRequestVersion
    ) -> TripTask:
        """在缺字段状态下提交完整结构化请求并继续搜索。"""
        task = self.tasks.get(task_id)
        if task.state not in {
            TaskState.NEEDS_CLARIFICATION,
            TaskState.NEEDS_STRUCTURED_INPUT,
        }:
            raise WorkflowError(f"Cannot complete a structured request in {task.state.value}")
        if request.task_id != task.task_id or request.traveler_id != task.employee.employee_id:
            raise WorkflowError("Structured request identity does not match the draft task")
        request = self._canonicalize_request_cities(request)
        validate_trip_request(request).require_valid()
        self._transition(task, TaskState.DRAFT)
        task.request = request
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.clarification_question = None
        task.failure = None
        self._audit(task, "STRUCTURED_FALLBACK_SUBMITTED", request, request.version)
        return self._search_and_plan(task, self._policy_for(task))

    @staticmethod
    def _empty_intent_fields() -> dict[str, Any]:
        """返回空意图字段模板。"""
        return {
            "origin": None,
            "destination": None,
            "departure_after": None,
            "arrive_by": None,
            "return_after": None,
            "return_before": None,
            "hotel_check_in": None,
            "hotel_check_out": None,
            "client_location": None,
            "booking_scope": None,
            "lodging_requirement": LodgingRequirement.UNSPECIFIED.value,
            "hard_constraints": [],
            "soft_preferences": [],
        }

    @staticmethod
    def _timezone_aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    def _note_llm_success(self) -> None:
        self.llm_runtime_status = "ok"

    def _note_llm_failure(self, failure_class: Any) -> None:
        from corporate_travel_agent.agent.llm_failure import health_status_for

        self.llm_runtime_status = health_status_for(
            failure_class, configured=self.tool_calling_language_model is not None
        )

    @staticmethod
    def _require_intent_entrypoint(
        task: TripTask, expected: IntentEntrypoint
    ) -> None:
        actual = task.metadata.get("intent_entrypoint")
        # Persisted tasks created before entrypoint separation are legacy-compatible.
        if actual is None and expected is IntentEntrypoint.LEGACY:
            return
        if actual != expected.value:
            raise WorkflowError(
                f"Task uses {actual or 'unknown'} intent entrypoint; "
                f"continue it through the {expected.value} entrypoint"
            )

    def _canonicalize_request_cities(self, request: TripRequestVersion) -> TripRequestVersion:
        """规范化请求中的城市名。

        **航段与住宿站的城市也要规范化。** 它们和 `origin` / `destination` 一样
        会被原样送去 Provider 查询——只规范化扁平字段的话，多城行程里
        "去上海、再去东京"的后两段会拿着没规范化的名字去搜。
        """
        canonical = self.city_normalizer.canonicalize
        return replace(
            request,
            origin=canonical(request.origin),
            destination=canonical(request.destination),
            journey=tuple(
                replace(
                    leg,
                    origin=canonical(leg.origin),
                    destination=canonical(leg.destination),
                )
                for leg in request.journey
            ),
            stays=tuple(
                replace(stay, city=canonical(stay.city)) for stay in request.stays
            ),
        )

    @staticmethod
    def _validate_message(message: str) -> str:
        """校验用户消息非空等基本约束。"""
        normalized = message.strip()
        if not normalized:
            raise WorkflowError("Message cannot be empty")
        if len(normalized) > 4000:
            raise WorkflowError("Message exceeds the 4000 character limit")
        return normalized

    def retry_or_replan(self, task_id: str) -> TripTask:
        """在无可行方案或 Provider 失败后按策略重试/重规划。"""
        task = self.tasks.get(task_id)
        prior_state = task.state
        if task.state not in {
            TaskState.PROVIDER_FAILED,
            TaskState.NO_FEASIBLE_OPTION,
            TaskState.RECONFIRMATION_REQUIRED,
            TaskState.WAITING_FOR_PROVIDER,
        }:
            raise WorkflowError(f"Cannot replan task in {task.state.value}")
        if task.approval and task.approval.status is ApprovalStatus.APPROVED:
            task.approval.status = ApprovalStatus.INVALIDATED
            self._audit(task, "APPROVAL_INVALIDATED", task.approval.subject_hash, "replan")
        task.selected_option_id = None
        retry_metadata = self._provider_retry_metadata(task)
        if prior_state is TaskState.WAITING_FOR_PROVIDER and retry_metadata is not None:
            retry_metadata["status"] = "scheduled"
            retry_metadata["next_retry_at"] = self.clock().isoformat()
            retry_metadata["trigger"] = "manual"
            self._audit(
                task,
                "PROVIDER_DELAYED_RETRY_REQUESTED",
                {"trigger": "manual"},
                self._public_provider_retry_metadata(retry_metadata),
            )
        elif retry_metadata is not None:
            task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
            self._audit(
                task,
                "PROVIDER_RETRY_CYCLE_RESET",
                {"trigger": "manual_replan", "from_state": prior_state.value},
                {"previous_status": retry_metadata.get("status")},
            )
        return self._search_and_plan(task, self._policy_for(task))

    def revise_request(self, task_id: str, request: TripRequestVersion) -> TripTask:
        """用新版结构化请求替换并重新搜索。"""
        task = self.tasks.get(task_id)
        request = self._canonicalize_request_cities(request)
        validate_trip_request(request).require_valid()
        current_request = self._request(task)
        if request.task_id != task.task_id or request.version <= current_request.version:
            raise WorkflowError("A revision must keep task_id and increase request version")
        if task.state not in {
            TaskState.WAITING_FOR_USER,
            TaskState.WAITING_FOR_APPROVAL,
            TaskState.PROVIDER_FAILED,
            TaskState.NO_FEASIBLE_OPTION,
            TaskState.RECONFIRMATION_REQUIRED,
            TaskState.WAITING_FOR_PROVIDER,
        }:
            raise WorkflowError(f"Cannot revise task in {task.state.value}")
        old_version = current_request.version
        if task.approval:
            task.approval.status = ApprovalStatus.INVALIDATED
            self._audit(task, "APPROVAL_INVALIDATED", task.approval.subject_hash, request.version)
        task.request = request
        task.options = []
        task.selected_option_id = None
        task.approval = None
        task.booking_intent = None
        task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
        self._audit(task, "REQUEST_REVISED", old_version, request.version)
        return self._search_and_plan(task, self._policy_for(task))

    def select_option(
        self,
        task_id: str,
        option_id: str,
        *,
        business_reason: str | None = None,
    ) -> TripTask:
        """用户选中某方案，进入审批或再校验/交接路径。"""
        task = self.tasks.get(task_id)
        if task.state is not TaskState.WAITING_FOR_USER:
            raise WorkflowError(f"Cannot select an option in {task.state.value}")
        option = next((item for item in task.options if item.option_id == option_id), None)
        if option is None:
            raise WorkflowError(f"Unknown option {option_id}")
        if not option.feasibility.feasible:
            raise WorkflowError("INV-002: an infeasible option cannot be selected")
        decision = option.policy_decision
        # **"禁止"要在最前面挡住，不能只看聚合结论。** 政策引擎的严重度是
        # "证据不足 > 禁止"，所以一条既违规又缺数据的方案聚合出来是
        # INSUFFICIENT_EVIDENCE；而那一档现在是可选的（走人工审批），
        # 只认 outcome 的话，被禁的方案会从"判不了"这道门溜出去。
        if decision.forbidden_rule_ids:
            raise WorkflowError("INV-003: a forbidden option cannot proceed")
        # 判不了、又拿不出可批材料的方案同样不能选：批的人看不出自己在批什么。
        unreviewable = unreviewable_reasons(decision)
        if unreviewable:
            raise WorkflowError(
                f"INV-003: an option with no reviewable policy evidence cannot proceed "
                f"({', '.join(unreviewable)})"
            )
        # 需审批和证据不足都要人来定，但定的是两件事：前者是"违规但值得破例吗"，
        # 后者是"系统查不到公司的标准，请你确认"。走同一条审批路径，
        # 材料里分开写（见 `_new_approval` 的 violations）。
        needs_human_judgment = decision.outcome in {
            PolicyOutcome.REQUIRES_APPROVAL,
            PolicyOutcome.INSUFFICIENT_EVIDENCE,
        }
        if needs_human_judgment and (not business_reason or not business_reason.strip()):
            raise WorkflowError("A business reason is required for a policy exception")

        task.selected_option_id = option_id
        self._audit(task, "OPTION_SELECTED", option_id, decision.outcome.value)
        if needs_human_judgment:
            assert business_reason is not None
            task.approval = self._new_approval(task, business_reason.strip())
            self._transition(task, TaskState.WAITING_FOR_APPROVAL)
            self._audit(
                task,
                "APPROVAL_REQUESTED",
                option_id,
                task.approval.subject_hash,
                option.inventory_refs,
                outbox=(self._approval_event(task, "APPROVAL_REQUESTED"),),
            )
            return task
        if decision.outcome is not PolicyOutcome.COMPLIANT:
            raise WorkflowError("INV-003: non-compliant option cannot proceed")

        self._transition(task, TaskState.REVALIDATING)
        return self._revalidate_selected(task)

    def decide_approval(
        self,
        task_id: str,
        *,
        approver_id: str,
        approved: bool,
        reason: str,
    ) -> TripTask:
        """审批人对例外审批作出通过/驳回。"""
        task = self.tasks.get(task_id)
        approval = task.approval
        if task.state is not TaskState.WAITING_FOR_APPROVAL or approval is None:
            raise WorkflowError("The task has no pending approval")
        if approval.approver_id != approver_id:
            raise WorkflowError("The actor is not assigned to this approval")
        if approval.status is not ApprovalStatus.PENDING:
            raise WorkflowError("The approval has already been decided")
        if self.clock() >= approval.expires_at:
            approval.status = ApprovalStatus.INVALIDATED
            self._audit(
                task,
                "APPROVAL_INVALIDATED",
                approval.subject_hash,
                "expired",
                outbox=(self._approval_event(task, "APPROVAL_INVALIDATED"),),
            )
            task.selected_option_id = None
            task.failure = "The approval expired; select an option and request approval again"
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            raise WorkflowError("The approval has expired")
        if approval.subject_hash != self._approval_subject_hash(task):
            approval.status = ApprovalStatus.INVALIDATED
            self._audit(
                task,
                "APPROVAL_INVALIDATED",
                approval.subject_hash,
                "subject_changed",
                outbox=(self._approval_event(task, "APPROVAL_INVALIDATED"),),
            )
            task.selected_option_id = None
            task.failure = "The approval subject changed; select an option again"
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            raise WorkflowError("INV-004: approval subject has changed")

        decided_at = self.clock()
        step = approval.pending_step
        if step is not None:
            step.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            step.decided_at = decided_at
            step.reason = reason
        if not approved:
            approval.status = ApprovalStatus.REJECTED
            approval.decision_reason = reason
            self._audit(
                task,
                "APPROVAL_DECIDED",
                approver_id,
                approval.status.value,
                outbox=(self._approval_event(task, "APPROVAL_DECIDED"),),
            )
            task.selected_option_id = None
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            return task

        if step is not None and approval.remaining_steps > 0:
            # 这一级批了，还有下一级：审批单还挂着，只是换了个人。任务状态不动，
            # 投影里的"当前待谁批"跟着换，下一级的收件箱才看得见它。
            approval.current_step += 1
            approval.approver_id = approval.steps[approval.current_step].approver_id
            self._audit(
                task,
                "APPROVAL_STEP_ADVANCED",
                approver_id,
                {
                    "step_index": approval.current_step,
                    "next_approver_id": approval.approver_id,
                    "step_label": approval.steps[approval.current_step].label,
                },
                outbox=(self._approval_event(task, "APPROVAL_STEP_REQUESTED"),),
            )
            return task

        approval.status = ApprovalStatus.APPROVED
        approval.decision_reason = reason
        self._audit(
            task,
            "APPROVAL_DECIDED",
            approver_id,
            approval.status.value,
            outbox=(self._approval_event(task, "APPROVAL_DECIDED"),),
        )
        self._transition(task, TaskState.REVALIDATING)
        return self._revalidate_selected(task)

    def mark_handed_off(self, task_id: str) -> TripTask:
        """标记 Booking Intent 已交接给外部 Provider。"""
        task = self.tasks.get(task_id)
        if task.state is not TaskState.READY_FOR_HANDOFF or task.booking_intent is None:
            raise WorkflowError("No validated handoff is ready")
        self._transition(task, TaskState.HANDED_OFF)
        self._audit(task, "HANDOFF_COMPLETED", task.booking_intent.intent_id, task.state.value)
        return task

    def confirm_booking(
        self,
        task_id: str,
        *,
        order_references: Sequence[str],
        total_amount: Decimal,
        currency: str,
        reported_by: str,
        booked_at: datetime | None = None,
        note: str | None = None,
    ) -> TripTask:
        """员工回填"我订好了，订单号是这个、花了这么多"。

        允许从两个状态进来：`READY_FOR_HANDOFF`（拿着链接去订了，回来直接填单号——
        这时先按交接完成记一笔，再记确认，`HANDOFF_COMPLETED` 事件不会少）和
        `HANDED_OFF`（先前点过"我去订了"，现在补单号）。交接链接过没过期在这里不管：
        链接的有效期管的是"能不能去订"，人已经订完回来了。

        一个任务只有一条确认，第二次进来直接拒绝：这条记录一写进去就进了业务指标，
        改它等于让历史曲线悄悄变形。填错了开新任务。

        **系统核不了订单号和金额。** 这里只校验形状（见 `validate_booking_confirmation_values`），
        `source` 固定为 `SELF_REPORTED`，读指标的人必须带着这一点看。
        """
        task = self.tasks.get(task_id)
        if task.booking_confirmation is not None:
            raise WorkflowError("The booking has already been confirmed for this task")
        if task.state not in {TaskState.READY_FOR_HANDOFF, TaskState.HANDED_OFF}:
            raise WorkflowError(f"Cannot confirm a booking in {task.state.value}")
        intent = task.booking_intent
        option = task.selected_option()
        if intent is None or option is None or intent.selected_option_id != option.option_id:
            raise WorkflowError("No validated handoff to confirm a booking against")
        if not reported_by or not reported_by.strip():
            raise WorkflowError("A booking confirmation must name who reported it")
        try:
            values = validate_booking_confirmation_values(
                order_references=order_references,
                total_amount=total_amount,
                currency=currency,
                booked_at=booked_at,
                now=self.clock(),
                note=note,
            )
        except BookingConfirmationValidationError as exc:
            raise WorkflowError(str(exc)) from exc

        if task.state is TaskState.READY_FOR_HANDOFF:
            self._transition(task, TaskState.HANDED_OFF)
            self._audit(task, "HANDOFF_COMPLETED", intent.intent_id, task.state.value)

        confirmation = BookingConfirmation(
            confirmation_id=str(uuid4()),
            intent_id=intent.intent_id,
            option_id=option.option_id,
            option_version=option.version,
            order_references=values.order_references,
            total_amount=values.total_amount,
            currency=values.currency,
            source=BookingConfirmationSource.SELF_REPORTED,
            reported_by=reported_by.strip(),
            reported_at=self.clock(),
            booked_at=values.booked_at,
            note=values.note,
        )
        # 先挂到聚合上再迁移：迁移会写审计并持久化整个聚合，确认记录得在那一笔里。
        task.booking_confirmation = confirmation
        self._transition(task, TaskState.BOOKING_CONFIRMED)
        self._audit(
            task,
            "BOOKING_CONFIRMED",
            {
                "order_reference_count": len(confirmation.order_references),
                "total_amount": str(confirmation.total_amount),
                "currency": confirmation.currency,
                "source": confirmation.source.value,
                "reported_by": confirmation.reported_by,
            },
            confirmation.confirmation_id,
            (intent.intent_id, option.option_id),
            # 费控、财务这类下游系统要的就是这一条：谁、哪个成本中心、花了多少、什么时候。
            outbox=(
                OutboxEventDraft(
                    event_type="BOOKING_CONFIRMED",
                    payload={
                        "task_id": task.task_id,
                        "confirmation_id": confirmation.confirmation_id,
                        "employee_id": task.employee.employee_id,
                        "cost_center": task.employee.cost_center,
                        "order_reference_count": len(confirmation.order_references),
                        "total_amount": str(confirmation.total_amount),
                        "currency": confirmation.currency,
                        "booked_at": confirmation.booked_at.isoformat(),
                        "source": confirmation.source.value,
                    },
                ),
            ),
        )
        return task

    def _search_and_plan(self, task: TripTask, policy: PolicySnapshot) -> TripTask:
        """调用 Provider 搜库存，规划可行方案并迁移状态。"""
        self._transition(task, TaskState.SEARCHING)
        task.failure = None
        task.metadata.pop("no_feasible_reasons", None)
        request = self._request(task)
        transport_legs = request.transport_legs()
        # 一段交通一次、一站住宿一次。此前住宿无论几站都只算一次，
        # 多城行程会在预算够两次时开搜、搜到第二站才发现调用用光。
        stays = request.lodging_stays()
        # 两段起再加一次"整票"搜索：一次请求问完整条行程，供应商按 IATA 票价构造
        # 规则给一个覆盖全程的价——**不是几张单程相加**。实测整票便宜 15%–76%，
        # 连普通往返都便宜 18%–23%（`reports/evaluation-runs/multicity-pricing-*/`）。
        # 整票和分段购买是两种真正不同的走法，一起摆出来由人取舍。
        wants_journey_fare = len(transport_legs) >= self.journey_fare_min_legs and hasattr(
            self.provider, "search_multi_city"
        )
        required_calls = len(transport_legs) + len(stays) + int(wants_journey_fare)
        if task.tool_calls_remaining < required_calls:
            return self._stop_for_tool_budget(
                task,
                "provider.search_inventory",
                required_calls=required_calls,
            )
        if not self._prepare_provider_operation(task, resume_operation="SEARCH"):
            return task
        try:
            # 一段一次搜索，段数由行程自己说了算。此前这里是写死的"去程一次、返程一次"
            # ——第三段没有位置可搜，规划器再能规划多城也拿不到货。
            leg_snapshots: list[InventorySnapshot] = []
            #: 每段的查询留一份，好让搜完能说出"这个快照是拿什么参数搜来的"。
            searched: list[SearchProvenance] = []
            for index, leg in enumerate(transport_legs):
                leg_query = TransportSearchQuery(
                    origin=leg.origin,
                    destination=leg.destination,
                    depart_after=leg.depart_after,
                    arrive_before=leg.arrive_before,
                )
                leg_snapshots.append(
                    self._invoke_tool(
                        task,
                        tool_name=transport_search_tool_name(index),
                        tool_kind="PROVIDER",
                        input_value=leg_query,
                        # 每轮各自绑住自己的 query：闭包晚绑定会让所有段都搜最后一段。
                        operation=lambda query=leg_query: self.provider.search_transport(
                            query
                        ),
                    )
                )
                searched.append(
                    self._transport_provenance(leg_query, leg_snapshots[-1])
                )
            journey_snapshot: InventorySnapshot | None = None
            if wants_journey_fare:
                leg_queries = [
                    TransportSearchQuery(
                        origin=leg.origin,
                        destination=leg.destination,
                        depart_after=leg.depart_after,
                        arrive_before=leg.arrive_before,
                    )
                    for leg in transport_legs
                ]
                try:
                    journey_snapshot = self._invoke_tool(
                        task,
                        tool_name="provider.search_transport.journey",
                        tool_kind="PROVIDER",
                        input_value=JourneySearchQuery(tuple(leg_queries)),
                        operation=lambda: self.provider.search_multi_city(leg_queries),
                    )
                except ProviderError as exc:
                    # **整票搜不到不算失败。** 它是一条额外的、更便宜的走法；
                    # 供应商给不出来就退回分段购买——少省一笔钱，不是这趟走不了。
                    # 分段那几次搜索已经成功，不该被这一次拖垮。
                    task.metadata["journey_fare_unavailable"] = str(exc)[:200]

            # 一站一次搜索。此前只搜一次，城市写死是 `request.destination`
            # ——多城行程的第二站根本没有被搜过。
            hotel_snapshots: list[InventorySnapshot] = []
            for index, stay in enumerate(stays):
                hotel_query = HotelSearchQuery(
                    city=stay.city,
                    check_in=stay.check_in,
                    check_out=stay.check_out,
                )
                hotel_snapshots.append(
                    self._invoke_tool(
                        task,
                        tool_name=hotel_search_tool_name(index),
                        tool_kind="PROVIDER",
                        input_value=hotel_query,
                        operation=lambda query=hotel_query: self.provider.search_hotels(
                            query
                        ),
                    )
                )
                searched.append(
                    self._hotel_provenance(hotel_query, hotel_snapshots[-1])
                )
        except ToolBudgetExceeded:
            self.provider_circuit_breaker.record_success()
            return self._stop_for_tool_budget(task, "provider.search_inventory")
        except ProviderError as exc:
            # Multi-leg recon: outbound may have succeeded before inbound/hotel failed.
            leg_recon = recon_search_legs(task.tool_calls)
            decision = classify_tool_failure(
                tool_name=str(leg_recon.get("recovery", {}).get("failed_leg") or "provider.search"),
                tool_kind="PROVIDER",
                retryable=isinstance(exc, RetryableProviderError),
                response_received=getattr(exc, "response_received", None),
                attempt=self.max_provider_attempts,
                max_attempts=self.max_provider_attempts,
                will_auto_retry=False,
            )
            # Prefer recon action when any leg already succeeded (partial search).
            if leg_recon.get("partial") or leg_recon.get("started_legs"):
                recovery_payload = leg_recon["recovery"]
            else:
                recovery_payload = decision.as_dict()
            task.failure = str(exc)
            task.metadata["search_failure_recon"] = leg_recon
            task.metadata["last_recovery"] = recovery_payload
            # Do not keep partial options from an aborted multi-leg search.
            task.options = []
            self._audit(
                task,
                "PROVIDER_SEARCH_ATTEMPTS_EXHAUSTED",
                request,
                {"failure": task.failure, "recovery": recovery_payload, "recon": leg_recon},
            )
            if isinstance(exc, RetryableProviderError):
                self.provider_circuit_breaker.record_failure()
                self._schedule_provider_retry(
                    task,
                    resume_operation="SEARCH",
                    error=exc,
                )
                return task
            self.provider_circuit_breaker.record_success()
            self._mark_provider_retry_terminal(task, status="failed")
            self._transition(task, TaskState.PROVIDER_FAILED)
            return task

        self.provider_circuit_breaker.record_success()
        snapshots = [
            *leg_snapshots,
            *([journey_snapshot] if journey_snapshot is not None else []),
            *hotel_snapshots,
        ]
        invalid_snapshots = self._invalid_snapshot_ids(snapshots)
        if invalid_snapshots:
            task.failure = "provider returned expired or invalid inventory snapshots: " + ", ".join(
                invalid_snapshots
            )
            self._mark_provider_retry_terminal(task, status="failed")
            self._transition(task, TaskState.PROVIDER_FAILED)
            self._audit(
                task,
                "INVENTORY_SNAPSHOT_REJECTED",
                tuple(invalid_snapshots),
                task.failure,
            )
            return task
        self._complete_provider_retry(task)
        self._record_searches(task, searched)
        self._record_coverage_notices(task, snapshots)
        self._audit_snapshots(task, snapshots)
        self._transition(task, TaskState.PLANNING)

        task.options = self.planner.plan(
            request=request,
            employee=task.employee,
            policy=policy,
            leg_offers=[self._transports(item) for item in leg_snapshots],
            journey_fares=_fares_from(self._transports(journey_snapshot)),
            hotel_offers=[self._hotels(item) for item in hotel_snapshots],
            profile=self._travel_profile(task),
            budget=self._budget_snapshot(task),
            now=self.clock(),
        )
        if not task.options:
            reasons = self._no_feasible_reasons(
                request,
                leg_snapshots,
                hotel_snapshots,
                policy=policy,
            )
            task.metadata["no_feasible_reasons"] = reasons
            task.failure = "; ".join(reasons)
            self._transition(task, TaskState.NO_FEASIBLE_OPTION)
            self._audit(task, "NO_FEASIBLE_OPTION", request.version, reasons)
            return task

        self._transition(task, TaskState.OPTIONS_READY)
        self._audit(
            task,
            "OPTIONS_VERIFIED",
            request.version,
            [item.option_id for item in task.options],
            tuple(ref for item in task.options for ref in item.inventory_refs),
        )
        self._transition(task, TaskState.WAITING_FOR_USER)
        return task

    def _no_feasible_reasons(
        self,
        request: TripRequestVersion,
        leg_snapshots: Sequence[InventorySnapshot],
        hotel_snapshots: Sequence[InventorySnapshot],
        *,
        policy: PolicySnapshot | None = None,
    ) -> tuple[str, ...]:
        """汇总无可行动方案的原因文案。"""
        reasons: list[str] = []
        leg_pools = [self._transports(item) for item in leg_snapshots]
        stay_pools = [self._hotels(item) for item in hotel_snapshots]
        hotels = [offer for pool in stay_pools for offer in pool]
        for index in range(planned_leg_count(request)):
            snapshot = leg_snapshots[index] if index < len(leg_snapshots) else None
            if index < len(leg_pools) and leg_pools[index]:
                continue
            reasons.append(
                self._time_window_filter_reason(snapshot)
                or f"no {_leg_phrase(request, index)} inventory matched the requested "
                "route and time window"
            )
        for index, stay in enumerate(request.lodging_stays()):
            if index < len(stay_pools) and stay_pools[index]:
                continue
            # 第一站的说法一个字没动；第二站起才把城市名写进话里。
            reasons.append(
                "no hotel inventory matched the requested city and dates"
                if index == 0
                else f"no hotel inventory matched {stay.city} for the requested dates"
            )
        if reasons:
            return tuple(reasons)

        # Inventory exists but every combination failed feasibility/policy.
        # Surface the dominant evidence gaps so operators are not left with a
        # generic message (e.g. USD flight + CNY hotel → currency evidence).
        if policy is not None:
            priced_currencies = sorted(
                {
                    *(offer.currency for pool in leg_pools for offer in pool),
                    *(item.currency for item in hotels),
                }
            )
            if priced_currencies and priced_currencies != [policy.currency]:
                reasons.append(
                    "inventory currencies "
                    f"[{', '.join(priced_currencies)}] cannot be combined under policy "
                    f"currency {policy.currency} without an approved FX snapshot "
                    "(pricing.currency → INSUFFICIENT_EVIDENCE)"
                )
            if hotels:
                unknown_cap_cities = sorted(
                    {
                        hotel.city
                        for hotel in hotels
                        if hotel.currency == policy.currency
                        and hotel.city not in policy.hotel_city_caps
                    }
                )
                if unknown_cap_cities:
                    reasons.append(
                        "no hotel nightly cap is configured for cities: "
                        + ", ".join(unknown_cap_cities)
                        + " (hotel.city.nightly_cap → INSUFFICIENT_EVIDENCE)"
                    )
            # Sample a few combos for feasibility / policy rejection codes.
            sample_reasons = self._sample_itinerary_rejection_reasons(
                request,
                policy,
                leg_pools[: planned_leg_count(request)],
                stay_pools[: len(request.lodging_stays())],
            )
            reasons.extend(sample_reasons)
        if not reasons:
            reasons.append(
                "inventory was found, but every complete itinerary failed a hard "
                "constraint or policy evidence requirement"
            )
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _time_window_filter_reason(snapshot: InventorySnapshot | None) -> str | None:
        if snapshot is None:
            return None
        blob = " ".join(snapshot.provider_warnings)
        if "filtered_by_arrive_before" in blob:
            return (
                "supplier returned flights, but all arrived after the requested "
                "arrive_by window"
            )
        if "filtered_by_depart_after" in blob:
            return (
                "supplier returned flights, but all departed before the requested "
                "departure_after window"
            )
        if "filtered_expired" in blob:
            return "supplier returned flights, but every offer had already expired"
        return None

    def _sample_itinerary_rejection_reasons(
        self,
        request: TripRequestVersion,
        policy: PolicySnapshot,
        leg_pools: Sequence[list[TransportOffer]],
        stay_pools: Sequence[list[HotelOffer]],
        *,
        sample_limit: int = 3,
        combination_limit: int = 64,
    ) -> list[str]:
        """Return a few concrete rejection codes from sampled flight×hotel combos.

        只在**每段都有货**的前提下才走到这里（上面为空的段已经各自出过话），
        所以直接按段取样即可。段数一多组合数是指数的，用 ``combination_limit``
        兜住——这是给运维看的取样，不是穷举。
        """
        from itertools import islice, product

        from corporate_travel_agent.planning.feasibility import FeasibilityValidator
        from corporate_travel_agent.policy.engine import PolicyEngine
        from corporate_travel_agent.services.repositories import NotFoundError

        sampled_legs = [pool[:sample_limit] for pool in leg_pools if pool]
        if not sampled_legs:
            return []
        sampled_stays = [pool[:sample_limit] for pool in stay_pools if pool]
        try:
            employee = self.tasks.get(request.task_id).employee
        except NotFoundError:
            employee = self.employees.get(request.traveler_id)

        validator = FeasibilityValidator()
        engine = PolicyEngine()
        counts: dict[str, int] = {}
        leg_count = len(sampled_legs)
        combinations = product(*sampled_legs, *sampled_stays)
        for combination in islice(combinations, combination_limit):
            transports = list(combination[:leg_count])
            hotels = list(combination[leg_count:])
            feasibility = validator.validate(
                request,
                transports,
                hotels,
                policy.arrival_buffer_minutes,
                now=self.clock(),
            )
            if not feasibility.feasible:
                for reason in feasibility.reasons:
                    key = f"feasibility:{reason}"
                    counts[key] = counts.get(key, 0) + 1
                continue
            decision = engine.evaluate(employee, policy, transports, hotels, now=self.clock())
            if decision.outcome in {
                PolicyOutcome.FORBIDDEN,
                PolicyOutcome.INSUFFICIENT_EVIDENCE,
            }:
                for item in decision.evidence:
                    if item.outcome in {
                        PolicyOutcome.FORBIDDEN,
                        PolicyOutcome.INSUFFICIENT_EVIDENCE,
                    }:
                        key = f"policy:{item.rule_id}={item.outcome.value}"
                        counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return [f"{code} (x{count})" for code, count in ranked[:4]]

    def _revalidate_selected(self, task: TripTask) -> TripTask:
        """对选中方案再校验价格/可用性，再生成 Booking Intent。"""
        option = task.selected_option()
        if option is None:
            raise WorkflowError("No selected option")
        if task.tool_calls_remaining < 1:
            return self._stop_for_tool_budget(task, "provider.revalidate")
        if not self._prepare_provider_operation(task, resume_operation="REVALIDATE"):
            return task
        try:
            result = self._invoke_tool(
                task,
                tool_name="provider.revalidate",
                tool_kind="PROVIDER",
                input_value={"refs": option.inventory_refs},
                operation=lambda: self.provider.revalidate(option.inventory_refs),
            )
        except ToolBudgetExceeded:
            self.provider_circuit_breaker.record_success()
            return self._stop_for_tool_budget(task, "provider.revalidate")
        except ProviderError as exc:
            task.failure = str(exc)
            self._audit(
                task,
                "PROVIDER_REVALIDATION_ATTEMPTS_EXHAUSTED",
                option.inventory_refs,
                task.failure,
            )
            if isinstance(exc, RetryableProviderError):
                self.provider_circuit_breaker.record_failure()
                self._schedule_provider_retry(
                    task,
                    resume_operation="REVALIDATE",
                    error=exc,
                )
                return task
            self.provider_circuit_breaker.record_success()
            self._mark_provider_retry_terminal(task, status="failed")
            self._transition(task, TaskState.PROVIDER_FAILED)
            return task
        self.provider_circuit_breaker.record_success()
        self._audit(task, "INVENTORY_REVALIDATED", option.inventory_refs, result)
        if result.status is RevalidationStatus.PROVIDER_FAILED:
            task.failure = "; ".join(result.warnings)
            self._mark_provider_retry_terminal(task, status="failed")
            self._transition(task, TaskState.PROVIDER_FAILED)
            return task
        if result.status in {
            RevalidationStatus.PRICE_CHANGED,
            RevalidationStatus.UNAVAILABLE,
        }:
            self._complete_provider_retry(task)
            self._transition(task, TaskState.RECONFIRMATION_REQUIRED)
            return task

        idempotency_key = stable_hash(
            {
                "task_id": task.task_id,
                "request_version": self._request(task).version,
                "option_id": option.option_id,
                "option_version": option.version,
            }
        )
        if task.booking_intent is None:
            try:
                handoff = self._invoke_tool(
                    task,
                    tool_name="provider.create_deep_link",
                    tool_kind="PROVIDER",
                    input_value={
                        "option_id": option.option_id,
                        "option_version": option.version,
                        "inventory_refs": option.inventory_refs,
                    },
                    operation=lambda: self.provider.create_deep_link(option),
                )
            except ToolBudgetExceeded:
                self.provider_circuit_breaker.record_success()
                return self._stop_for_tool_budget(task, "provider.create_deep_link")
            except ProviderError as exc:
                task.failure = str(exc)
                self._audit(
                    task,
                    "PROVIDER_HANDOFF_ATTEMPTS_EXHAUSTED",
                    option.option_id,
                    task.failure,
                )
                if isinstance(exc, RetryableProviderError):
                    self.provider_circuit_breaker.record_failure()
                    self._schedule_provider_retry(
                        task,
                        resume_operation="REVALIDATE",
                        error=exc,
                    )
                    return task
                self.provider_circuit_breaker.record_success()
                self._mark_provider_retry_terminal(task, status="failed")
                self._transition(task, TaskState.PROVIDER_FAILED)
                return task
            if not self._timezone_aware(handoff.expires_at) or handoff.expires_at <= self.clock():
                task.failure = "provider returned an expired or invalid handoff"
                self._mark_provider_retry_terminal(task, status="failed")
                self._transition(task, TaskState.PROVIDER_FAILED)
                self._audit(
                    task,
                    "PROVIDER_HANDOFF_REJECTED",
                    option.option_id,
                    task.failure,
                )
                return task
            task.booking_intent = BookingIntent(
                intent_id=str(uuid4()),
                idempotency_key=idempotency_key,
                selected_option_id=option.option_id,
                selected_option_version=option.version,
                handoff=handoff,
                revalidated_at=result.checked_at,
            )
        elif task.booking_intent.idempotency_key != idempotency_key:
            raise WorkflowError("INV-006: conflicting BookingIntent")
        self.provider_circuit_breaker.record_success()
        self._complete_provider_retry(task)
        self._pin_provenance(task, option)
        self._transition(task, TaskState.READY_FOR_HANDOFF)
        self._audit(task, "BOOKING_INTENT_CREATED", idempotency_key, task.booking_intent.intent_id)
        return task

    def process_due_provider_retries(self, *, limit: int = 20) -> tuple[str, ...]:
        """处理到期的延迟 Provider 重试队列，返回处理过的 task_id。"""
        """Run due, persisted provider retries without blocking the original request."""
        if limit < 1:
            raise ValueError("provider retry processing limit must be at least 1")
        processed: list[str] = []
        with self._delayed_retry_lock:
            now = self._aware_datetime(self.clock())
            claim_due = getattr(self.tasks, "claim_due_provider_retries", None)
            claimed = callable(claim_due)
            if claimed:
                due_tasks = list(
                    claim_due(
                        worker_id=self.provider_retry_worker_id,
                        now=now,
                        lease_duration=self.provider_retry_lease_duration,
                        limit=limit,
                    )
                )
                self._provider_retry_metrics["claimed"] += len(due_tasks)
            else:
                list_due = getattr(self.tasks, "list_due_provider_retries", None)
                if callable(list_due):
                    due_tasks = list(list_due(now=now, limit=limit))
                else:
                    due_tasks = sorted(
                        (
                            task
                            for task in self.tasks.list_tasks()
                            if task.state is TaskState.WAITING_FOR_PROVIDER
                            and self._provider_retry_is_due(task)
                        ),
                        key=lambda task: (
                            self._provider_retry_datetime(
                                (self._provider_retry_metadata(task) or {}).get("next_retry_at")
                            )
                            or now,
                            task.task_id,
                        ),
                    )[:limit]
            for task in due_tasks:
                if not claimed and not self._provider_retry_is_due(task):
                    continue
                metadata = self._provider_retry_metadata(task)
                if metadata is None:
                    continue
                due_at = self._provider_retry_datetime(metadata.get("next_retry_at"))
                if due_at is None:
                    due_at = self._provider_retry_datetime(metadata.get("lease_until"))
                if due_at is not None:
                    lag = max((now - due_at).total_seconds(), 0.0)
                    self._provider_retry_metrics["oldest_due_lag_seconds"] = max(
                        self._provider_retry_metrics["oldest_due_lag_seconds"],
                        lag,
                    )
                attempt_token = metadata.get("attempt_token")
                if claimed and not isinstance(attempt_token, str):
                    continue
                if task.state in {TaskState.SEARCHING, TaskState.REVALIDATING}:
                    self._provider_retry_metrics["reclaimed"] += 1
                    task.state = TaskState.WAITING_FOR_PROVIDER
                    metadata["status"] = "scheduled"
                resume_operation = str(metadata.get("resume_operation") or "SEARCH")
                try:
                    if resume_operation == "REVALIDATE":
                        self._transition(task, TaskState.REVALIDATING)
                        self._revalidate_selected(task)
                    else:
                        self._search_and_plan(task, self._policy_for(task))
                    processed.append(task.task_id)
                    self._provider_retry_metrics["completed"] += 1
                    retry = self._provider_retry_metadata(task) or {}
                    if retry.get("status") == "exhausted":
                        self._provider_retry_metrics["exhausted"] += 1
                except ConcurrentUpdateError:
                    self._provider_retry_metrics["lease_conflict"] += 1
                finally:
                    if claimed and isinstance(attempt_token, str):
                        release = getattr(self.tasks, "release_provider_retry_claim", None)
                        if callable(release) and not release(
                            task,
                            attempt_token=attempt_token,
                        ):
                            self._provider_retry_metrics["lease_conflict"] += 1
        return tuple(processed)

    def provider_retry_metrics(self) -> dict[str, int | float]:
        """返回延迟重试工作器指标快照。"""
        return dict(self._provider_retry_metrics)

    def provider_retry_status(self, task: TripTask) -> dict[str, object] | None:
        """公开可读的 Provider 延迟重试状态；无则 None。"""
        metadata = self._provider_retry_metadata(task)
        if metadata is None:
            return None
        return {
            **self._public_provider_retry_metadata(metadata),
            "circuit": self.provider_circuit_breaker.snapshot(),
        }

    def _prepare_provider_operation(self, task: TripTask, *, resume_operation: str) -> bool:
        """准备 Provider 操作；熔断打开时可能调度延迟重试。"""
        metadata = self._provider_retry_metadata(task)
        if (
            metadata is not None
            and metadata.get("status") == "scheduled"
            and self._retry_attempt_count(metadata)
            >= self.delayed_provider_retry_policy.max_attempts
        ):
            self._schedule_provider_retry(task, resume_operation=resume_operation)
            return False
        if not self.provider_circuit_breaker.try_acquire():
            task.failure = "Provider is temporarily unavailable; a delayed retry is scheduled"
            self._schedule_provider_retry(task, resume_operation=resume_operation)
            return False

        if metadata is None or metadata.get("status") != "scheduled":
            return True
        completed = self._retry_attempt_count(metadata)
        metadata.update(
            {
                "status": "running",
                "delayed_attempts_completed": completed + 1,
                "next_retry_at": None,
                "last_attempt_started_at": self.clock().isoformat(),
            }
        )
        self._audit(
            task,
            "PROVIDER_DELAYED_RETRY_STARTED",
            {
                "attempt": completed + 1,
                "max_attempts": self.delayed_provider_retry_policy.max_attempts,
                "resume_operation": resume_operation,
            },
            self._public_provider_retry_metadata(metadata),
        )
        return True

    def _schedule_provider_retry(
        self,
        task: TripTask,
        *,
        resume_operation: str,
        error: RetryableProviderError | None = None,
    ) -> None:
        """把任务排入延迟 Provider 重试队列。"""
        existing = self._provider_retry_metadata(task) or {}
        completed = self._retry_attempt_count(existing)
        now = self._aware_datetime(self.clock())
        if completed >= self.delayed_provider_retry_policy.max_attempts:
            metadata = {
                **existing,
                "schema_version": 1,
                "status": "exhausted",
                "resume_operation": resume_operation,
                "delayed_attempts_completed": completed,
                "max_delayed_attempts": self.delayed_provider_retry_policy.max_attempts,
                "next_retry_at": None,
                "exhausted_at": now.isoformat(),
                "last_error_code": (
                    error.error_code if error else existing.get("last_error_code")
                ),
                "last_error_layer": (
                    error.layer if error else existing.get("last_error_layer")
                ),
                "user_message": "供应商持续不可用，自动重试已用尽，请稍后手动重试。",
            }
            self._set_provider_retry_metadata(task, metadata)
            self._transition(task, TaskState.PROVIDER_FAILED)
            self._audit(
                task,
                "PROVIDER_DELAYED_RETRIES_EXHAUSTED",
                {
                    "completed": completed,
                    "max_attempts": self.delayed_provider_retry_policy.max_attempts,
                },
                self._public_provider_retry_metadata(metadata),
            )
            return

        delay_seconds = self.delayed_provider_retry_policy.delay_after(completed)
        next_retry_at = now + timedelta(seconds=delay_seconds)
        circuit = self.provider_circuit_breaker.snapshot()
        circuit_open_until = self._provider_retry_datetime(circuit.get("open_until"))
        if circuit_open_until is not None and circuit_open_until > next_retry_at:
            next_retry_at = circuit_open_until
        metadata = {
            **existing,
            "schema_version": 1,
            "status": "scheduled",
            "resume_operation": resume_operation,
            "delayed_attempts_completed": completed,
            "max_delayed_attempts": self.delayed_provider_retry_policy.max_attempts,
            "retry_schedule_seconds": list(
                self.delayed_provider_retry_policy.delays_seconds[
                    : self.delayed_provider_retry_policy.max_attempts
                ]
            ),
            "next_retry_at": next_retry_at.isoformat(),
            "last_failure_at": now.isoformat(),
            "last_error_code": error.error_code if error else "PROVIDER_CIRCUIT_OPEN",
            "last_error_layer": error.layer if error else "provider_circuit_breaker",
            "circuit_open_until": circuit.get("open_until"),
            "user_message": "供应商暂时不可用，任务会在后台自动重试。",
        }
        self._set_provider_retry_metadata(task, metadata)
        self._transition(task, TaskState.WAITING_FOR_PROVIDER)
        self._audit(
            task,
            "PROVIDER_DELAYED_RETRY_SCHEDULED",
            {
                "next_attempt": completed + 1,
                "max_attempts": self.delayed_provider_retry_policy.max_attempts,
                "resume_operation": resume_operation,
            },
            self._public_provider_retry_metadata(metadata),
        )

    def _complete_provider_retry(self, task: TripTask) -> None:
        """完成一次延迟 Provider 重试并清理元数据。"""
        metadata = self._provider_retry_metadata(task)
        if metadata is None or metadata.get("status") not in {"running", "scheduled"}:
            return
        metadata.update(
            {
                "status": "recovered",
                "next_retry_at": None,
                "recovered_at": self.clock().isoformat(),
                "user_message": "供应商已恢复，任务已继续执行。",
            }
        )
        self._audit(
            task,
            "PROVIDER_DELAYED_RETRY_RECOVERED",
            {
                "completed": self._retry_attempt_count(metadata),
                "resume_operation": metadata.get("resume_operation"),
            },
            self._public_provider_retry_metadata(metadata),
        )

    def _mark_provider_retry_terminal(self, task: TripTask, *, status: str) -> None:
        metadata = self._provider_retry_metadata(task)
        if metadata is None:
            return
        metadata.update(
            {
                "status": status,
                "next_retry_at": None,
                "finished_at": self.clock().isoformat(),
            }
        )

    def _provider_retry_is_due(self, task: TripTask) -> bool:
        metadata = self._provider_retry_metadata(task)
        if metadata is None or metadata.get("status") != "scheduled":
            return False
        next_retry_at = self._provider_retry_datetime(metadata.get("next_retry_at"))
        return next_retry_at is not None and next_retry_at <= self._aware_datetime(self.clock())

    @staticmethod
    def _provider_retry_metadata(task: TripTask) -> dict[str, Any] | None:
        value = task.metadata.get(PROVIDER_RETRY_METADATA_KEY)
        return value if isinstance(value, dict) else None

    @staticmethod
    def _set_provider_retry_metadata(task: TripTask, metadata: dict[str, Any]) -> None:
        task.metadata[PROVIDER_RETRY_METADATA_KEY] = metadata

    @staticmethod
    def _retry_attempt_count(metadata: dict[str, Any]) -> int:
        value = metadata.get("delayed_attempts_completed", 0)
        if not isinstance(value, (int, str)):
            return 0
        try:
            return max(int(value), 0)
        except ValueError:
            return 0

    @staticmethod
    def _public_provider_retry_metadata(metadata: dict[str, Any]) -> dict[str, object]:
        keys = (
            "status",
            "resume_operation",
            "delayed_attempts_completed",
            "max_delayed_attempts",
            "next_retry_at",
            "last_error_code",
            "last_error_layer",
            "user_message",
        )
        return {key: metadata.get(key) for key in keys}

    @classmethod
    def _provider_retry_datetime(cls, value: object) -> datetime | None:
        if isinstance(value, datetime):
            return cls._aware_datetime(value)
        if not isinstance(value, str):
            return None
        try:
            return cls._aware_datetime(datetime.fromisoformat(value))
        except ValueError:
            return None

    @staticmethod
    def _aware_datetime(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value

    def _restore_provider_circuit(self) -> None:
        waiting = self._tasks_by_state(TaskState.WAITING_FOR_PROVIDER)
        for task in waiting:
            metadata = self._provider_retry_metadata(task)
            if metadata is None:
                continue
            open_until = self._provider_retry_datetime(metadata.get("circuit_open_until"))
            if open_until is not None:
                self.provider_circuit_breaker.restore_open_until(open_until)

    def _tasks_by_state(self, state: TaskState, *, limit: int = 10_000) -> tuple[TripTask, ...]:
        list_by_state = getattr(self.tasks, "list_by_state", None)
        if callable(list_by_state):
            return list_by_state(state.value, limit=limit)
        return tuple(task for task in self.tasks.list_tasks() if task.state is state)

    def recover_interrupted_tasks(self) -> tuple[str, ...]:
        """恢复卡在 STARTED/中断态的任务，返回受影响 task_id。"""
        recovered: list[str] = []
        candidates = self.tasks.list_tasks()
        for task in candidates:
            interrupted_state = task.state
            started_calls = [
                record for record in task.tool_calls if record.status is ToolCallStatus.STARTED
            ]
            transient_state = task.state in {
                TaskState.SEARCHING,
                TaskState.PLANNING,
                TaskState.REVALIDATING,
            }
            if not transient_state and not (
                task.state is TaskState.DRAFT and started_calls
            ):
                continue
            activity_times = [
                record.completed_at or record.started_at
                for record in task.tool_calls
                if record.completed_at is not None or record.started_at is not None
            ]
            if not activity_times:
                # Without a persisted lease or activity timestamp this task may
                # belong to another live worker; do not steal it during startup.
                continue
            latest_activity = max(self._aware_datetime(value) for value in activity_times)
            age_seconds = (self._aware_datetime(self.clock()) - latest_activity).total_seconds()
            if age_seconds < self.interrupted_task_stale_seconds:
                continue
            for record in started_calls:
                record.status = ToolCallStatus.FAILED
                record.completed_at = self.clock()
                record.error_type = "InterruptedToolCall"
                decision = classify_tool_failure(
                    tool_name=record.tool_name,
                    tool_kind=record.tool_kind,
                    retryable=False,
                    response_received=None,
                    attempt=1,
                    max_attempts=1,
                    will_auto_retry=False,
                    interrupted=True,
                )
                record.side_effect_class = decision.side_effect_class.value
                record.recovery_action = decision.recovery_action.value
                record.retryable = False
            leg_recon = recon_search_legs(task.tool_calls)
            had_in_flight_call = bool(started_calls)
            task.failure = (
                "Interrupted external call recovered after process restart"
                if had_in_flight_call
                else "Interrupted workflow recovered after process restart"
            )
            task.metadata["search_failure_recon"] = leg_recon
            task.metadata["last_recovery"] = {
                "side_effect_class": (
                    SideEffectClass.EXTERNAL_WRITE_POSSIBLE.value
                    if had_in_flight_call
                    else SideEffectClass.EXTERNAL_READ.value
                ),
                "recovery_action": RecoveryAction.RETRY_AFTER_RECON.value,
                "retryable": False,
                "reason": (
                    "process_restart_interrupted_tool"
                    if had_in_flight_call
                    else "process_restart_incomplete_state_transition"
                ),
                "successful_legs": leg_recon.get("successful_legs") or [],
                "failed_leg": leg_recon.get("recovery", {}).get("failed_leg"),
                "will_auto_retry": False,
            }
            target = (
                TaskState.NEEDS_STRUCTURED_INPUT
                if task.state is TaskState.DRAFT
                else TaskState.PROVIDER_FAILED
            )
            self._transition(task, target)
            self._audit(
                task,
                "INTERRUPTED_TASK_RECOVERED",
                tuple(record.tool_name for record in started_calls)
                or (f"state:{interrupted_state.value}",),
                {"target": target.value, "recovery": task.metadata["last_recovery"]},
            )
            recovered.append(task.task_id)
        return tuple(recovered)

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
        """调用单个工具；仅对明确可重试故障做有界重试并写审计。"""

        retry_of: int | None = None
        reason_code: str | None = None
        max_attempts = self.max_llm_attempts if tool_kind == "LLM" else self.max_provider_attempts
        for attempt in range(1, max_attempts + 1):
            with self._tool_budget_lock:
                if counts_toward_budget and task.tool_calls_used >= task.tool_call_limit:
                    raise ToolBudgetExceeded(f"Tool call budget exhausted before {tool_name}")
                record = ToolCallRecord(
                    sequence=len(task.tool_calls) + 1,
                    tool_name=tool_name,
                    tool_kind=tool_kind,
                    status=ToolCallStatus.STARTED,
                    started_at=self.clock(),
                    retry_of=retry_of,
                    reason_code=reason_code,
                    counts_toward_budget=counts_toward_budget,
                )
                task.tool_calls.append(record)

            trace_started_at = datetime.now(UTC)
            trace_started_ns = perf_counter_ns()
            trace_state_before = task.state.value
            self._audit(
                task,
                "TOOL_CALL_STARTED",
                {
                    "sequence": record.sequence,
                    "tool_name": tool_name,
                    "attempt": attempt,
                },
                {"used": task.tool_calls_used, "limit": task.tool_call_limit},
            )
            try:
                slots = self._llm_slots if tool_kind == "LLM" else self._provider_slots
                if not slots.acquire(timeout=self.tool_acquire_timeout_seconds):
                    if tool_kind == "LLM":
                        raise LanguageModelError(
                            "Language model concurrency limit reached",
                            error_code="LLM_BULKHEAD_FULL",
                            layer="orchestrator_bulkhead",
                            retryable=True,
                            response_received=False,
                        )
                    raise RetryableProviderError(
                        "Provider concurrency limit reached",
                        error_code="PROVIDER_BULKHEAD_FULL",
                        layer="orchestrator_bulkhead",
                        response_received=False,
                    )
                try:
                    result = operation()
                finally:
                    slots.release()
            except Exception as exc:
                duration_ms = (perf_counter_ns() - trace_started_ns) / 1_000_000
                error_details = self._tool_error_details(exc, tool_kind)
                retryable = (
                    bool(error_details["retryable"])
                    and attempt < max_attempts
                    and task.tool_calls_remaining > 0
                )
                recovery = classify_tool_failure(
                    tool_name=tool_name,
                    tool_kind=tool_kind,
                    retryable=bool(error_details["retryable"]),
                    response_received=error_details.get("response_received"),
                    attempt=attempt,
                    max_attempts=max_attempts,
                    will_auto_retry=retryable,
                )
                # Safety: never auto-retry when classification forbids it (e.g. write_possible).
                if recovery.side_effect_class in {
                    SideEffectClass.EXTERNAL_WRITE_POSSIBLE,
                    SideEffectClass.EXTERNAL_WRITE_CONFIRMED,
                }:
                    retryable = False
                with self._tool_budget_lock:
                    record.status = ToolCallStatus.FAILED
                    record.completed_at = self.clock()
                    record.error_type = error_details["cause_type"]
                    record.error_code = error_details["error_code"]
                    record.error_layer = error_details["error_layer"]
                    record.retryable = error_details["retryable"]
                    record.side_effect_class = recovery.side_effect_class.value
                    record.recovery_action = recovery.recovery_action.value
                error_details = {
                    **error_details,
                    "side_effect_class": recovery.side_effect_class.value,
                    "recovery_action": recovery.recovery_action.value,
                    "recovery_reason": recovery.reason,
                }
                task.metadata["last_recovery"] = recovery.as_dict()
                self._audit(
                    task,
                    "TOOL_CALL_FAILED",
                    {"sequence": record.sequence, "tool_name": tool_name},
                    {
                        **error_details,
                        "will_retry": retryable,
                    },
                )
                self._record_trace(
                    WorkflowTraceEvent(
                        kind="tool",
                        name=tool_name,
                        status="failure",
                        started_at=trace_started_at,
                        duration_ms=duration_ms,
                        state_before=trace_state_before,
                        state_after=task.state.value,
                        input_value=input_value,
                        output_value=error_details,
                        tool_kind=tool_kind,
                        tool_call_sequence=record.sequence,
                        error_type=record.error_type,
                        error_message_code=record.error_code,
                        error_layer=record.error_layer,
                        error_cause_type=error_details["cause_type"],
                        error_cause_chain=error_details["cause_chain"],
                        response_received=error_details["response_received"],
                        http_status=error_details["http_status"],
                        provider_request_id=error_details["request_id"],
                        retryable=record.retryable,
                        retry_of=retry_of,
                        reason_code=reason_code,
                        side_effect_class=recovery.side_effect_class.value,
                        recovery_action=recovery.recovery_action.value,
                    )
                )
                if not retryable:
                    raise
                retry_of = record.sequence
                reason_code = (
                    TRANSIENT_LLM_RETRY_REASON if tool_kind == "LLM" else TRANSIENT_RETRY_REASON
                )
                retry_delay = self._retry_delay_seconds(attempt)
                self._audit(
                    task,
                    "TOOL_CALL_RETRY_SCHEDULED",
                    {
                        "sequence": record.sequence,
                        "tool_name": tool_name,
                        "reason_code": reason_code,
                    },
                    {
                        "delay_ms": round(retry_delay * 1000, 3),
                        "recovery": recovery.as_dict(),
                    },
                )
                self.retry_sleep(retry_delay)
                continue

            duration_ms = (perf_counter_ns() - trace_started_ns) / 1_000_000
            with self._tool_budget_lock:
                record.status = ToolCallStatus.SUCCEEDED
                record.completed_at = self.clock()
            self._audit(
                task,
                "TOOL_CALL_SUCCEEDED",
                {"sequence": record.sequence, "tool_name": tool_name},
                {"remaining": task.tool_calls_remaining},
            )
            metadata = getattr(result, "metadata", None)
            self._record_trace(
                WorkflowTraceEvent(
                    kind="tool",
                    name=tool_name,
                    status="success",
                    started_at=trace_started_at,
                    duration_ms=duration_ms,
                    state_before=trace_state_before,
                    state_after=task.state.value,
                    input_value=input_value,
                    output_value=result,
                    evidence_refs=self._trace_evidence_refs(result, input_value),
                    tool_kind=tool_kind,
                    tool_call_sequence=record.sequence,
                    retry_of=retry_of,
                    reason_code=reason_code,
                    input_tokens=getattr(metadata, "input_tokens", None),
                    output_tokens=getattr(metadata, "output_tokens", None),
                    cached_input_tokens=getattr(metadata, "cached_input_tokens", None),
                    cache_write_input_tokens=getattr(metadata, "cache_write_input_tokens", None),
                    reasoning_output_tokens=getattr(metadata, "reasoning_output_tokens", None),
                    total_tokens=getattr(metadata, "total_tokens", None),
                )
            )
            return result
        raise WorkflowError(f"Retry loop for {tool_name} ended without a result")

    def _retry_delay_seconds(self, failed_attempt: int) -> float:
        jitter = min(max(float(self.retry_jitter()), 0.0), 1.0)
        exponential = self.retry_backoff_base_seconds * (2 ** (failed_attempt - 1))
        return min(exponential * (1 + 0.2 * jitter), 5.0)

    @staticmethod
    def _tool_error_details(exc: Exception, tool_kind: str) -> dict[str, Any]:
        if isinstance(exc, LanguageModelError):
            details = {
                "error_type": type(exc).__name__,
                **exc.trace_details(),
            }
            details["cause_type"] = details["cause_type"] or type(exc).__name__
            details["cause_chain"] = details["cause_chain"] or (type(exc).__name__,)
            return details
        if isinstance(exc, ProviderError):
            details = {
                "error_type": type(exc).__name__,
                **exc.trace_details(),
            }
            details["cause_type"] = details["cause_type"] or type(exc).__name__
            details["cause_chain"] = details["cause_chain"] or (type(exc).__name__,)
            return details
        cause_type = type(exc).__name__
        retryable = isinstance(exc, RetryableProviderError)
        return {
            "error_type": cause_type,
            "error_code": ("PROVIDER_TRANSIENT_ERROR" if retryable else "PROVIDER_ERROR"),
            "error_layer": ("provider_adapter" if tool_kind == "PROVIDER" else "tool_adapter"),
            "cause_type": cause_type,
            "cause_chain": (cause_type,),
            "retryable": retryable,
            "response_received": None,
            "http_status": None,
            "request_id": None,
        }

    def _stop_for_tool_budget(
        self,
        task: TripTask,
        operation: str,
        *,
        required_calls: int = 1,
    ) -> TripTask:
        """工具预算耗尽时进入 TOOL_BUDGET_EXHAUSTED。"""
        self._mark_provider_retry_terminal(task, status="tool_budget_exhausted")
        task.failure = (
            f"Insufficient tool-call budget for {operation}: "
            f"requires {required_calls}, remaining {task.tool_calls_remaining}, "
            f"limit {task.tool_call_limit}"
        )
        self._transition(task, TaskState.TOOL_BUDGET_EXHAUSTED)
        self._audit(
            task,
            "TOOL_BUDGET_EXHAUSTED",
            {
                "operation": operation,
                "required_calls": required_calls,
            },
            {
                "used": task.tool_calls_used,
                "remaining": task.tool_calls_remaining,
                "limit": task.tool_call_limit,
            },
        )
        return task

    def _new_approval(self, task: TripTask, business_reason: str) -> ApprovalRequest:
        """为需审批方案创建 ApprovalRequest。

        审批人要看的是**为什么轮到他来定**，而"违规了"和"系统判不了"是两个
        不同的理由。判不了的规则加 `unjudged:` 前缀写进同一串里：既不和真的
        违规规则混淆，也不用另开一个字段（那要动持久化、投影和 API）。
        """
        option = task.selected_option()
        if option is None:
            raise WorkflowError("No selected option")
        created_at = self.clock()
        violations = _approval_subject_rules(option.policy_decision)
        steps = self._approval_steps(task, self._policy_for(task), option.total_cost, violations)
        return ApprovalRequest(
            approval_id=str(uuid4()),
            subject_hash=self._approval_subject_hash(task),
            option_id=option.option_id,
            option_version=option.version,
            trip_request_version=self._request(task).version,
            policy_snapshot_id=task.policy_snapshot_id,
            employee_snapshot_id=task.employee.snapshot_id,
            violations=violations,
            business_reason=business_reason,
            approver_id=steps[0].approver_id,
            approved_price=option.total_cost,
            created_at=created_at,
            expires_at=created_at + timedelta(hours=24),
            steps=steps,
            current_step=0,
        )

    @staticmethod
    def _approval_steps(
        task: TripTask,
        policy: PolicySnapshot,
        price: Decimal,
        violations: tuple[str, ...],
    ) -> tuple[ApprovalStep, ...]:
        """审批链：直属经理永远是第一级，之后按政策的分级条件追加。

        触发条件看两样：方案总价过了某一档，或者违规/判不了的规则里有某一条
        （"判不了"的规则带 `unjudged:` 前缀，去掉前缀再比）。同一个人不会出现两次。
        """
        rule_ids = {item.removeprefix("unjudged:") for item in violations}
        steps = [ApprovalStep(approver_id=task.employee.manager_id, label="manager")]
        for tier in policy.approval_tiers:
            if not tier.applies(price, rule_ids):
                continue
            if any(step.approver_id == tier.approver_id for step in steps):
                continue
            steps.append(ApprovalStep(approver_id=tier.approver_id, label=tier.label))
        return tuple(steps)

    def _approval_event(self, task: TripTask, event_type: str) -> OutboxEventDraft:
        """给外部审批系统（或收件箱）的通知：只放 ID、金额、规则和申请理由，不放正文原文。"""
        approval = task.approval
        assert approval is not None
        option = task.selected_option()
        step = approval.pending_step
        return OutboxEventDraft(
            event_type=event_type,
            payload={
                "task_id": task.task_id,
                "approval_id": approval.approval_id,
                "status": approval.status.value,
                "approver_id": approval.approver_id,
                "step_index": approval.current_step,
                "step_label": step.label if step is not None else "manager",
                "step_count": len(approval.steps) or 1,
                "employee_id": task.employee.employee_id,
                "requester_id": task.requested_by,
                "approved_price": str(approval.approved_price),
                "currency": option.currency if option is not None else None,
                "violations": list(approval.violations),
                "business_reason": approval.business_reason,
                "decision_reason": approval.decision_reason,
                "expires_at": approval.expires_at.isoformat(),
            },
        )

    def _approval_subject_hash(self, task: TripTask) -> str:
        option = task.selected_option()
        if option is None:
            raise WorkflowError("No selected option")
        return stable_hash(
            {
                "trip_request_version": self._request(task).version,
                "employee_snapshot_id": task.employee.snapshot_id,
                "policy_snapshot_id": task.policy_snapshot_id,
                "option_id": option.option_id,
                "option_version": option.version,
                "approved_price": str(option.total_cost),
                "violations": _approval_subject_rules(option.policy_decision),
            }
        )

    def _travel_profile(self, task: TripTask) -> EmployeeTravelProfileSnapshot | None:
        """这位员工的习惯画像；没接历史来源就是 None。

        **算出来就钉进任务**（`task.metadata["travel_profile"]`），此后这趟任务
        再规划多少次都用同一份。和政策快照同一个道理：员工下个月习惯变了，
        这趟任务"当初为什么这么排"仍然答得上来。

        算不出来不让整趟任务失败——画像只是排序上的加成，读历史出问题就当没有它，
        照常按这一轮说的话排。**这一层永远不该是任务失败的理由。**
        """
        if self.trip_history is None:
            return None
        pinned = task.metadata.get("travel_profile")
        if pinned is not None:
            return _travel_profile_from_metadata(pinned)
        try:
            profile = derive_travel_profile(task.employee, self.trip_history)
        except Exception:  # noqa: BLE001 — 见 docstring：画像失败不该拖垮任务
            self._audit(task, "TRAVEL_PROFILE_UNAVAILABLE", task.employee.employee_id, None)
            return None
        task.metadata["travel_profile"] = asdict(profile)
        self._audit(
            task,
            "TRAVEL_PROFILE_PINNED",
            profile.snapshot_id,
            [item.name for item in profile.preferences],
        )
        return profile

    def _budget_snapshot(self, task: TripTask) -> BudgetSnapshot | None:
        """这趟任务规划时的预算余额；没接账本、没成本中心或政策没配预算都是 None。

        **算出来就钉进任务**（`task.metadata["budget_snapshot"]`），和习惯画像同一个
        道理：别人下一秒确认了一单、余额变了，这趟"当初为什么这么判"仍然答得上来。
        算不出来（账本读失败）不让任务失败——那一刻规则判成"判不了"，请人定。
        """
        pinned = task.metadata.get("budget_snapshot")
        if pinned is not None:
            return _budget_snapshot_from_metadata(pinned)
        if self.budget_ledger is None:
            return None
        try:
            snapshot = derive_budget_snapshot(
                task.employee, self._policy_for(task), self.budget_ledger, now=self.clock()
            )
        except Exception:  # noqa: BLE001 — 见 docstring：账本失败不该拖垮任务
            self._audit(task, "BUDGET_SNAPSHOT_UNAVAILABLE", task.employee.employee_id, None)
            return None
        if snapshot is None:
            return None
        task.metadata["budget_snapshot"] = asdict(snapshot)
        self._audit(
            task,
            "BUDGET_SNAPSHOT_PINNED",
            snapshot.snapshot_id,
            {"remaining": str(snapshot.remaining), "currency": snapshot.currency},
        )
        return snapshot

    def _policy_for(self, task: TripTask) -> PolicySnapshot:
        """加载任务绑定的政策快照。"""
        try:
            policy = self.policies.snapshot(task.policy_snapshot_id)
        except KeyError as exc:
            raise WorkflowError("Historical policy snapshot is unavailable") from exc
        expected_hash = task.metadata.get("policy_content_hash")
        if expected_hash and expected_hash != policy.content_hash:
            raise WorkflowError("Historical policy snapshot content has changed")
        return policy

    @staticmethod
    def _request(task: TripTask) -> TripRequestVersion:
        if task.request is None:
            raise WorkflowError("The task does not have a complete TripRequestVersion")
        return task.request

    def _transition(self, task: TripTask, target: TaskState) -> None:
        """经状态机校验后迁移任务状态。"""
        previous = task.state
        task.state = self.state_machine.transition(previous, target)
        self._audit(task, "STATE_TRANSITION", previous.value, target.value)

    def _audit(
        self,
        task: TripTask,
        event_type: str,
        input_value: object,
        output_value: object,
        evidence_refs: tuple[str, ...] = (),
        *,
        outbox: Sequence[OutboxEventDraft] = (),
    ) -> None:
        """写审计事件并可选推送轨迹观察者。

        `outbox` 是要**随这次状态变化一起**进发件箱的事件：仓储把任务更新、审计事件和
        发件箱事件放进同一笔事务。审批单建了却没通知出去、或通知出去了审批单其实没建成，
        都是不能接受的半截。
        """
        event = new_audit_event(
            task.task_id,
            event_type,
            input_value=input_value,
            output_value=output_value,
            evidence_refs=evidence_refs,
        )
        if outbox:
            self.tasks.record(task, event, outbox_events=tuple(outbox))
        else:
            self.tasks.record(task, event)
        if event_type.startswith("TOOL_CALL_"):
            return
        state_before = task.state.value
        state_after = task.state.value
        if event_type == "STATE_TRANSITION":
            state_before = str(input_value)
            state_after = str(output_value)
        self._record_trace(
            WorkflowTraceEvent(
                kind=self._trace_kind(event_type),
                name=event_type,
                status="success",
                started_at=datetime.now(UTC),
                duration_ms=0.0,
                state_before=state_before,
                state_after=state_after,
                input_value=input_value,
                output_value=output_value,
                evidence_refs=evidence_refs,
            )
        )

    def _record_trace(self, event: WorkflowTraceEvent) -> None:
        """向轨迹观察者投递事件（若已配置）。"""
        if self.trace_observer is not None:
            self.trace_observer.record(event)

    @staticmethod
    def _trace_kind(event_type: str) -> str:
        if event_type == "STATE_TRANSITION":
            return "state_transition"
        if "RECOVER" in event_type or event_type.startswith("RETRY"):
            return "recovery"
        if event_type.startswith(("APPROVAL_", "OPTIONS_", "POLICY_")):
            return "policy"
        if event_type in {
            "TASK_CREATED_FROM_MESSAGE",
            "CLARIFICATION_RECEIVED",
            "STRUCTURED_FALLBACK_SUBMITTED",
        }:
            return "user"
        return "audit"

    @staticmethod
    def _trace_evidence_refs(result: object, input_value: object) -> tuple[str, ...]:
        refs: list[str] = []
        # `items` 这个名字在 InventorySnapshot 上是一串报价，在 dict 上却是个方法。
        # 工具循环的返回是 dict，直接迭代会炸——只认真正可迭代的那种。
        items = getattr(result, "items", ())
        if isinstance(items, (list, tuple)):
            for item in items:
                ref_id = getattr(item, "ref_id", None)
                if isinstance(ref_id, str):
                    refs.append(ref_id)
        if isinstance(result, dict):
            for option in result.get("options", ()) or ():
                ref_id = option.get("ref_id") if isinstance(option, dict) else None
                if isinstance(ref_id, str):
                    refs.append(ref_id)
        current_prices = getattr(result, "current_prices", None)
        if isinstance(current_prices, dict):
            refs.extend(str(key) for key in current_prices)
        refs.extend(str(item) for item in getattr(result, "unavailable_refs", ()))
        if isinstance(input_value, dict):
            for key in ("refs", "inventory_refs"):
                values = input_value.get(key, ())
                if isinstance(values, (list, tuple)):
                    refs.extend(str(item) for item in values)
        return tuple(dict.fromkeys(refs))

    def _record_coverage_notices(self, task: TripTask, snapshots: list[InventorySnapshot]) -> None:
        """Carry provider-declared coverage caveats through to the user.

        A snapshot that a provider labels incomplete must never be presented as
        a full search. Silently dropping these warnings makes an incomplete
        result look exhaustive, so they are preserved as evidence-linked
        notices rather than folded into failure text.
        """

        notices = tuple(
            dict.fromkeys(
                f"{snapshot.snapshot_id}: {warning}"
                for snapshot in snapshots
                for warning in snapshot.provider_warnings
            )
        )
        if notices:
            task.metadata[PARTIAL_COVERAGE_METADATA_KEY] = notices
            self._audit(task, "PROVIDER_COVERAGE_INCOMPLETE", tuple(notices), task.state.value)
        else:
            task.metadata.pop(PARTIAL_COVERAGE_METADATA_KEY, None)

    def _pin_provenance(self, task: TripTask, option: TravelOptionVersion) -> None:
        """交接这一刻，把这条方案的依据链算一个哈希钉住。

        **钉的是哈希，不是那份记录本身。** 记录始终由已落库的不可变对象推导
        （`services/provenance.py`），另存一份就等于多一个会和事实分叉的事实源。
        存下指纹就够了：以后任何时候重算再比对，能改的地方都会露出来。
        """
        record = option_provenance(
            task=task,
            option=option,
            snapshots=self.tasks.snapshots(task.task_id),
            events=self.tasks.events(task.task_id),
            policy=self._policy_for(task),
        )
        digest = provenance_hash(record)
        task.metadata["provenance"] = {
            "option_id": option.option_id,
            "option_version": option.version,
            "hash": digest,
            "pinned_at": self.clock().isoformat(),
            # 缺口一并钉住：当时就说不清的地方，事后不该被悄悄补上。
            "gaps": list(record["gaps"]),
        }
        self._audit(task, "PROVENANCE_PINNED", option.option_id, digest)

    def verify_provenance(self, task_id: str) -> dict[str, Any]:
        """重算这条链，和交接时钉住的指纹比对。

        对得上，说明从交接到现在没有任何一环被改过；对不上，返回值会说清是
        哪一份记录变了——**它不自己修复，也不解释原因**，那是人要看的东西。
        """
        task = self.tasks.get(task_id)
        pinned = task.metadata.get("provenance")
        if not pinned:
            return {"status": "NOT_PINNED", "task_id": task_id}
        option = next(
            (item for item in task.options if item.option_id == pinned["option_id"]),
            None,
        )
        if option is None:
            return {"status": "OPTION_MISSING", "task_id": task_id, "pinned": pinned}
        record = option_provenance(
            task=task,
            option=option,
            snapshots=self.tasks.snapshots(task_id),
            events=self.tasks.events(task_id),
            policy=self._policy_for(task),
        )
        digest = provenance_hash(record)
        return {
            "status": "MATCH" if digest == pinned["hash"] else "MISMATCH",
            "task_id": task_id,
            "option_id": pinned["option_id"],
            "pinned_hash": pinned["hash"],
            "recomputed_hash": digest,
            "pinned_at": pinned["pinned_at"],
        }

    def option_provenance_record(self, task_id: str, option_id: str) -> dict[str, Any]:
        """取一条方案的依据链。任何时候都能取，不必等到交接。"""
        task = self.tasks.get(task_id)
        option = next(
            (item for item in task.options if item.option_id == option_id), None
        )
        if option is None:
            raise WorkflowError(f"Unknown option {option_id}")
        return option_provenance(
            task=task,
            option=option,
            snapshots=self.tasks.snapshots(task_id),
            events=self.tasks.events(task_id),
            policy=self._policy_for(task),
        )

    def _record_searches(
        self, task: TripTask, searches: Sequence[SearchProvenance]
    ) -> None:
        """把这一轮的搜索出处并进任务，按 snapshot_id 去重。

        去重是必要的：同一条任务会多轮搜索，重验也会再走一次，而快照是不可变的
        ——同一个 snapshot_id 的出处不该记两遍。
        """
        known = {item.snapshot_id for item in task.searches}
        for item in searches:
            if item.snapshot_id in known:
                continue
            known.add(item.snapshot_id)
            task.searches.append(item)

    @staticmethod
    def _transport_provenance(
        query: TransportSearchQuery, snapshot: InventorySnapshot
    ) -> SearchProvenance:
        """结构化入口的出处。

        这条路上**没有用户原话可抄**——日期来自已经校验过的 `TripRequestVersion`，
        不是从一句话里读出来的。`date_evidence` 因此留空，如实反映"这一天的依据
        在请求版本里，不在对话里"。
        """
        return SearchProvenance(
            kind="transport",
            parameters=(
                ("origin", query.origin),
                ("destination", query.destination),
                ("depart_after", query.depart_after.isoformat()),
                ("arrive_by", query.arrive_before.isoformat()),
            ),
            snapshot_id=snapshot.snapshot_id,
            query_hash=snapshot.query_hash,
            captured_at=snapshot.captured_at,
            valid_until=snapshot.valid_until,
        )

    @staticmethod
    def _hotel_provenance(
        query: HotelSearchQuery, snapshot: InventorySnapshot
    ) -> SearchProvenance:
        return SearchProvenance(
            kind="hotel",
            parameters=(
                ("city", query.city),
                ("check_in", query.check_in.isoformat()),
                ("check_out", query.check_out.isoformat()),
            ),
            snapshot_id=snapshot.snapshot_id,
            query_hash=snapshot.query_hash,
            captured_at=snapshot.captured_at,
            valid_until=snapshot.valid_until,
        )

    def _audit_snapshots(self, task: TripTask, snapshots: list[InventorySnapshot]) -> None:
        for snapshot in snapshots:
            self.tasks.add_snapshot(task.task_id, snapshot)
            self._audit(
                task,
                "INVENTORY_SNAPSHOT_CAPTURED",
                snapshot.query_hash,
                snapshot.raw_payload_hash,
                tuple(item.ref_id for item in snapshot.items),
            )

    def _invalid_snapshot_ids(self, snapshots: list[InventorySnapshot]) -> list[str]:
        observed_at = self.clock()
        return [
            snapshot.snapshot_id
            for snapshot in snapshots
            if not self._timezone_aware(snapshot.captured_at)
            or not self._timezone_aware(snapshot.valid_until)
            or snapshot.valid_until <= snapshot.captured_at
            or snapshot.valid_until <= observed_at
        ]

    @staticmethod
    def _transports(snapshot: InventorySnapshot | None) -> list[TransportOffer]:
        if snapshot is None:
            return []
        return [item for item in snapshot.items if isinstance(item, TransportOffer)]

    @staticmethod
    def _hotels(snapshot: InventorySnapshot | None) -> list[HotelOffer]:
        if snapshot is None:
            return []
        return [item for item in snapshot.items if isinstance(item, HotelOffer)]


def _budget_snapshot_from_metadata(payload: dict) -> BudgetSnapshot:
    """从任务元数据里把钉住的预算快照读回来（金额和日期在 JSON 里是字符串）。"""
    return BudgetSnapshot(
        snapshot_id=str(payload["snapshot_id"]),
        cost_center=str(payload["cost_center"]),
        currency=str(payload["currency"]),
        limit=Decimal(str(payload["limit"])),
        spent=Decimal(str(payload["spent"])),
        period_from=date.fromisoformat(str(payload["period_from"])),
        period_to=date.fromisoformat(str(payload["period_to"])),
        computed_at=datetime.fromisoformat(str(payload["computed_at"])),
        source=str(payload.get("source", "booking_confirmations")),
    )


def _travel_profile_from_metadata(payload: dict) -> EmployeeTravelProfileSnapshot:
    """把钉在任务上的画像读回来。

    任务从数据库恢复时 metadata 是纯 JSON，`origin` 变回了字符串。这里显式还原成
    枚举——不还原的话，`planning/preferences.py` 里那张按枚举查的权重表会查不到，
    画像**静默失效**：不报错，只是排序悄悄变回没有画像的样子。
    """
    return EmployeeTravelProfileSnapshot(
        snapshot_id=payload["snapshot_id"],
        employee_id=payload["employee_id"],
        profile_version=payload.get("profile_version", 1),
        preferences=tuple(
            ProfilePreference(
                name=item["name"],
                origin=PreferenceOrigin(item["origin"]),
                evidence=item["evidence"],
            )
            for item in payload.get("preferences", ())
        ),
        preferred_hotels=tuple(payload.get("preferred_hotels", ())),
        cost_center=payload.get("cost_center"),
        derived_from_trips=payload.get("derived_from_trips", 0),
    )

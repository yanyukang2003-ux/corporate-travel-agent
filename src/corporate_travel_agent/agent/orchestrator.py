"""orchestrator：差旅工作流编排器（TripWorkflowOrchestrator）。

有界 Agent 循环的确定性控制器：按 TaskState 观察/决策，调用 Provider 与 LLM 端口，
再经确定性规划与政策校验后持久化并暂停。V1 不代付、不改签、不出票。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from random import random
from threading import BoundedSemaphore, RLock
from time import perf_counter_ns, sleep
from typing import Any, TypeVar
from uuid import uuid4

from corporate_travel_agent.agent.error_recovery import (
    RecoveryAction,
    RecoveryDecision,
    SideEffectClass,
    classify_tool_failure,
    hotel_search_tool_name,
    recon_search_legs,
    transport_search_tool_name,
)
from corporate_travel_agent.agent.ports import (
    LanguageModelError,
    LanguageModelPort,
    SemanticLanguageModelPort,
    WorkflowTraceEvent,
    WorkflowTraceObserverPort,
)
from corporate_travel_agent.domain.enums import (
    ApprovalStatus,
    IntentEntrypoint,
    LodgingRequirement,
    PolicyOutcome,
    RevalidationStatus,
    TaskState,
    ToolCallStatus,
)
from corporate_travel_agent.domain.models import (
    ApprovalRequest,
    BookingIntent,
    ConversationMessage,
    HotelOffer,
    InventorySnapshot,
    PolicySnapshot,
    ToolCallRecord,
    TransportOffer,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.domain.validation import (
    validate_trip_request,
    validate_trip_request_values,
)
from corporate_travel_agent.planning.feasibility import leg_spec, planned_leg_count
from corporate_travel_agent.planning.planner import ItineraryPlanner
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
    TravelInventoryProvider,
)
from corporate_travel_agent.services.audit import new_audit_event, stable_hash
from corporate_travel_agent.services.locations import CityNormalizer
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


def _is_full_route_revision_message(message: str) -> bool:
    """检测替换路线消息，需重置路线相关约束。"""
    text = message.casefold()
    english_route = " to " in text and any(
        token in text for token in ("book ", "rebook", "change route", "new route")
    )
    chinese_route = any(
        token in text for token in ("改飞", "改签", "改订", "重新订", "改为", "改成")
    ) and ("到" in text or "去" in text)
    return english_route or chinese_route


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
        language_model: LanguageModelPort | None = None,
        semantic_language_model: SemanticLanguageModelPort | None = None,
        planner: ItineraryPlanner | None = None,
        state_machine: StateMachine | None = None,
        clock: Callable[[], datetime] | None = None,
        timezone_name: str = "Asia/Shanghai",
        max_clarification_rounds: int = 5,
        max_tool_calls: int = 12,
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
        self.language_model = language_model
        self.semantic_language_model = semantic_language_model
        if semantic_language_model is not None:
            from corporate_travel_agent.agent.semantic_intent import (
                ConversationIntentInterpreter,
            )

            self.intent_interpreter = ConversationIntentInterpreter(semantic_language_model)
        else:
            self.intent_interpreter = None
        self.fallback_model = getattr(language_model, "fallback_model", None)
        self.llm_runtime_status = "unknown"
        self.planner = planner or ItineraryPlanner()
        self.state_machine = state_machine or StateMachine()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timezone_name = timezone_name
        self.max_clarification_rounds = max_clarification_rounds
        self.max_tool_calls = max_tool_calls
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

    def create_task(self, request: TripRequestVersion) -> TripTask:
        """用结构化 TripRequest 建任务并立即搜索规划。"""
        request = self._canonicalize_request_cities(request)
        validate_trip_request(request).require_valid()
        employee = self.employees.snapshot(request.traveler_id)
        policy = self.policies.current()
        task = TripTask(
            task_id=request.task_id,
            state=TaskState.DRAFT,
            request=request,
            employee=employee,
            policy_snapshot_id=policy.snapshot_id,
            tool_call_limit=self.max_tool_calls,
            metadata={
                "policy_content_hash": policy.content_hash,
                "intent_entrypoint": IntentEntrypoint.STRUCTURED.value,
            },
        )
        self.tasks.add(task)
        self._audit(task, "TASK_CREATED", request, {"state": task.state.value})
        return self._search_and_plan(task, policy)

    def create_task_from_message(
        self,
        message: str,
        *,
        traveler_id: str,
        task_id: str | None = None,
    ) -> TripTask:
        """用自然语言首条消息建任务，进入意图抽取与后续流程。"""
        message = self._validate_message(message)
        if self.language_model is None:
            raise LanguageModelUnavailable("No language model adapter is configured")
        employee = self.employees.snapshot(traveler_id)
        policy = self.policies.current()
        task = TripTask(
            task_id=task_id or str(uuid4()),
            state=TaskState.DRAFT,
            request=None,
            employee=employee,
            policy_snapshot_id=policy.snapshot_id,
            intent_fields=self._empty_intent_fields(),
            messages=[ConversationMessage(role="user", content=message)],
            tool_call_limit=self.max_tool_calls,
            metadata={
                "policy_content_hash": policy.content_hash,
                "intent_entrypoint": IntentEntrypoint.LEGACY.value,
            },
        )
        self.tasks.add(task)
        self._audit(task, "TASK_CREATED_FROM_MESSAGE", message, {"state": task.state.value})
        return self._extract_and_continue(task, message)

    def create_task_from_semantic_message(
        self,
        message: str,
        *,
        traveler_id: str,
        task_id: str | None = None,
    ) -> TripTask:
        """用新语义入口创建任务；只运行完整对话解释流程。"""
        message = self._validate_message(message)
        if self.intent_interpreter is None:
            raise LanguageModelUnavailable("No semantic language model adapter is configured")
        employee = self.employees.snapshot(traveler_id)
        policy = self.policies.current()
        task = TripTask(
            task_id=task_id or str(uuid4()),
            state=TaskState.DRAFT,
            request=None,
            employee=employee,
            policy_snapshot_id=policy.snapshot_id,
            intent_fields=self._empty_intent_fields(),
            messages=[ConversationMessage(role="user", content=message)],
            tool_call_limit=self.max_tool_calls,
            metadata={
                "policy_content_hash": policy.content_hash,
                "intent_entrypoint": IntentEntrypoint.SEMANTIC.value,
            },
        )
        self.tasks.add(task)
        self._audit(
            task,
            "SEMANTIC_TASK_CREATED_FROM_MESSAGE",
            message,
            {"state": task.state.value},
        )
        return self._interpret_and_continue_semantically(task)

    def submit_message(self, task_id: str, message: str) -> TripTask:
        """提交跟进消息：澄清、改意图或再抽取后继续。"""
        message = self._validate_message(message)
        task = self.tasks.get(task_id)
        self._require_intent_entrypoint(task, IntentEntrypoint.LEGACY)
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
            raise WorkflowError(f"Cannot submit clarification in {task.state.value}")
        prior_state = task.state
        # Structured option answers can proceed without an LLM (AskUserQuestion path).
        if (
            prior_state is TaskState.NEEDS_CLARIFICATION
            and self.language_model is None
        ):
            self._audit(task, "CLARIFICATION_RECEIVED", message, task.clarification_rounds)
            self._transition(task, TaskState.DRAFT)
            task.messages.append(ConversationMessage(role="user", content=message))
            applied = self._try_apply_clarification_options(task, message)
            if applied is not None:
                return applied
            raise LanguageModelUnavailable(
                "No language model adapter is configured; answer with a listed option "
                "label/letter, or configure OPENAI_API_KEY for free-text clarification"
            )
        if self.language_model is None:
            raise LanguageModelUnavailable("No language model adapter is configured")
        if prior_state is TaskState.NEEDS_CLARIFICATION:
            self._audit(task, "CLARIFICATION_RECEIVED", message, task.clarification_rounds)
        elif prior_state is TaskState.NEEDS_STRUCTURED_INPUT:
            # Re-open conversation after structured fallback.
            # Keep intent_fields as merge priors; clear terminal failure only.
            task.failure = None
            task.clarification_question = None
            task.metadata["structured_input_recovery"] = {
                "from_state": prior_state.value,
                "side_effect_class": SideEffectClass.TASK_LOCAL.value,
                "recovery_action": RecoveryAction.CLARIFY.value,
                "reason": "user_message_reopens_structured_fallback",
            }
            self._audit(
                task,
                "STRUCTURED_INPUT_RECOVERY",
                message,
                {"from_state": prior_state.value},
            )
        elif prior_state is TaskState.OUT_OF_SCOPE:
            task.failure = None
            task.clarification_question = None
            task.metadata["oos_reopen"] = {
                "from_state": prior_state.value,
                "side_effect_class": SideEffectClass.TASK_LOCAL.value,
                "recovery_action": RecoveryAction.CLARIFY.value,
                "reason": "user_message_reopens_out_of_scope",
            }
            self._audit(
                task,
                "OUT_OF_SCOPE_REOPEN",
                message,
                {"from_state": prior_state.value},
            )
        else:
            # Post-search revision: keep products until replacement intent ACCEPT.
            task.failure = None
            task.clarification_question = None
            task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
            task.metadata["pending_revision"] = {
                "from_state": prior_state.value,
                "had_options": bool(task.options),
                "had_request": task.request is not None,
                "selected_option_id": task.selected_option_id,
            }
            # New rebook message typically replaces OD; clear cities so anti-fabrication
            # cannot keep the superseded route unless the model re-grounds them.
            if _is_full_route_revision_message(message):
                task.metadata.pop("intent_calibration", None)
                task.intent_fields = {
                    **task.intent_fields,
                    "origin": None,
                    "destination": None,
                    "departure_after": None,
                    "arrive_by": None,
                    "return_after": None,
                    "return_before": None,
                    "booking_scope": None,
                    # Drop hotel from prior trip unless this message re-states hotel need.
                    "hotel_check_in": None,
                    "hotel_check_out": None,
                    "lodging_requirement": LodgingRequirement.UNSPECIFIED.value,
                    # Route-scoped constraints from the superseded trip (meeting
                    # buffer, mode, hotel) must be re-grounded by this message.
                    "hard_constraints": [],
                }
            self._audit(
                task,
                "INTENT_REVISION_RECEIVED",
                message,
                {
                    "from_state": prior_state.value,
                    "copy_on_write": True,
                    "had_options": bool(task.options),
                },
            )
        self._transition(task, TaskState.DRAFT)
        task.messages.append(ConversationMessage(role="user", content=message))
        # AskUserQuestion-style: map A/B labels or known options before another LLM call.
        if prior_state is TaskState.NEEDS_CLARIFICATION:
            applied = self._try_apply_clarification_options(task, message)
            if applied is not None:
                return applied
        return self._extract_and_continue(task, message)

    def submit_semantic_message(self, task_id: str, message: str) -> TripTask:
        """向新语义任务追加消息；完整 ledger 重新解释，不执行旧字段 patch。"""
        message = self._validate_message(message)
        task = self.tasks.get(task_id)
        self._require_intent_entrypoint(task, IntentEntrypoint.SEMANTIC)
        if self.intent_interpreter is None:
            raise LanguageModelUnavailable("No semantic language model adapter is configured")
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
            raise WorkflowError(f"Cannot submit semantic message in {task.state.value}")
        prior_state = task.state
        if prior_state is TaskState.NEEDS_CLARIFICATION:
            self._audit(task, "SEMANTIC_CLARIFICATION_RECEIVED", message, task.clarification_rounds)
        elif prior_state in {TaskState.NEEDS_STRUCTURED_INPUT, TaskState.OUT_OF_SCOPE}:
            task.failure = None
            task.clarification_question = None
            self._audit(
                task,
                "SEMANTIC_CONVERSATION_REOPENED",
                message,
                {"from_state": prior_state.value},
            )
        else:
            task.failure = None
            task.clarification_question = None
            task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
            task.metadata["pending_revision"] = {
                "from_state": prior_state.value,
                "had_options": bool(task.options),
                "had_request": task.request is not None,
                "selected_option_id": task.selected_option_id,
            }
            self._audit(
                task,
                "SEMANTIC_REVISION_RECEIVED",
                message,
                task.metadata["pending_revision"],
            )
        self._transition(task, TaskState.DRAFT)
        task.messages.append(ConversationMessage(role="user", content=message))
        return self._interpret_and_continue_semantically(task)

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

    def _extract_and_continue(self, task: TripTask, message: str) -> TripTask:
        """经 Claude Code 风格参数环抽取意图并继续。

        环（本地化自 Anthropic ``toolExecution.ts``）::
            模型参数 → L1 Schema → L2 业务 → REPAIR/CLARIFY/ACCEPT → 搜索。
        """
        if self.language_model is None:
            raise LanguageModelUnavailable("No language model adapter is configured")

        from corporate_travel_agent.agent.intent_calibration import (
            MAX_INTENT_REPAIR_ATTEMPTS,
            CalibrationTrace,
            apply_safe_defaults,
            merge_model_fields_anti_fabrication,
        )
        from corporate_travel_agent.agent.param_extraction_loop import (
            INTENT_EXTRACT_TOOL_NAME,
            LoopAction,
            ParamLoopTrace,
            build_tool_use_error_repair_payload,
            decide_param_loop,
        )

        previous_calibration = task.metadata.get("intent_calibration")
        previous_provenance = (
            previous_calibration.get("field_provenance", {})
            if isinstance(previous_calibration, dict)
            else {}
        )
        calibration = CalibrationTrace(field_provenance=dict(previous_provenance))
        loop_trace = ParamLoopTrace()
        classification = str(task.metadata.get("intent_classification") or "TRIP")
        repair_missing: frozenset[str] | None = None
        repair_payload: dict[str, Any] | None = None
        assumptions: list[str] = list(task.assumptions or ())
        tried_fallback_model = False
        skip_llm = False
        local_fallback_applied = False

        self._maybe_reset_superseded_destination(task, message)
        self._maybe_reset_superseded_origin(task, message)
        from corporate_travel_agent.agent.journey_semantics import infer_booking_scope

        task.intent_fields["booking_scope"] = infer_booking_scope(
            message,
            prior_fields=task.intent_fields,
        ).value
        from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

        evidence_parser = GroundedLocalIntentParser(self.city_normalizer)
        intent_evidence = evidence_parser.extract_evidence(
            message, reference_time=self.clock()
        )
        task.metadata["intent_evidence"] = intent_evidence.as_dict()
        if self.llm_runtime_status == "billing_blocked":
            assumptions.extend(
                self._apply_local_intent(task, message, calibration=calibration)
            )
            local_fallback_applied = True
            if self._local_intent_is_search_ready(task, message):
                task.metadata["llm_recovery"] = {
                    "fallback": "local_parser",
                    "skipped": "billing_blocked",
                }
                assumptions.append("local_parser: skipped billed model")
                task.assumptions = tuple(dict.fromkeys(assumptions))
                skip_llm = True
            else:
                from corporate_travel_agent.agent.llm_failure import LLMFailureClass

                blocked = LanguageModelError(
                    "Intent extraction skipped: language model billing is blocked",
                    error_code="OPENAI_HTTP_402",
                    layer="openai_http",
                    http_status=402,
                    retryable=False,
                )
                decision = classify_tool_failure(
                    tool_name=INTENT_EXTRACT_TOOL_NAME,
                    tool_kind="LLM",
                    retryable=False,
                    response_received=True,
                    attempt=1,
                    max_attempts=1,
                    will_auto_retry=False,
                )
                return self._fail_llm_extract(
                    task, message, decision, blocked, LLMFailureClass.BILLING
                )

        # Initial extract + optional schema-retry repairs (Claude Code pattern).
        for extract_index in range(0 if skip_llm else MAX_INTENT_REPAIR_ATTEMPTS + 1):
            context: dict[str, Any] = {
                "reference_time": self.clock().isoformat(),
                "timezone": self.timezone_name,
                "task_id": task.task_id,
                "traveler_id": task.employee.employee_id,
                "prior_fields": self._json_safe_fields(task.intent_fields),
                "intent_evidence": intent_evidence.as_dict(),
                "clarification_round": task.clarification_rounds,
                "max_clarification_rounds": self.max_clarification_rounds,
            }
            if repair_payload is not None:
                context["repair"] = repair_payload
                calibration.repair_attempts += 1
                calibration.repair_missing_before.append(tuple(sorted(repair_missing or ())))

            try:
                result = self._invoke_tool(
                    task,
                    tool_name=INTENT_EXTRACT_TOOL_NAME,
                    tool_kind="LLM",
                    counts_toward_budget=True,
                    input_value={
                        "message": message,
                        "task_id": task.task_id,
                        "traveler_id": task.employee.employee_id,
                        "context": {key: value for key, value in context.items() if key != "repair"}
                        | (
                            {
                                "repair_missing": sorted(repair_missing or ()),
                                "param_loop": "claude_code_param_loop_v1",
                            }
                            if repair_missing is not None
                            else {}
                        ),
                    },
                    operation=lambda context=context: self.language_model.extract_trip_intent(
                        message,
                        task_id=task.task_id,
                        traveler_id=task.employee.employee_id,
                        context=context,
                    ),
                )
            except ToolBudgetExceeded:
                return self._stop_for_tool_budget(task, INTENT_EXTRACT_TOOL_NAME)
            except LanguageModelError as exc:
                result = None
                from corporate_travel_agent.agent.llm_failure import (
                    LLMFailureClass,
                    classify_llm_failure,
                    should_try_fallback_model,
                )

                failure_class = classify_llm_failure(exc)
                self._note_llm_failure(failure_class)
                fallback_name = self.fallback_model
                if (
                    not tried_fallback_model
                    and should_try_fallback_model(failure_class)
                    and isinstance(fallback_name, str)
                    and fallback_name
                ):
                    tried_fallback_model = True
                    fallback_context = dict(context)
                    fallback_context["model_override"] = fallback_name
                    try:
                        result = self._invoke_tool(
                            task,
                            tool_name=INTENT_EXTRACT_TOOL_NAME,
                            tool_kind="LLM",
                            counts_toward_budget=True,
                            input_value={
                                "message": message,
                                "model_override": fallback_name,
                            },
                            operation=lambda fallback_context=fallback_context: (
                                self.language_model.extract_trip_intent(
                                    message,
                                    task_id=task.task_id,
                                    traveler_id=task.employee.employee_id,
                                    context=fallback_context,
                                )
                            ),
                        )
                        task.metadata["model_fallback"] = {
                            "original_model": getattr(self.language_model, "model", None),
                            "fallback_model": fallback_name,
                            "reason": failure_class.value,
                        }
                        self._note_llm_success()
                        self._audit(
                            task,
                            "LLM_MODEL_FALLBACK",
                            {"original_error": exc.error_code, "http_status": exc.http_status},
                            task.metadata["model_fallback"],
                        )
                    except (ToolBudgetExceeded, LanguageModelError):
                        result = None
                if result is None:
                    decision = classify_tool_failure(
                        tool_name=INTENT_EXTRACT_TOOL_NAME,
                        tool_kind="LLM",
                        retryable=bool(exc.retryable),
                        response_received=exc.response_received,
                        attempt=self.max_llm_attempts,
                        max_attempts=self.max_llm_attempts,
                        will_auto_retry=False,
                    )
                    task.failure = str(exc)
                    task.metadata["last_recovery"] = decision.as_dict()
                    task.metadata["llm_recovery"] = {
                        **decision.as_dict(),
                        "error_code": exc.error_code,
                        "layer": exc.layer,
                        "http_status": exc.http_status,
                        "failure_class": failure_class.value,
                    }
                    task.metadata["extract_failure"] = {
                        "class": failure_class.value,
                        "error_code": exc.error_code,
                        "http_status": exc.http_status,
                    }
                    transient_exhausted = failure_class in {
                        LLMFailureClass.RATE_LIMIT,
                        LLMFailureClass.CAPACITY,
                        LLMFailureClass.TRANSPORT,
                    }
                    if not local_fallback_applied and not transient_exhausted:
                        assumptions.extend(
                            self._apply_local_intent(
                                task, message, calibration=calibration
                            )
                        )
                        local_fallback_applied = True
                    if self._local_intent_is_search_ready(task, message):
                        task.metadata["llm_recovery"]["fallback"] = "local_parser"
                        task.metadata["recovered_extract_failure"] = str(exc)
                        task.failure = None
                        assumptions.append("local_parser: used after LLM failure")
                        task.assumptions = tuple(dict.fromkeys(assumptions))
                        break
                    return self._fail_llm_extract(
                        task, message, decision, exc, failure_class
                    )

            payload = result.payload
            self._note_llm_success()
            self._apply_capability_boundaries(
                task,
                message,
                model_codes=payload.unsupported_capabilities,
                model_conflicts=payload.conflicts,
                llm_judged=True,
            )
            classification = payload.classification
            from corporate_travel_agent.agent.intent_evidence import (
                validate_model_field_evidence,
            )

            extracted_fields = payload.fields.model_dump()
            evidence_validation = validate_model_field_evidence(
                extracted_fields=extracted_fields,
                provided_fields=set(payload.provided_fields),
                prior_fields=task.intent_fields,
                evidence=intent_evidence,
                reject_untraceable=(
                    result.metadata.evidence_contract_version == "source-span-v1"
                ),
            )
            task.metadata["intent_evidence_contract"] = (
                result.metadata.evidence_contract_version or "compatibility-audit"
            )
            calibration.field_evidence.update(evidence_validation.field_evidence)
            merged, merge_rejected, provenance = merge_model_fields_anti_fabrication(
                task.intent_fields,
                extracted_fields,
                set(evidence_validation.accepted_fields),
                user_message=message,
                repair_only_missing=repair_missing,
                provenance=calibration.field_provenance,
            )
            rejected = [*evidence_validation.rejected_fields, *merge_rejected]
            if rejected:
                calibration.repair_rejected_fields.extend(rejected)
            task.intent_fields = self._canonicalize_intent_cities(merged)
            calibration.field_provenance = provenance

            model_conflicts = tuple(
                value.strip()
                for value in payload.conflicts
                if value.strip() and len(value.strip()) <= 200
            )
            assumptions.extend(
                value.strip()
                for value in payload.assumptions
                if value.strip() and len(value.strip()) <= 200
            )

            # P0: deterministic whitelist defaults (never invent cities).
            # Runs before L1/L2 so business validation sees derived pairs.
            task.intent_fields, default_assumptions, provenance = apply_safe_defaults(
                task.intent_fields,
                classification=classification,
                reference_time=self.clock(),
                timezone_name=self.timezone_name,
                user_message=message,
                provenance=calibration.field_provenance,
            )
            calibration.field_provenance = provenance
            if default_assumptions:
                calibration.defaults_applied.extend(default_assumptions)
                assumptions.extend(default_assumptions)

            # L1 schema + L2 business → ACCEPT / REPAIR / CLARIFY / OUT_OF_SCOPE
            capability_conflicts = tuple(task.metadata.get("capability_boundary_conflicts") or ())
            decision = decide_param_loop(
                task.intent_fields,
                classification=classification,
                model_conflicts=tuple(model_conflicts) + capability_conflicts,
                extract_index=extract_index,
                max_repair_attempts=MAX_INTENT_REPAIR_ATTEMPTS,
                user_message=message,
            )
            loop_trace.record_round(
                extract_index=extract_index,
                decision=decision,
                rejected_fields=rejected,
            )
            calibration.param_loop = loop_trace.as_dict()

            # Soft (non-blocking) model conflicts → assumptions, never freeze search.
            if decision.soft_conflicts:
                for soft in decision.soft_conflicts:
                    assumptions.append(f"non_blocking_conflict:{soft[:180]}")
            task.metadata["soft_conflicts"] = list(decision.soft_conflicts)

            missing = decision.missing
            conflicts = decision.conflicts  # blocking only
            if extract_index == 0:
                calibration.initial_missing = missing

            task.missing_required_fields = missing
            task.intent_conflicts = conflicts
            task.assumptions = tuple(dict.fromkeys(assumptions))
            task.metadata["intent_confidence"] = payload.confidence
            task.metadata["intent_classification"] = classification
            task.metadata["manipulation_detected"] = (
                bool(task.metadata.get("manipulation_detected")) or payload.manipulation_detected
            )
            task.metadata.setdefault("llm_calls", []).append(asdict(result.metadata))
            task.metadata["intent_calibration"] = calibration.as_dict()
            task.metadata["param_loop"] = loop_trace.as_dict()
            task.metadata["param_loop_final_action"] = decision.action.value

            self._audit(
                task,
                "LLM_INTENT_EXTRACTED" if extract_index == 0 else "LLM_INTENT_REPAIR",
                {
                    "message": message,
                    "prompt_version": result.metadata.prompt_version,
                    "model": result.metadata.model,
                    "extract_index": extract_index,
                    "repair": repair_missing is not None,
                    "param_loop_action": decision.action.value,
                },
                {
                    "missing": missing,
                    "conflicts": conflicts,
                    "manipulation_detected": payload.manipulation_detected,
                    "rejected_fields": rejected,
                    "defaults_applied": default_assumptions,
                    "tool_use_error": decision.tool_use_error,
                    "loop_notes": list(decision.notes),
                },
            )

            if decision.action is LoopAction.OUT_OF_SCOPE:
                # Reject OOS when an in-progress trip is already grounded — model
                # mislabels must not brick revision turns (HANDOFF OOS reopen).
                # Also reject cafeteria/aside labels during slot collection.
                if self._should_reject_oos_classification(task, message):
                    from corporate_travel_agent.agent.intent_calibration import (
                        search_ready_missing,
                    )

                    grounded = self._has_grounded_trip_context(task)
                    ready = search_ready_missing(
                        task.intent_fields or {},
                        classification=str(
                            task.metadata.get("intent_classification") or "TRIP"
                        ),
                        user_message=message,
                    )
                    task.intent_conflicts = ()
                    task.failure = None
                    calibration.ready = False
                    calibration.final_missing = ready.missing
                    calibration.param_loop = loop_trace.as_dict()
                    task.metadata["intent_calibration"] = calibration.as_dict()
                    task.metadata["oos_rejected"] = {
                        "reason": (
                            "grounded_trip_context"
                            if grounded
                            else "in_progress_intake"
                        ),
                        "had_options": bool(task.options),
                        "had_request": task.request is not None,
                        "message": message[:200],
                    }
                    self._audit(
                        task,
                        "OUT_OF_SCOPE_REJECTED",
                        message,
                        task.metadata["oos_rejected"],
                    )
                    if grounded and not ready.missing:
                        task.missing_required_fields = ()
                        task.clarification_question = (
                            "当前行程仍然有效。若要修改，请说明新的出发地、目的地或时间；"
                            "若要放弃当前行程并重新规划，请明确说「重新规划」或「新的出差」。"
                        )
                        task.messages.append(
                            ConversationMessage(
                                role="assistant",
                                content=task.clarification_question,
                            )
                        )
                        self._transition(task, TaskState.NEEDS_CLARIFICATION)
                        return task
                    return self._request_clarification(
                        task,
                        missing=ready.missing or ("origin", "destination"),
                        conflicts=(),
                        uncertain=(),
                        tool_use_error=None,
                    )
                task.missing_required_fields = ()
                task.intent_conflicts = ()
                task.failure = "The request is outside the corporate travel planning scope"
                task.options = []
                task.selected_option_id = None
                task.booking_intent = None
                task.approval = None
                task.request = None
                task.metadata.pop("pending_revision", None)
                calibration.ready = False
                calibration.final_missing = ()
                calibration.param_loop = loop_trace.as_dict()
                task.metadata["intent_calibration"] = calibration.as_dict()
                self._transition(task, TaskState.OUT_OF_SCOPE)
                self._audit(task, "OUT_OF_SCOPE_REQUEST", message, classification)
                return task

            if decision.action is LoopAction.ACCEPT:
                calibration.ready = True
                calibration.final_missing = ()
                calibration.param_loop = loop_trace.as_dict()
                task.metadata["intent_calibration"] = calibration.as_dict()
                break

            if decision.action is LoopAction.REPAIR:
                repair_missing = decision.repairable_missing
                repair_payload = build_tool_use_error_repair_payload(
                    decision,
                    fields=task.intent_fields,
                    user_message=message,
                    classification=classification,
                )
                self._audit(
                    task,
                    "PARAM_LOOP_TOOL_USE_ERROR",
                    {
                        "extract_index": extract_index,
                        "repairable": sorted(repair_missing),
                    },
                    decision.tool_use_error,
                )
                continue

            # CLARIFY: cities missing, conflicts, or repair budget exhausted.
            # An empty/semantically unusable model response is an extraction
            # failure too. Only here—after the model repair budget—is the local
            # parser allowed to write canonical slots.
            if not conflicts and not local_fallback_applied:
                assumptions.extend(
                    self._apply_local_intent(task, message, calibration=calibration)
                )
                local_fallback_applied = True
                if self._local_intent_is_search_ready(
                    task, message, allow_infra_time_defaults=False
                ):
                    task.metadata["llm_recovery"] = {
                        "fallback": "local_parser",
                        "reason": "semantic_extraction_incomplete",
                    }
                    task.failure = None
                    task.missing_required_fields = ()
                    task.intent_conflicts = ()
                    calibration.ready = True
                    calibration.final_missing = ()
                    task.assumptions = tuple(dict.fromkeys(assumptions))
                    task.metadata["intent_calibration"] = calibration.as_dict()
                    break
            calibration.final_missing = missing
            calibration.ready = False
            calibration.param_loop = loop_trace.as_dict()
            task.metadata["intent_calibration"] = calibration.as_dict()
            break

        missing = task.missing_required_fields
        if not task.metadata.get("capability_boundaries") and (
            skip_llm or not task.metadata.get("llm_calls")
        ):
            self._apply_capability_boundaries(
                task,
                message,
                model_codes=(),
                model_conflicts=(),
                llm_judged=False,
            )
        from corporate_travel_agent.agent.clarification_questions import (
            detect_uncertain_slots,
        )

        uncertain = detect_uncertain_slots(
            task.intent_fields,
            user_message=message,
            assumptions=task.assumptions,
            classification=str(task.metadata.get("intent_classification") or "TRIP"),
        )
        if uncertain:
            task.metadata["uncertain_slots"] = list(uncertain)
        if missing or task.intent_conflicts or uncertain:
            return self._request_clarification(
                task,
                missing=missing,
                conflicts=task.intent_conflicts,
                uncertain=uncertain,
                tool_use_error=loop_trace.final_tool_use_error,
            )

        return self._begin_search_from_intent(task)

    def _interpret_and_continue_semantically(self, task: TripTask) -> TripTask:
        """Use one full-conversation interpretation and validate only at command compile."""
        from corporate_travel_agent.agent.search_command import compile_search_command
        from corporate_travel_agent.agent.semantic_intent import (
            ConversationLedger,
            IntentDecisionStatus,
            carried_grounding,
            semantic_fields,
        )

        if self.intent_interpreter is None:
            raise LanguageModelUnavailable("No semantic intent interpreter is configured")
        ledger = ConversationLedger.from_messages(task.messages)
        context: dict[str, Any] = {
            "reference_time": self.clock().isoformat(),
            "timezone": self.timezone_name,
            "clarification_round": task.clarification_rounds,
            "max_clarification_rounds": self.max_clarification_rounds,
        }
        try:
            result = self._invoke_tool(
                task,
                tool_name="llm.interpret_trip_intent",
                tool_kind="LLM",
                counts_toward_budget=True,
                input_value={
                    "task_id": task.task_id,
                    "traveler_id": task.employee.employee_id,
                    "conversation_turns": len(ledger.turns),
                    "latest_user_turn": ledger.latest_user_message,
                },
                operation=lambda: self.intent_interpreter.interpret(
                    ledger,
                    task_id=task.task_id,
                    traveler_id=task.employee.employee_id,
                    context=context,
                ),
            )
        except ToolBudgetExceeded:
            return self._stop_for_tool_budget(task, "llm.interpret_trip_intent")
        except LanguageModelError as exc:
            task.failure = str(exc)
            task.metadata["semantic_intent_failure"] = exc.trace_details()
            task.clarification_question = None
            self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self._audit(
                task,
                "SEMANTIC_INTENT_FAILED",
                {"turns": len(ledger.turns)},
                task.metadata["semantic_intent_failure"],
            )
            return task

        self._note_llm_success()
        decision = result.decision
        self._record_semantic_decision(task, decision)
        task.metadata.setdefault("llm_calls", []).append(asdict(result.metadata))
        task.metadata["intent_confidence"] = decision.confidence
        task.metadata["manipulation_detected"] = decision.manipulation_detected
        # This is the sole semantic-to-shared-domain projection in the semantic entrypoint.
        task.intent_fields = self._canonicalize_intent_cities(semantic_fields(decision))
        task.assumptions = tuple(decision.assumptions)

        if decision.status is IntentDecisionStatus.OUT_OF_SCOPE:
            task.request = None
            task.options = []
            task.selected_option_id = None
            task.failure = "The request is outside the corporate travel planning scope"
            task.missing_required_fields = ()
            task.intent_conflicts = tuple(decision.conflicts)
            self._transition(task, TaskState.OUT_OF_SCOPE)
            self._audit(task, "SEMANTIC_INTENT_OUT_OF_SCOPE", ledger.render(), decision)
            return task

        version = task.request.version + 1 if task.request is not None else 1
        # 证据随对话累积：先前轮次落实过、取值至今没变的字段，不必每轮重新引用一遍。
        prior_history = list(task.metadata.get("semantic_intent_history") or ())[:-1]
        compiled = compile_search_command(
            decision,
            task_id=task.task_id,
            traveler_id=task.employee.employee_id,
            version=version,
            city_normalizer=self.city_normalizer,
            created_at=self.clock(),
            already_grounded=carried_grounding(prior_history, decision.intent),
        )
        # 宿主替旅行者定下来的事必须当面说出口：编译期消掉的分歧（"这两个名字是同一座
        # 城市"、"住宿按行程推算"）和模型自己的假设一样，都要出现在任务上。
        task.assumptions = tuple(
            dict.fromkeys([*decision.assumptions, *compiled.assumptions])
        )
        if not compiled.ready:
            return self._pause_for_semantic_clarification(
                task,
                question=compiled.clarification_question,
                missing=compiled.missing,
                conflicts=compiled.conflicts,
                unsupported=decision.status is IntentDecisionStatus.UNSUPPORTED,
            )

        assert compiled.command is not None
        task.request = compiled.command.request
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.clarification_question = None
        task.failure = None
        task.metadata.pop("pending_revision", None)
        task.metadata.pop("clarification_questions", None)
        task.metadata.pop("clarification_pending_slots", None)
        task.metadata.pop("uncertain_slots", None)
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        self._audit(
            task,
            "SEARCH_COMMAND_COMPILED",
            decision.intent.summary,
            task.request,
        )
        return self._search_and_plan(task, self._policy_for(task))

    def _pause_for_semantic_clarification(
        self,
        task: TripTask,
        *,
        question: str | None,
        missing: tuple[str, ...],
        conflicts: tuple[str, ...],
        unsupported: bool,
    ) -> TripTask:
        """Pause without ever converting unresolved semantics into a search."""
        task.missing_required_fields = missing
        task.intent_conflicts = conflicts
        if not task.metadata.get("pending_revision"):
            task.request = None
            task.options = []
            task.selected_option_id = None
            task.booking_intent = None
            task.approval = None
        task.failure = None
        task.clarification_rounds += 1
        if task.clarification_rounds > self.max_clarification_rounds:
            task.clarification_question = None
            task.failure = (
                "Semantic ambiguity remains after the clarification limit; "
                "use the structured form"
            )
            self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self._audit(
                task,
                "SEMANTIC_CLARIFICATION_EXHAUSTED",
                {"missing": missing, "conflicts": conflicts},
                task.failure,
            )
            return task
        task.clarification_question = question or "请确认我对这次出行的理解。"
        task.messages.append(
            ConversationMessage(role="assistant", content=task.clarification_question)
        )
        self._transition(task, TaskState.NEEDS_CLARIFICATION)
        self._audit(
            task,
            "SEMANTIC_CLARIFICATION_REQUESTED",
            {
                "missing": missing,
                "conflicts": conflicts,
                "unsupported": unsupported,
            },
            task.clarification_question,
        )
        return task

    def _record_semantic_decision(self, task: TripTask, decision: Any) -> None:
        payload = decision.model_dump(mode="json")
        record = {
            "source": IntentEntrypoint.SEMANTIC.value,
            "turn_count": len(task.messages),
            "decision": payload,
        }
        task.metadata["semantic_intent"] = record
        history = list(task.metadata.get("semantic_intent_history") or ())
        history.append(record)
        task.metadata["semantic_intent_history"] = history[-20:]

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
    def _merge_intent_fields(
        current: dict[str, Any], extracted: dict[str, Any], provided: set[str]
    ) -> dict[str, Any]:
        """合并意图字段（委托防编造合并逻辑）。"""
        merged = dict(current)
        for field_name in provided:
            if field_name in merged:
                merged[field_name] = extracted[field_name]
        return merged

    @staticmethod
    def _validate_intent_fields(
        fields: dict[str, Any],
        *,
        classification: str = "TRIP",
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """校验意图字段完备性。"""
        if classification == "OUT_OF_SCOPE":
            return (), ()
        validation = validate_trip_request_values(fields)
        return validation.missing, validation.conflicts

    @staticmethod
    def _timezone_aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    def _apply_local_intent(
        self,
        task: TripTask,
        message: str,
        *,
        calibration: Any,
    ) -> list[str]:
        """用 L0 本地解析器填充/修补意图（仅目录可支撑槽）。"""
        from corporate_travel_agent.agent.intent_calibration import (
            apply_safe_defaults,
            merge_model_fields_anti_fabrication,
        )
        from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

        parser = GroundedLocalIntentParser(self.city_normalizer)
        extracted = parser.extract_trip_intent(
            message,
            task_id=task.task_id,
            traveler_id=task.employee.employee_id,
            context={
                "reference_time": self.clock().isoformat(),
                "timezone": self.timezone_name,
                "prior_fields": self._json_safe_fields(task.intent_fields),
            },
        )
        merged, _rejected, provenance = merge_model_fields_anti_fabrication(
            task.intent_fields,
            extracted.payload.fields.model_dump(),
            set(extracted.payload.provided_fields),
            user_message=message,
            provenance=calibration.field_provenance,
        )
        task.intent_fields = self._canonicalize_intent_cities(merged)
        calibration.field_provenance = {
            **provenance,
            **{
                name: "local_parser"
                for name in extracted.payload.provided_fields
                if task.intent_fields.get(name) not in (None, "", [])
            },
        }
        task.intent_fields, default_assumptions, provenance = apply_safe_defaults(
            task.intent_fields,
            classification=extracted.payload.classification,
            reference_time=self.clock(),
            timezone_name=self.timezone_name,
            user_message=message,
            provenance=calibration.field_provenance,
        )
        calibration.field_provenance = provenance
        task.metadata["intent_classification"] = extracted.payload.classification
        task.metadata["local_intent"] = {
            "provided_fields": list(extracted.payload.provided_fields),
            "classification": extracted.payload.classification,
        }
        notes = ["local_parser:applied"]
        notes.extend(default_assumptions)
        task.assumptions = tuple(dict.fromkeys([*task.assumptions, *notes]))
        return notes

    def _local_intent_is_search_ready(
        self,
        task: TripTask,
        message: str,
        *,
        allow_infra_time_defaults: bool = True,
    ) -> bool:
        from corporate_travel_agent.agent.intent_calibration import search_ready_missing

        classification = str(task.metadata.get("intent_classification") or "TRIP")
        fields = dict(task.intent_fields or {})
        notes = self._infra_fallback_time_defaults(fields) if allow_infra_time_defaults else []
        if notes:
            task.intent_fields = fields
            task.assumptions = tuple(dict.fromkeys([*task.assumptions, *notes]))
        ready = search_ready_missing(
            task.intent_fields or {},
            classification=classification,
            user_message=message,
        )
        task.missing_required_fields = ready.missing
        return ready.ready

    @staticmethod
    def _infra_fallback_time_defaults(fields: dict[str, Any]) -> list[str]:
        """When the LLM is down, complete a same-day arrive_by from departure only."""
        from datetime import time
        from zoneinfo import ZoneInfo

        from corporate_travel_agent.services.locations import resolve_location_timezone

        notes: list[str] = []
        departure = fields.get("departure_after")
        if fields.get("arrive_by") is None and isinstance(departure, datetime):
            dest_tz_name = resolve_location_timezone(
                fields.get("destination") if isinstance(fields.get("destination"), str) else None,
                fallback="Asia/Shanghai",
            )
            dest_tz = ZoneInfo(dest_tz_name)
            day = departure.astimezone(dest_tz).date() if departure.tzinfo else departure.date()
            fields["arrive_by"] = datetime.combine(day, time(18, 0), tzinfo=dest_tz)
            notes.append(
                "infra_fallback:arrive_by=18:00 destination-local "
                "same calendar day as departure_after"
            )
        return notes

    def _note_llm_success(self) -> None:
        self.llm_runtime_status = "ok"

    def _note_llm_failure(self, failure_class: Any) -> None:
        from corporate_travel_agent.agent.llm_failure import health_status_for

        self.llm_runtime_status = health_status_for(
            failure_class, configured=self.language_model is not None
        )

    def _fail_llm_extract(
        self,
        task: TripTask,
        message: str,
        decision: RecoveryDecision,
        exc: LanguageModelError,
        failure_class: Any,
    ) -> TripTask:
        """处理 LLM 抽取失败：分类、澄清；账单/鉴权不消耗澄清预算。"""
        from corporate_travel_agent.agent.clarification_questions import (
            detect_uncertain_slots,
        )
        from corporate_travel_agent.agent.intent_calibration import search_ready_missing
        from corporate_travel_agent.agent.llm_failure import (
            consumes_clarification_round,
            user_preamble,
        )

        recovery = decision.as_dict()
        spend_round = consumes_clarification_round(failure_class)
        if spend_round and task.clarification_rounds >= self.max_clarification_rounds:
            task.clarification_question = None
            task.failure = str(exc)
            self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self._audit(
                task,
                "LLM_INTENT_FAILED",
                message,
                {
                    "failure": task.failure,
                    "recovery": recovery,
                    "fallback": "structured_after_clarify_budget",
                    "failure_class": getattr(failure_class, "value", str(failure_class)),
                },
            )
            return task

        classification = str(task.metadata.get("intent_classification") or "TRIP")
        ready = search_ready_missing(
            task.intent_fields or {},
            classification=classification,
            user_message=message,
        )
        uncertain = detect_uncertain_slots(
            task.intent_fields or {},
            user_message=message,
            assumptions=task.assumptions,
            classification=classification,
        )
        missing = ready.missing
        if not missing and not uncertain:
            missing = ("origin", "destination", "departure_after", "arrive_by")

        preamble = user_preamble(failure_class)
        task.failure = str(exc)
        if spend_round:
            result = self._request_clarification(
                task,
                missing=missing,
                conflicts=task.intent_conflicts,
                uncertain=uncertain,
                tool_use_error=str(exc),
            )
        else:
            result = self._present_extract_failure(
                task,
                missing=missing,
                uncertain=uncertain,
                preamble=preamble,
            )
        if result.clarification_question and spend_round:
            combined = f"{preamble}\n\n{result.clarification_question}"
            result.clarification_question = combined
            if result.messages and result.messages[-1].role == "assistant":
                result.messages[-1] = ConversationMessage(
                    role="assistant",
                    content=combined,
                )
        result.metadata["llm_recovery"] = {
            **(result.metadata.get("llm_recovery") or {}),
            **recovery,
            "fallback": "clarify" if spend_round else "infra_degrade",
            "failure_class": getattr(failure_class, "value", str(failure_class)),
        }
        self._audit(
            result,
            "LLM_INTENT_FAILED",
            message,
            {
                "failure": result.failure,
                "recovery": recovery,
                "fallback": result.metadata["llm_recovery"]["fallback"],
                "missing": list(result.missing_required_fields),
                "clarification_rounds": result.clarification_rounds,
                "failure_class": getattr(failure_class, "value", str(failure_class)),
            },
        )
        return result

    def _present_extract_failure(
        self,
        task: TripTask,
        *,
        missing: tuple[str, ...] | list[str],
        uncertain: tuple[str, ...] | list[str],
        preamble: str,
    ) -> TripTask:
        """在基础设施失败后展示仍缺槽位，不消耗用户澄清预算。"""
        from corporate_travel_agent.agent.clarification_questions import (
            CLARIFICATION_PENDING_KEY,
            CLARIFICATION_QUESTIONS_KEY,
            build_clarification_bundle,
        )

        bundle = build_clarification_bundle(
            missing=tuple(missing),
            conflicts=(),
            uncertain=tuple(uncertain),
            fields=task.intent_fields,
        )
        prompt = preamble
        if bundle is not None:
            prompt = f"{preamble}\n\n{bundle.prompt_text}"
            task.missing_required_fields = tuple(bundle.missing)
            task.metadata[CLARIFICATION_QUESTIONS_KEY] = [
                item.as_dict() for item in bundle.questions
            ]
            task.metadata[CLARIFICATION_PENDING_KEY] = list(bundle.uncertain) + list(
                bundle.missing
            )
            task.metadata["uncertain_slots"] = list(bundle.uncertain)
        else:
            task.missing_required_fields = tuple(missing)
        task.clarification_question = prompt
        task.messages.append(ConversationMessage(role="assistant", content=prompt))
        self._transition(task, TaskState.NEEDS_CLARIFICATION)
        return task

    @staticmethod
    def _has_grounded_trip_context(task: TripTask) -> bool:
        """True when destroying the task as OOS would discard a usable trip."""

        if task.request is not None or task.options or task.selected_option_id:
            return True
        if task.booking_intent is not None or task.approval is not None:
            return True
        pending = task.metadata.get("pending_revision")
        if isinstance(pending, dict) and (
            pending.get("had_options") or pending.get("had_request")
        ):
            return True
        fields = task.intent_fields or {}
        if fields.get("origin") and fields.get("destination"):
            return True
        if fields.get("departure_after") or fields.get("arrive_by"):
            return True
        return False

    def _origin_named_in_message(self, message: str) -> str | None:
        from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

        parser = GroundedLocalIntentParser(self.city_normalizer)
        origin, _destination = parser._cities(message)
        return origin

    def _maybe_reset_superseded_origin(self, task: TripTask, message: str) -> None:
        current = (task.intent_fields or {}).get("origin")
        if not current:
            return
        named = self._origin_named_in_message(message)
        if not named:
            return
        current_name = self.city_normalizer.canonicalize(str(current))
        named_name = self.city_normalizer.canonicalize(named)
        if current_name.casefold() == named_name.casefold():
            return
        from zoneinfo import ZoneInfo

        from corporate_travel_agent.agent.intent_calibration import (
            _reinterpret_local,
            _same_zone_or_offset,
        )
        from corporate_travel_agent.services.locations import resolve_location_timezone

        task.intent_fields["origin"] = named_name
        departure = task.intent_fields.get("departure_after")
        origin_tz = ZoneInfo(
            resolve_location_timezone(named_name, fallback=self.timezone_name)
        )
        if (
            isinstance(departure, datetime)
            and departure.tzinfo is not None
            and not _same_zone_or_offset(departure, origin_tz)
        ):
            task.intent_fields["departure_after"] = _reinterpret_local(
                departure, origin_tz
            )
        task.metadata["origin_revision"] = {
            "from": current_name,
            "to": named_name,
            "message": message[:200],
        }
        self._audit(
            task,
            "ORIGIN_REVISION",
            message,
            task.metadata["origin_revision"],
        )

    def _apply_capability_boundaries(
        self,
        task: TripTask,
        message: str,
        *,
        model_codes: tuple[str, ...] | list[str],
        model_conflicts: tuple[str, ...] | list[str],
        llm_judged: bool,
    ) -> None:
        """合并能力边界披露，必要时阻断搜索。"""
        from corporate_travel_agent.agent.capability_boundaries import (
            resolve_capability_boundaries,
        )

        boundaries = resolve_capability_boundaries(
            user_message=message,
            model_codes=model_codes,
            model_conflicts=model_conflicts,
            llm_judged=llm_judged,
            city_normalizer=self.city_normalizer,
        )
        if not boundaries:
            task.metadata.pop("capability_boundaries", None)
            task.metadata.pop("capability_boundary_conflicts", None)
            if llm_judged:
                task.intent_conflicts = tuple(
                    item
                    for item in task.intent_conflicts
                    if not str(item).startswith("unsupported_capability:")
                )
            return
        task.metadata["capability_boundaries"] = [item.as_dict() for item in boundaries]
        conflicts = tuple(item.conflict_text for item in boundaries)
        task.metadata["capability_boundary_conflicts"] = list(conflicts)
        task.intent_conflicts = tuple(dict.fromkeys((*task.intent_conflicts, *conflicts)))

    def _destination_named_in_message(self, message: str) -> str | None:
        from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

        parser = GroundedLocalIntentParser(self.city_normalizer)
        _origin, destination = parser._cities(message)
        if destination:
            return destination
        mentions = parser._city_mentions(message)
        unique = list(dict.fromkeys(canonical for _index, canonical in mentions))
        if len(unique) == 1 and _origin is None:
            return unique[0]
        return None

    def _reset_route_scoped_slots(self, task: TripTask, *, keep_origin: bool) -> None:
        task.metadata.pop("intent_calibration", None)
        hard = [
            item
            for item in (task.intent_fields.get("hard_constraints") or [])
            if item != "arrive_before_meeting"
        ]
        task.intent_fields = {
            **task.intent_fields,
            "destination": None,
            "departure_after": None,
            "arrive_by": None,
            "return_after": None,
            "return_before": None,
            "hotel_check_in": None,
            "hotel_check_out": None,
            "hard_constraints": hard,
        }
        if not keep_origin:
            task.intent_fields["origin"] = None

    def _maybe_reset_superseded_destination(self, task: TripTask, message: str) -> None:
        current = (task.intent_fields or {}).get("destination")
        if not current:
            return
        named = self._destination_named_in_message(message)
        if not named:
            return
        current_name = self.city_normalizer.canonicalize(str(current))
        named_name = self.city_normalizer.canonicalize(named)
        if current_name.casefold() == named_name.casefold():
            return
        self._reset_route_scoped_slots(task, keep_origin=True)
        task.metadata["destination_revision"] = {
            "from": current_name,
            "to": named_name,
            "message": message[:200],
        }
        self._audit(
            task,
            "DESTINATION_REVISION_RESET",
            message,
            task.metadata["destination_revision"],
        )

    @staticmethod
    def _core_slots_ready(task: TripTask) -> bool:
        fields = task.intent_fields or {}
        return all(
            fields.get(name) not in (None, "")
            for name in ("origin", "destination", "departure_after", "arrive_by")
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

    @staticmethod
    def _prior_user_turn_count(task: TripTask) -> int:
        return sum(1 for item in task.messages if item.role == "user")

    @staticmethod
    def _message_is_explicit_scope_change(message: str) -> bool:
        return bool(
            re.search(
                r"演唱会|concert tickets?|purchase .{0,24}tickets?|"
                r"complete the payment|用公司卡|信用卡支付|"
                r"道歉信|写一封信|帮我买两张",
                message or "",
                re.I,
            )
        )

    def _should_reject_oos_classification(self, task: TripTask, message: str) -> bool:
        if self._message_is_explicit_scope_change(message):
            return False
        if self._has_grounded_trip_context(task):
            return True
        # Follow-up during slot collection (cafeteria chat, weather, etc.).
        return self._prior_user_turn_count(task) >= 2

    def _begin_search_from_intent(self, task: TripTask) -> TripTask:
        """核心槽位已接受后提交 copy-on-write 并搜索。"""
        task.metadata.pop("pending_revision", None)
        task.metadata.pop("clarification_questions", None)
        task.metadata.pop("clarification_pending_slots", None)
        task.metadata.pop("uncertain_slots", None)
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.request = self._build_request(task)
        task.clarification_question = None
        task.failure = None
        self._audit(task, "TRIP_REQUEST_STRUCTURED", task.intent_fields, task.request)
        return self._search_and_plan(task, self._policy_for(task))

    def _request_clarification(
        self,
        task: TripTask,
        *,
        missing: tuple[str, ...] | list[str],
        conflicts: tuple[str, ...] | list[str],
        uncertain: tuple[str, ...] | list[str],
        tool_use_error: str | None,
    ) -> TripTask:
        """写入 AskUserQuestion 风格澄清题并暂停。"""
        from corporate_travel_agent.agent.clarification_questions import (
            CLARIFICATION_PENDING_KEY,
            CLARIFICATION_QUESTIONS_KEY,
            build_clarification_bundle,
        )

        previous_pending = tuple(
            dict.fromkeys(
                list(task.metadata.get(CLARIFICATION_PENDING_KEY) or ())
                + list(task.missing_required_fields or ())
                + list(task.metadata.get("uncertain_slots") or ())
            )
        )
        new_pending = tuple(dict.fromkeys([*missing, *uncertain]))
        progressed = bool(set(previous_pending) - set(new_pending))
        if task.clarification_rounds >= self.max_clarification_rounds:
            if self._core_slots_ready(task):
                task.metadata["clarification_budget_search"] = {
                    "reason": "core_slots_ready",
                    "rounds": task.clarification_rounds,
                    "uncertain": list(uncertain),
                    "missing": list(missing),
                }
                self._audit(
                    task,
                    "CLARIFICATION_BUDGET_SEARCH",
                    task.clarification_rounds,
                    task.metadata["clarification_budget_search"],
                )
                return self._begin_search_from_intent(task)
            hard_cap = self.max_clarification_rounds + 2
            if not progressed or task.clarification_rounds >= hard_cap:
                task.failure = "Clarification budget exhausted; use the structured form"
                task.clarification_question = None
                task.metadata.pop(CLARIFICATION_QUESTIONS_KEY, None)
                self._transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
                self._audit(
                    task,
                    "CLARIFICATION_EXHAUSTED",
                    task.clarification_rounds,
                    task.failure,
                )
                return task

        original_instruction = next(
            (item.content for item in task.messages if item.role == "user"),
            "",
        )
        bundle = build_clarification_bundle(
            missing=tuple(missing),
            conflicts=tuple(conflicts),
            uncertain=tuple(uncertain),
            fields=task.intent_fields,
            user_message=original_instruction,
        )
        if bundle is None:
            # Fallback plain text (should be rare).
            task.clarification_rounds += 1
            task.clarification_question = "请补充出差关键信息后继续。"
            task.messages.append(
                ConversationMessage(role="assistant", content=task.clarification_question)
            )
            self._transition(task, TaskState.NEEDS_CLARIFICATION)
            return task

        task.clarification_rounds += 1
        task.clarification_question = bundle.prompt_text
        task.missing_required_fields = tuple(bundle.missing)
        task.metadata[CLARIFICATION_QUESTIONS_KEY] = [
            item.as_dict() for item in bundle.questions
        ]
        task.metadata[CLARIFICATION_PENDING_KEY] = list(bundle.uncertain) + list(
            bundle.missing
        )
        task.metadata["uncertain_slots"] = list(bundle.uncertain)
        task.messages.append(
            ConversationMessage(role="assistant", content=task.clarification_question)
        )
        self._transition(task, TaskState.NEEDS_CLARIFICATION)
        self._audit(
            task,
            "CLARIFICATION_REQUESTED",
            {
                "missing": list(bundle.missing),
                "conflicts": list(conflicts),
                "uncertain": list(bundle.uncertain),
                "questions": [item.id for item in bundle.questions],
                "tool_use_error": tool_use_error,
            },
            task.clarification_question,
        )
        return task

    def _try_apply_clarification_options(
        self, task: TripTask, message: str
    ) -> TripTask | None:
        """尽量用选项字母/标签无 LLM 落地，再重新闸控搜索。"""
        from corporate_travel_agent.agent.clarification_questions import (
            CLARIFICATION_ANSWERS_KEY,
            CLARIFICATION_QUESTIONS_KEY,
            apply_clarification_answer,
            apply_option_letter,
            detect_uncertain_slots,
            lookup_clarification_city,
            should_defer_structured_option_to_llm,
        )
        from corporate_travel_agent.agent.intent_calibration import search_ready_missing
        from corporate_travel_agent.agent.param_extraction_loop import decide_param_loop

        questions = task.metadata.get(CLARIFICATION_QUESTIONS_KEY) or []
        if not isinstance(questions, list):
            questions = []
        original_instruction = next(
            (item.content for item in task.messages if item.role == "user"),
            message,
        )

        letter_result = (
            apply_option_letter(
                task.intent_fields,
                message.strip(),
                questions,
                reference_time=self.clock(),
                timezone_name=self.timezone_name,
                context_message=original_instruction,
            )
            if questions
            else None
        )
        if letter_result is not None:
            updated, notes, free_hints = letter_result
        else:
            updated, notes, free_hints = apply_clarification_answer(
                task.intent_fields,
                message,
                reference_time=self.clock(),
                timezone_name=self.timezone_name,
                context_message=original_instruction,
            )
            # If nothing structured applied, let LLM extract handle free text.
            filled_client_location = False
            if not notes and free_hints and free_hints[0] == message.strip():
                pending_slots = [
                    slot
                    for question in questions
                    if isinstance(question, dict)
                    for slot in (question.get("slots") or [])
                ]
                city = lookup_clarification_city(message.strip())
                if (
                    city
                    and "origin" in pending_slots
                    and not updated.get("origin")
                    and str(updated.get("destination") or "").casefold() != city.casefold()
                ):
                    updated["origin"] = city
                    notes = [f"clarification:origin={city}"]
                    free_hints = []
                elif (
                    city
                    and "destination" in pending_slots
                    and not updated.get("destination")
                    and str(updated.get("origin") or "").casefold() != city.casefold()
                ):
                    updated["destination"] = city
                    notes = [f"clarification:destination={city}"]
                    free_hints = []
                elif "client_location" in pending_slots:
                    updated["client_location"] = message.strip()[:200]
                    notes = ["clarification:client_location"]
                    free_hints = []
                    filled_client_location = True
                else:
                    return None
            if not notes and not free_hints:
                return None
            # A full sentence that merely contains a shortcut (e.g. "北京到上海，8月5日…")
            # must not drop the rest of the user's instruction. Exact UI tokens
            # such as template:overnight are longer than 16 chars and must apply.
            if (
                not filled_client_location
                and should_defer_structured_option_to_llm(message, notes)
            ):
                return None

        task.intent_fields = self._canonicalize_intent_cities(updated)
        answers = list(task.metadata.get(CLARIFICATION_ANSWERS_KEY) or [])
        answers.append({"message": message, "notes": notes, "hints": free_hints})
        task.metadata[CLARIFICATION_ANSWERS_KEY] = answers[-10:]
        assumptions = list(task.assumptions or ())
        assumptions.extend(notes)
        task.assumptions = tuple(dict.fromkeys(assumptions))
        self._audit(
            task,
            "CLARIFICATION_OPTION_APPLIED",
            message,
            {
                "notes": notes,
                "hints": free_hints,
                "fields": self._json_safe_fields(task.intent_fields),
            },
        )

        if free_hints and notes:
            # Option applied but still needs free-text details (e.g. return:yes).
            task.clarification_question = "；".join(free_hints) + "。"
            task.messages.append(
                ConversationMessage(role="assistant", content=task.clarification_question)
            )
            self._transition(task, TaskState.NEEDS_CLARIFICATION)
            return task
        if free_hints and not notes:
            task.clarification_question = "；".join(free_hints) + "。"
            task.messages.append(
                ConversationMessage(role="assistant", content=task.clarification_question)
            )
            self._transition(task, TaskState.NEEDS_CLARIFICATION)
            return task

        classification = str(task.metadata.get("intent_classification") or "TRIP")
        decision = decide_param_loop(
            task.intent_fields,
            classification=classification,
            model_conflicts=(),
            extract_index=0,
            max_repair_attempts=0,
            user_message=message,
        )
        if decision.action.value != "accept":
            return self._request_clarification(
                task,
                missing=decision.missing,
                conflicts=decision.conflicts,
                uncertain=(),
                tool_use_error=decision.tool_use_error,
            )

        ready = search_ready_missing(
            task.intent_fields,
            classification=classification,
            user_message=original_instruction,
        )
        if not ready.ready:
            return self._request_clarification(
                task,
                missing=ready.missing,
                conflicts=ready.conflicts,
                uncertain=(),
                tool_use_error=None,
            )

        uncertain = detect_uncertain_slots(
            task.intent_fields,
            user_message=message,
            assumptions=task.assumptions,
            classification=classification,
        )
        # Drop slots the user just resolved via options.
        if notes:
            joined = " ".join(notes)
            if "return" in joined or "one_way" in joined:
                uncertain = tuple(s for s in uncertain if s != "return_trip")
            if "hotel" in joined:
                uncertain = tuple(s for s in uncertain if s != "hotel_need")
            if "flight" in joined or "train" in joined or "mode" in joined:
                uncertain = tuple(s for s in uncertain if s != "transport_mode")
        if uncertain:
            return self._request_clarification(
                task,
                missing=(),
                conflicts=(),
                uncertain=uncertain,
                tool_use_error=None,
            )

        task.metadata.pop(CLARIFICATION_QUESTIONS_KEY, None)
        task.metadata.pop("clarification_pending_slots", None)
        task.metadata.pop("uncertain_slots", None)
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.clarification_question = None
        task.failure = None
        task.metadata.pop("pending_revision", None)
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.request = self._build_request(task)
        self._audit(task, "TRIP_REQUEST_STRUCTURED", task.intent_fields, task.request)
        return self._search_and_plan(task, self._policy_for(task))

    @staticmethod
    def _clarification_question(missing: tuple[str, ...], conflicts: tuple[str, ...]) -> str:
        """Legacy plain-text helper retained for tests/callers."""
        from corporate_travel_agent.agent.clarification_questions import (
            build_clarification_bundle,
        )

        bundle = build_clarification_bundle(missing=missing, conflicts=conflicts)
        if bundle is not None:
            return bundle.prompt_text
        return "请补充出差关键信息。"

    @staticmethod
    def _json_safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.isoformat() if isinstance(value, (date, datetime)) else value
            for key, value in fields.items()
        }

    def _build_request(self, task: TripTask) -> TripRequestVersion:
        """从任务意图字段构造 TripRequestVersion。"""
        from corporate_travel_agent.agent.journey_semantics import booking_scope_for_fields

        fields = task.intent_fields
        return TripRequestVersion(
            task_id=task.task_id,
            version=1,
            traveler_id=task.employee.employee_id,
            origin=str(fields["origin"]),
            destination=str(fields["destination"]),
            departure_after=fields["departure_after"],
            arrive_by=fields["arrive_by"],
            return_after=fields["return_after"],
            return_before=fields["return_before"],
            hotel_check_in=fields["hotel_check_in"],
            hotel_check_out=fields["hotel_check_out"],
            hard_constraints=tuple(fields["hard_constraints"]),
            soft_preferences=tuple(fields["soft_preferences"]),
            booking_scope=booking_scope_for_fields(
                fields,
                user_message=next(
                    (item.content for item in reversed(task.messages) if item.role == "user"),
                    "",
                ),
            ),
            created_at=self.clock(),
        )

    def _canonicalize_intent_cities(self, fields: dict[str, Any]) -> dict[str, Any]:
        """规范化意图中的城市名。"""
        normalized = dict(fields)
        for field_name in ("origin", "destination"):
            value = normalized.get(field_name)
            if isinstance(value, str) and value.strip():
                normalized[field_name] = self.city_normalizer.canonicalize(value)
        return normalized

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
        if option.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL and (
            not business_reason or not business_reason.strip()
        ):
            raise WorkflowError("A business reason is required for a policy exception")

        task.selected_option_id = option_id
        self._audit(task, "OPTION_SELECTED", option_id, option.policy_decision.outcome.value)
        if option.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL:
            assert business_reason is not None
            task.approval = self._new_approval(task, business_reason.strip())
            self._transition(task, TaskState.WAITING_FOR_APPROVAL)
            self._audit(
                task,
                "APPROVAL_REQUESTED",
                option_id,
                task.approval.subject_hash,
                option.inventory_refs,
            )
            return task
        if option.policy_decision.outcome is not PolicyOutcome.COMPLIANT:
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
            self._audit(task, "APPROVAL_INVALIDATED", approval.subject_hash, "expired")
            task.selected_option_id = None
            task.failure = "The approval expired; select an option and request approval again"
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            raise WorkflowError("The approval has expired")
        if approval.subject_hash != self._approval_subject_hash(task):
            approval.status = ApprovalStatus.INVALIDATED
            self._audit(task, "APPROVAL_INVALIDATED", approval.subject_hash, "subject_changed")
            task.selected_option_id = None
            task.failure = "The approval subject changed; select an option again"
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            raise WorkflowError("INV-004: approval subject has changed")

        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.decision_reason = reason
        self._audit(task, "APPROVAL_DECIDED", approver_id, approval.status.value)
        if not approved:
            task.selected_option_id = None
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            return task

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
        required_calls = len(transport_legs) + len(stays)
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
        snapshots = [*leg_snapshots, *hotel_snapshots]
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
        self._record_coverage_notices(task, snapshots)
        self._audit_snapshots(task, snapshots)
        self._transition(task, TaskState.PLANNING)

        task.options = self.planner.plan(
            request=request,
            employee=task.employee,
            policy=policy,
            leg_offers=[self._transports(item) for item in leg_snapshots],
            hotel_offers=[self._hotels(item) for item in hotel_snapshots],
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
            decision = engine.evaluate(employee, policy, transports, hotels)
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
        """为需审批方案创建 ApprovalRequest。"""
        option = task.selected_option()
        if option is None:
            raise WorkflowError("No selected option")
        created_at = self.clock()
        return ApprovalRequest(
            approval_id=str(uuid4()),
            subject_hash=self._approval_subject_hash(task),
            option_id=option.option_id,
            option_version=option.version,
            trip_request_version=self._request(task).version,
            policy_snapshot_id=task.policy_snapshot_id,
            employee_snapshot_id=task.employee.snapshot_id,
            violations=option.policy_decision.violation_ids,
            business_reason=business_reason,
            approver_id=task.employee.manager_id,
            approved_price=option.total_cost,
            created_at=created_at,
            expires_at=created_at + timedelta(hours=24),
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
                "violations": option.policy_decision.violation_ids,
            }
        )

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
    ) -> None:
        """写审计事件并可选推送轨迹观察者。"""
        event = new_audit_event(
            task.task_id,
            event_type,
            input_value=input_value,
            output_value=output_value,
            evidence_refs=evidence_refs,
        )
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
        for item in getattr(result, "items", ()):
            ref_id = getattr(item, "ref_id", None)
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

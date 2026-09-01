"""供应商韧性：有界重试、延迟重试队列、熔断器恢复、重启后的中断任务恢复、工具调用闸门。

从 `TripWorkflowOrchestrator` 按职责拆出来的一块；方法原文逐字来自拆分前的 orchestrator.py。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter_ns
from typing import Any

from corporate_travel_agent.agent.error_recovery import (
    RecoveryAction,
    SideEffectClass,
    classify_tool_failure,
    recon_search_legs,
)
from corporate_travel_agent.agent.orchestrator.core import (
    TRANSIENT_LLM_RETRY_REASON,
    TRANSIENT_RETRY_REASON,
    ToolBudgetExceeded,
    ToolResult,
    WorkflowError,
)
from corporate_travel_agent.agent.ports import LanguageModelError, WorkflowTraceEvent
from corporate_travel_agent.domain.enums import TaskState, ToolCallStatus
from corporate_travel_agent.domain.models import ToolCallRecord, TripTask
from corporate_travel_agent.providers.base import ProviderError, RetryableProviderError
from corporate_travel_agent.services.provider_resilience import PROVIDER_RETRY_METADATA_KEY
from corporate_travel_agent.services.repositories import ConcurrentUpdateError


class ResilienceMixin:
    """供应商韧性：有界重试、延迟重试队列、熔断器恢复、重启后的中断任务恢复、工具调用闸门。

    混入 `TripWorkflowOrchestrator`；状态都在宿主实例上，这里只放方法。
    """

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

    def _note_llm_success(self) -> None:
        self.llm_runtime_status = "ok"

    def _note_llm_failure(self, failure_class: Any) -> None:
        from corporate_travel_agent.agent.llm_failure import health_status_for

        self.llm_runtime_status = health_status_for(
            failure_class, configured=self.tool_calling_language_model is not None
        )

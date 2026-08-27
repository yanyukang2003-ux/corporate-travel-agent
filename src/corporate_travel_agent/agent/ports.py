"""agent.ports：语言模型与工作流追踪的端口（Protocol）及结果类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from corporate_travel_agent.domain.models import TravelOptionVersion

from .schemas import IntentExtractionSchema

if TYPE_CHECKING:
    from .semantic_intent import IntentDecision


@dataclass(frozen=True, slots=True)
class LLMCallMetadata:
    """单次 LLM 调用的计费/用量元数据（可安全落审计）。"""

    prompt_version: str
    model: str
    duration_ms: int
    requested_model: str | None = None
    reasoning_effort: str | None = None
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    service_tier: str | None = None
    evidence_contract_version: str | None = None


@dataclass(frozen=True, slots=True)
class IntentExtractionResult:
    """意图抽取结果：结构化 payload + 调用元数据。"""

    payload: IntentExtractionSchema
    metadata: LLMCallMetadata


@dataclass(frozen=True, slots=True)
class IntentInterpretationResult:
    """完整对话语义解释结果及单次模型调用元数据。"""

    decision: IntentDecision
    metadata: LLMCallMetadata


@dataclass(frozen=True, slots=True)
class WorkflowTraceEvent:
    """有界 Agent 循环的一条观测事件（在 sink 侧脱敏后写入）。"""

    kind: str
    name: str
    status: str
    started_at: datetime
    duration_ms: float
    state_before: str | None
    state_after: str | None
    input_value: Any = None
    output_value: Any = None
    evidence_refs: tuple[str, ...] = ()
    tool_kind: str | None = None
    tool_call_sequence: int | None = None
    error_type: str | None = None
    error_message_code: str | None = None
    error_layer: str | None = None
    error_cause_type: str | None = None
    error_cause_chain: tuple[str, ...] = ()
    response_received: bool | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    retryable: bool | None = None
    retry_of: int | None = None
    reason_code: str | None = None
    side_effect_class: str | None = None
    recovery_action: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None


class WorkflowTraceObserverPort(Protocol):
    """接收工作流观测，不控制业务行为。"""

    def record(self, event: WorkflowTraceEvent) -> None: ...


class LanguageModelError(RuntimeError):
    """已脱敏的 LLM 失败，可安全写入任务与评测轨迹。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "LANGUAGE_MODEL_ERROR",
        layer: str = "language_model",
        cause_type: str | None = None,
        cause_chain: tuple[str, ...] = (),
        retryable: bool = False,
        response_received: bool | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.layer = layer
        self.cause_type = cause_type
        self.cause_chain = cause_chain
        self.retryable = retryable
        self.response_received = response_received
        self.http_status = http_status
        self.request_id = request_id

    def trace_details(self) -> dict[str, Any]:
        """导出可供轨迹记录的错误字段。"""
        return {
            "error_code": self.error_code,
            "error_layer": self.layer,
            "cause_type": self.cause_type,
            "cause_chain": self.cause_chain,
            "retryable": self.retryable,
            "response_received": self.response_received,
            "http_status": self.http_status,
            "request_id": self.request_id,
        }


class LanguageModelPort(Protocol):
    """受 Schema 约束的 LLM 能力；实现不得做政策裁决。"""

    prompt_version: str

    def extract_trip_intent(
        self, message: str, *, task_id: str, traveler_id: str, context: dict[str, Any]
    ) -> IntentExtractionResult: ...

    def propose_search_adjustment(
        self, failure_facts: tuple[str, ...], allowed_adjustments: tuple[str, ...]
    ) -> str | None: ...

    def explain_verified_options(
        self, options: tuple[TravelOptionVersion, ...]
    ) -> dict[str, str]: ...


class SemanticLanguageModelPort(Protocol):
    """完整对话语义解释端口；不暴露旧字段抽取能力。"""

    semantic_prompt_version: str

    def interpret_trip_intent(
        self,
        conversation: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, Any],
    ) -> IntentInterpretationResult: ...


class FactsOnlyExplanationAdapter:
    """离线回退：只用已验证事实拼解释，不编造，便于测试。"""

    prompt_version = "facts-only-v1"

    def explain_verified_options(
        self, options: tuple[TravelOptionVersion, ...]
    ) -> dict[str, str]:
        return {
            item.option_id: "; ".join(item.explanation_facts)
            for item in options
        }

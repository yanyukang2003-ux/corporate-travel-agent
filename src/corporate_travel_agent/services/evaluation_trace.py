"""评测什么：评测轨迹 schema 与记录器——脱敏步骤、指纹与终态产物。"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from corporate_travel_agent.agent.ports import WorkflowTraceEvent
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.redaction import redact_json

TRACE_SCHEMA_VERSION = 1
TRACE_RUNNER_VERSION = "agent-eval-runner-v1"
SHA256_PATTERN = r"^[a-f0-9]{64}$"

EvaluationMode = Literal[
    "deterministic_mock",
    "deterministic_live_provider",
    "model_mock",
    "real_llm_mock_provider",
    "model_replay",
    "model_live_provider",
    "model_live_provider_test_order",
    "production_observation",
]
TraceKind = Literal[
    "user",
    "llm",
    "tool",
    "policy",
    "state_transition",
    "recovery",
    "audit",
]
TraceStatus = Literal["success", "failure", "partial", "skipped"]
ToolChoiceExposure = Literal[
    "orchestrator_controlled",
    "allowlisted_model_choice",
    "open_model_choice",
    "not_applicable",
]


class TraceModel(BaseModel):
    """评测轨迹 Pydantic 模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class TraceFingerprint(TraceModel):
    """运行指纹（输入/配置哈希）。"""
    project_version: str
    code_revision: str | None
    dataset_id: str
    dataset_version: str
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    prompt_version: str | None
    requested_model: str | None
    actual_model: str | None
    runner_version: str = TRACE_RUNNER_VERSION
    price_table_version: str | None


class TraceTokenUsage(TraceModel):
    """轨迹中的 token 用量。"""
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class TraceStep(TraceModel):
    """单步轨迹事件。"""
    sequence: int = Field(ge=1)
    kind: TraceKind
    status: TraceStatus
    started_at: datetime
    duration_ms: float = Field(ge=0)
    state_before: str | None
    state_after: str | None
    name: str | None
    tool_kind: str | None = None
    tool_call_sequence: int | None = Field(default=None, ge=1)
    redacted_input: dict[str, Any] | None = None
    input_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    output_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    evidence_refs: tuple[str, ...] = ()
    error_type: str | None = None
    error_message_code: str | None = None
    error_layer: str | None = None
    error_cause_type: str | None = None
    error_cause_chain: tuple[str, ...] = ()
    response_received: bool | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str | None = None
    retryable: bool | None = None
    retry_of: int | None = Field(default=None, ge=1)
    reason_code: str | None = None
    side_effect_class: str | None = None
    recovery_action: str | None = None
    token_usage: TraceTokenUsage | None = None

    @field_validator("started_at")
    @classmethod
    def require_aware_started_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("trace timestamps must include a timezone")
        return value


class TraceFinal(TraceModel):
    """轨迹终态摘要。"""
    state: str | None
    policy_outcome: str | None
    booking_allowed: bool | None
    result_refs: tuple[str, ...]
    user_response_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    failure_reason: str | None


class EvaluationTrace(TraceModel):
    """完整评测轨迹文档。"""
    schema_version: Literal[1] = TRACE_SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    evaluation_mode: EvaluationMode
    started_at: datetime
    completed_at: datetime
    fingerprint: TraceFingerprint
    tool_choice_exposure: ToolChoiceExposure
    steps: tuple[TraceStep, ...]
    final: TraceFinal

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("trace timestamps must include a timezone")
        return value


class EvaluationTraceRecorder:
    """工作流观察端口实现：记录并脱敏评测轨迹。"""

    def __init__(
        self,
        *,
        run_id: str,
        case_id: str,
        attempt: int,
        evaluation_mode: EvaluationMode,
        fingerprint: TraceFingerprint,
        tool_choice_exposure: ToolChoiceExposure,
    ) -> None:
        self.run_id = run_id
        self.case_id = case_id
        self.attempt = attempt
        self.evaluation_mode = evaluation_mode
        self.fingerprint = fingerprint
        self.tool_choice_exposure = tool_choice_exposure
        self.started_at = datetime.now(UTC)
        self._steps: list[TraceStep] = []

    def record(self, event: WorkflowTraceEvent) -> None:
        safe_input = _json_compatible(event.input_value)
        safe_output = _json_compatible(event.output_value)
        redacted = redact_json(safe_input).payload
        if not isinstance(redacted, dict):
            redacted = {"value": redacted}
        token_usage = None
        if any(
            value is not None
            for value in (
                event.input_tokens,
                event.output_tokens,
                event.cached_input_tokens,
                event.cache_write_input_tokens,
                event.reasoning_output_tokens,
                event.total_tokens,
            )
        ):
            token_usage = TraceTokenUsage(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cached_input_tokens=event.cached_input_tokens,
                cache_write_input_tokens=event.cache_write_input_tokens,
                reasoning_output_tokens=event.reasoning_output_tokens,
                total_tokens=event.total_tokens,
            )
        self._steps.append(
            TraceStep(
                sequence=len(self._steps) + 1,
                kind=event.kind,
                status=event.status,
                started_at=event.started_at,
                duration_ms=round(event.duration_ms, 6),
                state_before=event.state_before,
                state_after=event.state_after,
                name=event.name,
                tool_kind=event.tool_kind,
                tool_call_sequence=event.tool_call_sequence,
                redacted_input=redacted,
                input_hash=stable_hash(safe_input),
                output_hash=stable_hash(safe_output),
                evidence_refs=tuple(dict.fromkeys(event.evidence_refs)),
                error_type=event.error_type,
                error_message_code=event.error_message_code,
                error_layer=event.error_layer,
                error_cause_type=event.error_cause_type,
                error_cause_chain=event.error_cause_chain,
                response_received=event.response_received,
                http_status=event.http_status,
                provider_request_id=event.provider_request_id,
                retryable=event.retryable,
                retry_of=event.retry_of,
                reason_code=event.reason_code,
                side_effect_class=event.side_effect_class,
                recovery_action=event.recovery_action,
                token_usage=token_usage,
            )
        )

    def finish(self, final: TraceFinal) -> EvaluationTrace:
        return EvaluationTrace(
            run_id=self.run_id,
            case_id=self.case_id,
            attempt=self.attempt,
            evaluation_mode=self.evaluation_mode,
            started_at=self.started_at,
            completed_at=datetime.now(UTC),
            fingerprint=self.fingerprint,
            tool_choice_exposure=self.tool_choice_exposure,
            steps=tuple(self._steps),
            final=final,
        )


def _json_compatible(value: object) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    )


def _json_default(value: object) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"Unsupported trace value: {type(value).__name__}")

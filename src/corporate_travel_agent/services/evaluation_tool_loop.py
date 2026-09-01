"""工具循环真实模型运行的统一记账：协议 §5 轨迹 JSONL、价目表成本账本、重试上限核对。

ADR-0003 删掉旧的真实模型 harness 时，这三样一起没了；四个 tool-loop runner 各自只记
一个 `estimated_cost_usd`。这里把它们收回来，runner 只需要做四件事：

1. 整轮开一个 `LiveRunLedger`（模型、提示词版本、数据集指纹、价目表）；
2. 每条用例 `ledger.open_case(...)` 拿一个 `LiveCaseRecorder`——走编排器的传
   `trace_observer=recorder`，直接用 `ToolLoopRunner` 的传 `invoke=recorder.invoke`；
3. 用例跑完 `recorder.finish(...)`；
4. 整轮结束 `ledger.write(output_dir)`，得到 `traces.jsonl` / `cost-ledger.jsonl` /
   `cost-summary.json` / `retry-cap-check.json` 和它们的 SHA-256。

**用量从哪来。** 编排器给 LLM 调用记轨迹时从返回值的 `metadata` 取 token，而工具循环的
返回值 `ModelTurn` 不带它；适配器每次调用都把用量放在 `last_call_metadata` 上。记录器在
事件进来时按适配器的 `call_count` 判断"是不是刚完成了一次新调用"，是就把那次的用量补进
事件——同一次调用不会算两遍。

**重试上限核对看的是轨迹本身。** 同一个工具名沿 `retry_of` 连起来的链条不得长于上限
（LLM 2 次、供应商 3 次）；`retryable=False` 或 HTTP 402 之后不许再有 `retry_of` 指回来。
这是旧 D10/D11 那两条回归在工具循环上的落点。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any, TypeVar
from uuid import uuid4

from corporate_travel_agent import __version__
from corporate_travel_agent.agent.orchestrator import MAX_LLM_ATTEMPTS, MAX_PROVIDER_ATTEMPTS
from corporate_travel_agent.agent.ports import WorkflowTraceEvent
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_performance import ModelPriceTable
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationTrace,
    EvaluationTraceRecorder,
    ToolChoiceExposure,
    TraceFinal,
    TraceFingerprint,
)

T = TypeVar("T")

LIVE_TRACE_RUNNER_VERSION = "tool-loop-live-trace-v1"
TRACES_FILE = "traces.jsonl"
COST_LEDGER_FILE = "cost-ledger.jsonl"
COST_SUMMARY_FILE = "cost-summary.json"
RETRY_CAP_FILE = "retry-cap-check.json"


@dataclass
class LiveRunLedger:
    """一整轮真实模型运行的账本：轨迹、逐次成本、重试上限发现。"""

    model: str
    prompt_version: str | None
    runner_version: str
    dataset_id: str
    dataset_version: str
    dataset_sha256: str
    price_table: ModelPriceTable | None = None
    price_table_sha256: str | None = None
    code_revision: str | None = None
    llm_attempt_cap: int = MAX_LLM_ATTEMPTS
    provider_attempt_cap: int = MAX_PROVIDER_ATTEMPTS
    traces: list[EvaluationTrace] = field(default_factory=list)
    cost_rows: list[dict[str, Any]] = field(default_factory=list)
    retry_findings: list[dict[str, Any]] = field(default_factory=list)

    def fingerprint(self) -> TraceFingerprint:
        return TraceFingerprint(
            project_version=__version__,
            code_revision=self.code_revision,
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            dataset_sha256=self.dataset_sha256,
            prompt_version=self.prompt_version,
            requested_model=self.model,
            actual_model=self.model,
            runner_version=self.runner_version,
            price_table_version=(
                self.price_table.price_table_version if self.price_table is not None else None
            ),
        )

    def open_case(
        self,
        case_id: str,
        attempt: int,
        model_adapter: Any,
        *,
        tool_choice_exposure: ToolChoiceExposure = "allowlisted_model_choice",
    ) -> LiveCaseRecorder:
        return LiveCaseRecorder(
            ledger=self,
            run_id=f"run-{uuid4()}",
            case_id=case_id,
            attempt=attempt,
            model_adapter=model_adapter,
            tool_choice_exposure=tool_choice_exposure,
        )

    def price_of(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        table = self.price_table
        if (
            table is None
            or table.status != "configured"
            or self.model not in table.models
            or input_tokens is None
            or output_tokens is None
        ):
            return None
        price = table.models[self.model]
        return (input_tokens * price.input + output_tokens * price.output) / 1_000_000

    def write(self, output_dir: str | Path) -> dict[str, Any]:
        """把四份产物写进目录（目录可以已存在，文件不能已存在），返回摘要与 SHA-256。"""
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        for name in (TRACES_FILE, COST_LEDGER_FILE, COST_SUMMARY_FILE, RETRY_CAP_FILE):
            if (root / name).exists():
                raise FileExistsError(
                    f"{root / name} already exists; live artifacts are append-never"
                )
        traces_payload = "".join(f"{trace.model_dump_json()}\n" for trace in self.traces).encode()
        ledger_payload = "".join(
            f"{json.dumps(row, ensure_ascii=False, sort_keys=True)}\n" for row in self.cost_rows
        ).encode()
        summary = self.cost_summary()
        summary_payload = json.dumps(
            summary, ensure_ascii=False, indent=2, sort_keys=True
        ).encode()
        retry_check = self.retry_cap_check()
        retry_payload = json.dumps(
            retry_check, ensure_ascii=False, indent=2, sort_keys=True
        ).encode()
        (root / TRACES_FILE).write_bytes(traces_payload)
        (root / COST_LEDGER_FILE).write_bytes(ledger_payload)
        (root / COST_SUMMARY_FILE).write_bytes(summary_payload)
        (root / RETRY_CAP_FILE).write_bytes(retry_payload)
        return {
            "traces_file": TRACES_FILE,
            "traces_sha256": _sha256(traces_payload),
            "trace_count": len(self.traces),
            "cost_ledger_file": COST_LEDGER_FILE,
            "cost_ledger_sha256": _sha256(ledger_payload),
            "cost_summary_file": COST_SUMMARY_FILE,
            "cost_summary_sha256": _sha256(summary_payload),
            "retry_cap_check_file": RETRY_CAP_FILE,
            "retry_cap_check_sha256": _sha256(retry_payload),
            "estimated_cost_usd": summary["estimated_cost_usd"],
            "cost_complete": summary["complete"],
            "retry_cap_passed": retry_check["passed"],
        }

    def cost_summary(self) -> dict[str, Any]:
        priced = [row for row in self.cost_rows if row["cost_usd"] is not None]
        per_case: dict[str, dict[str, Any]] = {}
        for row in self.cost_rows:
            bucket = per_case.setdefault(
                row["case_id"],
                {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            )
            bucket["llm_calls"] += 1
            bucket["input_tokens"] += int(row["input_tokens"] or 0)
            bucket["output_tokens"] += int(row["output_tokens"] or 0)
            if row["cost_usd"] is not None:
                bucket["cost_usd"] = round(bucket["cost_usd"] + row["cost_usd"], 12)
        table = self.price_table
        return {
            "model": self.model,
            "currency": table.currency if table is not None else None,
            "price_table_version": table.price_table_version if table is not None else None,
            "price_table_sha256": self.price_table_sha256,
            "llm_calls": len(self.cost_rows),
            "input_tokens": sum(int(row["input_tokens"] or 0) for row in self.cost_rows),
            "output_tokens": sum(int(row["output_tokens"] or 0) for row in self.cost_rows),
            "estimated_cost_usd": (
                round(sum(row["cost_usd"] for row in priced), 12) if priced else None
            ),
            # 每一次调用都有价才算完整；缺价或缺用量的调用会让总数只是下界
            "complete": bool(self.cost_rows) and len(priced) == len(self.cost_rows),
            "unpriced_calls": len(self.cost_rows) - len(priced),
            "per_case": per_case,
            "note": (
                "Cached-input discounts are not modelled; the estimate is an upper bound."
                if table is not None
                else "No price table: token counts only."
            ),
        }

    def retry_cap_check(self) -> dict[str, Any]:
        return {
            "llm_attempt_cap": self.llm_attempt_cap,
            "provider_attempt_cap": self.provider_attempt_cap,
            "traces_checked": len(self.traces),
            "findings": list(self.retry_findings),
            "passed": not self.retry_findings,
        }


class LiveCaseRecorder:
    """一条用例一份：既是编排器的 `trace_observer`，也是 `ToolLoopRunner` 的 `invoke` 钩子。"""

    def __init__(
        self,
        *,
        ledger: LiveRunLedger,
        run_id: str,
        case_id: str,
        attempt: int,
        model_adapter: Any,
        tool_choice_exposure: ToolChoiceExposure,
    ) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.case_id = case_id
        self.attempt = attempt
        self.model = model_adapter
        self._recorder = EvaluationTraceRecorder(
            run_id=run_id,
            case_id=case_id,
            attempt=attempt,
            evaluation_mode="model_live_provider",
            fingerprint=ledger.fingerprint(),
            tool_choice_exposure=tool_choice_exposure,
        )
        self._seen_model_calls = int(getattr(model_adapter, "call_count", 0) or 0)
        self._sequence = 0

    # -- WorkflowTraceObserverPort ----------------------------------------

    def record(self, event: WorkflowTraceEvent) -> None:
        event = self._with_usage(event)
        self._recorder.record(event)
        self._sequence += 1
        if event.tool_kind == "LLM" and event.kind == "tool":
            self.ledger.cost_rows.append(
                {
                    "run_id": self.run_id,
                    "case_id": self.case_id,
                    "attempt": self.attempt,
                    "sequence": self._sequence,
                    "name": event.name,
                    "status": event.status,
                    "model": self.ledger.model,
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "cost_usd": self.ledger.price_of(event.input_tokens, event.output_tokens),
                }
            )

    # -- ToolLoopRunner.invoke ---------------------------------------------

    def invoke(self, *, tool_name: str, tool_kind: str, operation: Callable[[], T]) -> T:
        """直接驱动 `ToolLoopRunner` 时的钩子：计时、记轨迹、原样抛错。这条路没有宿主重试。

        签名和 `TripWorkflowOrchestrator._invoke_tool` 的关键字一致——`ToolLoopRunner._call`
        按 `tool_name= / tool_kind= / operation=` 调它。
        """
        name, kind = tool_name, tool_kind
        started_at = datetime.now(UTC)
        started_ns = perf_counter_ns()
        try:
            result = operation()
        except Exception as exc:
            self.record(
                WorkflowTraceEvent(
                    kind="tool",
                    name=name,
                    status="failure",
                    started_at=started_at,
                    duration_ms=(perf_counter_ns() - started_ns) / 1_000_000,
                    state_before=None,
                    state_after=None,
                    tool_kind=kind,
                    error_type=type(exc).__name__,
                    error_message_code=getattr(exc, "error_code", None),
                    error_layer=getattr(exc, "layer", None),
                    response_received=getattr(exc, "response_received", None),
                    http_status=getattr(exc, "http_status", None),
                    retryable=getattr(exc, "retryable", None),
                )
            )
            raise
        self.record(
            WorkflowTraceEvent(
                kind="tool",
                name=name,
                status="success",
                started_at=started_at,
                duration_ms=(perf_counter_ns() - started_ns) / 1_000_000,
                state_before=None,
                state_after=None,
                output_value=result if isinstance(result, dict) else None,
                evidence_refs=_refs_from(result),
                tool_kind=kind,
            )
        )
        return result

    def finish(
        self,
        *,
        state: str | None,
        policy_outcome: str | None = None,
        booking_allowed: bool | None = None,
        result_refs: tuple[str, ...] = (),
        user_response: object = None,
        failure_reason: str | None = None,
    ) -> EvaluationTrace:
        trace = self._recorder.finish(
            TraceFinal(
                state=state,
                policy_outcome=policy_outcome,
                booking_allowed=booking_allowed,
                result_refs=tuple(dict.fromkeys(result_refs)),
                user_response_hash=stable_hash(user_response) if user_response else None,
                failure_reason=failure_reason,
            )
        )
        self.ledger.traces.append(trace)
        self.ledger.retry_findings.extend(
            check_retry_caps(
                trace,
                llm_cap=self.ledger.llm_attempt_cap,
                provider_cap=self.ledger.provider_attempt_cap,
            )
        )
        return trace

    # -- 内部 ----------------------------------------------------------------

    def _with_usage(self, event: WorkflowTraceEvent) -> WorkflowTraceEvent:
        if event.tool_kind != "LLM" or event.kind != "tool" or event.input_tokens is not None:
            return event
        count = getattr(self.model, "call_count", None)
        metadata = getattr(self.model, "last_call_metadata", None)
        if count is None or metadata is None or int(count) == self._seen_model_calls:
            return event
        self._seen_model_calls = int(count)
        return replace(
            event,
            input_tokens=getattr(metadata, "input_tokens", None),
            output_tokens=getattr(metadata, "output_tokens", None),
            cached_input_tokens=getattr(metadata, "cached_input_tokens", None),
            cache_write_input_tokens=getattr(metadata, "cache_write_input_tokens", None),
            reasoning_output_tokens=getattr(metadata, "reasoning_output_tokens", None),
            total_tokens=getattr(metadata, "total_tokens", None),
        )


def check_retry_caps(
    trace: EvaluationTrace,
    *,
    llm_cap: int = MAX_LLM_ATTEMPTS,
    provider_cap: int = MAX_PROVIDER_ATTEMPTS,
) -> list[dict[str, Any]]:
    """按轨迹核对重试上限。返回发现的问题；空列表就是通过。

    - 同一工具沿 `retry_of` 连起来的链条长度 ≤ 上限（LLM 2 次、供应商 3 次）；
    - `retryable=False` 的失败之后不许有 `retry_of` 指回来；
    - HTTP 402（计费失败）之后不许有 `retry_of` 指回来。
    """
    findings: list[dict[str, Any]] = []
    steps = [step for step in trace.steps if step.kind == "tool"]
    by_call_sequence = {
        step.tool_call_sequence: step for step in steps if step.tool_call_sequence is not None
    }
    chain_head: dict[int, int] = {}
    chain_len: dict[int, int] = {}
    for step in steps:
        seq = step.tool_call_sequence
        if seq is None:
            continue
        if step.retry_of is None:
            chain_head[seq] = seq
            chain_len[seq] = 1
            continue
        head = chain_head.get(step.retry_of, step.retry_of)
        chain_head[seq] = head
        chain_len[head] = chain_len.get(head, 1) + 1
        previous = by_call_sequence.get(step.retry_of)
        if previous is not None and previous.retryable is False:
            findings.append(
                {
                    "case_id": trace.case_id,
                    "attempt": trace.attempt,
                    "kind": "retried_non_retryable_failure",
                    "name": step.name,
                    "retry_of": step.retry_of,
                    "sequence": seq,
                }
            )
        if previous is not None and previous.http_status == 402:
            findings.append(
                {
                    "case_id": trace.case_id,
                    "attempt": trace.attempt,
                    "kind": "retried_after_billing_failure",
                    "name": step.name,
                    "retry_of": step.retry_of,
                    "sequence": seq,
                }
            )
    for head, length in chain_len.items():
        step = by_call_sequence.get(head)
        if step is None:
            continue
        cap = llm_cap if step.tool_kind == "LLM" else provider_cap
        if length > cap:
            findings.append(
                {
                    "case_id": trace.case_id,
                    "attempt": trace.attempt,
                    "kind": "attempts_over_cap",
                    "name": step.name,
                    "tool_kind": step.tool_kind,
                    "attempts": length,
                    "cap": cap,
                    "first_sequence": head,
                }
            )
    return findings


def _refs_from(result: object) -> tuple[str, ...]:
    if not isinstance(result, dict):
        return ()
    refs = [
        str(option.get("ref_id"))
        for option in result.get("options", ()) or ()
        if isinstance(option, dict) and option.get("ref_id")
    ]
    return tuple(dict.fromkeys(refs))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()

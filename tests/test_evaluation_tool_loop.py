"""工具循环真实模型运行的记账：轨迹 JSONL、成本账本、重试上限——离线用假模型验。

ADR-0003 删旧 harness 时丢的三样，这里是它们在工具循环上的回归。模型是剧本，不联网。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from corporate_travel_agent.agent.ports import (
    LanguageModelError,
    LLMCallMetadata,
    WorkflowTraceEvent,
)
from corporate_travel_agent.agent.tool_loop import (
    ToolExchange,
    ToolExecutor,
    ToolInvocation,
    ToolLoopRunner,
    ToolSpec,
)
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system
from corporate_travel_agent.domain.enums import TaskState, ToolCallStatus
from corporate_travel_agent.services.evaluation_performance import ModelPriceTable, ModelTokenPrice
from corporate_travel_agent.services.evaluation_tool_loop import (
    COST_LEDGER_FILE,
    COST_SUMMARY_FILE,
    RETRY_CAP_FILE,
    TRACES_FILE,
    LiveRunLedger,
    check_retry_caps,
)
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationTrace,
    EvaluationTraceRecorder,
    TraceFinal,
)

READY = "8月5号从北京去上海，8月5日上午10点前到，不住酒店"
SCRIPT = [
    (
        "search_transport",
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5日上午10点前到",
        },
    ),
    ("propose_options", {"transport_refs": "__FOUND__", "summary": "早班机 08:35 到"}),
]
PRICES = ModelPriceTable(
    schema_version=1,
    price_table_version="test-prices",
    status="configured",
    currency="USD",
    unit="per_1m_tokens",
    effective_at="2026-09-01",
    source="test",
    models={"fake-model": ModelTokenPrice(input=1.0, output=2.0)},
)


class FakeAdapter:
    """像真适配器一样计数、把每次用量放在 `last_call_metadata` 上；可预设第一次失败。"""

    model = "fake-model"
    prompt_version = "tool-loop-v3"

    def __init__(self, script, *, fail_first: LanguageModelError | None = None) -> None:
        self._script = list(script)
        self._fail_first = fail_first
        self.call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_call_metadata: LLMCallMetadata | None = None
        self.found_refs: list[str] = []

    def next_tool_call(
        self,
        *,
        conversation: str,
        transcript: Sequence[ToolExchange],
        tools: Sequence[ToolSpec],
        context: Mapping[str, Any],
    ) -> ToolInvocation:
        del conversation, tools, context
        if self._fail_first is not None:
            error, self._fail_first = self._fail_first, None
            raise error
        self.call_count += 1
        self.input_tokens += 100
        self.output_tokens += 10
        self.last_call_metadata = LLMCallMetadata(
            prompt_version=self.prompt_version,
            model=self.model,
            duration_ms=1,
            input_tokens=100,
            output_tokens=10,
        )
        for exchange in transcript:
            if exchange.ok:
                for option in exchange.result.get("options", ()):  # type: ignore[union-attr]
                    ref = option.get("ref_id")
                    if ref and ref not in self.found_refs:
                        self.found_refs.append(str(ref))
        name, args = self._script.pop(0)
        resolved = {
            key: (self.found_refs[:1] if value == "__FOUND__" else value)
            for key, value in args.items()
        }
        return ToolInvocation(name=name, arguments=resolved)


def _ledger() -> LiveRunLedger:
    return LiveRunLedger(
        model="fake-model",
        prompt_version="tool-loop-v3",
        runner_version="test-runner-v1",
        dataset_id="test-cases",
        dataset_version="1",
        dataset_sha256="0" * 64,
        price_table=PRICES,
        price_table_sha256="1" * 64,
    )


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_orchestrator_path_writes_traces_costs_and_retry_check(tmp_path) -> None:
    adapter = FakeAdapter(SCRIPT)
    ledger = _ledger()
    recorder = ledger.open_case("C-1", 1, adapter)
    workflow, _ = build_demo_system(
        tool_calling_language_model=adapter, clock=lambda: DEMO_CLOCK, trace_observer=recorder
    )
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")
    assert task.state is TaskState.WAITING_FOR_USER
    recorder.finish(
        state=task.state.value,
        result_refs=tuple(ref for option in task.options for ref in option.inventory_refs),
        user_response=[option.option_id for option in task.options],
    )

    summary = ledger.write(tmp_path / "live")
    traces = [
        EvaluationTrace.model_validate_json(line)
        for line in (tmp_path / "live" / TRACES_FILE).read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(traces) == 1
    llm_steps = [
        step for step in traces[0].steps if step.kind == "tool" and step.tool_kind == "LLM"
    ]
    assert len(llm_steps) == 2
    assert all(
        step.token_usage is not None and step.token_usage.input_tokens == 100
        for step in llm_steps
    )
    assert any(step.name == "tool.search_transport" for step in traces[0].steps)
    assert traces[0].evaluation_mode == "model_live_provider"
    assert traces[0].fingerprint.price_table_version == "test-prices"

    rows = _read_jsonl(tmp_path / "live" / COST_LEDGER_FILE)
    assert len(rows) == 2 and all(row["cost_usd"] == pytest.approx(0.00012) for row in rows)
    cost = json.loads((tmp_path / "live" / COST_SUMMARY_FILE).read_text())
    assert cost["llm_calls"] == 2 and cost["complete"] is True
    assert cost["estimated_cost_usd"] == pytest.approx(0.00024)
    assert summary["estimated_cost_usd"] == pytest.approx(0.00024)
    retry = json.loads((tmp_path / "live" / RETRY_CAP_FILE).read_text())
    assert retry["passed"] is True and retry["traces_checked"] == 1
    # 产物只写一次，不许覆盖历史证据
    with pytest.raises(FileExistsError):
        ledger.write(tmp_path / "live")


def test_loop_runner_path_records_every_call_through_invoke(tmp_path) -> None:
    adapter = FakeAdapter(SCRIPT)
    ledger = _ledger()
    recorder = ledger.open_case("C-2", 1, adapter)
    workflow, provider = build_demo_system(clock=lambda: DEMO_CLOCK)
    executor = ToolExecutor(
        provider=provider,
        policy_engine=workflow.planner.policy_engine,
        employee=workflow.employees.snapshot("E1001"),
        policy=workflow.policies.current(),
        city_normalizer=workflow.city_normalizer,
        now=DEMO_CLOCK,
        fallback_timezone="Asia/Shanghai",
        conversation=READY,
    )
    outcome = ToolLoopRunner(model=adapter, executor=executor, invoke=recorder.invoke).run(READY)
    assert outcome.kind == "propose_options"
    trace = recorder.finish(state=outcome.kind, result_refs=tuple(outcome.transport_refs))

    names = [(step.name, step.tool_kind, step.status) for step in trace.steps]
    assert names == [
        ("llm.next_tool_call", "LLM", "success"),
        ("tool.search_transport", "PROVIDER", "success"),
        ("llm.next_tool_call", "LLM", "success"),
    ]
    assert trace.steps[1].evidence_refs  # 搜到的引用进了证据
    assert len(ledger.cost_rows) == 2
    assert ledger.cost_summary()["estimated_cost_usd"] == pytest.approx(0.00024)


def _synthetic_trace(events: list[WorkflowTraceEvent]) -> EvaluationTrace:
    recorder = EvaluationTraceRecorder(
        run_id="run-x",
        case_id="C-x",
        attempt=1,
        evaluation_mode="model_live_provider",
        fingerprint=_ledger().fingerprint(),
        tool_choice_exposure="allowlisted_model_choice",
    )
    for event in events:
        recorder.record(event)
    return recorder.finish(
        TraceFinal(
            state=None,
            policy_outcome=None,
            booking_allowed=None,
            result_refs=(),
            failure_reason=None,
        )
    )


def _event(seq: int, *, status: str, retry_of: int | None = None, **extra) -> WorkflowTraceEvent:
    return WorkflowTraceEvent(
        kind="tool",
        name="llm.next_tool_call",
        status=status,
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        duration_ms=1.0,
        state_before=None,
        state_after=None,
        tool_kind="LLM",
        tool_call_sequence=seq,
        retry_of=retry_of,
        **extra,
    )


def test_retry_cap_check_flags_over_cap_chains_and_forbidden_retries() -> None:
    clean = _synthetic_trace(
        [_event(1, status="failure", retryable=True), _event(2, status="success", retry_of=1)]
    )
    assert check_retry_caps(clean) == []

    over = _synthetic_trace(
        [
            _event(1, status="failure", retryable=True),
            _event(2, status="failure", retry_of=1, retryable=True),
            _event(3, status="success", retry_of=2),
        ]
    )
    kinds = [item["kind"] for item in check_retry_caps(over)]
    assert kinds == ["attempts_over_cap"]

    billing = _synthetic_trace(
        [
            _event(1, status="failure", retryable=False, http_status=402),
            _event(2, status="success", retry_of=1),
        ]
    )
    kinds = sorted(item["kind"] for item in check_retry_caps(billing))
    assert kinds == ["retried_after_billing_failure", "retried_non_retryable_failure"]


def test_tool_loop_retries_a_429_once_and_never_retries_a_402() -> None:
    """旧 D10/D11 在工具循环上的落点：瞬时故障有界重试，计费失败绝不重试。"""
    transient = LanguageModelError(
        "rate limited",
        error_code="LLM_RATE_LIMITED",
        layer="openai_transport",
        retryable=True,
        response_received=True,
        http_status=429,
    )
    adapter = FakeAdapter(SCRIPT, fail_first=transient)
    ledger = _ledger()
    recorder = ledger.open_case("R-429", 1, adapter)
    workflow, _ = build_demo_system(
        tool_calling_language_model=adapter,
        clock=lambda: DEMO_CLOCK,
        trace_observer=recorder,
        retry_sleep=lambda _: None,
    )
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")
    assert task.state is TaskState.WAITING_FOR_USER
    llm_records = [record for record in task.tool_calls if record.tool_name == "llm.next_tool_call"]
    assert [record.status for record in llm_records[:2]] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCEEDED,
    ]
    assert llm_records[0].retryable is True
    assert llm_records[1].retry_of == llm_records[0].sequence
    recorder.finish(state=task.state.value)
    assert ledger.retry_cap_check()["passed"] is True

    billing = LanguageModelError(
        "payment required",
        error_code="LLM_BILLING_FAILURE",
        layer="openai_transport",
        retryable=False,
        response_received=True,
        http_status=402,
    )
    adapter = FakeAdapter(SCRIPT, fail_first=billing)
    recorder = ledger.open_case("R-402", 1, adapter)
    workflow, _ = build_demo_system(
        tool_calling_language_model=adapter,
        clock=lambda: DEMO_CLOCK,
        trace_observer=recorder,
        retry_sleep=lambda _: None,
    )
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")
    assert task.state is TaskState.NEEDS_STRUCTURED_INPUT
    llm_records = [record for record in task.tool_calls if record.tool_name == "llm.next_tool_call"]
    assert len(llm_records) == 1
    assert llm_records[0].status is ToolCallStatus.FAILED
    assert all(record.retry_of is None for record in task.tool_calls)
    recorder.finish(state=task.state.value, failure_reason=task.failure)
    assert ledger.retry_cap_check()["passed"] is True

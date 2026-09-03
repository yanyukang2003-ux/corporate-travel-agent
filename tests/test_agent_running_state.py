"""AGENT_RUNNING：工具循环有自己的格子，不再事后补 SEARCHING → PLANNING 让边合法（ADR-0009）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from corporate_travel_agent.agent.ports import WorkflowTraceEvent
from corporate_travel_agent.agent.tool_loop import ToolExchange, ToolInvocation, ToolSpec
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system
from corporate_travel_agent.domain.enums import IntentEntrypoint, TaskState, ToolCallStatus
from corporate_travel_agent.domain.models import ToolCallRecord, TripTask
from corporate_travel_agent.workflow.state_machine import InvalidTransition, StateMachine


class _Collector:
    def __init__(self) -> None:
        self.events: list[WorkflowTraceEvent] = []

    def record(self, event: WorkflowTraceEvent) -> None:
        self.events.append(event)

    def transitions(self) -> list[tuple[str | None, str | None]]:
        return [
            (event.state_before, event.state_after)
            for event in self.events
            if event.kind == "state_transition"
        ]


class _ScriptedToolModel:
    """先搜一段，再把搜到的交出去。不联网、不花钱。"""

    prompt_version = "agent-running-scripted-v1"

    def __init__(self, script: Sequence[tuple[str, dict[str, Any]]]) -> None:
        self._script = list(script)
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
        for exchange in transcript:
            if exchange.ok and isinstance(exchange.result, dict):
                for option in exchange.result.get("options", ()):
                    ref = option.get("ref_id")
                    if ref and ref not in self.found_refs:
                        self.found_refs.append(str(ref))
        name, args = self._script.pop(0)
        resolved = {
            key: (self.found_refs[:1] if value == "__FOUND__" else value)
            for key, value in args.items()
        }
        return ToolInvocation(name=name, arguments=resolved)


def test_the_loop_runs_in_agent_running_and_only_plans_after_delivery() -> None:
    collector = _Collector()
    model = _ScriptedToolModel(
        [
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
    )
    workflow, _ = build_demo_system(
        clock=lambda: DEMO_CLOCK, tool_calling_language_model=model, trace_observer=collector
    )

    task = workflow.create_task_from_agentic_message(
        "8月5日上午10点前到上海，从北京出发", traveler_id="E1001"
    )

    assert task.state is TaskState.WAITING_FOR_USER
    assert collector.transitions() == [
        ("DRAFT", "AGENT_RUNNING"),
        ("AGENT_RUNNING", "PLANNING"),
        ("PLANNING", "OPTIONS_READY"),
        ("OPTIONS_READY", "WAITING_FOR_USER"),
    ]


def test_a_question_leaves_agent_running_without_touching_the_search_states() -> None:
    collector = _Collector()
    model = _ScriptedToolModel([("ask_traveler", {"question": "哪天出发？"})])
    workflow, _ = build_demo_system(
        clock=lambda: DEMO_CLOCK, tool_calling_language_model=model, trace_observer=collector
    )

    task = workflow.create_task_from_agentic_message("去上海开会", traveler_id="E1001")

    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert collector.transitions() == [
        ("DRAFT", "AGENT_RUNNING"),
        ("AGENT_RUNNING", "NEEDS_CLARIFICATION"),
    ]


def test_the_state_machine_no_longer_lets_draft_skip_the_loop() -> None:
    machine = StateMachine()
    assert machine.transition(TaskState.DRAFT, TaskState.AGENT_RUNNING) is TaskState.AGENT_RUNNING
    for target in (TaskState.NEEDS_CLARIFICATION, TaskState.OUT_OF_SCOPE, TaskState.PLANNING):
        try:
            machine.transition(TaskState.DRAFT, target)
        except InvalidTransition:
            continue
        raise AssertionError(f"DRAFT -> {target.value} should require the loop")
    # 循环从不借道 SEARCHING。
    try:
        machine.transition(TaskState.AGENT_RUNNING, TaskState.SEARCHING)
    except InvalidTransition:
        pass
    else:
        raise AssertionError("AGENT_RUNNING -> SEARCHING must not exist")


def _interrupted_task(workflow: Any, task_id: str, *, tool_kind: str) -> TripTask:
    started = DEMO_CLOCK - timedelta(minutes=5)
    task = TripTask(
        task_id=task_id,
        state=TaskState.AGENT_RUNNING,
        request=None,
        employee=workflow.employees.snapshot("E1001"),
        policy_snapshot_id=workflow.policies.current().snapshot_id,
        metadata={"intent_entrypoint": IntentEntrypoint.AGENTIC.value},
        tool_calls=[
            ToolCallRecord(
                sequence=1,
                tool_name="llm.next_tool_call" if tool_kind == "LLM" else "tool.search_transport",
                tool_kind=tool_kind,
                status=ToolCallStatus.STARTED,
                started_at=started,
            )
        ],
    )
    workflow.tasks.add(task)
    return task


def test_restart_recovery_sends_an_interrupted_loop_to_the_form_or_to_reconciliation() -> None:
    workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
    llm_only = _interrupted_task(workflow, "loop-llm", tool_kind="LLM")
    provider_in_flight = _interrupted_task(workflow, "loop-provider", tool_kind="PROVIDER")

    restarted, _ = build_demo_system(
        clock=lambda: DEMO_CLOCK,
        task_repository=workflow.tasks,
        interrupted_task_stale_seconds=0,
    )

    assert restarted.tasks.get(llm_only.task_id).state is TaskState.NEEDS_STRUCTURED_INPUT
    assert restarted.tasks.get(provider_in_flight.task_id).state is TaskState.PROVIDER_FAILED

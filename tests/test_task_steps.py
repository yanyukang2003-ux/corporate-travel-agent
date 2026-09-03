"""过程记录：把任务的每一步收集起来、按先后整理。只读已落库的东西，不改任务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi.testclient import TestClient

from corporate_travel_agent.agent.tool_loop import ToolExchange, ToolInvocation, ToolSpec
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.services.task_steps import collect_task_steps

READY = "8月5号从北京去上海，8月5日上午10点前到，不住酒店"


class ScriptedToolModel:
    """按剧本吐工具调用的假模型；`__FOUND__` 换成本轮真搜到的引用。

    像真适配器一样把每次"请求/响应"记进 `exchanges`——函数级过程记录靠它。
    """

    prompt_version = "scripted-v1"

    def __init__(self, script: Sequence[tuple[str, dict[str, Any]]]) -> None:
        self._script = list(script)
        self.found_refs: list[str] = []
        self.exchanges: list[dict[str, Any]] = []

    def next_tool_call(
        self,
        *,
        conversation: str,
        transcript: Sequence[ToolExchange],
        tools: Sequence[ToolSpec],
        context: Mapping[str, Any],
    ) -> ToolInvocation:
        del tools, context
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
        self.exchanges.append(
            {
                "function": "ScriptedToolModel.next_tool_call",
                "api": "scripted",
                "prompt_version": self.prompt_version,
                "request": {
                    "model": "scripted",
                    "messages": [
                        {"role": "system", "content": "scripted system prompt"},
                        {"role": "user", "content": conversation},
                    ],
                    "tools": [{"type": "function", "function": {"name": "scripted"}}],
                },
                "response": {
                    "content": None,
                    "tool_calls": [{"name": name, "arguments": resolved}],
                },
            }
        )
        return ToolInvocation(name=name, arguments=resolved)


_SCRIPT = [
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


def _agentic_task():
    workflow, _ = build_demo_system(
        tool_calling_language_model=ScriptedToolModel(_SCRIPT), clock=lambda: DEMO_CLOCK
    )
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")
    return workflow, task


def test_steps_are_ordered_and_cover_every_source() -> None:
    workflow, task = _agentic_task()
    steps = collect_task_steps(
        task,
        events=workflow.tasks.events(task.task_id),
        snapshots=workflow.tasks.snapshots(task.task_id),
    )

    kinds = [step["kind"] for step in steps]
    assert kinds.count("message") >= 1
    assert "tool_call" in kinds and "search" in kinds and "plan" in kinds and "milestone" in kinds
    # 序号连续，时间不倒流
    assert [step["sequence"] for step in steps] == list(range(1, len(steps) + 1))
    stamped = [step["at"] for step in steps if step["at"]]
    assert stamped == sorted(stamped)
    # 第一步是旅行者的原话
    assert steps[0]["kind"] == "message"
    assert READY in steps[0]["detail"]["content"]


def test_a_search_step_carries_evidence_assumption_and_results() -> None:
    workflow, task = _agentic_task()
    steps = collect_task_steps(
        task,
        events=workflow.tasks.events(task.task_id),
        snapshots=workflow.tasks.snapshots(task.task_id),
    )
    search = next(step for step in steps if step["kind"] == "search")
    assert search["detail"]["date_evidence"] == "8月5日上午10点前到"
    assert search["detail"]["assumption"]  # 「往前 18 小时」那句
    assert search["detail"]["result_count"] >= 1
    assert search["detail"]["samples"] and search["detail"]["samples"][0]["ref_id"]
    plan = next(step for step in steps if step["kind"] == "plan")
    assert plan["detail"]["options"][0]["route"].startswith("Beijing")
    assert plan["detail"]["options"][0]["policy_outcome"]


def test_steps_endpoint_returns_the_ordered_record() -> None:
    from corporate_travel_agent.api import main as api_main

    client = TestClient(api_main.app)
    previous = api_main.workflow.clock
    api_main.workflow.clock = lambda: DEMO_CLOCK
    try:
        task = api_main.workflow.create_task(make_demo_request(task_id="steps-api-1"))
        response = client.get(f"/trip-tasks/{task.task_id}/steps")
    finally:
        api_main.workflow.clock = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(payload["steps"]) > 0
    kinds = {step["kind"] for step in payload["steps"]}
    assert "search" in kinds and "plan" in kinds and "milestone" in kinds
    assert client.get("/trip-tasks/no-such-task/steps").status_code == 404


def test_steps_reach_function_level_with_full_payloads() -> None:
    """函数级：LLM 步骤带完整请求/响应报文，工具步骤带参数和完整结果，每轮循环一步装配。"""
    workflow, task = _agentic_task()
    steps = collect_task_steps(
        task,
        events=workflow.tasks.events(task.task_id),
        snapshots=workflow.tasks.snapshots(task.task_id),
    )

    assert all("function" in step for step in steps)

    run = next(step for step in steps if step["kind"] == "loop_run")
    assert run["detail"]["conversation"].startswith("[turn:0")
    assert run["detail"]["context"]["reference_time"]
    # 成功的终局动作就是 outcome 本身：kind/summary/refs 就是 propose_options 的参数
    assert run["detail"]["outcome"]["kind"] == "propose_options"
    assert run["detail"]["outcome"]["transport_refs"]
    assert run["detail"]["refused_exchanges"] == []

    llm = next(
        step
        for step in steps
        if step["kind"] == "tool_call" and step["detail"]["tool_name"] == "llm.next_tool_call"
    )
    assert llm["detail"]["llm_request"]["messages"][0]["role"] == "system"
    assert llm["detail"]["llm_response"]["tool_calls"][0]["name"] == "search_transport"

    search_call = next(
        step
        for step in steps
        if step["kind"] == "tool_call" and step["detail"]["tool_name"] == "tool.search_transport"
    )
    assert search_call["detail"]["arguments"]["origin"] == "北京"
    assert search_call["detail"]["result"]["option_count"] >= 1
    assert search_call["detail"]["result"]["options"][0]["ref_id"]
    assert "ToolExecutor.search_transport" in search_call["function"]

"""流式入口：SSE 把过程一步步推出去，最后给完整任务。剧本模型，不联网不花钱。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from corporate_travel_agent.agent.tool_loop import ToolExchange, ToolInvocation, ToolSpec
from corporate_travel_agent.demo import DEMO_CLOCK


class ScriptedToolModel:
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


@pytest.fixture()
def scripted_app():
    from corporate_travel_agent.api import main as api_main

    previous_clock = api_main.workflow.clock
    previous_model = api_main.workflow.tool_calling_language_model
    api_main.workflow.clock = lambda: DEMO_CLOCK
    api_main.workflow.tool_calling_language_model = ScriptedToolModel(
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
    try:
        yield TestClient(api_main.app)
    finally:
        api_main.workflow.clock = previous_clock
        api_main.workflow.tool_calling_language_model = previous_model


def _events(body: str) -> list[tuple[str, Any]]:
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line]
        if not lines:
            continue
        name = next(line[len("event: "):] for line in lines if line.startswith("event: "))
        data = next(line[len("data: "):] for line in lines if line.startswith("data: "))
        events.append((name, json.loads(data)))
    return events


def test_streaming_create_emits_steps_then_the_full_task(scripted_app) -> None:
    with scripted_app.stream(
        "POST",
        "/agentic/trip-tasks/stream",
        json={
            "message": "8月5号从北京去上海，8月5日上午10点前到，不住酒店",
            "traveler_id": "E1001",
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in response.iter_text())

    events = _events(body)
    names = [name for name, _ in events]
    assert names[0] == "accepted"
    assert names[-1] == "done"
    assert "step" in names and "task" in names
    # 步骤和 GET /steps 同一结构；最后的任务是完整公开视图
    first_step = next(data for name, data in events if name == "step")
    assert {"kind", "title", "detail"} <= set(first_step)
    task = next(data for name, data in events if name == "task")
    assert task["state"] == "WAITING_FOR_USER"
    assert task["options"]
    assert task["task_id"] == next(data for name, data in events if name == "accepted")["task_id"]


def test_streaming_failure_becomes_an_error_event(scripted_app) -> None:
    from corporate_travel_agent.api import main as api_main

    api_main.workflow.tool_calling_language_model = None
    with scripted_app.stream(
        "POST",
        "/agentic/trip-tasks/stream",
        json={"message": "8月5号去上海", "traveler_id": "E1001"},
    ) as response:
        body = "".join(chunk for chunk in response.iter_text())
    events = _events(body)
    names = [name for name, _ in events]
    assert "error" in names and "task" not in names and names[-1] == "done"

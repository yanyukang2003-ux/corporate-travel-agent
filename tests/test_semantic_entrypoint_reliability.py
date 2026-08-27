"""语义入口的出错路径与留痕。

`test_semantic_intent.py` 覆盖的是"一切正常时理解得对不对"。本文件覆盖的是
出问题时系统停在哪里、留下了什么证据：

- 模型调用失败 / 模型给的引用对不上原话；
- 工具调用次数用超；
- 反复追问仍问不清；
- 成功编译查询、判定超范围时写下的审计事件；
- 语义判定历史的留存上限。

所有用例都用脚本化的假模型，不联网、不花钱。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from semantic_fixtures import (
    READY_MESSAGE,
    ScriptedSemanticModel,
    needs_clarification,
    semantic_decision,
    semantic_intent,
)

from corporate_travel_agent.agent.ports import LanguageModelError
from corporate_travel_agent.agent.semantic_intent import EvidenceRef, IntentDecisionStatus
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.audit import stable_hash

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _workflow(model: ScriptedSemanticModel, **kwargs: object):
    workflow, _ = build_demo_system(
        semantic_language_model=model,
        clock=lambda: datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
        **kwargs,
    )
    return workflow


def _event_types(workflow, task_id: str) -> list[str]:
    return [event.event_type for event in workflow.tasks.events(task_id)]


def _events(workflow, task_id: str, event_type: str) -> list[object]:
    return [
        event for event in workflow.tasks.events(task_id) if event.event_type == event_type
    ]


def _last_event(workflow, task_id: str, event_type: str):
    events = _events(workflow, task_id, event_type)
    assert events, f"expected an audit event of type {event_type}"
    return events[-1]


def _provider_tool_names(task) -> list[str]:
    return [item.tool_name for item in task.tool_calls if item.tool_kind == "PROVIDER"]


# --- 模型不可用 ---------------------------------------------------------------


def test_model_failure_pauses_for_structured_input_and_records_the_reason() -> None:
    """模型挂了就停在结构化表单，不能假装读懂，也不能去查库存。"""
    failure = LanguageModelError(
        "upstream refused the request",
        error_code="LLM_UNAVAILABLE",
        layer="language_model",
        cause_type="APIConnectionError",
        retryable=False,
        response_received=False,
        http_status=503,
    )
    model = ScriptedSemanticModel(raises=failure)
    workflow = _workflow(model)

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert task.state is TaskState.NEEDS_STRUCTURED_INPUT
    assert task.request is None
    assert task.clarification_question is None
    assert "upstream refused the request" in (task.failure or "")
    recorded = task.metadata["semantic_intent_failure"]
    assert recorded["error_code"] == "LLM_UNAVAILABLE"
    assert recorded["http_status"] == 503
    assert recorded["retryable"] is False
    assert "SEMANTIC_INTENT_FAILED" in _event_types(workflow, task.task_id)
    assert _provider_tool_names(task) == []
    assert "semantic_intent" not in task.metadata


def test_model_failure_never_falls_back_to_the_legacy_extraction_path() -> None:
    """没有兼容回退：语义入口宁可停下，也不偷偷走旧链路。"""
    model = ScriptedSemanticModel(
        raises=LanguageModelError("timeout", error_code="LLM_TIMEOUT", retryable=False)
    )
    workflow = _workflow(model)

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert model.extract_calls == 0
    assert task.metadata["intent_entrypoint"] == "semantic"
    assert task.state is TaskState.NEEDS_STRUCTURED_INPUT


def test_ungrounded_evidence_is_treated_as_a_model_failure_not_a_valid_answer() -> None:
    """模型引用的原话在对话里找不到，等于这次解释作废，不是"低置信度接受"。"""
    model = ScriptedSemanticModel(
        [
            semantic_decision(
                evidence=[
                    EvidenceRef(turn_index=0, field="origin", quote="北京"),
                    EvidenceRef(turn_index=0, field="destination", quote="上海"),
                    EvidenceRef(turn_index=0, field="departure_after", quote="8月5日"),
                    EvidenceRef(
                        turn_index=0, field="arrive_by", quote="用户从来没说过的这句话"
                    ),
                ]
            )
        ]
    )
    workflow = _workflow(model)

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert task.state is TaskState.NEEDS_STRUCTURED_INPUT
    assert task.request is None
    failure = task.metadata["semantic_intent_failure"]
    assert failure["error_code"] == "INTENT_EVIDENCE_QUOTE_INVALID"
    assert failure["error_layer"] == "semantic_intent"
    assert _provider_tool_names(task) == []


def test_a_failed_interpretation_can_be_reopened_by_the_next_message() -> None:
    """模型恢复后，用户再发一句就能接着走，不必新建任务。"""
    model = ScriptedSemanticModel(
        raises=LanguageModelError("boom", error_code="LLM_UNAVAILABLE", retryable=False)
    )
    workflow = _workflow(model)
    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")
    assert task.state is TaskState.NEEDS_STRUCTURED_INPUT

    model.raises = None
    model.decisions.append(
        semantic_decision(
            evidence=[
                EvidenceRef(turn_index=1, field="origin", quote="北京"),
                EvidenceRef(turn_index=1, field="destination", quote="上海"),
                EvidenceRef(turn_index=1, field="departure_after", quote="8月5日"),
                EvidenceRef(turn_index=1, field="arrive_by", quote="8月6日上午10点"),
            ]
        )
    )

    task = workflow.submit_semantic_message(task.task_id, READY_MESSAGE)

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.failure is None
    assert "SEMANTIC_CONVERSATION_REOPENED" in _event_types(workflow, task.task_id)


# --- 工具调用次数用超 ---------------------------------------------------------


def test_running_out_of_tool_budget_stops_before_the_next_interpretation() -> None:
    """预算用完就停在 TOOL_BUDGET_EXHAUSTED，并写明是卡在哪一步。"""
    model = ScriptedSemanticModel([needs_clarification()])
    workflow = _workflow(model, max_tool_calls=1)

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")
    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert task.tool_calls_remaining == 0

    task = workflow.submit_semantic_message(task.task_id, "北京")

    assert task.state is TaskState.TOOL_BUDGET_EXHAUSTED
    assert "llm.interpret_trip_intent" in (task.failure or "")
    # 审计事件只存哈希，不落明文；因此按哈希核对它记的确实是这一步。
    budget_event = _last_event(workflow, task.task_id, "TOOL_BUDGET_EXHAUSTED")
    assert budget_event.input_hash == stable_hash(
        {"operation": "llm.interpret_trip_intent", "required_calls": 1}
    )
    # 第二次解释根本没发生：模型只被调用过一次。
    assert model.calls == 1
    assert _provider_tool_names(task) == []


# --- 反复问不清 ---------------------------------------------------------------


def test_clarification_limit_hands_over_to_the_structured_form_without_searching() -> None:
    """问到上限还没问清，转结构化表单——绝不因为"字段凑齐了"就去查。"""
    model = ScriptedSemanticModel([needs_clarification()])
    workflow = _workflow(model)
    workflow.max_clarification_rounds = 1

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")
    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert task.clarification_rounds == 1

    task = workflow.submit_semantic_message(task.task_id, "还是没想好")

    assert task.state is TaskState.NEEDS_STRUCTURED_INPUT
    assert task.clarification_question is None
    assert "structured form" in (task.failure or "")
    assert "origin" in task.missing_required_fields
    exhausted = _last_event(workflow, task.task_id, "SEMANTIC_CLARIFICATION_EXHAUSTED")
    assert exhausted.input_hash == stable_hash(
        {"missing": task.missing_required_fields, "conflicts": task.intent_conflicts}
    )
    assert exhausted.output_hash == stable_hash(task.failure)
    assert task.request is None
    assert _provider_tool_names(task) == []


def test_clarification_rounds_are_counted_on_the_task_not_reset_by_new_messages() -> None:
    model = ScriptedSemanticModel([needs_clarification()])
    workflow = _workflow(model)
    workflow.max_clarification_rounds = 4

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")
    for answer in ("再想想", "还没定"):
        task = workflow.submit_semantic_message(task.task_id, answer)

    assert task.clarification_rounds == 3
    assert task.state is TaskState.NEEDS_CLARIFICATION


# --- 审计留痕 -----------------------------------------------------------------


def test_compiling_a_search_command_is_audited_before_any_provider_call() -> None:
    """查库存之前必须先留下"我按这个理解去查"的痕迹，事后可核对。"""
    model = ScriptedSemanticModel([semantic_decision()])
    workflow = _workflow(model)

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    events = workflow.tasks.events(task.task_id)
    types = [event.event_type for event in events]
    assert "SEMANTIC_TASK_CREATED_FROM_MESSAGE" in types
    # 进入 SEARCHING（开始查库存）之前，必须已经写下按哪个理解去查。
    entered_searching = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "STATE_TRANSITION"
        and event.output_hash == stable_hash(TaskState.SEARCHING.value)
    )
    assert types.index("SEARCH_COMMAND_COMPILED") < entered_searching
    compiled = _last_event(workflow, task.task_id, "SEARCH_COMMAND_COMPILED")
    summary = task.metadata["semantic_intent"]["decision"]["intent"]["summary"]
    assert compiled.input_hash == stable_hash(summary)
    assert compiled.output_hash == stable_hash(task.request)


def test_clarification_request_is_audited_with_what_is_still_missing() -> None:
    model = ScriptedSemanticModel([needs_clarification("请确认从北京还是上海出发？")])
    workflow = _workflow(model)

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert "origin" in task.missing_required_fields
    requested = _last_event(workflow, task.task_id, "SEMANTIC_CLARIFICATION_REQUESTED")
    assert requested.input_hash == stable_hash(
        {
            "missing": task.missing_required_fields,
            "conflicts": task.intent_conflicts,
            "unsupported": False,
        }
    )
    assert requested.output_hash == stable_hash("请确认从北京还是上海出发？")


def test_out_of_scope_is_audited_and_never_reaches_the_provider() -> None:
    model = ScriptedSemanticModel(
        [
            semantic_decision(
                semantic_intent(summary="用户在问报销政策，不是要订行程"),
                status=IntentDecisionStatus.OUT_OF_SCOPE,
                conflicts=["not a travel booking request"],
                evidence=[EvidenceRef(turn_index=0, field="scope", quote="北京")],
            )
        ]
    )
    workflow = _workflow(model)

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert task.state is TaskState.OUT_OF_SCOPE
    assert task.request is None
    assert task.intent_conflicts == ("not a travel booking request",)
    assert "SEMANTIC_INTENT_OUT_OF_SCOPE" in _event_types(workflow, task.task_id)
    assert _provider_tool_names(task) == []


def test_semantic_decision_history_keeps_the_last_twenty_interpretations() -> None:
    """留痕有上限，避免任务无限膨胀；最新一条永远在最后。"""
    model = ScriptedSemanticModel([needs_clarification()])
    workflow = _workflow(model, max_tool_calls=40)
    workflow.max_clarification_rounds = 100

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")
    for index in range(24):
        task = workflow.submit_semantic_message(task.task_id, f"再想想 {index}")

    history = task.metadata["semantic_intent_history"]
    assert len(history) == 20
    assert history[-1] == task.metadata["semantic_intent"]
    assert {item["source"] for item in history} == {"semantic"}
    assert history[-1]["turn_count"] > history[0]["turn_count"]


# --- 日期已过 -----------------------------------------------------------------


def test_a_departure_date_that_has_passed_is_reported_as_such_not_re_asked() -> None:
    """用户确认了一个已经过去的日期，就直接告诉他日期已过，不再兜圈子问。"""
    past = semantic_decision(
        semantic_intent(
            summary="8月5日从北京去上海",
            departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI),
            arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI),
        ),
        # 模型自以为读懂了，还给了一句无关的追问；宿主的结论必须盖过它。
        clarification_question="要我顺便订酒店吗？",
    )
    model = ScriptedSemanticModel([past])
    workflow, _ = build_demo_system(
        semantic_language_model=model,
        # 参照时刻在出发日之后：这一天已经过去了。
        clock=lambda: datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI),
    )

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert task.request is None
    assert _provider_tool_names(task) == []
    assert any(item.startswith("日期已过") for item in task.intent_conflicts), (
        task.intent_conflicts
    )
    question = task.clarification_question or ""
    assert question.startswith("日期已过")
    assert "2026-08-05" in question
    # 模型那句无关的追问不许盖过"日期已过"这个确定性结论。
    assert "订酒店" not in question


def test_a_return_date_that_has_passed_is_caught_too() -> None:
    past_return = semantic_decision(
        semantic_intent(
            summary="返程写成了去年",
            departure_after=datetime(2026, 9, 1, 8, 0, tzinfo=SHANGHAI),
            arrive_by=datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI),
            return_after=datetime(2026, 8, 10, 18, 0, tzinfo=SHANGHAI),
            return_before=datetime(2026, 8, 10, 23, 0, tzinfo=SHANGHAI),
        )
    )
    workflow, _ = build_demo_system(
        semantic_language_model=ScriptedSemanticModel([past_return]),
        clock=lambda: datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI),
    )

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert _provider_tool_names(task) == []
    assert any("返程日期" in item for item in task.intent_conflicts), task.intent_conflicts


def test_a_future_date_still_compiles_and_searches() -> None:
    """新规则只拦已过去的日期，未来的日期照常走完整链路。"""
    workflow, _ = build_demo_system(
        semantic_language_model=ScriptedSemanticModel([semantic_decision()]),
        clock=lambda: datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.request is not None
    assert not any(item.startswith("日期已过") for item in task.intent_conflicts)


def test_departing_later_today_is_not_treated_as_a_past_date() -> None:
    """比的是日历日，不是精确时刻：今天早上出发的行程，下午问也不该被判过期。"""
    today = semantic_decision(
        semantic_intent(
            summary="今天出发",
            departure_after=datetime(2026, 8, 5, 6, 0, tzinfo=SHANGHAI),
            arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI),
        )
    )
    workflow, _ = build_demo_system(
        semantic_language_model=ScriptedSemanticModel([today]),
        # 已经是当天下午，晚于 departure_after 这个瞬间，但同一天。
        clock=lambda: datetime(2026, 8, 5, 14, 0, tzinfo=SHANGHAI),
    )

    task = workflow.create_task_from_semantic_message(READY_MESSAGE, traveler_id="E1001")

    assert not any(item.startswith("日期已过") for item in task.intent_conflicts), (
        task.intent_conflicts
    )

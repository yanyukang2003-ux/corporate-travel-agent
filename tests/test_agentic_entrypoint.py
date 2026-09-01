"""工具循环入口接进宿主之后，端到端这条路要真的走得通，硬边界一条都不许松。

`tests/test_tool_loop.py` 验的是循环内核；这里验的是**接线**：预算和审计有没有
真的覆盖每一次循环调用、终局动作有没有落成正确的任务状态、政策还是不是由确定性
代码说了算。模型是写死的剧本，不联网、不花钱。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.agent.tool_loop import ToolExchange, ToolInvocation, ToolSpec
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system
from corporate_travel_agent.domain.enums import IntentEntrypoint, TaskState

# demo 库存里北京→上海：CA-EVE 前一晚 21:50 到，MU-EARLY 08:35 到，MU-COMFORT 12:15 到。
READY = "8月5号从北京去上海，8月5日上午10点前到，不住酒店"


class ScriptedToolModel:
    """按剧本吐工具调用的假模型。`__FOUND__` 会被换成本轮真搜到的引用。"""

    def __init__(self, script: Sequence[tuple[str, dict[str, Any]]]) -> None:
        self._script = list(script)
        self.seen_transcripts: list[int] = []
        self.seen_tools: list[tuple[str, ...]] = []
        self.found_refs: list[str] = []

    def next_tool_call(
        self,
        *,
        conversation: str,
        transcript: Sequence[ToolExchange],
        tools: Sequence[ToolSpec],
        context: Mapping[str, Any],
    ) -> ToolInvocation:
        del conversation, context
        self.seen_transcripts.append(len(transcript))
        self.seen_tools.append(tuple(spec.name for spec in tools))
        for exchange in transcript:
            if exchange.ok:
                for option in exchange.result.get("options", ()):  # type: ignore[union-attr]
                    ref = option.get("ref_id")
                    if ref and ref not in self.found_refs:
                        self.found_refs.append(str(ref))
        if not self._script:
            raise AssertionError("剧本用完了，循环还在要下一步")
        name, args = self._script.pop(0)
        resolved = {
            key: (self.found_refs[:1] if value == "__FOUND__" else value)
            for key, value in args.items()
        }
        return ToolInvocation(name=name, arguments=resolved)


def _workflow(script, **kwargs):
    return build_demo_system(
        tool_calling_language_model=ScriptedToolModel(script),
        clock=lambda: DEMO_CLOCK,
        **kwargs,
    )


_SEARCH_AND_PROPOSE = [
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


def test_the_question_that_started_all_this_is_gone() -> None:
    """旧链路对这句话答"另外我还需要知道：哪天出发"——用户原话里就写着 8 月 5 号。"""
    workflow, _ = _workflow(_SEARCH_AND_PROPOSE)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.clarification_question is None
    assert task.options
    # 只说了"几点前到"，搜索窗口从时限往前算，而且当面说了出来。
    assert task.assumptions == (
        "搜索窗口从 2026-08-04 16:00（Beijing 当地）起算，"
        "到达时限往前 18 小时，这样前一晚出发也能被搜到；"
        "因为你要求 08月05日 10:00 前到达",
    )


def test_every_loop_call_is_counted_and_audited() -> None:
    """预算和审计不是循环自己另起一套，是复用宿主那一套——否则等于绕过了闸门。"""
    workflow, _ = _workflow(_SEARCH_AND_PROPOSE)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    names = [record.tool_name for record in task.tool_calls]
    # 两轮选工具 + 一次真实搜索，一次都没漏进审计。
    assert names.count("llm.next_tool_call") == 2
    assert "tool.search_transport" in names
    assert task.tool_calls_used == len(names)
    assert all(record.counts_toward_budget for record in task.tool_calls)


def test_the_loop_cannot_outspend_the_shared_tool_budget() -> None:
    """轮数上限按**剩余预算**算，循环在被预算掐断之前就该自己停下。

    一轮最多花两次预算（选工具 + 执行工具）。此前轮数直接取 max_tool_calls，
    循环永远是被预算杀掉的，"最后一轮只给终局工具"那道保险根本轮不到生效。
    """
    endless = [("lookup_city", {"name": "北京"})] * 30
    workflow, _ = _workflow(endless, agentic_tool_call_limit=4)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert task.tool_calls_used <= 4
    assert task.state is not TaskState.TOOL_BUDGET_EXHAUSTED
    assert not task.options
    assert task.failure


def test_the_last_round_only_offers_terminal_tools() -> None:
    """**没得搜了，它只能交付或者开口问人。**

    真模型实测：一趟多城行程里两段没货，它就一条条换路线继续搜到预算见底，
    用户拿到的是一句"预算用完了"。加守卫拦得住某一种绕法，拦不住下一种。
    收窄最后一轮的工具清单才是结构性的答案——边界仍然在工具签名上。
    """
    script = [("lookup_city", {"name": "北京"})] * 10
    model = ScriptedToolModel(script)
    workflow, _ = build_demo_system(
        tool_calling_language_model=model,
        clock=lambda: DEMO_CLOCK,
        agentic_tool_call_limit=6,
    )
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert model.seen_tools, "模型一次都没被问过"
    assert "search_transport" in model.seen_tools[0]
    assert set(model.seen_tools[-1]) == {"ask_traveler", "propose_options"}
    # 收窄的是**能调什么**，不只是看得见什么：最后一轮里搜索工具连派发表都进不去。
    last = task.metadata["agentic_transcript"][-1]
    assert last["ok"] is False
    assert last["tool"] == "lookup_city"


def test_asking_is_an_action_the_model_chooses() -> None:
    """提问不再是编译失败的副产品，是模型主动挑的一个工具——所以它能引用真实选项。"""
    script = [
        (
            "search_transport",
            {
                "origin": "北京",
                "destination": "上海",
                "arrive_by": "2026-08-05T10:00:00+08:00",
                "date_evidence": "8月5号",
            },
        ),
        ("ask_traveler", {"question": "早上 08:35 到的那班 950 元，可以吗？"}),
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.options
    assert task.clarification_question == "早上 08:35 到的那班 950 元，可以吗？"
    assert task.messages[-1].role == "assistant"
    # 问题是在搜过之后问的：模型手上有真实库存才开口。
    assert any(record.tool_name == "tool.search_transport" for record in task.tool_calls)


def test_an_empty_second_leg_does_not_kill_the_first() -> None:
    """上海→杭州没票，北京→上海照样交出去。整单失败是旧出口，不是 Codex 的做法。"""
    message = "8月5号从北京去上海，10点前到，8月6号去杭州，不住酒店"
    script = [
        (
            "search_transport",
            {
                "origin": "北京",
                "destination": "上海",
                "arrive_by": "2026-08-05T10:00:00+08:00",
                "date_evidence": "8月5号",
            },
        ),
        (
            "search_transport",
            {
                "origin": "上海",
                "destination": "杭州",
                "arrive_by": "2026-08-06T23:59:00+08:00",
                "date_evidence": "8月6号去杭州",
            },
        ),
        ("propose_options", {"transport_refs": "__FOUND__", "summary": "先订北京到上海"}),
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(message, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.options
    assert task.request is not None
    legs = task.request.transport_legs()
    assert [(leg.origin, leg.destination) for leg in legs] == [("Beijing", "Shanghai")]
    assert task.clarification_question
    assert "Hangzhou" in task.clarification_question or "杭州" in task.clarification_question
    # 空段说明必须写进方案自己的摘要，不能只躺在追问里。看方案的人
    # 否则会以为这一张票就是全程。
    assert "查不了火车票" in task.clarification_question
    for option in task.options:
        assert any("查不了火车票" in fact for fact in option.explanation_facts)
        assert any(
            "Hangzhou" in fact or "杭州" in fact for fact in option.explanation_facts
        )


def test_options_still_come_from_the_deterministic_planner() -> None:
    """**模型说"这几条可以"不算数。** 方案由规划器和政策引擎产出，模型只提议。"""
    workflow, _ = _workflow(_SEARCH_AND_PROPOSE)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert task.options
    for option in task.options:
        assert option.policy_decision is not None
        assert option.inventory_refs
        assert all("查不了火车票" not in fact for fact in option.explanation_facts)


def test_an_invented_reference_never_becomes_an_option() -> None:
    """没搜过就报一个引用 = 凭空造库存。

    `MU-EARLY` 是 demo 库存里真实存在的编号，但**本轮没搜过**——所以它一样不算数。
    交付关卡认的是"这一轮真的拿回来过"，不是"这个编号在世界上存在"。
    模型可以改，改不出来就一直交付不了，绝不会变成摆给用户的方案。
    """
    invented = ("propose_options", {"transport_refs": ["MU-EARLY"], "summary": "x"})
    workflow, _ = _workflow([invented] * 20)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert not task.options
    assert task.state is not TaskState.WAITING_FOR_USER
    assert task.failure


def test_the_request_records_one_leg_per_search() -> None:
    """行程有几段是**数出来的**——数模型实际搜了几段，不是让它先声明。

    对话里两段的日期都要有出处，否则第二段会被日期出处那道关卡拦下（拦得对）。
    """
    round_trip = "8月5号从北京去上海，上午10点前到，8月6号回北京，不住酒店"
    script = [
        (
            "search_transport",
            {
                "origin": "北京",
                "destination": "上海",
                "arrive_by": "2026-08-05T10:00:00+08:00",
                "date_evidence": "8月5号",
            },
        ),
        (
            "search_transport",
            {
                "origin": "上海",
                "destination": "北京",
                "arrive_by": "2026-08-06T23:00:00+08:00",
                "date_evidence": "8月6号回北京",
            },
        ),
        ("propose_options", {"transport_refs": "__FOUND__", "summary": "往返"}),
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(round_trip, traveler_id="E1001")

    assert task.request is not None
    legs = task.request.transport_legs()
    assert [(leg.origin, leg.destination) for leg in legs] == [
        ("Beijing", "Shanghai"),
        ("Shanghai", "Beijing"),
    ]


def test_a_bad_tool_call_does_not_kill_the_task() -> None:
    """入口拒绝只废掉那一次调用；模型改一次就继续。旧链路这里整条编译停住。"""
    script = [
        (
            "search_transport",
            {
                "origin": "北京",
                "destination": "上海",
                "arrive_by": "下周三",
                "date_evidence": "8月5号",
            },
        ),
        *_SEARCH_AND_PROPOSE,
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    transcript = task.metadata["agentic_transcript"]
    assert transcript[0]["ok"] is False
    assert transcript[0]["tool"] == "search_transport"


def test_a_follow_up_message_replays_the_whole_conversation() -> None:
    """追问的回答不是"补一格"，是整段对话重跑——上一轮的错误不会被带下来。"""
    script = [
        ("ask_traveler", {"question": "哪天到？"}),
        *_SEARCH_AND_PROPOSE,
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message("从北京去上海", traveler_id="E1001")
    assert task.state is TaskState.NEEDS_CLARIFICATION

    task = workflow.submit_agentic_message(task.task_id, "8月5日上午10点前到，不住酒店")
    assert task.state is TaskState.WAITING_FOR_USER
    assert task.options


def test_agentic_tasks_refuse_the_other_entrypoints() -> None:
    """入口互不串门：结构化建的任务不能用工具循环续聊，反之亦然。

    旧的 legacy / semantic 入口已删除（ADR-0003）；已持久化的旧任务仍带着那两个
    `intent_entrypoint` 值，同样会被这道检查拒绝。
    """
    from corporate_travel_agent.demo import make_demo_request

    workflow, _ = _workflow([("ask_traveler", {"question": "哪天到？"})])
    structured = workflow.create_task(make_demo_request(task_id="structured-task"))
    assert structured.metadata["intent_entrypoint"] == IntentEntrypoint.STRUCTURED.value
    with pytest.raises(WorkflowError, match="agentic"):
        workflow.submit_agentic_message(structured.task_id, "8月5日到")

    task = workflow.create_task_from_agentic_message("从北京去上海", traveler_id="E1001")
    assert task.metadata["intent_entrypoint"] == IntentEntrypoint.AGENTIC.value
    task.metadata["intent_entrypoint"] = IntentEntrypoint.SEMANTIC.value
    with pytest.raises(WorkflowError, match="agentic"):
        workflow.submit_agentic_message(task.task_id, "8月5日到")


def test_the_agentic_entrypoint_gets_its_own_budget() -> None:
    """循环一轮花两次预算，旧链路一个动作花一次——所以它需要更高的上限。

    另外两条入口保持 12 不动：它们有冻结的评测基线，改了预算旧数字就不能直接对比。
    """
    workflow, _ = _workflow(_SEARCH_AND_PROPOSE)
    agentic = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert agentic.tool_call_limit == 20
    assert workflow.max_tool_calls == 12


# ---------------------------------------------------------------------------
# 交付契约：要求跟着方案走；没货和越界各有各的落点
# ---------------------------------------------------------------------------


def _search_beijing_shanghai() -> tuple[str, dict[str, Any]]:
    return (
        "search_transport",
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5日上午10点前到",
        },
    )


def test_a_declared_hard_constraint_is_enforced_by_the_planner() -> None:
    """此前循环写出来的请求一条要求都不带："只坐高铁"到了规划器就没了。"""
    script = [
        _search_beijing_shanghai(),
        (
            "propose_options",
            {
                "transport_refs": "__FOUND__",
                "summary": "只看高铁",
                "hard_constraints": ["train_only"],
            },
        ),
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(
        "8月5号从北京去上海，8月5日上午10点前到，只坐高铁，不住酒店", traveler_id="E1001"
    )

    # 时间窗里只有飞机：要求被真的执行了，结果就是没有可行方案，而不是把飞机端上来。
    assert task.state is TaskState.NO_FEASIBLE_OPTION
    assert task.request is not None
    assert task.request.hard_constraints == ("train_only",)
    assert task.request.scoped_hard_constraints[0].whole_journey


def test_a_declared_preference_changes_the_ranking_penalty() -> None:
    def run(soft: list[str]):
        script = [
            _search_beijing_shanghai(),
            (
                "propose_options",
                {"transport_refs": "__FOUND__", "summary": "x", "soft_preferences": soft},
            ),
        ]
        workflow, _ = _workflow(script)
        return workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    plain = run([])
    avoiding_early = run(["avoid_early_departure"])

    assert plain.request is not None and plain.request.soft_preferences == ()
    assert avoiding_early.request is not None
    assert avoiding_early.request.soft_preferences == ("avoid_early_departure",)
    early = {
        option.outbound.ref_id: option.preference_penalty for option in avoiding_early.options
    }
    assert early["MU-EARLY"] > 0
    assert all(option.preference_penalty == 0 for option in plain.options)
    # 候选池和政策结论一个字不变——偏好只改排序。
    assert {o.option_id for o in plain.options} == {o.option_id for o in avoiding_early.options}


def test_an_unknown_requirement_name_is_refused_and_recoverable() -> None:
    script = [
        _search_beijing_shanghai(),
        (
            "propose_options",
            {"transport_refs": "__FOUND__", "summary": "x", "hard_constraints": ["no_red_eye"]},
        ),
        (
            "propose_options",
            {
                "transport_refs": "__FOUND__",
                "summary": "x",
                "open_questions": ["系统做不到「不要红眼航班」这个要求，方案里可能有晚班"],
            },
        ),
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.options
    assert task.request is not None and task.request.hard_constraints == ()
    transcript = task.metadata["agentic_transcript"]
    rejected = [item for item in transcript if item["tool"] == "propose_options" and not item["ok"]]
    assert len(rejected) == 1
    assert "不要红眼航班" in (task.clarification_question or "")


def test_hotel_required_without_a_hotel_search_is_refused() -> None:
    script = [
        _search_beijing_shanghai(),
        (
            "propose_options",
            {"transport_refs": "__FOUND__", "summary": "x", "hard_constraints": ["hotel_required"]},
        ),
        ("propose_options", {"transport_refs": "__FOUND__", "summary": "x"}),
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.request is not None and task.request.hard_constraints == ()


def test_all_empty_searches_end_in_no_feasible_option_not_a_clarification_round() -> None:
    message = "8月5号从北京去深圳，8月5日上午10点前到，不住酒店"
    script = [
        (
            "search_transport",
            {
                "origin": "北京",
                "destination": "深圳",
                "arrive_by": "2026-08-05T10:00:00+08:00",
                "date_evidence": "8月5日上午10点前到",
            },
        ),
        ("ask_traveler", {"question": "北京到深圳这个时间窗里没有可用交通，要不要换个时间？"}),
        (
            "search_transport",
            {
                "origin": "北京",
                "destination": "深圳",
                "arrive_by": "2026-08-06T10:00:00+08:00",
                "date_evidence": "8月6号",
            },
        ),
        ("ask_traveler", {"question": "8月6号也没有。"}),
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message(message, traveler_id="E1001")

    assert task.state is TaskState.NO_FEASIBLE_OPTION
    assert task.clarification_rounds == 0
    assert task.clarification_question == "北京到深圳这个时间窗里没有可用交通，要不要换个时间？"
    assert task.messages[-1].role == "assistant"
    reasons = task.metadata["no_feasible_reasons"]
    assert any("Beijing→Shenzhen" in item or "北京→深圳" in item for item in reasons)
    # 搜过的证据留下来：出处和快照都在，"我们搜过、是空的"本身就是溯源的一部分。
    assert task.searches
    assert not task.options

    # 这个状态可以接着说话：旅行者换个日期，循环重跑。
    task = workflow.submit_agentic_message(task.task_id, "那改成8月6号")
    assert task.state is TaskState.NO_FEASIBLE_OPTION
    assert task.clarification_rounds == 0
    assert task.messages[-1].content == "8月6号也没有。"


def test_an_out_of_scope_request_lands_in_out_of_scope_and_can_be_reopened() -> None:
    script = [
        ("ask_traveler", {"question": "这不是差旅需求；需要安排出差吗？", "out_of_scope": True}),
        *_SEARCH_AND_PROPOSE,
    ]
    workflow, _ = _workflow(script)
    task = workflow.create_task_from_agentic_message("帮我写一份周报", traveler_id="E1001")

    assert task.state is TaskState.OUT_OF_SCOPE
    assert task.clarification_rounds == 0
    assert task.clarification_question == "这不是差旅需求；需要安排出差吗？"
    assert task.messages[-1].role == "assistant"
    assert not any(record.tool_kind == "PROVIDER" for record in task.tool_calls)

    # 判错了，旅行者再说一句就回来了。
    task = workflow.submit_agentic_message(task.task_id, READY)
    assert task.state is TaskState.WAITING_FOR_USER
    assert task.options

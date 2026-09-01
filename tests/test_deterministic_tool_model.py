"""工具循环的离线替身：它只做真模型会做的事，而且每一步都不猜。

替身存在的理由是让产品入口能进 CI（见 `tests/test_product_entrypoint_evaluation.py`）；
这里验的是替身本身：什么时候搜、什么时候问、交付时带不带旅行者说过的要求。
"""

from __future__ import annotations

import pytest

from corporate_travel_agent.agent.deterministic_semantic_interpreter import (
    DeterministicSemanticInterpreter,
)
from corporate_travel_agent.agent.deterministic_tool_model import (
    OUT_OF_SCOPE_QUESTION,
    DeterministicToolCallingModel,
)
from corporate_travel_agent.agent.semantic_intent import ConversationLedger, IntentDecisionStatus
from corporate_travel_agent.agent.tool_loop import (
    DEFAULT_TOOLS,
    ToolExchange,
    ToolInvocation,
)
from corporate_travel_agent.domain.models import ConversationMessage
from corporate_travel_agent.services.evaluation_dataset import load_evaluation_dataset

CONTEXT = {"reference_time": "2026-07-20T09:00:00+00:00", "timezone": "Asia/Shanghai"}
ROUND_TRIP_WITH_HOTEL = (
    "I need to book a corporate trip from St. Louis to Baltimore. I cannot leave before "
    "2026-08-04T06:00:00Z and I must land by 2026-08-04T13:00:00Z. Coming home no earlier "
    "than 2026-08-06T15:00:00Z and back by 2026-08-06T23:00:00Z. The hotel runs 2026-08-04 "
    "to 2026-08-06. I also need a hotel booked for the nights in between. The trip has to be "
    "a direct connection. Keep the total as cheap as you can."
)


def _conversation(*messages: str) -> str:
    return ConversationLedger.from_messages(
        [ConversationMessage(role="user", content=item) for item in messages]
    ).render()


def _turn(model: DeterministicToolCallingModel, conversation: str, transcript=()):
    return model.next_turn(
        conversation=conversation,
        transcript=tuple(transcript),
        tools=DEFAULT_TOOLS,
        context=CONTEXT,
    )


def _search_exchange(origin: str, destination: str, refs: tuple[str, ...]) -> ToolExchange:
    return ToolExchange(
        invocation=ToolInvocation(
            "search_transport",
            {"origin": origin, "destination": destination, "arrive_by": "x", "date_evidence": "y"},
        ),
        ok=True,
        result={
            "origin": origin,
            "destination": destination,
            "options": [{"ref_id": ref} for ref in refs],
            "option_count": len(refs),
        },
    )


@pytest.fixture(scope="module")
def model() -> DeterministicToolCallingModel:
    return DeterministicToolCallingModel()


def test_a_ready_message_opens_with_one_search_per_leg_and_per_stay(model) -> None:
    conversation = _conversation(ROUND_TRIP_WITH_HOTEL)
    turn = _turn(model, conversation)

    names = [call.name for call in turn.calls]
    assert names == ["search_transport", "search_transport", "search_hotels"]
    outbound, inbound, hotel = turn.calls
    assert (outbound.arguments["origin"], outbound.arguments["destination"]) == (
        "St. Louis",
        "Baltimore",
    )
    assert (inbound.arguments["origin"], inbound.arguments["destination"]) == (
        "Baltimore",
        "St. Louis",
    )
    assert outbound.arguments["arrive_by"].startswith("2026-08-04T13:00:00")
    assert inbound.arguments["arrive_by"].startswith("2026-08-06T23:00:00")
    # 每一段的日期出处都是对话里的原话——工具边界会逐字核对，编的对不上。
    for call in (outbound, inbound):
        assert call.arguments["date_evidence"] in ROUND_TRIP_WITH_HOTEL
    assert hotel.arguments == {
        "city": "Baltimore",
        "check_in": "2026-08-04",
        "check_out": "2026-08-06",
    }


def test_a_missing_deadline_asks_instead_of_searching(model) -> None:
    turn = _turn(model, _conversation("I need to book a corporate trip from Beijing to Shanghai."))

    assert [call.name for call in turn.calls] == ["ask_traveler"]
    assert turn.calls[0].arguments["question"]
    assert not turn.calls[0].arguments.get("out_of_scope")


def test_an_out_of_scope_request_is_flagged_not_clarified(model) -> None:
    """替身和语义替身共用一个解释器：它判越界的那句话，替身就打 out_of_scope 标记。"""
    dataset = load_evaluation_dataset("data/evaluation/derived-v2")
    interpreter = DeterministicSemanticInterpreter()
    out_of_scope_message = next(
        case.message
        for case in dataset.intent_cases
        if case.expected.classification == "OUT_OF_SCOPE"
        and interpreter.interpret_trip_intent(
            _conversation(case.message), task_id="t", traveler_id="e", context=dict(CONTEXT)
        ).decision.status
        is IntentDecisionStatus.OUT_OF_SCOPE
    )

    turn = _turn(model, _conversation(out_of_scope_message))

    assert [call.name for call in turn.calls] == ["ask_traveler"]
    assert turn.calls[0].arguments == {"question": OUT_OF_SCOPE_QUESTION, "out_of_scope": True}


def test_the_second_turn_hands_over_real_refs_and_declares_requirements(model) -> None:
    conversation = _conversation(ROUND_TRIP_WITH_HOTEL)
    transcript = [
        _search_exchange("St. Louis", "Baltimore", ("OUT-1", "OUT-2")),
        _search_exchange("Baltimore", "St. Louis", ("IN-1",)),
        ToolExchange(
            invocation=ToolInvocation("search_hotels", {}),
            ok=True,
            result={"options": [{"ref_id": "HT-1"}], "option_count": 1},
        ),
    ]

    turn = _turn(model, conversation, transcript)

    assert [call.name for call in turn.calls] == ["propose_options"]
    args = turn.calls[0].arguments
    assert args["transport_refs"] == ["OUT-1", "OUT-2", "IN-1"]
    assert args["hotel_refs"] == ["HT-1"]
    assert args["open_questions"] == []
    # 旅行者说过的要求跟着方案走：直飞是硬要求，最便宜是偏好，订酒店是硬要求。
    assert set(args["hard_constraints"]) == {"direct_only", "hotel_required"}
    assert args["soft_preferences"] == ["lowest_cost"]


def test_an_empty_leg_becomes_an_open_question_not_a_dead_end(model) -> None:
    conversation = _conversation(ROUND_TRIP_WITH_HOTEL)
    transcript = [
        _search_exchange("St. Louis", "Baltimore", ("OUT-1",)),
        _search_exchange("Baltimore", "St. Louis", ()),
    ]

    turn = _turn(model, conversation, transcript)

    args = turn.calls[0].arguments
    assert turn.calls[0].name == "propose_options"
    assert args["transport_refs"] == ["OUT-1"]
    assert any("Baltimore→St. Louis" in item for item in args["open_questions"])


def test_all_empty_searches_end_in_a_question_that_names_the_route(model) -> None:
    conversation = _conversation(ROUND_TRIP_WITH_HOTEL)
    transcript = [_search_exchange("St. Louis", "Baltimore", ())]

    turn = _turn(model, conversation, transcript)

    assert [call.name for call in turn.calls] == ["ask_traveler"]
    assert "St. Louis→Baltimore" in turn.calls[0].arguments["question"]
    assert not turn.calls[0].arguments.get("out_of_scope")


def test_a_rejected_delivery_retries_without_requirements_then_asks(model) -> None:
    """交付被工具边界拒了：先去掉要求再交一次，再被拒就开口——绝不无限重试。"""
    conversation = _conversation(ROUND_TRIP_WITH_HOTEL)
    rejected = ToolExchange(
        invocation=ToolInvocation("propose_options", {}),
        ok=False,
        result={"error": "hard_constraints 里有系统不认识的名字"},
    )
    transcript = [_search_exchange("St. Louis", "Baltimore", ("OUT-1",)), rejected]

    retry = _turn(model, conversation, transcript)
    assert retry.calls[0].name == "propose_options"
    assert "hard_constraints" not in retry.calls[0].arguments
    assert "soft_preferences" not in retry.calls[0].arguments

    give_up = _turn(model, conversation, [*transcript, rejected])
    assert [call.name for call in give_up.calls] == ["ask_traveler"]


def test_the_standin_carries_nothing_between_conversations(model) -> None:
    """评测里一个替身实例跑几百条用例；上一趟的理解不能漏进下一趟。"""
    ready = _turn(model, _conversation(ROUND_TRIP_WITH_HOTEL))
    _turn(model, _conversation("I need to book a corporate trip from Beijing to Shanghai."))
    again = _turn(model, _conversation(ROUND_TRIP_WITH_HOTEL))

    assert [call.name for call in again.calls] == [call.name for call in ready.calls]
    assert again.calls[0].arguments == ready.calls[0].arguments

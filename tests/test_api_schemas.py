"""响应模型和序列化字典逐键对齐：漏一个键，OpenAPI 就少一个字段，这里会报出来。"""

from __future__ import annotations

from decimal import Decimal

from corporate_travel_agent.api.schemas import (
    OptionOut,
    TaskResponse,
    TaskSummaryResponse,
    TripAggregateResponse,
)
from corporate_travel_agent.api.serializers import public_task, public_task_summary, public_trip
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request


def _workflow():
    workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
    return workflow


def test_task_response_matches_the_serializer_key_for_key() -> None:
    workflow = _workflow()
    task = workflow.create_task(make_demo_request(task_id="schema-1"))
    payload = public_task(workflow, task)
    assert set(payload) == set(TaskResponse.model_fields)
    parsed = TaskResponse.model_validate(payload)
    assert parsed.options, "the demo request should produce options"
    assert set(payload["options"][0]) == set(OptionOut.model_fields)
    # 金额此前以数字发出去；响应模型不能把它变成字符串。
    assert isinstance(parsed.options[0].total_cost, float)
    assert parsed.options[0].total_cost == float(task.options[0].total_cost)


def test_task_response_after_selection_and_confirmation() -> None:
    workflow = _workflow()
    task = workflow.create_task(make_demo_request(task_id="schema-2"))
    compliant = next(
        item for item in task.options if item.policy_decision.outcome.value == "COMPLIANT"
    )
    task = workflow.select_option(task.task_id, compliant.option_id)
    task = workflow.confirm_booking(
        task.task_id,
        order_references=["ORD-1"],
        total_amount=Decimal("1000"),
        currency=task.options[0].currency,
        reported_by="E1001",
    )
    payload = public_task(workflow, task)
    parsed = TaskResponse.model_validate(payload)
    assert parsed.booking_intent is not None
    assert parsed.booking_confirmation is not None
    assert parsed.booking_confirmation.total_amount == 1000.0

    trip = workflow.trips.get(task.trip_id)
    trip_payload = public_trip(trip)
    assert set(trip_payload) == set(TripAggregateResponse.model_fields)
    parsed_trip = TripAggregateResponse.model_validate(trip_payload)
    assert parsed_trip.watch is not None and parsed_trip.watch.legs


def test_task_summary_matches_its_model() -> None:
    workflow = _workflow()
    workflow.create_task(make_demo_request(task_id="schema-3"))
    summary = workflow.tasks.list_task_summaries(limit=1)[0]
    payload = public_task_summary(summary)
    assert set(payload) == set(TaskSummaryResponse.model_fields)
    TaskSummaryResponse.model_validate(payload)

"""Structured AskUserQuestion-style clarification gates."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.clarification_questions import (
    apply_clarification_answer,
    apply_option_letter,
    build_clarification_bundle,
    detect_uncertain_slots,
    should_defer_structured_option_to_llm,
)
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import LodgingRequirement, TaskState
from corporate_travel_agent.domain.models import ConversationMessage, TripTask

SH = ZoneInfo("Asia/Shanghai")
FIXED = datetime(2026, 8, 12, 12, 0, tzinfo=SH)


def test_detect_return_uncertainty_from_message() -> None:
    fields = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": FIXED,
        "arrive_by": FIXED,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": [],
        "soft_preferences": [],
    }
    uncertain = detect_uncertain_slots(
        fields,
        user_message="北京到上海开会，当天下午回",
        assumptions=(),
    )
    assert "return_trip" in uncertain


def test_compare_train_and_flight_is_not_treated_as_mode_conflict() -> None:
    fields = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": FIXED,
        "arrive_by": FIXED,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": [],
        "soft_preferences": ["compare_train_and_flight"],
    }
    assert "transport_mode" not in detect_uncertain_slots(
        fields,
        user_message="Compare train and flight options for Beijing to Shanghai",
        assumptions=(),
    )


def test_detect_client_location_when_proximity_requested() -> None:
    fields = {
        "origin": "New York",
        "destination": "Philadelphia",
        "departure_after": FIXED,
        "arrive_by": FIXED,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": datetime(2026, 8, 25).date(),
        "hotel_check_out": datetime(2026, 8, 26).date(),
        "hard_constraints": ["hotel_required"],
        "soft_preferences": ["hotel_near_client"],
    }
    uncertain = detect_uncertain_slots(
        fields,
        user_message="下周二纽约到费城，酒店离客户公司近一点",
        assumptions=(),
    )
    assert "client_location" in uncertain

    skipped = dict(fields, client_location="SKIPPED")
    assert "client_location" not in detect_uncertain_slots(
        skipped,
        user_message="下周二纽约到费城，酒店离客户公司近一点",
        assumptions=(),
    )


def test_detect_hotel_uncertainty_from_user_text_not_soft_pref() -> None:
    fields = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": FIXED,
        "arrive_by": FIXED,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": [],
        "soft_preferences": ["hotel_near_client"],
    }
    # Soft pref alone does not block search.
    assert "hotel_need" not in detect_uncertain_slots(
        fields, user_message="北京到上海出差", assumptions=()
    )
    # Explicit hotel mention does.
    uncertain = detect_uncertain_slots(
        fields,
        user_message="北京到上海出差，住一晚酒店",
        assumptions=(),
    )
    assert "hotel_need" in uncertain


def test_build_bundle_has_options() -> None:
    bundle = build_clarification_bundle(uncertain=("return_trip", "hotel_need"))
    assert bundle is not None
    ids = {q.id for q in bundle.questions}
    assert "return_trip" in ids
    assert "hotel_need" in ids
    assert "A." in bundle.prompt_text or "A." in bundle.prompt_text.replace(" ", "")
    for q in bundle.questions:
        assert 2 <= len(q.options) <= 4


def test_overnight_template_resyncs_existing_hotel_dates() -> None:
    fields = {
        "origin": "New York",
        "destination": "Philadelphia",
        "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=ZoneInfo("America/New_York")),
        "arrive_by": datetime(2026, 8, 25, 18, 0, tzinfo=ZoneInfo("America/New_York")),
        "return_after": datetime(2026, 8, 26, 13, 0, tzinfo=ZoneInfo("America/New_York")),
        "return_before": datetime(2026, 8, 26, 22, 0, tzinfo=ZoneInfo("America/New_York")),
        "hotel_check_in": datetime(2026, 8, 25).date(),
        "hotel_check_out": datetime(2026, 8, 26).date(),
        "lodging_requirement": LodgingRequirement.REQUIRED.value,
        "hard_constraints": ["hotel_required"],
        "soft_preferences": ["hotel_near_client"],
    }

    updated, notes, _ = apply_clarification_answer(
        fields,
        "template:overnight",
        reference_time=FIXED,
        timezone_name="America/New_York",
    )

    assert updated["hotel_check_in"] == updated["arrive_by"].date()
    assert updated["hotel_check_out"] == updated["return_after"].date()
    assert any("hotel_check_in" in item for item in notes)


def test_apply_return_no_and_letter() -> None:
    fields = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": FIXED,
        "arrive_by": FIXED,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": [],
        "soft_preferences": ["hotel_near_client"],
    }
    updated, notes, hints = apply_clarification_answer(
        fields, "只要去程", reference_time=FIXED, timezone_name="Asia/Shanghai"
    )
    assert "one_way" in " ".join(notes) or "clarification:one_way" in notes
    assert updated["return_after"] is None

    bundle = build_clarification_bundle(uncertain=("return_trip",))
    assert bundle is not None
    questions = [q.as_dict() for q in bundle.questions]
    letter = apply_option_letter(
        fields, "B", questions, reference_time=FIXED, timezone_name="Asia/Shanghai"
    )
    assert letter is not None
    updated2, notes2, _ = letter
    assert notes2


def test_hotel_clarification_answers_update_lodging_requirement() -> None:
    fields = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": FIXED,
        "arrive_by": FIXED,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "lodging_requirement": LodgingRequirement.UNSPECIFIED.value,
        "hard_constraints": [],
        "soft_preferences": ["hotel_near_client"],
    }

    required, _, required_hints = apply_clarification_answer(
        fields,
        "hotel:required",
        reference_time=FIXED,
        timezone_name="Asia/Shanghai",
    )
    assert required["lodging_requirement"] == LodgingRequirement.REQUIRED.value
    assert "hotel_required" in required["hard_constraints"]
    assert required_hints

    skipped, _, _ = apply_clarification_answer(
        required,
        "hotel:skip",
        reference_time=FIXED,
        timezone_name="Asia/Shanghai",
    )
    assert skipped["lodging_requirement"] == LodgingRequirement.NOT_REQUIRED.value
    assert "hotel_required" not in skipped["hard_constraints"]


def test_orchestrator_clarifies_before_search_without_llm() -> None:
    """Deterministic path: force intent fields + uncertain message via clarification API."""
    workflow, _ = build_demo_system(clock=lambda: FIXED)
    # Build a draft task that would have been ready except return uncertainty.
    employee = workflow.employees.snapshot("E1001")
    policy = workflow.policies.current()
    task = TripTask(
        task_id="clarify-return-1",
        state=TaskState.DRAFT,
        request=None,
        employee=employee,
        policy_snapshot_id=policy.snapshot_id,
        intent_fields={
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": datetime(2026, 8, 19, 8, 0, tzinfo=SH),
            "arrive_by": datetime(2026, 8, 20, 10, 0, tzinfo=SH),
            "return_after": None,
            "return_before": None,
            "hotel_check_in": None,
            "hotel_check_out": None,
            "hard_constraints": [],
            "soft_preferences": ["hotel_near_client"],
        },
        assumptions=("用户说当天下午回",),
        messages=[ConversationMessage(role="user", content="北京到上海，当天下午回，酒店近一点")],
        metadata={"intent_classification": "TRIP"},
    )
    workflow.tasks.add(task)
    # Use public clarification entry as if param loop accepted.
    result = workflow._request_clarification(
        task,
        missing=(),
        conflicts=(),
        uncertain=detect_uncertain_slots(
            task.intent_fields,
            user_message="北京到上海，当天下午回，酒店近一点",
            assumptions=task.assumptions,
        ),
        tool_use_error=None,
    )
    assert result.state is TaskState.NEEDS_CLARIFICATION
    assert result.clarification_question
    assert result.metadata.get("clarification_questions")
    assert (
        "return" in result.clarification_question.lower()
        or "返程" in result.clarification_question
    )

    # Choose one-way via label; hotel still uncertain → may ask hotel next or clear hotel.
    next_task = workflow.submit_message(task.task_id, "只要去程")
    # Without language model, submit_message may fail for free path; option path should work.
    # demo system has no LLM by default — option application should not need LLM if structured.
    assert next_task.state in {
        TaskState.NEEDS_CLARIFICATION,
        TaskState.WAITING_FOR_USER,
        TaskState.NO_FEASIBLE_OPTION,
        TaskState.OPTIONS_READY,
        TaskState.SEARCHING,
        TaskState.PLANNING,
    }
    # One-way applied
    assert next_task.intent_fields.get("return_after") is None


def test_exact_ui_tokens_are_not_deferred_to_llm() -> None:
    assert should_defer_structured_option_to_llm("template:overnight", ["applied"]) is False
    assert should_defer_structured_option_to_llm("template:day_trip", ["applied"]) is False
    assert should_defer_structured_option_to_llm("return:same_day_afternoon", ["applied"]) is False
    assert should_defer_structured_option_to_llm(
        "北京到上海，template:overnight",
        ["applied"],
    )


def test_clock_only_answer_fills_arrive_by() -> None:
    ny = ZoneInfo("America/New_York")
    fields = {
        "origin": "New York",
        "destination": "Philadelphia",
        "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=ny),
        "arrive_by": None,
        "return_after": datetime(2026, 8, 26, 13, 0, tzinfo=ny),
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": [],
        "soft_preferences": [],
    }
    updated, notes, hints = apply_clarification_answer(
        fields,
        "18点",
        reference_time=FIXED,
        timezone_name="America/New_York",
    )
    assert not hints
    assert updated["arrive_by"] is not None
    assert updated["arrive_by"].hour == 18
    assert any("arrive_by" in item for item in notes)


def test_evening_chinese_clock_fills_arrive_by() -> None:
    ny = ZoneInfo("America/New_York")
    fields = {
        "origin": "New York",
        "destination": "Philadelphia",
        "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=ny),
        "arrive_by": None,
        "return_after": datetime(2026, 8, 26, 13, 0, tzinfo=ny),
        "return_before": datetime(2026, 8, 26, 22, 0, tzinfo=ny),
        "hotel_check_in": datetime(2026, 8, 25).date(),
        "hotel_check_out": datetime(2026, 8, 26).date(),
        "hard_constraints": [],
        "soft_preferences": [],
    }
    updated, notes, hints = apply_clarification_answer(
        fields,
        "晚上九点",
        reference_time=FIXED,
        timezone_name="America/New_York",
    )
    assert not hints
    assert updated["arrive_by"] is not None
    assert updated["arrive_by"].hour == 21
    assert updated["arrive_by"].date() == datetime(2026, 8, 25).date()
    assert any("arrive_by" in item for item in notes)


def test_arrive_only_bundle_uses_clock_options_not_trip_templates() -> None:
    bundle = build_clarification_bundle(missing=("arrive_by",))
    assert bundle is not None
    times = next(item for item in bundle.questions if item.id == "times")
    assert times.input_kind == "time_range"
    values = [option.value for option in times.options]
    assert "template:overnight" not in values
    assert "template:day_trip" not in values


def test_submit_evening_clock_applies_without_llm() -> None:
    workflow, _ = build_demo_system(clock=lambda: FIXED)
    employee = workflow.employees.snapshot("E1001")
    policy = workflow.policies.current()
    ny = ZoneInfo("America/New_York")
    bundle = build_clarification_bundle(missing=("arrive_by",))
    assert bundle is not None
    task = TripTask(
        task_id="clarify-evening-1",
        state=TaskState.NEEDS_CLARIFICATION,
        request=None,
        employee=employee,
        policy_snapshot_id=policy.snapshot_id,
        intent_fields={
            "origin": "New York",
            "destination": "Philadelphia",
            "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=ny),
            "arrive_by": None,
            "return_after": datetime(2026, 8, 26, 13, 0, tzinfo=ny),
            "return_before": datetime(2026, 8, 26, 22, 0, tzinfo=ny),
            "hotel_check_in": datetime(2026, 8, 25).date(),
            "hotel_check_out": datetime(2026, 8, 26).date(),
            "lodging_requirement": LodgingRequirement.REQUIRED.value,
            "hard_constraints": ["hotel_required"],
            "soft_preferences": ["hotel_near_client"],
        },
        missing_required_fields=("arrive_by",),
        clarification_question=bundle.prompt_text,
        messages=[
            ConversationMessage(role="user", content="下周二纽约到费城，周三回来"),
            ConversationMessage(role="assistant", content=bundle.prompt_text),
        ],
        metadata={
            "intent_classification": "TRIP",
            "clarification_questions": [item.as_dict() for item in bundle.questions],
        },
    )
    workflow.tasks.add(task)
    next_task = workflow.submit_message(task.task_id, "晚上九点")
    assert next_task.intent_fields.get("arrive_by") is not None
    assert next_task.intent_fields["arrive_by"].hour == 21
    assert "arrive_by" not in next_task.missing_required_fields


def test_submit_client_skip_does_not_block_search() -> None:
    workflow, _ = build_demo_system(clock=lambda: FIXED)
    employee = workflow.employees.snapshot("E1001")
    policy = workflow.policies.current()
    ny = ZoneInfo("America/New_York")
    bundle = build_clarification_bundle(uncertain=("client_location",))
    assert bundle is not None
    task = TripTask(
        task_id="clarify-client-1",
        state=TaskState.NEEDS_CLARIFICATION,
        request=None,
        employee=employee,
        policy_snapshot_id=policy.snapshot_id,
        intent_fields={
            "origin": "New York",
            "destination": "Philadelphia",
            "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=ny),
            "arrive_by": datetime(2026, 8, 25, 21, 0, tzinfo=ny),
            "return_after": datetime(2026, 8, 26, 13, 0, tzinfo=ny),
            "return_before": datetime(2026, 8, 26, 22, 0, tzinfo=ny),
            "hotel_check_in": datetime(2026, 8, 25).date(),
            "hotel_check_out": datetime(2026, 8, 26).date(),
            "lodging_requirement": LodgingRequirement.REQUIRED.value,
            "hard_constraints": ["hotel_required"],
            "soft_preferences": ["hotel_near_client"],
        },
        missing_required_fields=(),
        clarification_question=bundle.prompt_text,
        messages=[
            ConversationMessage(role="user", content="酒店离客户公司近一点"),
            ConversationMessage(role="assistant", content=bundle.prompt_text),
        ],
        metadata={
            "intent_classification": "TRIP",
            "clarification_questions": [item.as_dict() for item in bundle.questions],
        },
    )
    workflow.tasks.add(task)
    skipped = workflow.submit_message(task.task_id, "client:skip")
    assert skipped.intent_fields.get("client_location") == "SKIPPED"
    assert skipped.state is not TaskState.NEEDS_CLARIFICATION or (
        "client_location" not in (skipped.metadata.get("uncertain_slots") or [])
    )

    located_id = "clarify-client-2"
    located = TripTask(
        task_id=located_id,
        state=TaskState.NEEDS_CLARIFICATION,
        request=None,
        employee=employee,
        policy_snapshot_id=policy.snapshot_id,
        intent_fields=dict(task.intent_fields),
        missing_required_fields=(),
        clarification_question=bundle.prompt_text,
        messages=list(task.messages),
        metadata={
            "intent_classification": "TRIP",
            "clarification_questions": [item.as_dict() for item in bundle.questions],
        },
    )
    workflow.tasks.add(located)
    answered = workflow.submit_message(located_id, "客户公司在 Center City")
    assert answered.intent_fields.get("client_location") == "客户公司在 Center City"


def test_submit_overnight_option_applies_without_llm() -> None:
    """Clicking the time template must not wait on an LLM round-trip."""
    workflow, _ = build_demo_system(clock=lambda: FIXED)
    employee = workflow.employees.snapshot("E1001")
    policy = workflow.policies.current()
    ny = ZoneInfo("America/New_York")
    bundle = build_clarification_bundle(missing=("arrive_by",))
    assert bundle is not None
    task = TripTask(
        task_id="clarify-overnight-1",
        state=TaskState.NEEDS_CLARIFICATION,
        request=None,
        employee=employee,
        policy_snapshot_id=policy.snapshot_id,
        intent_fields={
            "origin": "New York",
            "destination": "Philadelphia",
            "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=ny),
            "arrive_by": None,
            "return_after": datetime(2026, 8, 26, 13, 0, tzinfo=ny),
            "return_before": datetime(2026, 8, 26, 22, 0, tzinfo=ny),
            "hotel_check_in": datetime(2026, 8, 25).date(),
            "hotel_check_out": datetime(2026, 8, 26).date(),
            "lodging_requirement": LodgingRequirement.REQUIRED.value,
            "hard_constraints": ["hotel_required"],
            "soft_preferences": ["hotel_near_client"],
        },
        missing_required_fields=("arrive_by",),
        clarification_question=bundle.prompt_text,
        messages=[
            ConversationMessage(role="user", content="下周二从纽约去费城，周三返回"),
            ConversationMessage(role="assistant", content=bundle.prompt_text),
        ],
        metadata={
            "intent_classification": "TRIP",
            "clarification_questions": [item.as_dict() for item in bundle.questions],
        },
    )
    workflow.tasks.add(task)
    next_task = workflow.submit_message(task.task_id, "template:overnight")
    assert next_task.intent_fields.get("arrive_by") is not None
    assert next_task.intent_fields["arrive_by"].hour == 18
    assert next_task.state is not TaskState.NEEDS_CLARIFICATION or (
        "arrive_by" not in next_task.missing_required_fields
    )


def test_city_clarification_uses_selectable_options() -> None:
    bundle = build_clarification_bundle(
        missing=("origin",),
        fields={"origin": None, "destination": "Beijing"},
    )
    assert bundle is not None
    cities = next(item for item in bundle.questions if item.id == "cities")
    values = [option.value for option in cities.options]
    assert "origin:Shanghai" in values
    assert "origin:Beijing" not in values
    assert all(option.value.startswith("origin:") for option in cities.options)

    updated, notes, _ = apply_clarification_answer(
        {"origin": None, "destination": "Beijing"},
        "origin:Shanghai",
        reference_time=FIXED,
    )
    assert updated["origin"] == "Shanghai"
    assert any("origin=Shanghai" in item for item in notes)


def test_time_clarification_uses_windows_not_free_text() -> None:
    bundle = build_clarification_bundle(missing=("departure_after", "arrive_by"))
    assert bundle is not None
    times = next(item for item in bundle.questions if item.id == "times")
    assert times.input_kind == "time_range"
    assert "template:day_trip" not in [option.value for option in times.options]


def test_submit_shanghai_fills_origin_without_llm() -> None:
    """自由文本「上海」在出发城市待补时应直接落地，不再让城市题刷新回来。"""
    from datetime import datetime as dt

    clock_time = dt(2026, 8, 21, 16, 0, tzinfo=SH)
    workflow, _ = build_demo_system(clock=lambda: clock_time)
    employee = workflow.employees.snapshot("E1001")
    policy = workflow.policies.current()
    bundle = build_clarification_bundle(
        missing=("origin", "departure_after", "arrive_by"),
        fields={"origin": None, "destination": "Beijing"},
    )
    assert bundle is not None
    task = TripTask(
        task_id="clarify-origin-shanghai",
        state=TaskState.NEEDS_CLARIFICATION,
        request=None,
        employee=employee,
        policy_snapshot_id=policy.snapshot_id,
        intent_fields={
            "origin": None,
            "destination": "Beijing",
            "departure_after": None,
            "arrive_by": None,
            "return_after": None,
            "return_before": None,
            "hotel_check_in": None,
            "hotel_check_out": None,
            "hard_constraints": [],
            "soft_preferences": [],
        },
        missing_required_fields=("origin", "departure_after", "arrive_by"),
        clarification_rounds=3,
        clarification_question=bundle.prompt_text,
        messages=[
            ConversationMessage(role="user", content="2.31回公司，下周三去北京"),
            ConversationMessage(role="assistant", content=bundle.prompt_text),
        ],
        metadata={
            "intent_classification": "TRIP",
            "clarification_questions": [item.as_dict() for item in bundle.questions],
            "clarification_pending_slots": ["origin", "departure_after", "arrive_by"],
        },
    )
    workflow.tasks.add(task)
    next_task = workflow.submit_message(task.task_id, "上海")
    assert next_task.intent_fields["origin"] == "Shanghai"
    assert "origin" not in next_task.missing_required_fields
    question_ids = [
        item.get("id")
        for item in (next_task.metadata.get("clarification_questions") or [])
        if isinstance(item, dict)
    ]
    assert "cities" not in question_ids
    assert next_task.state is not TaskState.NEEDS_STRUCTURED_INPUT


def test_time_window_keeps_next_wednesday_from_original_message() -> None:
    fields = {
        "origin": "Shanghai",
        "destination": "Beijing",
        "departure_after": None,
        "arrive_by": None,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": [],
        "soft_preferences": [],
    }
    from datetime import datetime as dt

    now = dt(2026, 8, 21, 16, 0, tzinfo=SH)
    updated, notes, _ = apply_clarification_answer(
        fields,
        "window:08:00-18:00",
        reference_time=now,
        timezone_name="Asia/Shanghai",
        context_message="2.31回公司，下周三去北京",
    )
    assert updated["departure_after"].date().isoformat() == "2026-08-26"
    assert updated["departure_after"].hour == 8
    assert updated["arrive_by"].hour == 18
    assert updated["arrive_by"].date().isoformat() == "2026-08-26"
    assert notes


def test_invalid_date_and_return_from_beijing() -> None:
    from corporate_travel_agent.agent.intent_calibration import (
        apply_return_from_city_semantics,
        iter_invalid_date_tokens,
        message_is_return_leg_only,
        search_ready_missing,
    )
    from corporate_travel_agent.agent.clarification_questions import detect_uncertain_slots
    from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

    message = "2.31从北京回来"
    assert "2.31" in iter_invalid_date_tokens(message)
    assert message_is_return_leg_only(message)
    from corporate_travel_agent.agent.clarification_questions import lookup_clarification_city as lookup_city

    origin, destination = GroundedLocalIntentParser()._cities(message)
    assert origin is None
    assert lookup_city(destination or "") == "Beijing"

    updated, notes, _ = apply_return_from_city_semantics(
        {"origin": "Beijing", "destination": "Beijing"},
        user_message=message,
        provenance={"origin": "model", "destination": "model"},
    )
    assert updated["origin"] is None
    assert updated["destination"] == "Beijing"
    assert notes

    uncertain = detect_uncertain_slots(
        updated,
        user_message=message,
        assumptions=(),
    )
    assert "return_trip" not in uncertain
    assert "travel_date" in uncertain

    ready = search_ready_missing(updated, classification="TRIP", user_message=message)
    assert "origin" in ready.missing
    assert "return_after" in ready.missing
    bundle = build_clarification_bundle(
        missing=ready.missing,
        uncertain=uncertain,
        conflicts=("2.31 不是有效公历日期",),
        fields=updated,
        user_message=message,
    )
    assert bundle is not None
    ids = [item.id for item in bundle.questions]
    assert "return_trip" not in ids
    assert "cities" in ids
    times = next(item for item in bundle.questions if item.input_kind == "time_range")
    assert times.id == "return_times"
    assert "只要去程" not in times.question
    assert "2.31" in times.question


def test_iso_return_window_applies() -> None:
    fields = {
        "origin": "Shanghai",
        "destination": "Beijing",
        "departure_after": None,
        "arrive_by": None,
        "return_after": None,
        "return_before": None,
        "hard_constraints": [],
        "soft_preferences": [],
    }
    updated, notes, hints = apply_clarification_answer(
        fields,
        "return_window:2026-09-01T13:00/2026-09-01T18:00",
        reference_time=FIXED,
        timezone_name="Asia/Shanghai",
    )
    assert not hints
    assert updated["return_after"].hour == 13
    assert updated["return_before"].hour == 18
    assert updated["return_after"].date().isoformat() == "2026-09-01"
    assert notes


from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from corporate_travel_agent.agent.orchestrator import (
    TripWorkflowOrchestrator,
    WorkflowError,
)
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    ApprovalStatus,
    PolicyOutcome,
    TaskState,
    ToolCallStatus,
    TransportMode,
)
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    LevelTravelRule,
    PolicySnapshot,
    ToolCallRecord,
    TransportOffer,
    TripRequestVersion,
)
from corporate_travel_agent.domain.validation import TripRequestValidationError
from corporate_travel_agent.planning.feasibility import FeasibilityValidator
from corporate_travel_agent.policy.engine import PolicyEngine
from corporate_travel_agent.providers.base import ProviderError, RetryableProviderError
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    InMemoryTaskRepository,
)

FIXED_NOW = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)


def _policy(*, currency: str = "CNY") -> PolicySnapshot:
    return PolicySnapshot(
        snapshot_id="edge-policy-v1",
        policy_version="edge-v1",
        level_rules={
            "L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",)),
            "L2": LevelTravelRule(("ECONOMY", "BUSINESS"), ("SECOND_CLASS",)),
        },
        hotel_city_caps={"Shanghai": Decimal("600")},
        arrival_buffer_minutes=60,
        exception_allowed_rule_ids=frozenset(),
        effective_from=date(2026, 1, 1),
        currency=currency,
    )


def _employee(level: str = "L1") -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id=f"employee-{level}-v1",
        employee_id=f"EDGE-{level}",
        level=level,
        department="Evaluation",
        home_city="Beijing",
        manager_id="EDGE-MANAGER",
    )


def _offer(
    ref_id: str,
    *,
    mode: TransportMode = TransportMode.FLIGHT,
    seat_class: str = "ECONOMY",
    price: str = "500",
    currency: str = "CNY",
    is_direct: bool = True,
    depart_at: datetime | None = None,
    arrive_at: datetime | None = None,
) -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="edge-catalog",
        provider="edge-mock",
        mode=mode,
        origin="Beijing",
        destination="Shanghai",
        depart_at=depart_at or datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
        arrive_at=arrive_at or datetime(2026, 8, 5, 4, 0, tzinfo=UTC),
        price=Decimal(price),
        seat_class=seat_class,
        currency=currency,
        is_direct=is_direct,
    )


def test_structured_hotel_requirement_cannot_silently_drop_the_hotel() -> None:
    workflow, _ = build_demo_system(clock=lambda: FIXED_NOW)
    request = replace(
        make_demo_request(task_id="edge-hotel-required"),
        hotel_check_in=None,
        hotel_check_out=None,
        hard_constraints=("hotel_required",),
    )

    with pytest.raises(TripRequestValidationError, match="hotel_check_in"):
        workflow.create_task(request)


def test_train_and_direct_only_are_enforced_by_the_real_workflow() -> None:
    employee = _employee()
    policy = _policy()
    offers = [
        _offer(
            "TRAIN-CONNECTING",
            mode=TransportMode.TRAIN,
            seat_class="SECOND_CLASS",
            price="200",
            is_direct=False,
        ),
        _offer(
            "TRAIN-DIRECT",
            mode=TransportMode.TRAIN,
            seat_class="SECOND_CLASS",
            price="300",
        ),
        _offer("FLIGHT-DIRECT", mode=TransportMode.FLIGHT, price="100"),
    ]
    workflow = TripWorkflowOrchestrator(
        tasks=InMemoryTaskRepository(),
        employees=InMemoryEmployeeDirectory([employee]),
        policies=InMemoryPolicyRepository(policy),
        provider=MockProvider(offers, [], clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )
    request = TripRequestVersion(
        task_id="edge-train-direct",
        version=1,
        traveler_id=employee.employee_id,
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
        arrive_by=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
        hard_constraints=("train_only", "direct_only"),
    )

    task = workflow.create_task(request)

    assert task.state is TaskState.WAITING_FOR_USER
    assert {option.outbound.ref_id for option in task.options} == {"TRAIN-DIRECT"}


def test_prefer_flight_is_not_silently_ignored() -> None:
    employee = _employee()
    policy = _policy()
    offers = [
        _offer("TRAIN", mode=TransportMode.TRAIN, seat_class="SECOND_CLASS"),
        _offer("FLIGHT"),
    ]
    workflow = TripWorkflowOrchestrator(
        tasks=InMemoryTaskRepository(),
        employees=InMemoryEmployeeDirectory([employee]),
        policies=InMemoryPolicyRepository(policy),
        provider=MockProvider(offers, [], clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )
    request = TripRequestVersion(
        task_id="edge-prefer-flight",
        version=1,
        traveler_id=employee.employee_id,
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
        arrive_by=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
        soft_preferences=("prefer_flight",),
    )

    task = workflow.create_task(request)

    assert task.options[0].outbound.ref_id == "FLIGHT"
    assert task.options[0].preference_penalty < task.options[1].preference_penalty


def test_multiple_levels_forbidden_and_insufficient_evidence_are_distinct() -> None:
    policy = _policy()
    business = _offer("BUSINESS", seat_class="BUSINESS")
    engine = PolicyEngine()

    forbidden = engine.evaluate(_employee("L1"), policy, [business], None)
    compliant = engine.evaluate(_employee("L2"), policy, [business], None)
    unknown = engine.evaluate(_employee("L9"), policy, [business], None)

    assert forbidden.outcome is PolicyOutcome.FORBIDDEN
    assert compliant.outcome is PolicyOutcome.COMPLIANT
    assert unknown.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE


def test_mixed_currency_fails_closed_without_an_fx_snapshot() -> None:
    decision = PolicyEngine().evaluate(
        _employee("L1"),
        _policy(currency="CNY"),
        [_offer("USD-FLIGHT", currency="USD")],
        None,
    )

    assert decision.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE
    currency_evidence = next(
        item for item in decision.evidence if item.rule_id == "pricing.currency"
    )
    assert currency_evidence.actual == "USD"
    assert currency_evidence.threshold == "CNY"


def test_no_feasible_reports_mixed_inventory_currency_gap() -> None:
    """Flight USD + hotel CNY under USD policy surfaces an explicit currency reason."""
    from corporate_travel_agent.domain.models import HotelOffer

    transports = [
        TransportOffer(
            ref_id="FL-USD",
            snapshot_id="snap-t",
            provider="mock",
            mode=TransportMode.FLIGHT,
            origin="New York",
            destination="Philadelphia",
            depart_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
            arrive_at=datetime(2026, 8, 20, 11, 0, tzinfo=UTC),
            price=Decimal("100"),
            seat_class="ECONOMY",
            currency="USD",
        )
    ]
    hotels = [
        HotelOffer(
            ref_id="HT-CNY",
            snapshot_id="snap-h",
            provider="mock",
            name="PHL Hotel",
            city="Philadelphia",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
            nightly_price=Decimal("500"),
            commute_minutes=20,
            currency="CNY",
        )
    ]
    provider = MockProvider(transports, hotels, clock=lambda: FIXED_NOW)
    workflow, _ = build_demo_system(provider=provider, clock=lambda: FIXED_NOW)
    # Override policy currency to USD with PHL cap so only currency blocks.
    policy = replace(
        workflow.policies.current(),
        currency="USD",
        hotel_city_caps={"Philadelphia": Decimal("800")},
    )
    workflow.policies = InMemoryPolicyRepository((policy,), current_snapshot_id=policy.snapshot_id)

    request = TripRequestVersion(
        task_id="trip-mixed-fx",
        version=1,
        traveler_id="E1001",
        origin="New York",
        destination="Philadelphia",
        departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
        arrive_by=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        return_after=None,
        return_before=None,
        hotel_check_in=date(2026, 8, 20),
        hotel_check_out=date(2026, 8, 21),
        hard_constraints=("hotel_required",),
        soft_preferences=(),
    )
    task = workflow.create_task(request)
    assert task.state is TaskState.NO_FEASIBLE_OPTION
    joined = " ".join(task.metadata.get("no_feasible_reasons") or ())
    assert "CNY" in joined and "USD" in joined
    assert "FX snapshot" in joined or "pricing.currency" in joined


def test_policy_outside_effective_window_is_insufficient_evidence() -> None:
    expired = replace(_policy(), effective_to=date(2026, 8, 4))
    still_valid = replace(_policy(), effective_from=date(2026, 8, 5), effective_to=None)
    engine = PolicyEngine()
    segment = _offer("FLIGHT-WINDOW")

    expired_decision = engine.evaluate(_employee("L1"), expired, [segment], None)
    valid_decision = engine.evaluate(_employee("L1"), still_valid, [segment], None)

    assert expired_decision.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE
    window_evidence = next(
        item for item in expired_decision.evidence if item.rule_id == "policy.effective_window"
    )
    assert window_evidence.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE
    assert valid_decision.outcome is PolicyOutcome.COMPLIANT


def test_cross_timezone_feasibility_compares_absolute_instants() -> None:
    request = TripRequestVersion(
        task_id="edge-timezones",
        version=1,
        traveler_id="EDGE-L1",
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime.fromisoformat("2026-08-05T08:00:00+08:00"),
        arrive_by=datetime.fromisoformat("2026-08-05T14:00:00+08:00"),
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
    )
    offer = _offer(
        "UTC-FLIGHT",
        depart_at=datetime.fromisoformat("2026-08-05T01:00:00+00:00"),
        arrive_at=datetime.fromisoformat("2026-08-05T04:00:00+00:00"),
    )

    result = FeasibilityValidator().validate(
        request, offer, None, None, 60, now=datetime.fromisoformat("2026-08-05T00:00:00+00:00")
    )

    assert result.feasible


def test_arrival_buffer_only_applies_to_meeting_constraint() -> None:
    arrive_by = datetime.fromisoformat("2026-08-20T10:00:00-07:00")
    base_request = TripRequestVersion(
        task_id="edge-arrival-deadline",
        version=1,
        traveler_id="EDGE-L1",
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime.fromisoformat("2026-08-20T13:00:00+08:00"),
        arrive_by=arrive_by,
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
    )
    offer = _offer(
        "PACIFIC-FLIGHT",
        depart_at=datetime.fromisoformat("2026-08-20T14:00:00+08:00"),
        arrive_at=arrive_by,
    )
    validator = FeasibilityValidator()

    pacific_now = datetime.fromisoformat("2026-08-20T00:00:00+08:00")
    plain_arrival = validator.validate(
        base_request, offer, None, None, 60, now=pacific_now
    )
    meeting_arrival = validator.validate(
        replace(base_request, hard_constraints=("arrive_before_meeting",)),
        offer,
        None,
        None,
        60,
        now=pacific_now,
    )

    assert plain_arrival.feasible is True
    assert meeting_arrival.feasible is False
    assert meeting_arrival.reasons == (
        "outbound cannot meet arrival and safety-buffer requirement",
    )


@pytest.mark.parametrize("approved", [True, False])
def test_approval_decisions_have_complete_terminal_paths(approved: bool) -> None:
    workflow, _ = build_demo_system(clock=lambda: FIXED_NOW)
    task = workflow.create_task(make_demo_request(task_id=f"edge-approval-{approved}"))
    option = next(
        item
        for item in task.options
        if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    )
    task = workflow.select_option(
        task.task_id,
        option.option_id,
        business_reason="Customer meeting location requires the exception",
    )

    task = workflow.decide_approval(
        task.task_id,
        approver_id=task.employee.manager_id,
        approved=approved,
        reason="Reviewed",
    )

    assert task.state is (TaskState.READY_FOR_HANDOFF if approved else TaskState.WAITING_FOR_USER)
    assert (task.booking_intent is not None) is approved


def test_expired_approval_returns_to_a_recoverable_user_state() -> None:
    now = [FIXED_NOW]
    workflow, _ = build_demo_system(clock=lambda: now[0])
    task = workflow.create_task(make_demo_request(task_id="edge-expired-approval"))
    option = next(
        item
        for item in task.options
        if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    )
    task = workflow.select_option(
        task.task_id,
        option.option_id,
        business_reason="Business need",
    )
    now[0] += timedelta(hours=25)

    with pytest.raises(WorkflowError, match="expired"):
        workflow.decide_approval(
            task.task_id,
            approver_id=task.employee.manager_id,
            approved=True,
            reason="Too late",
        )

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.approval is not None
    assert task.approval.status is ApprovalStatus.INVALIDATED
    assert task.selected_option_id is None


def test_provider_error_during_revalidation_becomes_a_retryable_failure() -> None:
    workflow, provider = build_demo_system(clock=lambda: FIXED_NOW)
    task = workflow.create_task(make_demo_request(task_id="edge-provider-revalidation"))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )

    def fail_revalidation(refs: tuple[str, ...]):
        _ = refs
        raise ProviderError("revalidation network failure")

    provider.revalidate = fail_revalidation
    task = workflow.select_option(task.task_id, option.option_id)

    assert task.state is TaskState.PROVIDER_FAILED
    assert task.failure == "revalidation network failure"
    assert task.booking_intent is None


def test_started_external_call_is_recovered_after_restart() -> None:
    workflow, provider = build_demo_system(clock=lambda: FIXED_NOW)
    task = workflow.create_task(make_demo_request(task_id="edge-interrupted-call"))
    task.state = TaskState.REVALIDATING
    task.tool_calls.append(
        ToolCallRecord(
            sequence=task.tool_calls_used + 1,
            tool_name="provider.revalidate",
            tool_kind="PROVIDER",
            status=ToolCallStatus.STARTED,
            started_at=FIXED_NOW,
        )
    )

    recovered_workflow = TripWorkflowOrchestrator(
        tasks=workflow.tasks,
        employees=workflow.employees,
        policies=workflow.policies,
        provider=provider,
        clock=lambda: FIXED_NOW + timedelta(minutes=1),
    )
    recovered = recovered_workflow.tasks.get(task.task_id)

    assert recovered.state is TaskState.PROVIDER_FAILED
    assert recovered.tool_calls[-1].status is ToolCallStatus.FAILED
    assert recovered.tool_calls[-1].error_type == "InterruptedToolCall"


@pytest.mark.parametrize(
    "interrupted_state",
    [TaskState.SEARCHING, TaskState.PLANNING, TaskState.REVALIDATING],
)
def test_successful_call_before_state_snapshot_is_recovered_after_restart(
    interrupted_state: TaskState,
) -> None:
    workflow, provider = build_demo_system(clock=lambda: FIXED_NOW)
    task = workflow.create_task(make_demo_request(task_id=f"edge-{interrupted_state.value}"))
    assert task.tool_calls
    assert all(record.status is not ToolCallStatus.STARTED for record in task.tool_calls)
    task.state = interrupted_state

    recovered_workflow = TripWorkflowOrchestrator(
        tasks=workflow.tasks,
        employees=workflow.employees,
        policies=workflow.policies,
        provider=provider,
        clock=lambda: FIXED_NOW + timedelta(minutes=1),
    )
    recovered = recovered_workflow.tasks.get(task.task_id)

    assert recovered.state is TaskState.PROVIDER_FAILED
    assert recovered.booking_intent is None
    assert recovered.metadata["last_recovery"]["reason"] == (
        "process_restart_incomplete_state_transition"
    )
    assert recovered.metadata["last_recovery"]["successful_legs"]


def test_recent_transient_task_is_not_stolen_from_another_worker() -> None:
    workflow, provider = build_demo_system(clock=lambda: FIXED_NOW)
    task = workflow.create_task(make_demo_request(task_id="edge-recent-transient"))
    task.state = TaskState.SEARCHING

    recovered_workflow = TripWorkflowOrchestrator(
        tasks=workflow.tasks,
        employees=workflow.employees,
        policies=workflow.policies,
        provider=provider,
        clock=lambda: FIXED_NOW,
    )

    assert recovered_workflow.tasks.get(task.task_id).state is TaskState.SEARCHING


def test_provider_bulkhead_fails_with_a_bounded_audited_error() -> None:
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        max_provider_attempts=1,
        max_concurrent_provider_calls=1,
        tool_acquire_timeout_seconds=0,
    )
    task = workflow.create_task(make_demo_request(task_id="edge-provider-bulkhead"))
    workflow._provider_slots.acquire()
    try:
        with pytest.raises(RetryableProviderError, match="concurrency limit"):
            workflow._invoke_tool(
                task,
                tool_name="provider.search_transport.outbound",
                tool_kind="PROVIDER",
                input_value={"test": "bulkhead"},
                operation=lambda: pytest.fail("operation must not run without a slot"),
            )
    finally:
        workflow._provider_slots.release()

    assert task.tool_calls[-1].status is ToolCallStatus.FAILED
    assert task.tool_calls[-1].error_code == "PROVIDER_BULKHEAD_FULL"

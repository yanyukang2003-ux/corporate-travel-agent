from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config

from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    PolicyOutcome,
    TaskState,
    ToolCallStatus,
)
from corporate_travel_agent.domain.models import ToolCallRecord
from corporate_travel_agent.services.audit import new_audit_event
from corporate_travel_agent.services.repositories import (
    ConcurrentUpdateError,
    SnapshotConflictError,
)
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository

FIXED_NOW = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)


def database_url(tmp_path, name: str = "travel-agent.db") -> str:
    return f"sqlite+pysqlite:///{tmp_path / name}"


def new_repository(tmp_path, name: str = "travel-agent.db") -> SQLAlchemyTaskRepository:
    repository = SQLAlchemyTaskRepository(database_url(tmp_path, name))
    repository.create_schema()
    return repository


def test_task_events_and_snapshots_survive_repository_restart(tmp_path) -> None:
    repository = new_repository(tmp_path)
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=repository,
    )
    task = workflow.create_task(make_demo_request(task_id="persistent-trip"))
    event_count = len(repository.events(task.task_id))

    assert task.state is TaskState.WAITING_FOR_USER
    assert len(repository.snapshots(task.task_id)) == 3
    repository.dispose()

    restarted_repository = SQLAlchemyTaskRepository(database_url(tmp_path))
    restarted_workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=restarted_repository,
    )
    restored = restarted_repository.get(task.task_id)
    assert task.task_id in {
        item.task_id for item in restarted_repository.list_tasks()
    }
    option = next(
        item
        for item in restored.options
        if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )

    assert isinstance(restored.request.departure_after, datetime)
    assert isinstance(restored.options[0].total_cost, Decimal)
    assert len(restarted_repository.events(task.task_id)) == event_count
    restored_snapshots = restarted_repository.snapshots(task.task_id)
    assert len(restored_snapshots) == 3
    assert all(item.raw_response is not None for item in restored_snapshots)
    assert all(
        item.raw_payload_hash == item.raw_response.sha256
        for item in restored_snapshots
        if item.raw_response
    )

    restored = restarted_workflow.select_option(restored.task_id, option.option_id)
    assert restored.state is TaskState.READY_FOR_HANDOFF
    assert restored.booking_intent is not None
    restarted_repository.dispose()

    final_repository = SQLAlchemyTaskRepository(database_url(tmp_path))
    final = final_repository.get(task.task_id)
    assert final.state is TaskState.READY_FOR_HANDOFF
    assert final.booking_intent.idempotency_key == restored.booking_intent.idempotency_key
    assert len(final_repository.events(task.task_id)) > event_count
    final_repository.dispose()


def test_booking_confirmation_survives_a_restart(tmp_path) -> None:
    """回填的订单号、金额（Decimal）、来源和差额，重启之后一个都不能少。"""
    from corporate_travel_agent.domain.enums import BookingConfirmationSource

    repository = new_repository(tmp_path, "confirmation.db")
    workflow, _ = build_demo_system(clock=lambda: FIXED_NOW, task_repository=repository)
    task = workflow.create_task(make_demo_request(task_id="persistent-confirmation"))
    option = next(
        item
        for item in task.options
        if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(task.task_id, option.option_id)
    workflow.confirm_booking(
        task.task_id,
        order_references=["PNR-77", "HTL-3"],
        total_amount=option.total_cost + Decimal("5.25"),
        currency=option.currency,
        reported_by="E1001",
        note="前台升了房型",
    )
    repository.dispose()

    restarted = SQLAlchemyTaskRepository(database_url(tmp_path, "confirmation.db"))
    restored = restarted.get(task.task_id)
    assert restored.state is TaskState.BOOKING_CONFIRMED
    confirmation = restored.booking_confirmation
    assert confirmation is not None
    assert confirmation.order_references == ("PNR-77", "HTL-3")
    assert isinstance(confirmation.total_amount, Decimal)
    assert confirmation.total_amount == option.total_cost + Decimal("5.25")
    assert confirmation.source is BookingConfirmationSource.SELF_REPORTED
    assert isinstance(confirmation.booked_at, datetime)
    assert confirmation.booked_at.tzinfo is not None
    assert confirmation.note == "前台升了房型"
    assert restored.booking_cost_variance() == Decimal("5.25")
    event_types = [item.event_type for item in restarted.events(task.task_id)]
    assert event_types.count("HANDOFF_COMPLETED") == 1
    assert event_types.count("BOOKING_CONFIRMED") == 1
    assert task.task_id in {
        item.task_id for item in restarted.list_by_state(TaskState.BOOKING_CONFIRMED.value)
    }
    restarted.dispose()


def test_intent_any_fields_restore_date_and_datetime_types(tmp_path) -> None:
    repository = new_repository(tmp_path, "intent.db")
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=repository,
    )
    employee = workflow.employees.snapshot("E1001")
    policy = workflow.policies.current()
    from corporate_travel_agent.domain.models import TripTask

    task = TripTask(
        task_id="persistent-intent",
        state=TaskState.NEEDS_CLARIFICATION,
        request=None,
        employee=employee,
        policy_snapshot_id=policy.snapshot_id,
        intent_fields={
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": datetime(2026, 8, 5, 5, tzinfo=UTC),
            "arrive_by": datetime(2026, 8, 6, 2, tzinfo=UTC),
            "return_after": None,
            "return_before": None,
            "hotel_check_in": date(2026, 8, 5),
            "hotel_check_out": date(2026, 8, 6),
            "hard_constraints": [],
            "soft_preferences": [],
        },
    )
    repository.add(task)
    repository.record(
        task,
        new_audit_event(
            task.task_id,
            "PERSISTENCE_TEST",
            input_value="draft",
            output_value="stored",
        ),
    )
    repository.dispose()

    restored_repository = SQLAlchemyTaskRepository(database_url(tmp_path, "intent.db"))
    restored = restored_repository.get(task.task_id)

    assert isinstance(restored.intent_fields["departure_after"], datetime)
    assert isinstance(restored.intent_fields["arrive_by"], datetime)
    assert isinstance(restored.intent_fields["hotel_check_in"], date)
    assert isinstance(restored.intent_fields["hotel_check_out"], date)
    restored_repository.dispose()


def test_interrupted_provider_call_is_recovered_after_repository_restart(tmp_path) -> None:
    repository = new_repository(tmp_path, "interrupted.db")
    workflow, _ = build_demo_system(clock=lambda: FIXED_NOW, task_repository=repository)
    task = workflow.create_task(make_demo_request(task_id="persistent-interrupted"))
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
    repository.record(
        task,
        new_audit_event(
            task.task_id,
            "SIMULATED_PROCESS_INTERRUPTION",
            input_value="provider.revalidate",
            output_value="process stopped",
        ),
    )
    repository.dispose()

    restarted_repository = SQLAlchemyTaskRepository(
        database_url(tmp_path, "interrupted.db")
    )
    build_demo_system(
        clock=lambda: FIXED_NOW + timedelta(minutes=1),
        task_repository=restarted_repository,
    )
    recovered = restarted_repository.get(task.task_id)

    assert recovered.state is TaskState.PROVIDER_FAILED
    assert recovered.tool_calls[-1].status is ToolCallStatus.FAILED
    assert recovered.tool_calls[-1].error_type == "InterruptedToolCall"
    assert "restart" in recovered.failure
    restarted_repository.dispose()


def test_delayed_provider_retry_survives_repository_restart(tmp_path) -> None:
    now = [FIXED_NOW]
    repository = new_repository(tmp_path, "delayed-retry.db")
    workflow, provider = build_demo_system(
        clock=lambda: now[0],
        task_repository=repository,
        retry_sleep=lambda _: None,
    )
    provider.fail_search_remaining = 3
    task = workflow.create_task(make_demo_request(task_id="persistent-delayed-retry"))

    assert task.state is TaskState.WAITING_FOR_PROVIDER
    assert task.metadata["provider_retry"]["delayed_attempts_completed"] == 0
    repository.dispose()

    now[0] += timedelta(seconds=61)
    restarted_repository = SQLAlchemyTaskRepository(
        database_url(tmp_path, "delayed-retry.db")
    )
    restarted_workflow, _ = build_demo_system(
        clock=lambda: now[0],
        task_repository=restarted_repository,
        retry_sleep=lambda _: None,
    )

    assert restarted_workflow.process_due_provider_retries() == (task.task_id,)
    recovered = restarted_repository.get(task.task_id)
    assert recovered.state is TaskState.WAITING_FOR_USER
    assert recovered.metadata["provider_retry"]["status"] == "recovered"
    assert recovered.metadata["provider_retry"]["delayed_attempts_completed"] == 1
    restarted_repository.dispose()


def test_provider_retry_claim_lease_and_fencing_reject_stale_worker(tmp_path) -> None:
    now = [FIXED_NOW]
    repository = new_repository(tmp_path, "retry-lease.db")
    workflow, provider = build_demo_system(
        clock=lambda: now[0],
        task_repository=repository,
        retry_sleep=lambda _: None,
        max_tool_calls=20,
    )
    provider.fail_search_remaining = 99
    task = workflow.create_task(make_demo_request(task_id="leased-provider-retry"))
    assert task.state is TaskState.WAITING_FOR_PROVIDER

    now[0] += timedelta(seconds=61)
    first = repository.claim_due_provider_retries(
        worker_id="worker-a",
        now=now[0],
        lease_duration=timedelta(seconds=120),
    )
    assert len(first) == 1
    first_token = first[0].metadata["provider_retry"]["attempt_token"]
    first[0].state = TaskState.SEARCHING
    first[0].metadata["provider_retry"].update(
        {"status": "running", "next_retry_at": None}
    )
    repository.record(
        first[0],
        new_audit_event(
            task.task_id,
            "PROVIDER_ATTEMPT_INFLIGHT",
            input_value=first_token,
            output_value="worker-a",
        ),
    )
    assert repository.claim_due_provider_retries(
        worker_id="worker-b",
        now=now[0],
        lease_duration=timedelta(seconds=120),
    ) == ()

    now[0] += timedelta(seconds=121)
    second = repository.claim_due_provider_retries(
        worker_id="worker-b",
        now=now[0],
        lease_duration=timedelta(seconds=120),
    )
    assert len(second) == 1
    second_token = second[0].metadata["provider_retry"]["attempt_token"]
    assert second_token != first_token

    with pytest.raises(ConcurrentUpdateError):
        repository.record(
            first[0],
            new_audit_event(
                task.task_id,
                "STALE_PROVIDER_RESULT",
                input_value=first_token,
                output_value="must be fenced",
            ),
        )
    assert repository.release_provider_retry_claim(
        second[0], attempt_token=second_token
    )
    repository.dispose()


def test_optimistic_revision_rejects_stale_task_updates(tmp_path) -> None:
    repository = new_repository(tmp_path, "concurrency.db")
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=repository,
    )
    created = workflow.create_task(make_demo_request(task_id="concurrent-trip"))
    first = repository.get(created.task_id)
    stale = repository.get(created.task_id)

    first.metadata["writer"] = "first"
    repository.record(
        first,
        new_audit_event(
            first.task_id,
            "FIRST_WRITE",
            input_value="first",
            output_value="saved",
        ),
    )
    stale.metadata["writer"] = "stale"
    stale_revision = stale.persistence_revision

    with pytest.raises(ConcurrentUpdateError):
        repository.record(
            stale,
            new_audit_event(
                stale.task_id,
                "STALE_WRITE",
                input_value="stale",
                output_value="rejected",
            ),
        )

    assert stale.persistence_revision == stale_revision
    assert repository.get(created.task_id).metadata["writer"] == "first"
    repository.dispose()


def test_snapshot_rows_are_immutable_and_migration_is_runnable(tmp_path, monkeypatch) -> None:
    migration_url = database_url(tmp_path, "migration.db")
    monkeypatch.setenv("DATABASE_URL", migration_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    repository = SQLAlchemyTaskRepository(migration_url)
    repository.check_schema()
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=repository,
    )
    task = workflow.create_task(make_demo_request(task_id="immutable-snapshot"))
    snapshot = repository.snapshots(task.task_id)[0]

    repository.add_snapshot(task.task_id, snapshot)
    changed = replace(snapshot, raw_payload_hash="f" * 64)
    with pytest.raises(SnapshotConflictError):
        repository.add_snapshot(task.task_id, changed)
    repository.dispose()

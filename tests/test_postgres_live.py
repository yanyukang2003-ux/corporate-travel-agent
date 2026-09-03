"""只有真 Postgres 才有的行为：`SKIP LOCKED` 争抢、JSONB 载荷、联合写入的版本一致、旧载荷升级。

`TEST_DATABASE_URL` 没设就整文件跳过；CI 的 postgres-smoke 作业设了它。**这个库会被清空重建**，
只能指向一次性的测试库。SQLite 上跑的那些测试抓不到这里的事：2026-09-02 真链路实测的
"Trip … was updated concurrently" 就是 SQLite 单测漏掉、Postgres 上才撞出来的。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Thread

import pytest
from sqlalchemy import text

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, TripEventType, TripStatus
from corporate_travel_agent.services.repositories import ConcurrentUpdateError
from corporate_travel_agent.services.serialization import SCHEMA_VERSION

URL = os.getenv("TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not URL.startswith("postgresql"), reason="TEST_DATABASE_URL (postgresql+psycopg://...) not set"
)


@pytest.fixture
def system():
    from corporate_travel_agent.services.sqlalchemy_repository import (
        Base,
        SQLAlchemyTaskRepository,
    )
    from corporate_travel_agent.services.trips import SQLAlchemyTripRepository

    repository = SQLAlchemyTaskRepository(URL)
    Base.metadata.drop_all(repository.engine)
    Base.metadata.create_all(repository.engine)
    trips = SQLAlchemyTripRepository(repository.engine)
    workflow, _ = build_demo_system(
        clock=lambda: DEMO_CLOCK, task_repository=repository, trip_repository=trips
    )
    try:
        yield workflow, repository, trips
    finally:
        repository.dispose()


def _booked(workflow, task_id: str):
    task = workflow.create_task(make_demo_request(task_id=task_id))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(task.task_id, option.option_id)
    return workflow.confirm_booking(
        task.task_id,
        order_references=[f"PNR-{task_id}"],
        total_amount=option.total_cost,
        currency=option.currency,
        reported_by="E1001",
    ), option


def test_dialect_is_postgres(system) -> None:
    _, repository, _ = system
    assert repository.engine.dialect.name == "postgresql"


def test_two_workers_claim_disjoint_trips_with_skip_locked(system) -> None:
    workflow, _, trips = system
    due = DEMO_CLOCK
    for index in range(8):
        task, _ = _booked(workflow, f"pg-claim-{index}")
        trip = trips.get(task.trip_id)
        trip.watch = replace(trip.watch, next_check_at=due)
        trips.save(trip)

    barrier = Barrier(2)
    claimed: dict[str, list[str]] = {"a": [], "b": []}

    def worker(name: str) -> None:
        barrier.wait()
        for _ in range(3):
            batch = trips.claim_due_flight_checks(
                worker_id=name, now=due, lease_duration=timedelta(minutes=5), limit=2
            )
            claimed[name].extend(item.trip_id for item in batch)

    threads = [Thread(target=worker, args=("a",)), Thread(target=worker, args=("b",))]
    for item in threads:
        item.start()
    for item in threads:
        item.join()
    seen_a, seen_b = set(claimed["a"]), set(claimed["b"])
    assert seen_a.isdisjoint(seen_b)
    assert len(seen_a | seen_b) == 8
    assert len(claimed["a"]) + len(claimed["b"]) == 8  # 没有一趟被领两次
    # 租约在：再领一轮什么都没有；保存释放后又能领。
    assert trips.claim_due_flight_checks(
        worker_id="c", now=due, lease_duration=timedelta(minutes=5)
    ) == ()
    trips.save(trips.get(sorted(seen_a | seen_b)[0]))
    assert len(
        trips.claim_due_flight_checks(worker_id="c", now=due, lease_duration=timedelta(minutes=5))
    ) == 1


def test_joint_write_keeps_the_revision_column_and_payload_in_step(system) -> None:
    workflow, repository, trips = system
    task, _ = _booked(workflow, "pg-rebook")
    change = workflow.report_trip_event(
        task.trip_id,
        event_type=TripEventType.MEETING_MOVED,
        new_arrive_by=task.request.arrive_by + timedelta(hours=6),
        reported_by="E1001",
    )
    with repository.engine.connect() as connection:
        column, payload = connection.execute(
            text(
                "SELECT revision, (payload->'trip'->>'persistence_revision')::int "
                "FROM trips WHERE trip_id = :id"
            ),
            {"id": task.trip_id},
        ).one()
    assert column == payload
    option = next(
        item for item in change.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(change.task_id, option.option_id)
    workflow.confirm_booking(
        change.task_id,
        order_references=["PNR-2"],
        total_amount=option.total_cost,
        currency=option.currency,
        reported_by="E1001",
    )
    assert trips.get(task.trip_id).status is TripStatus.REBOOKED


def test_booking_confirmation_is_one_transaction(system) -> None:
    workflow, repository, trips = system
    task = workflow.create_task(make_demo_request(task_id="pg-confirm"))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(task.task_id, option.option_id)
    workflow.mark_handed_off(task.task_id)
    before = [item.event_type for item in repository.events(task.task_id)]
    stale = trips.get(task.trip_id)
    trips.save(trips.get(task.trip_id))
    original_get = trips.get
    trips.get = lambda trip_id: stale if trip_id == task.trip_id else original_get(trip_id)  # type: ignore[method-assign]
    try:
        with pytest.raises(ConcurrentUpdateError):
            workflow.confirm_booking(
                task.task_id,
                order_references=["PNR-X"],
                total_amount=option.total_cost,
                currency=option.currency,
                reported_by="E1001",
            )
    finally:
        trips.get = original_get  # type: ignore[method-assign]
    assert repository.get(task.task_id).state is TaskState.HANDED_OFF
    assert [item.event_type for item in repository.events(task.task_id)] == before
    with repository.engine.connect() as connection:
        pending = connection.execute(
            text("SELECT count(*) FROM outbox_events WHERE event_type = 'BOOKING_CONFIRMED'")
        ).scalar_one()
    assert pending == 0
    assert trips.get(task.trip_id).watch is None


def test_find_by_task_uses_the_trip_id_projection(system) -> None:
    workflow, repository, trips = system
    task = workflow.create_task(make_demo_request(task_id="pg-find"))
    with repository.engine.connect() as connection:
        projected = connection.execute(
            text("SELECT trip_id FROM trip_tasks WHERE task_id = 'pg-find'")
        ).scalar_one()
    assert projected == task.trip_id
    assert trips.find_by_task("pg-find").trip_id == task.trip_id


def test_version_one_jsonb_rows_are_upgraded_on_read_and_by_the_tool(system) -> None:
    workflow, repository, _ = system
    task = workflow.create_task(make_demo_request(task_id="pg-old"))
    with repository.engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE trip_tasks SET payload = jsonb_set(
                    jsonb_set(payload, '{schema_version}', '1'),
                    '{task,options}',
                    (SELECT jsonb_agg(
                        (o - 'legs' - 'stays')
                        || jsonb_build_object('outbound', o->'legs'->0,
                                              'inbound', o->'legs'->1,
                                              'hotel', o->'stays'->0)
                    ) FROM jsonb_array_elements(payload->'task'->'options') o)
                ), payload_schema_version = 1
                WHERE task_id = 'pg-old'
                """
            )
        )
    loaded = repository.get("pg-old")
    assert [o.option_id for o in loaded.options] == [o.option_id for o in task.options]
    assert loaded.options[0].legs == task.options[0].legs
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "DATABASE_URL": URL}
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples/upgrade_task_payloads.py"), "--apply"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert next(t for t in report["tables"] if t["table"] == "trip_tasks")["upgraded"] == 1
    with repository.engine.connect() as connection:
        version, column = connection.execute(
            text(
                "SELECT (payload->>'schema_version')::int, payload_schema_version "
                "FROM trip_tasks WHERE task_id = 'pg-old'"
            )
        ).one()
    assert version == SCHEMA_VERSION and column == SCHEMA_VERSION

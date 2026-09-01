"""任务和差旅同一笔事务；重启恢复不再全表扫描。

HANDOFF §8 记的两条窄缝：改期任务建了而差旅没记上（`find_by_task` 找不到它）；
`recover_interrupted_tasks()` 用 `list_tasks()` 把历史任务全部反序列化一遍。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    IntentEntrypoint,
    PolicyOutcome,
    TaskState,
    ToolCallStatus,
    TripEventType,
    TripStatus,
)
from corporate_travel_agent.domain.models import ToolCallRecord, TripTask
from corporate_travel_agent.services.repositories import ConcurrentUpdateError, NotFoundError
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository
from corporate_travel_agent.services.trips import SQLAlchemyTripRepository


def _sql_system(tmp_path, name: str = "txn.db"):
    repository = SQLAlchemyTaskRepository(f"sqlite+pysqlite:///{tmp_path / name}")
    repository.create_schema()
    trips = SQLAlchemyTripRepository(repository.engine)
    workflow, _ = build_demo_system(
        clock=lambda: DEMO_CLOCK, task_repository=repository, trip_repository=trips
    )
    return workflow, repository, trips


def _booked(workflow, task_id: str):
    task = workflow.create_task(make_demo_request(task_id=task_id))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(task.task_id, option.option_id)
    return workflow.confirm_booking(
        task.task_id,
        order_references=["PNR-T"],
        total_amount=option.total_cost,
        currency=option.currency,
        reported_by="E1001",
    ), option


def test_a_planning_task_and_its_trip_land_together(tmp_path) -> None:
    workflow, repository, trips = _sql_system(tmp_path)
    task = workflow.create_task(make_demo_request(task_id="txn-plan"))

    assert repository.get("txn-plan").trip_id == task.trip_id
    trip = trips.get(task.trip_id)
    assert trip.status is TripStatus.PLANNED
    assert trip.task_ids == ("txn-plan",)
    assert trips.find_by_task("txn-plan").trip_id == task.trip_id


def test_a_change_task_its_trip_link_and_the_event_are_one_write(tmp_path) -> None:
    """以前这里是三次写：任务、差旅（追加任务）、差旅（追加事件）。现在一次。"""
    workflow, repository, trips = _sql_system(tmp_path)
    booked, option = _booked(workflow, "txn-booked")

    change = workflow.report_trip_event(
        booked.trip_id,
        event_type=TripEventType.FLIGHT_CHANGED,
        ref_id=option.legs[0].ref_id,
        note="航司取消了这一班",
        reported_by="carrier-feed",
    )

    trip = trips.get(booked.trip_id)
    assert trip.status is TripStatus.CHANGE_REQUESTED
    assert trip.task_ids == ("txn-booked", change.task_id)
    assert trip.events[-1].opened_task_id == change.task_id
    assert trip.events[-1].event_id == change.change_event_id
    assert trips.find_by_task(change.task_id).trip_id == booked.trip_id
    assert repository.get(change.task_id).parent_task_id == "txn-booked"


def test_when_the_trip_write_conflicts_the_task_row_is_rolled_back(tmp_path) -> None:
    """要么一起成、要么一起回滚：差旅乐观锁撞了，任务行不能留下半截。"""
    workflow, repository, trips = _sql_system(tmp_path)
    first = workflow.create_task(make_demo_request(task_id="txn-first"))
    stale = trips.get(first.trip_id)
    fresh = trips.get(first.trip_id)
    fresh.status = TripStatus.CHANGE_REQUESTED
    trips.save(fresh)  # 版本号 +1，`stale` 手里的是旧版本

    orphan = TripTask(
        task_id="txn-orphan",
        state=TaskState.DRAFT,
        request=make_demo_request(task_id="txn-orphan"),
        employee=workflow.employees.snapshot("E1001"),
        policy_snapshot_id=workflow.policies.current().snapshot_id,
        metadata={"intent_entrypoint": IntentEntrypoint.STRUCTURED.value},
        trip_id=first.trip_id,
        parent_task_id="txn-first",
    )
    stale.task_ids = (*stale.task_ids, "txn-orphan")
    with pytest.raises(ConcurrentUpdateError):
        repository.add_with_trip(orphan, trip=stale, trip_is_new=False)

    with pytest.raises(NotFoundError):
        repository.get("txn-orphan")
    assert trips.get(first.trip_id).task_ids == ("txn-first",)


def test_list_by_states_only_deserializes_the_states_asked_for(tmp_path) -> None:
    workflow, repository, _trips = _sql_system(tmp_path)
    task = workflow.create_task(make_demo_request(task_id="txn-state"))

    assert [item.task_id for item in repository.list_by_states([task.state.value])] == [
        "txn-state"
    ]
    assert repository.list_by_states([TaskState.SEARCHING.value]) == ()
    assert repository.list_by_states([]) == ()
    # 墙钟预过滤：把"早于很久以前"传进去就什么都不返回
    long_ago = repository.get("txn-state").tool_calls[0].started_at - timedelta(days=3650)
    assert repository.list_by_states([task.state.value], updated_before=long_ago) == ()


def test_restart_recovery_does_not_touch_list_tasks(tmp_path, monkeypatch) -> None:
    """恢复扫描走 `list_by_states`；`list_tasks()` 一次都不该被叫到。"""
    workflow, repository, trips = _sql_system(tmp_path)
    task = workflow.create_task(make_demo_request(task_id="txn-stuck"))
    # 模拟进程在搜索途中被杀：状态停在 SEARCHING，一条工具调用还是 STARTED
    task.state = TaskState.SEARCHING
    task.tool_calls.append(
        ToolCallRecord(
            sequence=len(task.tool_calls) + 1,
            tool_name="provider.search_transport.outbound",
            tool_kind="PROVIDER",
            status=ToolCallStatus.STARTED,
            started_at=DEMO_CLOCK - timedelta(minutes=5),
        )
    )
    workflow._audit(task, "TEST_PROCESS_KILLED", "simulated crash", {"state": task.state.value})

    def _forbidden():
        raise AssertionError("recovery must not scan the whole table")

    monkeypatch.setattr(repository, "list_tasks", _forbidden)
    # 重启发生在 5 分钟之后：最后一次活动早于陈旧阈值（30 秒），才算被中断
    restarted, _ = build_demo_system(
        clock=lambda: DEMO_CLOCK + timedelta(minutes=5),
        task_repository=repository,
        trip_repository=trips,
    )
    recovered = repository.get("txn-stuck")
    assert recovered.state is TaskState.PROVIDER_FAILED
    assert restarted.recover_interrupted_tasks() == ()

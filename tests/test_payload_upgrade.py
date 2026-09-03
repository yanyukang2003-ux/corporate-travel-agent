"""载荷版本与升级链：旧形状的载荷读得出来、升得上去、升不上去的说得清楚。

2026-08-29 方案从 `outbound/inbound/hotel` 改成 `legs/stays` 之后，开发库里 8 月的任务再也
反序列化不了，列表接口碰到就 500。守住的东西：
- 版本 1 的任务载荷读取时自动升到当前版本，方案的三个槽变成有序列表；
- 升级后仍不合模型的行抛 `PayloadIncompatible`，API 给明确错误、列表跳过它而不是 500；
- `examples/upgrade_task_payloads.py` 能把库里的旧行批量改写成当前版本，干跑不动库。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.services.serialization import (
    SCHEMA_VERSION,
    PayloadIncompatible,
    UnsupportedPayloadVersion,
    deserialize_task,
    deserialize_trip,
    serialize_task,
    upgrade_payload,
)
from corporate_travel_agent.services.sqlalchemy_repository import (
    SQLAlchemyTaskRepository,
    TaskRow,
)
from corporate_travel_agent.services.trips import SQLAlchemyTripRepository

ROOT = Path(__file__).resolve().parents[1]


def _v1_payload(task) -> dict:
    """把当前任务序列化后改回版本 1 的形状：方案三个槽、旧版本号。"""
    payload = copy.deepcopy(serialize_task(task))
    payload["schema_version"] = 1
    for option in payload["task"]["options"]:
        legs = option.pop("legs")
        stays = option.pop("stays")
        option["outbound"] = legs[0]
        option["inbound"] = legs[1] if len(legs) > 1 else None
        option["hotel"] = stays[0] if stays else None
    return payload


def _task_with_options():
    workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
    return workflow.create_task(make_demo_request(task_id="upgrade-me"))


def test_current_version_is_two_and_new_payloads_carry_it() -> None:
    assert SCHEMA_VERSION == 2
    assert serialize_task(_task_with_options())["schema_version"] == 2


def test_a_version_one_task_payload_is_upgraded_on_read() -> None:
    task = _task_with_options()
    v1 = _v1_payload(task)
    assert "outbound" in v1["task"]["options"][0]

    upgraded, changed = upgrade_payload(v1)
    assert changed and upgraded["schema_version"] == 2
    first = upgraded["task"]["options"][0]
    assert "outbound" not in first and "hotel" not in first
    assert [leg["ref_id"] for leg in first["legs"]] == [leg.ref_id for leg in task.options[0].legs]
    assert [stay["ref_id"] for stay in first["stays"]] == [
        stay.ref_id for stay in task.options[0].stays
    ]

    restored = deserialize_task(v1)
    assert [o.option_id for o in restored.options] == [o.option_id for o in task.options]
    assert restored.options[0].legs == task.options[0].legs
    assert restored.options[0].stays == task.options[0].stays
    # 原载荷没被就地改坏。
    assert v1["schema_version"] == 1 and "outbound" in v1["task"]["options"][0]


def test_version_one_trip_payloads_only_need_the_version_bump() -> None:
    workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
    task = workflow.create_task(make_demo_request(task_id="trip-v1"))
    from corporate_travel_agent.services.serialization import serialize_trip

    payload = serialize_trip(workflow.trips.get(task.trip_id))
    payload["schema_version"] = 1
    assert deserialize_trip(payload).trip_id == task.trip_id


def test_unknown_or_future_versions_are_refused() -> None:
    with pytest.raises(UnsupportedPayloadVersion):
        upgrade_payload({"schema_version": 99, "task": {}})
    with pytest.raises(UnsupportedPayloadVersion):
        upgrade_payload({"task": {}})


def test_an_unupgradable_payload_says_so_instead_of_leaking_pydantic() -> None:
    broken = {"schema_version": 1, "task": {"task_id": "x", "options": [{"outbound": {}}]}}
    with pytest.raises(PayloadIncompatible) as caught:
        deserialize_task(broken)
    assert caught.value.version == 1
    assert "schema_version 1" in str(caught.value)


def _sql_system(tmp_path):
    repository = SQLAlchemyTaskRepository(f"sqlite+pysqlite:///{tmp_path / 'upgrade.db'}")
    repository.create_schema()
    trips = SQLAlchemyTripRepository(repository.engine)
    workflow, _ = build_demo_system(
        clock=lambda: DEMO_CLOCK, task_repository=repository, trip_repository=trips
    )
    return workflow, repository


def _downgrade_row(repository, task_id: str) -> None:
    with Session(repository.engine) as session, session.begin():
        row = session.get(TaskRow, task_id)
        session.execute(
            update(TaskRow)
            .where(TaskRow.task_id == task_id)
            .values(payload=_v1_payload(deserialize_task(row.payload)), payload_schema_version=1)
        )


def test_the_repository_reads_old_rows_and_the_tool_rewrites_them(tmp_path) -> None:
    workflow, repository = _sql_system(tmp_path)
    task = workflow.create_task(make_demo_request(task_id="old-row"))
    _downgrade_row(repository, "old-row")
    with Session(repository.engine) as session:
        stored = session.get(TaskRow, "old-row")
        assert stored.payload["schema_version"] == 1

    # 读取时升级，业务代码看不出差别。
    loaded = repository.get("old-row")
    assert loaded.options[0].legs == task.options[0].legs

    env = {"PYTHONPATH": str(ROOT / "src"), "DATABASE_URL": str(repository.engine.url)}
    dry = subprocess.run(
        [sys.executable, str(ROOT / "examples/upgrade_task_payloads.py")],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert dry.returncode == 0, dry.stderr
    report = json.loads(dry.stdout)
    tasks_table = next(item for item in report["tables"] if item["table"] == "trip_tasks")
    assert report["mode"] == "dry-run" and tasks_table["upgraded"] == 1
    with Session(repository.engine) as session:
        assert session.get(TaskRow, "old-row").payload["schema_version"] == 1  # 干跑不动库

    applied = subprocess.run(
        [sys.executable, str(ROOT / "examples/upgrade_task_payloads.py"), "--apply"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert applied.returncode == 0, applied.stderr
    with Session(repository.engine) as session:
        row = session.get(TaskRow, "old-row")
        assert row.payload["schema_version"] == SCHEMA_VERSION
        assert row.payload_schema_version == SCHEMA_VERSION
        assert "legs" in row.payload["task"]["options"][0]
        # 不是业务更新：版本号列不动。
        assert row.revision == session.execute(
            select(TaskRow.revision).where(TaskRow.task_id == "old-row")
        ).scalar_one()
    again = subprocess.run(
        [sys.executable, str(ROOT / "examples/upgrade_task_payloads.py")],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert json.loads(again.stdout)["tables"][0]["upgraded"] == 0


def test_a_broken_row_is_reported_and_left_alone(tmp_path) -> None:
    workflow, repository = _sql_system(tmp_path)
    workflow.create_task(make_demo_request(task_id="broken-row"))
    with Session(repository.engine) as session, session.begin():
        session.execute(
            text("UPDATE trip_tasks SET payload = :p WHERE task_id = 'broken-row'"),
            {"p": json.dumps({"schema_version": 1, "task": {"task_id": "broken-row"}})},
        )
    with pytest.raises(PayloadIncompatible):
        repository.get("broken-row")
    env = {"PYTHONPATH": str(ROOT / "src"), "DATABASE_URL": str(repository.engine.url)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples/upgrade_task_payloads.py"), "--apply"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert result.returncode == 2
    tasks_table = next(
        item for item in json.loads(result.stdout)["tables"] if item["table"] == "trip_tasks"
    )
    assert [item["id"] for item in tasks_table["failed"]] == ["broken-row"]


def test_the_api_lists_around_an_incompatible_row_and_names_it_on_direct_read(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from corporate_travel_agent.api import main as api_main

    workflow, repository = _sql_system(tmp_path)
    workflow.create_task(make_demo_request(task_id="good-row"))
    workflow.create_task(make_demo_request(task_id="bad-row"))
    with Session(repository.engine) as session, session.begin():
        session.execute(
            text("UPDATE trip_tasks SET payload = :p WHERE task_id = 'bad-row'"),
            {"p": json.dumps({"schema_version": 1, "task": {"task_id": "bad-row"}})},
        )
    previous = api_main.workflow
    api_main.workflow = workflow
    try:
        client = TestClient(api_main.app)
        listed = client.get("/trip-tasks?summary=false&limit=10")
        assert listed.status_code == 200
        assert [item["task_id"] for item in listed.json()] == ["good-row"]
        summaries = client.get("/trip-tasks?summary=true&limit=10")
        assert {item["task_id"] for item in summaries.json()} == {"good-row", "bad-row"}
        direct = client.get("/trip-tasks/bad-row")
        assert direct.status_code == 500
        assert "upgrade_task_payloads" in direct.json()["detail"]
    finally:
        api_main.workflow = previous

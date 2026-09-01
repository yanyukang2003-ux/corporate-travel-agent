"""Phase A–D persistence extensions: projections, config, outbox."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config

from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.config_repository import (
    SQLAlchemyConfigRepository,
    import_policy_configuration_to_database,
)
from corporate_travel_agent.services.outbox import SQLAlchemyOutboxStore
from corporate_travel_agent.services.policy_config import load_policy_configuration
from corporate_travel_agent.services.provider_resilience import PROVIDER_RETRY_METADATA_KEY
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository
from corporate_travel_agent.services.task_projections import projection_fields

FIXED_NOW = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)


def database_url(tmp_path, name: str = "phases.db") -> str:
    return f"sqlite+pysqlite:///{tmp_path / name}"


def new_repository(tmp_path, name: str = "phases.db") -> SQLAlchemyTaskRepository:
    repository = SQLAlchemyTaskRepository(database_url(tmp_path, name))
    repository.create_schema()
    return repository


def test_projection_columns_track_employee_and_retry(tmp_path) -> None:
    repository = new_repository(tmp_path)
    workflow, provider = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=repository,
        retry_sleep=lambda _: None,
    )
    provider.fail_search_remaining = 3
    task = workflow.create_task(make_demo_request(task_id="phase-b-retry"))

    assert task.state is TaskState.WAITING_FOR_PROVIDER
    summaries = repository.list_task_summaries(employee_id="E1001", limit=10)
    assert any(item.task_id == task.task_id for item in summaries)
    due = repository.list_due_provider_retries(
        now=FIXED_NOW + timedelta(seconds=120),
        limit=10,
    )
    assert [item.task_id for item in due] == [task.task_id]

    fields = projection_fields(repository.get(task.task_id))
    assert fields["employee_id"] == "E1001"
    assert fields["manager_id"]
    assert fields["next_retry_at"] is not None
    repository.dispose()


def test_list_by_employee_and_state(tmp_path) -> None:
    repository = new_repository(tmp_path, "list.db")
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=repository,
    )
    task = workflow.create_task(make_demo_request(task_id="phase-b-list"))
    by_employee = repository.list_by_employee("E1001")
    assert task.task_id in {item.task_id for item in by_employee}
    by_state = repository.list_by_state(task.state.value)
    assert task.task_id in {item.task_id for item in by_state}
    repository.dispose()


def test_alembic_head_includes_phase_tables(tmp_path, monkeypatch) -> None:
    migration_url = database_url(tmp_path, "migrate-head.db")
    monkeypatch.setenv("DATABASE_URL", migration_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    repository = SQLAlchemyTaskRepository(migration_url)
    repository.check_schema()
    status = repository.operational_status()
    assert status["backend"] == "sqlite"
    assert status["alembic_version"] == "0009_expense_records"

    outbox = SQLAlchemyOutboxStore(repository.engine)
    event = outbox.enqueue(
        aggregate_type="trip_task",
        aggregate_id="t1",
        event_type="TEST",
        payload={"ok": True},
    )
    assert outbox.unpublished_count() == 1
    outbox.mark_published(event.event_id)
    assert outbox.unpublished_count() == 0
    repository.dispose()


def test_policy_config_round_trip_in_database(tmp_path) -> None:
    repository = new_repository(tmp_path, "config.db")
    loaded = load_policy_configuration()
    config_id = import_policy_configuration_to_database(
        repository.engine,
        loaded,
        activate=True,
    )
    store = SQLAlchemyConfigRepository(repository.engine)
    restored = store.load_active_or_raise()
    assert restored.sha256 == loaded.sha256
    assert restored.config.config_version == loaded.config.config_version
    assert restored.source_type == "postgres"
    assert config_id
    # Idempotent import of same content.
    again = import_policy_configuration_to_database(repository.engine, loaded)
    assert again == config_id
    repository.dispose()


def test_due_retry_query_used_after_schedule(tmp_path) -> None:
    now = [FIXED_NOW]
    repository = new_repository(tmp_path, "due.db")
    workflow, provider = build_demo_system(
        clock=lambda: now[0],
        task_repository=repository,
        retry_sleep=lambda _: None,
    )
    provider.fail_search_remaining = 3
    task = workflow.create_task(make_demo_request(task_id="phase-b-process-due"))
    assert task.state is TaskState.WAITING_FOR_PROVIDER

    now[0] = FIXED_NOW + timedelta(seconds=61)
    processed = workflow.process_due_provider_retries()
    assert processed == (task.task_id,)
    recovered = repository.get(task.task_id)
    assert recovered.state is TaskState.WAITING_FOR_USER
    assert recovered.metadata[PROVIDER_RETRY_METADATA_KEY]["status"] == "recovered"
    repository.dispose()


def test_memory_repository_supports_new_query_methods() -> None:
    from corporate_travel_agent.services.repositories import InMemoryTaskRepository

    repository = InMemoryTaskRepository()
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        task_repository=repository,
    )
    task = workflow.create_task(make_demo_request(task_id="memory-query"))
    summaries = repository.list_task_summaries(employee_id="E1001")
    assert summaries[0].task_id == task.task_id
    assert repository.list_by_state(task.state.value)

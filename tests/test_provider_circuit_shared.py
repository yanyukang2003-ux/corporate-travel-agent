"""多实例共享的熔断状态：A 实例判定供应商挂了，B 实例下一次调用前就知道。

之前熔断器是每个进程一份：三个 API 实例会各自把供应商撞一遍才各自打开。
现在"打开到几点"写进共享存储；探测仍按进程各自做。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from corporate_travel_agent.services.provider_resilience import (
    CircuitRecord,
    CircuitState,
    InMemoryProviderCircuitStore,
    ProviderCircuitBreaker,
)
from corporate_travel_agent.services.sqlalchemy_repository import (
    SQLAlchemyProviderCircuitStore,
    SQLAlchemyTaskRepository,
)

START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _pair(store, *, open_seconds: float = 60.0):
    clock = [START]
    tick = lambda: clock[0]  # noqa: E731
    first = ProviderCircuitBreaker(clock=tick, open_seconds=open_seconds, store=store)
    second = ProviderCircuitBreaker(clock=tick, open_seconds=open_seconds, store=store)
    return first, second, clock


def test_a_failure_in_one_instance_opens_the_circuit_in_the_other() -> None:
    a, b, clock = _pair(InMemoryProviderCircuitStore())
    assert a.try_acquire() and b.try_acquire()

    a.record_failure()

    # B 自己一次都没失败过，也不再往供应商撞
    assert b.try_acquire() is False
    assert b.snapshot()["state"] == "OPEN"
    assert b.snapshot()["open_until"] == a.snapshot()["open_until"]

    # 冷却结束：A 半开探测成功，B 跟着闭合
    clock[0] = START + timedelta(seconds=61)
    assert a.try_acquire() is True
    a.record_success()
    assert b.try_acquire() is True
    assert b.snapshot()["state"] == "CLOSED"


def test_a_success_elsewhere_closes_a_breaker_that_is_still_cooling() -> None:
    a, b, clock = _pair(InMemoryProviderCircuitStore())
    b.record_failure()
    assert b.try_acquire() is False

    # 另一实例探测成功（它的本地状态是闭合的，写回共享记录）
    a.record_success()

    assert b.try_acquire() is True
    assert b.snapshot()["state"] == "CLOSED"


def test_restored_open_until_is_shared_too() -> None:
    a, b, _clock = _pair(InMemoryProviderCircuitStore())
    a.restore_open_until(START + timedelta(seconds=300))
    assert b.try_acquire() is False


class _BrokenStore:
    backend_name = "broken"

    def load(self, key: str):
        raise RuntimeError("database is away")

    def save(self, key: str, record: CircuitRecord) -> None:
        raise RuntimeError("database is away")


def test_store_failures_fall_back_to_local_state() -> None:
    """数据库抖一下不该把供应商调用也一起卡死：读写失败只记日志，本地状态照常。"""
    breaker = ProviderCircuitBreaker(clock=lambda: START, open_seconds=60, store=_BrokenStore())
    assert breaker.try_acquire() is True
    breaker.record_failure()
    assert breaker.try_acquire() is False
    assert breaker.snapshot()["shared_store"] == "broken"


def test_sql_store_round_trips_and_is_shared_across_engines(tmp_path) -> None:
    repository = SQLAlchemyTaskRepository(f"sqlite+pysqlite:///{tmp_path / 'circuit.db'}")
    repository.create_schema()
    store = SQLAlchemyProviderCircuitStore(repository.engine)
    assert store.load("travel_provider") is None

    store.save(
        "travel_provider",
        CircuitRecord(
            state=CircuitState.OPEN,
            opened_at=START,
            open_until=START + timedelta(seconds=60),
            updated_at=START,
        ),
    )
    loaded = store.load("travel_provider")
    assert loaded is not None
    assert loaded.state is CircuitState.OPEN
    assert loaded.open_until == START + timedelta(seconds=60)

    # 两个熔断器各自拿一个存储对象（模拟两个进程），共用同一张表
    a = ProviderCircuitBreaker(
        clock=lambda: START + timedelta(seconds=1), open_seconds=60, store=store
    )
    b = ProviderCircuitBreaker(
        clock=lambda: START + timedelta(seconds=1),
        open_seconds=60,
        store=SQLAlchemyProviderCircuitStore(repository.engine),
    )
    assert a.try_acquire() is False
    assert b.try_acquire() is False
    a.record_success()
    assert b.try_acquire() is True
    repository.dispose()

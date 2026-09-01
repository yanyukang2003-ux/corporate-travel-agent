"""供应商弹性：熔断器（可接共享状态存储）、延迟重试策略，以及后台延迟重试调度器。

熔断器本身仍是每个进程一份；接上 `ProviderCircuitStateStore` 之后，"打开到几点"这一条
在所有实例之间共享：A 实例判定供应商挂了，B 实例下一次调用前就知道，不再各自撞一遍。
半开探测（冷却结束后放一个请求去试）仍按进程各自做，最多每个实例一个探测，可以接受。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from threading import Event, Lock, Thread
from typing import Protocol

LOGGER = logging.getLogger(__name__)

DEFAULT_CIRCUIT_OPEN_SECONDS = 60.0
DEFAULT_MAX_DELAYED_PROVIDER_ATTEMPTS = 3
DEFAULT_DELAYED_PROVIDER_RETRY_SECONDS = (60.0, 180.0, 600.0)
PROVIDER_RETRY_METADATA_KEY = "provider_retry"


class CircuitState(StrEnum):
    """熔断器状态：闭合 / 打开 / 半开。"""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class ProviderDelayedRetryPolicy:
    """供应商延迟重试次数与退避秒数表。"""

    max_attempts: int = DEFAULT_MAX_DELAYED_PROVIDER_ATTEMPTS
    delays_seconds: tuple[float, ...] = DEFAULT_DELAYED_PROVIDER_RETRY_SECONDS

    def __post_init__(self) -> None:
        if not 0 <= self.max_attempts <= 3:
            raise ValueError("max delayed provider attempts must be between 0 and 3")
        if len(self.delays_seconds) < self.max_attempts:
            raise ValueError("delayed retry schedule must cover every delayed attempt")
        if any(not isfinite(delay) or delay < 0 for delay in self.delays_seconds):
            raise ValueError("delayed retry seconds must be finite and non-negative")

    def delay_after(self, completed_attempts: int) -> float:
        """返回已完成若干次延迟尝试后，下一次应等待的秒数。"""
        if completed_attempts >= self.max_attempts:
            raise ValueError("delayed provider retry attempts are exhausted")
        return self.delays_seconds[completed_attempts]


@dataclass(frozen=True)
class CircuitRecord:
    """熔断状态的持久化形态：状态、打开时刻、打开到几点、最后更新时刻。"""

    state: CircuitState
    opened_at: datetime | None
    open_until: datetime | None
    updated_at: datetime


class ProviderCircuitStateStore(Protocol):
    """多实例共享的熔断状态存储。`key` 区分不同供应商。"""

    backend_name: str

    def load(self, key: str) -> CircuitRecord | None: ...

    def save(self, key: str, record: CircuitRecord) -> None: ...


class InMemoryProviderCircuitStore:
    """内存版共享存储：单测和无数据库演示用，也是"两个熔断器共用一份状态"的最小样板。"""

    backend_name = "memory"

    def __init__(self) -> None:
        self._records: dict[str, CircuitRecord] = {}
        self._lock = Lock()

    def load(self, key: str) -> CircuitRecord | None:
        with self._lock:
            return self._records.get(key)

    def save(self, key: str, record: CircuitRecord) -> None:
        with self._lock:
            self._records[key] = record


DEFAULT_CIRCUIT_KEY = "travel_provider"


class ProviderCircuitBreaker:
    """熔断器：打开后冷却，半开态仅允许一次探测请求。

    不传 `store` 就是原来的进程内实现。传了 `store`，每次取许可前先读一次共享记录，
    失败/成功都写回去——**共享的是"打开到几点"，不是探测**。存储读写失败只记日志，
    退回本地状态：数据库抖一下不该把供应商调用也一起卡死。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        open_seconds: float = DEFAULT_CIRCUIT_OPEN_SECONDS,
        store: ProviderCircuitStateStore | None = None,
        key: str = DEFAULT_CIRCUIT_KEY,
    ) -> None:
        if not isfinite(open_seconds) or open_seconds < 0:
            raise ValueError("provider circuit open seconds must be finite and non-negative")
        self._clock = clock
        self.open_seconds = open_seconds
        self.store = store
        self.key = key
        self._state = CircuitState.CLOSED
        self._opened_at: datetime | None = None
        self._open_until: datetime | None = None
        self._half_open_probe_in_flight = False
        self._lock = Lock()

    def try_acquire(self) -> bool:
        """尝试获取调用许可；熔断打开或半开探测占用中时返回 False。"""
        now = self._aware(self._clock())
        with self._lock:
            self._adopt_shared(now)
            if self._state is CircuitState.OPEN:
                if self._open_until is not None and now < self._open_until:
                    return False
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe_in_flight = False
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_probe_in_flight:
                    return False
                self._half_open_probe_in_flight = True
            return True

    def record_success(self) -> None:
        """记录成功并复位为闭合状态。"""
        now = self._aware(self._clock())
        with self._lock:
            self._state = CircuitState.CLOSED
            self._opened_at = None
            self._open_until = None
            self._half_open_probe_in_flight = False
            self._publish(now)

    def record_failure(self) -> None:
        """记录失败并打开熔断，进入冷却窗口。"""
        now = self._aware(self._clock())
        with self._lock:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._open_until = now + timedelta(seconds=self.open_seconds)
            self._half_open_probe_in_flight = False
            self._publish(now)

    def restore_open_until(self, open_until: datetime) -> None:
        """按持久化的 open_until 恢复打开态（跨进程重启延续冷却）。"""
        now = self._aware(self._clock())
        open_until = self._aware(open_until)
        if open_until <= now:
            return
        with self._lock:
            if self._open_until is None or open_until > self._open_until:
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._open_until = open_until
                self._half_open_probe_in_flight = False
                self._publish(now)

    def snapshot(self) -> dict[str, object]:
        """返回熔断器当前状态快照。"""
        with self._lock:
            return {
                "state": self._state.value,
                "open_seconds": self.open_seconds,
                "opened_at": self._opened_at.isoformat() if self._opened_at else None,
                "open_until": self._open_until.isoformat() if self._open_until else None,
                "shared_store": self.store.backend_name if self.store is not None else None,
            }

    # -- 共享状态 ---------------------------------------------------------

    def _adopt_shared(self, now: datetime) -> None:
        """取许可前把共享记录并进来。持锁调用。

        共享记录说"打开到 T 且 T 还没到" → 本地跟着打开（别的实例已经判定供应商挂了）。
        共享记录说"闭合"而本地还在冷却 → 本地也闭合（别的实例已经探测成功）。
        本地在半开探测中 → 不动，探测结果自己会写回去。
        """
        if self.store is None:
            return
        try:
            record = self.store.load(self.key)
        except Exception:  # noqa: BLE001 - 存储故障不该把供应商调用一起卡死
            LOGGER.warning("provider circuit store load failed; using local state", exc_info=True)
            return
        if record is None:
            return
        open_until = self._aware(record.open_until) if record.open_until is not None else None
        if open_until is not None and open_until > now:
            if self._state is not CircuitState.OPEN or self._open_until != open_until:
                self._state = CircuitState.OPEN
                self._opened_at = (
                    self._aware(record.opened_at) if record.opened_at is not None else now
                )
                self._open_until = open_until
                self._half_open_probe_in_flight = False
            return
        # 共享记录说闭合，而且比本地这次打开更新（别的实例已经探测成功）→ 本地也闭合，
        # 不必再各自探一次。本地的打开总是先写进存储再返回，所以"更旧的闭合记录"
        # 只在存储写失败时出现，那时保留本地判断。
        if record.state is CircuitState.CLOSED and self._state is not CircuitState.CLOSED:
            updated_at = self._aware(record.updated_at)
            if self._opened_at is None or updated_at >= self._opened_at:
                self._state = CircuitState.CLOSED
                self._opened_at = None
                self._open_until = None
                self._half_open_probe_in_flight = False

    def _publish(self, now: datetime) -> None:
        """把本地状态写回共享存储。持锁调用；写失败只记日志。"""
        if self.store is None:
            return
        record = CircuitRecord(
            state=self._state,
            opened_at=self._opened_at,
            open_until=self._open_until,
            updated_at=now,
        )
        try:
            self.store.save(self.key, record)
        except Exception:  # noqa: BLE001
            LOGGER.warning("provider circuit store save failed; state kept locally", exc_info=True)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value


class DelayedRetryProcessor(Protocol):
    """处理到期供应商延迟重试的协议接口。"""

    def process_due_provider_retries(self, *, limit: int = 20) -> tuple[str, ...]: ...


class ProviderRetryScheduler:
    """在请求线程之外轮询并执行已持久化的供应商延迟重试。"""

    def __init__(
        self,
        processor: DelayedRetryProcessor,
        *,
        poll_seconds: float = 5.0,
    ) -> None:
        if not isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("provider retry poll seconds must be finite and greater than zero")
        self.processor = processor
        self.poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """启动守护线程；已在运行时幂等返回。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._run,
            name="provider-delayed-retry",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        """停止领取新任务，并等待当前轮询迭代排空；超时返回 False。"""

        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(timeout_seconds, 0.0))
            if thread.is_alive():
                return False
        self._thread = None
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.processor.process_due_provider_retries()
            except Exception:
                LOGGER.exception("provider delayed retry worker iteration failed")
            self._stop.wait(self.poll_seconds)

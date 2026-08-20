"""供应商弹性：进程内熔断器、延迟重试策略，以及后台延迟重试调度器。"""

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


class ProviderCircuitBreaker:
    """进程内熔断器：打开后冷却，半开态仅允许一次探测请求。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        open_seconds: float = DEFAULT_CIRCUIT_OPEN_SECONDS,
    ) -> None:
        if not isfinite(open_seconds) or open_seconds < 0:
            raise ValueError("provider circuit open seconds must be finite and non-negative")
        self._clock = clock
        self.open_seconds = open_seconds
        self._state = CircuitState.CLOSED
        self._opened_at: datetime | None = None
        self._open_until: datetime | None = None
        self._half_open_probe_in_flight = False
        self._lock = Lock()

    def try_acquire(self) -> bool:
        """尝试获取调用许可；熔断打开或半开探测占用中时返回 False。"""
        now = self._aware(self._clock())
        with self._lock:
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
        with self._lock:
            self._state = CircuitState.CLOSED
            self._opened_at = None
            self._open_until = None
            self._half_open_probe_in_flight = False

    def record_failure(self) -> None:
        """记录失败并打开熔断，进入冷却窗口。"""
        now = self._aware(self._clock())
        with self._lock:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._open_until = now + timedelta(seconds=self.open_seconds)
            self._half_open_probe_in_flight = False

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

    def snapshot(self) -> dict[str, object]:
        """返回熔断器当前状态快照。"""
        with self._lock:
            return {
                "state": self._state.value,
                "open_seconds": self.open_seconds,
                "opened_at": self._opened_at.isoformat() if self._opened_at else None,
                "open_until": self._open_until.isoformat() if self._open_until else None,
            }

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

"""watch worker 的调度线程：在请求线程之外按固定间隔跑 `process_due_flight_checks`。

和 `ProviderRetryScheduler` 同一个形状：守护线程、`stop()` 等当前一轮排空。
API 进程 `PROCESS_ROLE=api` 不起它；`worker` / `all` 才起，或者用
`examples/run_trip_watch_worker.py` 单独跑一个进程——队列在 `trips` 表里，靠租约互斥。
"""

from __future__ import annotations

import logging
from math import isfinite
from threading import Event, Thread
from typing import Protocol

LOGGER = logging.getLogger(__name__)

DEFAULT_TRIP_WATCH_POLL_SECONDS = 60.0
DEFAULT_TRIP_WATCH_LEASE_SECONDS = 300.0


class FlightCheckProcessor(Protocol):
    def process_due_flight_checks(self, *, limit: int = 20) -> tuple[str, ...]: ...


class TripWatchScheduler:
    """轮询到点的差旅并问动态源。"""

    def __init__(self, processor: FlightCheckProcessor, *, poll_seconds: float = 60.0) -> None:
        if not isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("trip watch poll seconds must be finite and greater than zero")
        self.processor = processor
        self.poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="trip-watch", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
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
                self.processor.process_due_flight_checks()
            except Exception:
                LOGGER.exception("trip watch worker iteration failed")
            self._stop.wait(self.poll_seconds)

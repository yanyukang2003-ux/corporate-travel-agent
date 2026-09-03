"""航班动态源端口：watch worker 按票号和日期问"这一段现在怎么样了"。

## 端口

`FlightStatusPort.status_of(leg, now=...)` 拿一段被观察的交通（票号、航线、计划起降），
回一份 `FlightStatusReport`。**它只回事实，不做判断**——延误 40 分钟要不要改期，由
`services/change_impact.assess_flight_change` 按到场时限和政策缓冲算。

## 实现

- `NullFlightStatusSource`：没接动态源。永远回 `UNKNOWN`；worker 看到它就不领取任务。
  这是默认值——没接的东西不该假装接了。
- `InMemoryFlightStatusSource`：进程内的一张表，测试和演示用；也是"航司推送"的落点：
  入站 webhook 把推来的状态写进这里，worker 下一轮就读到。
- 真实供应商（VariFlight、AeroDataBox、企业 TMC 的查询接口）实现同一个 `status_of`。
  它们的请求形状各不相同，仓库里不放一个没有凭证就验不了的适配器。

`FlightStatusError` 是动态源暂时答不上来（超时、限流）：worker 记一笔、按节奏下次再问，
不当成航班有变化。
"""

from __future__ import annotations

import os
from datetime import datetime
from threading import RLock
from typing import Protocol

from corporate_travel_agent.domain.enums import FlightStatusKind
from corporate_travel_agent.domain.models import FlightStatusReport, TripWatchLeg


class FlightStatusError(RuntimeError):
    """动态源这一次没答上来：超时、限流、5xx。不是航班有变化。"""


class FlightStatusPort(Protocol):
    """航班动态源。"""

    name: str

    def status_of(self, leg: TripWatchLeg, *, now: datetime) -> FlightStatusReport: ...


class NullFlightStatusSource:
    """没接动态源：永远"判不了"。worker 看到它就不领任务。"""

    name = "none"
    configured = False

    def status_of(self, leg: TripWatchLeg, *, now: datetime) -> FlightStatusReport:
        return FlightStatusReport(
            ref_id=leg.ref_id,
            status=FlightStatusKind.UNKNOWN,
            observed_at=now,
            source=self.name,
            note="no flight status source configured",
        )


class InMemoryFlightStatusSource:
    """进程内的动态表：测试、演示，以及入站推送的落点。

    没登记过的票按计划回（`SCHEDULED`，不带预计时刻）；登记过的按登记的回。
    `record()` 覆盖同一票号的旧状态——动态源只有"现在怎么样"，没有历史；历史在观察对象和审计里。
    """

    name = "memory"
    configured = True

    def __init__(self) -> None:
        self._reports: dict[str, FlightStatusReport] = {}
        self._lock = RLock()
        self.queries: list[str] = []

    def record(self, report: FlightStatusReport) -> None:
        with self._lock:
            self._reports[report.ref_id] = report

    def clear(self, ref_id: str) -> None:
        with self._lock:
            self._reports.pop(ref_id, None)

    def status_of(self, leg: TripWatchLeg, *, now: datetime) -> FlightStatusReport:
        with self._lock:
            self.queries.append(leg.ref_id)
            known = self._reports.get(leg.ref_id)
        if known is not None:
            return known
        return FlightStatusReport(
            ref_id=leg.ref_id,
            status=FlightStatusKind.SCHEDULED,
            observed_at=now,
            source=self.name,
        )


def flight_status_source_from_environment(
    environ: dict[str, str] | None = None,
) -> FlightStatusPort:
    """按 `FLIGHT_STATUS_SOURCE` 装配：`none`（默认）或 `memory`。

    `memory` 在生产没有意义——它只被入站推送喂。写成显式选项而不是默认，是为了让
    健康检查里的 `trip_watch.source` 说真话：没接就是 `none`。
    """
    env = os.environ if environ is None else environ
    value = (env.get("FLIGHT_STATUS_SOURCE") or "none").strip().casefold()
    if value == "none":
        return NullFlightStatusSource()
    if value == "memory":
        return InMemoryFlightStatusSource()
    raise RuntimeError("FLIGHT_STATUS_SOURCE must be none or memory")

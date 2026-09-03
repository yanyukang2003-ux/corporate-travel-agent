#!/usr/bin/env python3
"""D20：订好之后的追踪与变更——每类至少 10 条，可多轮，出报告目录。

    # 确定性场景（航班动态、影响评估、worker、webhook、取消、事件接口），$0，不联网
    PYTHONPATH=src .venv/bin/python examples/run_trip_change_evaluation.py --offline \
        --output reports/evaluation-runs/trip-change-eval-<日期>

    # 加上真模型的聊天变更（会议改期 / 取消 / 航变自述 / 该问的时候问），跑 3 轮
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/bin/python examples/run_trip_change_evaluation.py --runs 3 \
        --output reports/evaluation-runs/trip-change-eval-<日期>

装配：演示库存 + 钉住的时钟（`DEMO_CLOCK`，2026-08-01）+ 内存仓储 + 进程内动态源；聊天场景用
`.env` 里的真模型。库存不用真的——量的是确定性判定和模型把话读成变更请求的能力，不是供应商。
每条用例各订一趟新差旅（会议改期、取消、航变都会让差旅离开 BOOKED，不能复用）。

每类的判据写在各自的函数里；`REPORT.md` 按类给出通过数，多轮时另给"每轮都过"的条数。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# 装配必须在 import api.main 之前定：内存仓储、演示库存、进程内动态源、鉴权关闭。
os.environ["DATABASE_URL"] = ""
os.environ["TRAVEL_PROVIDER"] = "mock"
os.environ["FLIGHT_STATUS_SOURCE"] = "memory"
os.environ["FLIGHT_STATUS_WEBHOOK_SECRET"] = "eval-secret"
os.environ["PROCESS_ROLE"] = "api"
os.environ["AUTH_ENABLED"] = "false"
os.environ["RAW_RESPONSE_STORE_DIR"] = ""
os.environ.pop("ENVIRONMENT", None)


def _eval_policy_file() -> str:
    """演示政策的副本，只把成本中心预算放大：一趟 1830 美元，几百趟下来会撞上 20000 的额度，
    那是政策在起作用，不是这轮要量的东西。"""
    import json as _json
    from pathlib import Path as _Path

    source = _Path(
        os.environ.get("POLICY_CONFIG_FILE")
        or (
            _Path(__file__).resolve().parents[1]
            / "src/corporate_travel_agent/config/default_policy.json"
        )
    )
    data = _json.loads(source.read_text(encoding="utf-8"))
    for policy in data.get("policies", []):
        for budget in policy.get("cost_center_budgets", []) or []:
            if isinstance(budget, dict) and "amount" in budget:
                budget["amount"] = "100000000"
    target = _Path(os.environ.get("TRIP_CHANGE_EVAL_TMP", "/tmp")) / "trip-change-eval-policy.json"
    target.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


os.environ["POLICY_CONFIG_FILE"] = _eval_policy_file()

from fastapi.testclient import TestClient  # noqa: E402

from corporate_travel_agent.api import main as api_main  # noqa: E402
from corporate_travel_agent.demo import DEMO_CLOCK  # noqa: E402
from corporate_travel_agent.domain.enums import FlightStatusKind  # noqa: E402
from corporate_travel_agent.domain.models import FlightStatusReport  # noqa: E402

SECRET = b"eval-secret"
TZ = "+08:00"
#: 演示库存里合规方案的两段：去程 MU-EARLY 08-05 06:20→08:35（到场时限 08-06 10:00，缓冲 60 分钟
#: → 最晚 09:00）；返程 MU-RETURN 08-06 18:00→20:20（窗口到 23:00，不留缓冲）。
OUT_ARRIVE = datetime.fromisoformat("2026-08-05T08:35:00+08:00")
OUT_DEPART = datetime.fromisoformat("2026-08-05T06:20:00+08:00")
RET_ARRIVE = datetime.fromisoformat("2026-08-06T20:20:00+08:00")
RET_DEPART = datetime.fromisoformat("2026-08-06T18:00:00+08:00")
BOOK = {
    "traveler_id": "E1001",
    "origin": "Beijing",
    "destination": "Shanghai",
    "departure_after": "2026-08-05T05:00:00+08:00",
    "arrive_by": "2026-08-06T10:00:00+08:00",
    "return_after": "2026-08-06T17:00:00+08:00",
    "return_before": "2026-08-06T23:00:00+08:00",
    "hard_constraints": ["arrive_before_meeting"],
}


@dataclass
class CaseResult:
    category: str
    case_id: str
    run: int
    ok: bool
    expected: str
    actual: str
    detail: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class Harness:
    def __init__(self) -> None:
        self.clock = {"now": DEMO_CLOCK}
        api_main.workflow.clock = lambda: self.clock["now"]
        self.client = TestClient(api_main.app)
        self.workflow = api_main.workflow
        self.source = api_main.workflow.flight_status_source
        self.model = api_main.workflow.tool_calling_language_model
        self.price = None
        self.booked = 0

    # -- 基础动作 -----------------------------------------------------------

    def book(self) -> dict[str, Any]:
        self.clock["now"] = DEMO_CLOCK
        created = self.client.post("/trip-tasks", json=BOOK).json()
        option = next(
            (o for o in created.get("options", []) if o["policy_outcome"] == "COMPLIANT"), None
        )
        if option is None:
            raise RuntimeError(f"no compliant option: {created.get('failure') or created}")
        task_id = created["task_id"]
        self.client.post(
            f"/trip-tasks/{task_id}/select-option", json={"option_id": option["option_id"]}
        )
        self.booked += 1
        confirmed = self.client.post(
            f"/trip-tasks/{task_id}/booking-confirmation",
            json={
                "order_references": [f"PNR-EVAL-{self.booked}"],
                "total_amount": str(option["total_cost"]),
                "currency": option["currency"],
            },
        )
        if confirmed.status_code != 200:
            raise RuntimeError(f"confirm failed: {confirmed.text}")
        trip = self.client.get(f"/trips/{created['trip_id']}").json()
        refs = [leg["ref_id"] for leg in trip["watch"]["legs"]]
        return {"task_id": task_id, "trip_id": created["trip_id"], "trip": trip, "refs": refs}

    def status(self, trip_id: str, ref: str, status: str, **extra: Any) -> tuple[int, dict]:
        payload = {"ref_id": ref, "status": status, "source": "eval-feed", **extra}
        response = self.client.post(f"/trips/{trip_id}/flight-status", json=payload)
        return response.status_code, response.json()

    def trip(self, trip_id: str) -> dict[str, Any]:
        return self.client.get(f"/trips/{trip_id}").json()

    def task(self, task_id: str) -> dict[str, Any]:
        return self.client.get(f"/trip-tasks/{task_id}").json()

    def audits(self, task_id: str, event_type: str) -> int:
        events = self.client.get(f"/trip-tasks/{task_id}/audit-events").json()
        return sum(1 for item in events if item["event_type"] == event_type)

    def notices(self) -> int:
        pending = self.workflow.tasks.outbox.list_unpublished(limit=10_000)
        return sum(1 for item in pending if item.event_type == "TRIP_FLIGHT_STATUS_NOTICE")

    def webhook(self, body: str, *, signature: str | None = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if signature is None:
            signature = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
        if signature:
            headers["X-Flight-Status-Signature"] = signature
        response = self.client.post("/flight-status/webhook", content=body, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def chat(self, task_id: str, message: str) -> tuple[int, dict[str, Any]]:
        response = self.client.post(
            f"/agentic/trip-tasks/{task_id}/messages", json={"message": message}
        )
        return response.status_code, response.json()

    def llm_snapshot(self) -> tuple[int, int, int]:
        model = self.model
        if model is None:
            return (0, 0, 0)
        return (
            int(getattr(model, "call_count", 0)),
            int(getattr(model, "input_tokens", 0)),
            int(getattr(model, "output_tokens", 0)),
        )


def iso(moment: datetime) -> str:
    return moment.isoformat()


# ---------------------------------------------------------------------------
# 确定性场景
# ---------------------------------------------------------------------------


def category_delay_notify(h: Harness, run: int, results: list[CaseResult]) -> None:
    """去程延误但仍早于 08-06 09:00：只通知，不开改期任务。同一趟差旅上连报 10 条不同的延误。"""
    booked = h.book()
    trip_id, task_id, ref = booked["trip_id"], booked["task_id"], booked["refs"][0]
    cases = [
        ("arrive", 15),
        ("arrive", 20),
        ("arrive", 25),
        ("arrive", 30),
        ("arrive", 45),
        ("arrive", 60),
        ("arrive", 90),
        ("arrive", 120),
        ("arrive", 180),
        ("depart", 40),
    ]
    for index, (mode, minutes) in enumerate(cases, start=1):
        before_audit = h.audits(task_id, "FLIGHT_STATUS_OBSERVED")
        before_notice = h.notices()
        extra = (
            {"estimated_arrive_at": iso(OUT_ARRIVE + timedelta(minutes=minutes))}
            if mode == "arrive"
            else {"estimated_depart_at": iso(OUT_DEPART + timedelta(minutes=minutes))}
        )
        code, body = h.status(trip_id, ref, "DELAYED", **extra)
        observation = body.get("observation", {})
        ok = (
            code == 200
            and observation.get("verdict") == "NOTIFY_ONLY"
            and observation.get("opened_task_id") is None
            and body["trip"]["status"] == "BOOKED"
            and h.audits(task_id, "FLIGHT_STATUS_OBSERVED") == before_audit + 1
            and h.notices() == before_notice + 1
        )
        results.append(
            CaseResult(
                "delay_notify",
                f"delay_notify_{index:02d}",
                run,
                ok,
                "NOTIFY_ONLY, BOOKED, +1 audit, +1 notice",
                f"{code} {observation.get('verdict')} {body.get('trip', {}).get('status')}",
                {"mode": mode, "minutes": minutes, "reasons": observation.get("reasons")},
            )
        )
        # 同一条动态第二次进来：不通知、不记审计。
        before_audit = h.audits(task_id, "FLIGHT_STATUS_OBSERVED")
        before_notice = h.notices()
        code2, body2 = h.status(trip_id, ref, "DELAYED", **extra)
        ok2 = (
            code2 == 200
            and body2["observation"]["verdict"] == "NOTIFY_ONLY"
            and h.audits(task_id, "FLIGHT_STATUS_OBSERVED") == before_audit
            and h.notices() == before_notice
        )
        results.append(
            CaseResult(
                "dedupe",
                f"dedupe_{index:02d}",
                run,
                ok2,
                "same report again: no new audit, no new notice",
                f"{code2} audits {h.audits(task_id, 'FLIGHT_STATUS_OBSERVED')} "
                f"notices {h.notices()}",
                {"mode": mode, "minutes": minutes},
            )
        )


def category_delay_small(h: Harness, run: int, results: list[CaseResult]) -> None:
    """变化在 15 分钟以内：记观察，不通知。"""
    booked = h.book()
    trip_id, ref = booked["trip_id"], booked["refs"][0]
    for minutes in range(1, 11):
        before_notice = h.notices()
        code, body = h.status(
            trip_id,
            ref,
            "DELAYED",
            estimated_arrive_at=iso(OUT_ARRIVE + timedelta(minutes=minutes)),
        )
        observation = body.get("observation", {})
        ok = (
            code == 200
            and observation.get("verdict") == "NO_CHANGE"
            and observation.get("opened_task_id") is None
            and h.notices() == before_notice
            and body["trip"]["status"] == "BOOKED"
        )
        results.append(
            CaseResult(
                "delay_small",
                f"delay_small_{minutes:02d}",
                run,
                ok,
                "NO_CHANGE, no notice",
                f"{code} {observation.get('verdict')}",
                {"minutes": minutes, "reasons": observation.get("reasons")},
            )
        )


def _expect_rebook(
    h: Harness,
    category: str,
    case_id: str,
    run: int,
    booked: dict,
    ref: str,
    code: int,
    body: dict,
    detail: dict,
) -> CaseResult:
    observation = body.get("observation", {})
    opened = observation.get("opened_task_id")
    change = h.task(opened) if opened else {}
    original = h.task(booked["task_id"])
    offered = {leg["ref_id"] for item in change.get("options", []) for leg in item["legs"]}
    trip = body.get("trip", {})
    ok = (
        code == 200
        and observation.get("verdict") == "REBOOK_REQUIRED"
        and bool(opened)
        and trip.get("status") == "CHANGE_REQUESTED"
        and change.get("is_change_task") is True
        and change.get("parent_task_id") == booked["task_id"]
        and change.get("change_event", {}).get("excluded_refs") == [ref]
        and ref not in offered
        and (change.get("change_event", {}).get("impact") or {}).get("verdict") == "REBOOK_REQUIRED"
        and original.get("state") == "BOOKING_CONFIRMED"
        and trip.get("events", [{}])[-1].get("reported_by") == "flight-status:eval-feed"
    )
    return CaseResult(
        category,
        case_id,
        run,
        ok,
        "REBOOK_REQUIRED, change task with ticket excluded, original untouched",
        f"{code} {observation.get('verdict')} trip={trip.get('status')} "
        f"change={change.get('state')}",
        {
            **detail,
            "reasons": observation.get("reasons"),
            "options": len(change.get("options", [])),
        },
    )


def category_delay_rebook(h: Harness, run: int, results: list[CaseResult]) -> None:
    """去程新到达晚于 08-06 09:00（时限减缓冲）：开改期任务。每条一趟新差旅。"""
    arrivals = [
        "09:01",
        "09:05",
        "09:15",
        "09:30",
        "10:00",
        "10:30",
        "11:00",
        "12:00",
        "13:00",
        "14:00",
    ]
    for index, clock in enumerate(arrivals, start=1):
        booked = h.book()
        ref = booked["refs"][0]
        arrive = datetime.fromisoformat(f"2026-08-06T{clock}:00+08:00")
        code, body = h.status(
            booked["trip_id"],
            ref,
            "DELAYED",
            estimated_depart_at=iso(arrive - timedelta(hours=2)),
            estimated_arrive_at=iso(arrive),
        )
        results.append(
            _expect_rebook(
                h,
                "delay_rebook",
                f"delay_rebook_{index:02d}",
                run,
                booked,
                ref,
                code,
                body,
                {"new_arrival": clock},
            )
        )


def category_cancelled(h: Harness, run: int, results: list[CaseResult]) -> None:
    """取消：去程 5 条、返程 5 条，都开改期任务，被取消的票不再端上来。"""
    for index in range(1, 11):
        booked = h.book()
        leg = 0 if index <= 5 else 1
        ref = booked["refs"][leg]
        code, body = h.status(booked["trip_id"], ref, "CANCELLED", note=f"eval cancel {index}")
        results.append(
            _expect_rebook(
                h,
                "cancelled",
                f"cancelled_{index:02d}",
                run,
                booked,
                ref,
                code,
                body,
                {"leg": leg, "ref": ref},
            )
        )


def category_return_leg(h: Harness, run: int, results: list[CaseResult]) -> None:
    """返程（不留缓冲，窗口到 23:00）：10 条混合期望。"""
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("DELAYED", {"estimated_arrive_at": iso(RET_ARRIVE + timedelta(minutes=10))}, "NO_CHANGE"),
        (
            "DELAYED",
            {"estimated_arrive_at": iso(RET_ARRIVE + timedelta(minutes=20))},
            "NOTIFY_ONLY",
        ),
        (
            "DELAYED",
            {"estimated_arrive_at": iso(RET_ARRIVE + timedelta(minutes=60))},
            "NOTIFY_ONLY",
        ),
        (
            "DELAYED",
            {"estimated_arrive_at": iso(RET_ARRIVE + timedelta(minutes=150))},
            "NOTIFY_ONLY",
        ),
        (
            "DELAYED",
            {"estimated_arrive_at": iso(RET_ARRIVE + timedelta(minutes=161))},
            "REBOOK_REQUIRED",
        ),
        (
            "DELAYED",
            {"estimated_arrive_at": iso(RET_ARRIVE + timedelta(minutes=180))},
            "REBOOK_REQUIRED",
        ),
        (
            "DELAYED",
            {"estimated_arrive_at": iso(RET_ARRIVE - timedelta(minutes=30))},
            "NOTIFY_ONLY",
        ),
        (
            "DELAYED",
            {"estimated_depart_at": iso(RET_DEPART + timedelta(minutes=30))},
            "NOTIFY_ONLY",
        ),
        ("DEPARTED", {}, "NO_CHANGE"),
        ("LANDED", {}, "NO_CHANGE"),
    ]
    for index, (status, extra, expected) in enumerate(cases, start=1):
        booked = h.book()
        ref = booked["refs"][1]
        code, body = h.status(booked["trip_id"], ref, status, **extra)
        observation = body.get("observation", {})
        verdict = observation.get("verdict")
        opened = observation.get("opened_task_id")
        trip_status = body.get("trip", {}).get("status")
        if expected == "REBOOK_REQUIRED":
            ok = verdict == expected and bool(opened) and trip_status == "CHANGE_REQUESTED"
        else:
            ok = verdict == expected and opened is None and trip_status == "BOOKED"
        results.append(
            CaseResult(
                "return_leg",
                f"return_leg_{index:02d}",
                run,
                ok and code == 200,
                expected,
                f"{code} {verdict} trip={trip_status}",
                {"status": status, **extra, "reasons": observation.get("reasons")},
            )
        )


def category_informational(h: Harness, run: int, results: list[CaseResult]) -> None:
    """查不到 / 已起飞 / 已落地 / 按计划：什么都不做。"""
    statuses = [
        "UNKNOWN",
        "DEPARTED",
        "LANDED",
        "SCHEDULED",
        "UNKNOWN",
        "DEPARTED",
        "LANDED",
        "SCHEDULED",
        "UNKNOWN",
        "LANDED",
    ]
    for index, status in enumerate(statuses, start=1):
        booked = h.book()
        before_notice = h.notices()
        code, body = h.status(booked["trip_id"], booked["refs"][index % 2], status)
        observation = body.get("observation", {})
        ok = (
            code == 200
            and observation.get("verdict") == "NO_CHANGE"
            and observation.get("opened_task_id") is None
            and body["trip"]["status"] == "BOOKED"
            and h.notices() == before_notice
        )
        results.append(
            CaseResult(
                "informational",
                f"informational_{index:02d}",
                run,
                ok,
                "NO_CHANGE, no task, no notice",
                f"{code} {observation.get('verdict')}",
                {"status": status, "reasons": observation.get("reasons")},
            )
        )


def category_webhook(h: Harness, run: int, results: list[CaseResult]) -> None:
    """入站推送的门：密钥、签名、请求体形状、票号命中。"""
    booked = h.book()
    ref = booked["refs"][0]
    good = json.dumps(
        {
            "ref_id": ref,
            "status": "DELAYED",
            "estimated_arrive_at": iso(OUT_ARRIVE + timedelta(minutes=20)),
            "source": "tmc",
        }
    )

    def check(
        case_id: str, expected: str, ok: bool, actual: str, detail: dict | None = None
    ) -> None:
        results.append(CaseResult("webhook", case_id, run, ok, expected, actual, detail or {}))

    saved = os.environ.pop("FLIGHT_STATUS_WEBHOOK_SECRET")
    code, _ = h.webhook(good)
    os.environ["FLIGHT_STATUS_WEBHOOK_SECRET"] = saved
    check("webhook_01_no_secret", "503", code == 503, str(code))
    code, _ = h.webhook(good, signature="deadbeef")
    check("webhook_02_bad_signature", "401", code == 401, str(code))
    code, _ = h.webhook(good, signature="")
    check("webhook_03_missing_signature", "401", code == 401, str(code))
    other = hmac.new(SECRET, b"other body", hashlib.sha256).hexdigest()
    code, _ = h.webhook(good, signature=other)
    check("webhook_04_signature_over_other_body", "401", code == 401, str(code))
    code, body = h.webhook(json.dumps({"ref_id": "NOPE", "status": "CANCELLED"}))
    check("webhook_05_unknown_ticket", "404", code == 404, str(code))
    code, body = h.webhook("{not json")
    check("webhook_06_malformed_json", "422", code == 422, str(code))
    code, body = h.webhook(json.dumps({"ref_id": ref, "status": "EXPLODED"}))
    check("webhook_07_invalid_status", "422", code == 422, str(code))
    code, body = h.webhook(json.dumps({"ref_id": ref, "status": "DELAYED", "bogus": 1}))
    check("webhook_08_extra_field", "422", code == 422, str(code))
    code, body = h.webhook(good)
    mine = next(
        (r for r in body.get("results", []) if r["trip"]["trip_id"] == booked["trip_id"]), None
    )
    check(
        "webhook_09_by_ticket_fans_out",
        "200, my trip among results, NOTIFY_ONLY",
        code == 200 and mine is not None and mine["observation"]["verdict"] == "NOTIFY_ONLY",
        f"{code} trip_count={body.get('trip_count') if isinstance(body, dict) else body}",
    )
    with_trip = json.dumps(
        {"trip_id": booked["trip_id"], "ref_id": ref, "status": "CANCELLED", "source": "tmc"}
    )
    code, body = h.webhook(with_trip)
    only = body.get("results", [{}])[0] if isinstance(body, dict) else {}
    check(
        "webhook_10_by_trip_id_opens_change",
        "200, trip_count 1, REBOOK_REQUIRED, change task",
        code == 200
        and body.get("trip_count") == 1
        and only.get("observation", {}).get("verdict") == "REBOOK_REQUIRED"
        and bool(only.get("observation", {}).get("opened_task_id")),
        f"{code} {only.get('observation', {}).get('verdict')}",
    )


def category_worker(h: Harness, run: int, results: list[CaseResult]) -> None:
    """worker 轮询：只领到点的差旅；动态源说什么就判什么；领完释放、重排。四轮覆盖 10 趟。"""
    trips = [h.book() for _ in range(10)]
    ref = trips[0]["refs"][0]
    rounds = [
        (
            trips[0:3],
            FlightStatusReport(
                ref,
                FlightStatusKind.DELAYED,
                DEMO_CLOCK,
                "memory",
                estimated_arrive_at=OUT_ARRIVE + timedelta(minutes=30),
            ),
            "NOTIFY_ONLY",
            "BOOKED",
        ),
        (
            trips[3:6],
            FlightStatusReport(ref, FlightStatusKind.SCHEDULED, DEMO_CLOCK, "memory"),
            "NO_CHANGE",
            "BOOKED",
        ),
        (
            trips[6:8],
            FlightStatusReport(ref, FlightStatusKind.CANCELLED, DEMO_CLOCK, "memory"),
            "REBOOK_REQUIRED",
            "CHANGE_REQUESTED",
        ),
        (
            trips[8:10],
            FlightStatusReport(
                ref,
                FlightStatusKind.DELAYED,
                DEMO_CLOCK,
                "memory",
                estimated_arrive_at=datetime.fromisoformat("2026-08-06T09:30:00+08:00"),
            ),
            "REBOOK_REQUIRED",
            "CHANGE_REQUESTED",
        ),
    ]
    counter = 0
    # 观察对象排的第一次检查是起飞前 48 小时（08-03 06:20）；把时钟拨到那时，逐轮把"该查的"排到点。
    h.clock["now"] = OUT_DEPART - timedelta(hours=48)
    for due, report, verdict, trip_status in rounds:
        h.source.record(report)
        for item in due:
            trip = h.workflow.trips.get(item["trip_id"])
            trip.watch = replace(trip.watch, next_check_at=h.clock["now"])
            h.workflow.trips.save(trip)
        # 仓库里别的差旅（前面各类订的）next_check_at 也是 08-03 06:20：为了只领这一轮的，
        # 把它们统统推后一小时。
        due_ids = {d["trip_id"] for d in due}
        for trip in h.workflow.trips.list_all(limit=10_000):
            if trip.trip_id in due_ids or trip.watch is None:
                continue
            if trip.status.value in {"BOOKED", "REBOOKED"} and (
                trip.watch.next_check_at is not None and trip.watch.next_check_at <= h.clock["now"]
            ):
                trip.watch = replace(trip.watch, next_check_at=h.clock["now"] + timedelta(hours=1))
                h.workflow.trips.save(trip)
        processed = set(h.workflow.process_due_flight_checks(limit=50))
        for item in due:
            counter += 1
            current = h.trip(item["trip_id"])
            observation = next(
                (o for o in current["watch"]["observations"] if o["ref_id"] == ref), None
            )
            lease_free = item["trip_id"] not in getattr(h.workflow.trips, "_watch_leases", {})
            ok = (
                item["trip_id"] in processed
                and processed == {d["trip_id"] for d in due}
                and observation is not None
                and observation["verdict"] == verdict
                and current["status"] == trip_status
                and lease_free
                and (
                    current["watch"]["next_check_at"] is None
                    or datetime.fromisoformat(current["watch"]["next_check_at"]) > h.clock["now"]
                    or current["status"] == "CHANGE_REQUESTED"
                )
            )
            results.append(
                CaseResult(
                    "worker",
                    f"worker_{counter:02d}",
                    run,
                    ok,
                    f"claimed exactly the due trips; {verdict}; {trip_status}; lease released",
                    f"processed={len(processed)} verdict={observation and observation['verdict']} "
                    f"status={current['status']}",
                    {"round_status": report.status.value},
                )
            )
        h.source.clear(ref)
    h.clock["now"] = DEMO_CLOCK


def category_cancel_trip(h: Harness, run: int, results: list[CaseResult]) -> None:
    """取消整趟：各种状态下能不能取消，取消之后什么都不再接受。"""

    def check(case_id: str, expected: str, ok: bool, actual: str) -> None:
        results.append(CaseResult("cancel_trip", case_id, run, ok, expected, actual))

    planned = h.client.post("/trip-tasks", json=BOOK).json()
    r = h.client.post(f"/trips/{planned['trip_id']}/cancel", json={"reason": "planned"})
    check(
        "cancel_01_planned",
        "200 CANCELLED",
        r.status_code == 200 and r.json()["status"] == "CANCELLED",
        f"{r.status_code}",
    )
    booked = h.book()
    r = h.client.post(f"/trips/{booked['trip_id']}/cancel", json={"reason": "项目取消"})
    body = r.json()
    check(
        "cancel_02_booked",
        "200 CANCELLED, TRIP_CANCELLED event, next_check_at None",
        r.status_code == 200
        and body["status"] == "CANCELLED"
        and body["events"][-1]["event_type"] == "TRIP_CANCELLED"
        and body["watch"]["next_check_at"] is None,
        f"{r.status_code} {body.get('status')}",
    )
    r = h.client.post(f"/trips/{booked['trip_id']}/cancel", json={})
    check("cancel_03_twice", "409", r.status_code == 409, str(r.status_code))
    r = h.client.post(
        f"/trips/{booked['trip_id']}/events",
        json={"event_type": "MEETING_MOVED", "new_arrive_by": "2026-08-07T10:00:00+08:00"},
    )
    check("cancel_04_event_after_cancel", "409", r.status_code == 409, str(r.status_code))
    code, _ = h.status(booked["trip_id"], booked["refs"][0], "CANCELLED")
    check("cancel_05_flight_status_after_cancel", "409", code == 409, str(code))
    care = h.client.get("/duty-of-care").json()
    check(
        "cancel_06_not_on_duty_of_care",
        "absent",
        booked["trip_id"] not in {row["trip_id"] for row in care["travelers"]},
        "present/absent",
    )
    check(
        "cancel_07_task_state_untouched",
        "BOOKING_CONFIRMED",
        h.task(booked["task_id"])["state"] == "BOOKING_CONFIRMED",
        h.task(booked["task_id"])["state"],
    )
    check(
        "cancel_08_audit_and_outbox",
        "TRIP_CANCELLED audit + outbox",
        h.audits(booked["task_id"], "TRIP_CANCELLED") == 1
        and any(
            i.event_type == "TRIP_CANCELLED"
            for i in h.workflow.tasks.outbox.list_unpublished(limit=10_000)
        ),
        "checked",
    )
    r = h.client.post(
        f"/agentic/trip-tasks/{booked['task_id']}/messages", json={"message": "这趟不去了"}
    )
    check(
        "cancel_09_chat_after_cancel",
        "409 or 503 without model",
        r.status_code in {409, 503},
        str(r.status_code),
    )
    changing = h.book()
    h.client.post(
        f"/trips/{changing['trip_id']}/events",
        json={"event_type": "MEETING_MOVED", "new_arrive_by": "2026-08-07T10:00:00+08:00"},
    )
    r = h.client.post(f"/trips/{changing['trip_id']}/cancel", json={"reason": "中途取消"})
    check(
        "cancel_10_while_change_in_progress",
        "200 CANCELLED",
        r.status_code == 200 and r.json()["status"] == "CANCELLED",
        str(r.status_code),
    )


def category_events_api(h: Harness, run: int, results: list[CaseResult]) -> None:
    """表单路径 `POST /trips/{id}/events`：会议改期改哪一段、窗口平移、各种拒绝。"""

    def check(
        case_id: str, expected: str, ok: bool, actual: str, detail: dict | None = None
    ) -> None:
        results.append(CaseResult("events_api", case_id, run, ok, expected, actual, detail or {}))

    def move(trip_id: str, **payload: Any):
        return h.client.post(
            f"/trips/{trip_id}/events", json={"event_type": "MEETING_MOVED", **payload}
        )

    b = h.book()
    r = move(b["trip_id"], new_arrive_by="2026-08-07T10:00:00+08:00")
    t = r.json()
    check(
        "events_01_leg0_default",
        "change task, arrive_by 08-07 10:00, departure_after shifted +1d",
        r.status_code == 200
        and t["is_change_task"]
        and t["transport_legs"][0]["arrive_before"].startswith("2026-08-07T10:00")
        and t["transport_legs"][0]["depart_after"].startswith("2026-08-06T05:00"),
        f"{r.status_code} {t.get('transport_legs', [{}])[0]}",
    )
    b = h.book()
    r = move(b["trip_id"], new_arrive_by="2026-08-07T22:00:00+08:00", leg_index=1)
    t = r.json()
    check(
        "events_02_return_leg",
        "return_before 08-07 22:00, outbound untouched, return_after shifted",
        r.status_code == 200
        and t["transport_legs"][1]["arrive_before"].startswith("2026-08-07T22:00")
        and t["transport_legs"][0]["arrive_before"].startswith("2026-08-06T10:00")
        and t["transport_legs"][1]["depart_after"].startswith("2026-08-07T16:00"),
        f"{r.status_code} {t.get('transport_legs')}",
    )
    b = h.book()
    r = move(b["trip_id"], new_arrive_by="2026-08-07T10:00:00+08:00", leg_index=7)
    check("events_03_leg_out_of_range", "409", r.status_code == 409, str(r.status_code))
    r = move(b["trip_id"])
    check("events_04_missing_new_arrive_by", "409", r.status_code == 409, str(r.status_code))
    r = h.client.post(
        f"/trips/{b['trip_id']}/events", json={"event_type": "FLIGHT_CHANGED", "ref_id": "NOPE"}
    )
    check(
        "events_05_flight_changed_unknown_ticket", "409", r.status_code == 409, str(r.status_code)
    )
    r = h.client.post(f"/trips/{b['trip_id']}/events", json={"event_type": "TRIP_CANCELLED"})
    check("events_06_trip_cancelled_as_event", "409", r.status_code == 409, str(r.status_code))
    r = h.client.post(
        f"/trips/{b['trip_id']}/events",
        json={"event_type": "FLIGHT_CHANGED", "ref_id": b["refs"][1], "note": "manual"},
    )
    t = r.json()
    check(
        "events_07_flight_changed_manual",
        "change task, return ticket excluded",
        r.status_code == 200
        and t["change_event"]["excluded_refs"] == [b["refs"][1]]
        and t["change_event"]["impact"] is None,
        f"{r.status_code}",
    )
    r = move(b["trip_id"], new_arrive_by="2026-08-08T10:00:00+08:00")
    check("events_08_while_change_in_progress", "409", r.status_code == 409, str(r.status_code))
    b = h.book()
    r = move(b["trip_id"], new_arrive_by="2026-08-04T16:00:00+08:00")
    t = r.json()
    check(
        "events_09_moved_earlier",
        "arrive_by 08-04 16:00, departure_after shifted back, still < arrive_by",
        r.status_code == 200
        and t["transport_legs"][0]["arrive_before"].startswith("2026-08-04T16:00")
        and t["transport_legs"][0]["depart_after"] < t["transport_legs"][0]["arrive_before"],
        f"{r.status_code} {t.get('transport_legs', [{}])[0]}",
    )
    b = h.book()
    r = move(b["trip_id"], new_arrive_by="2026-08-07T10:00:00+08:00", leg_index=0)
    trip = h.trip(b["trip_id"])
    check(
        "events_10_event_recorded_with_leg_index",
        "trip CHANGE_REQUESTED, event leg_index 0",
        r.status_code == 200
        and trip["status"] == "CHANGE_REQUESTED"
        and trip["events"][0]["leg_index"] == 0
        and trip["events"][0]["opened_task_id"] == r.json()["task_id"],
        f"{trip['status']}",
    )


# ---------------------------------------------------------------------------
# 真模型：聊天变更
# ---------------------------------------------------------------------------

CHAT_MEETING_MOVED: list[tuple[str, int, str]] = [
    ("客户把会议改到8月7号上午10点前了，帮我改一下", 0, "2026-08-07T10:00:00+08:00"),
    ("会议推迟到8月8日，下午2点前到就行", 0, "2026-08-08T14:00:00+08:00"),
    ("改成8月6号早上9点到", 0, "2026-08-06T09:00:00+08:00"),
    ("Meeting moved to Aug 9, I need to arrive by 11am", 0, "2026-08-09T11:00:00+08:00"),
    ("会议提前到8月4号下午4点", 0, "2026-08-04T16:00:00+08:00"),
    ("8/7 10:00 前到上海就行", 0, "2026-08-07T10:00:00+08:00"),
    ("会议改到2026年8月10日中午12点前", 0, "2026-08-10T12:00:00+08:00"),
    ("返程改一下，8月7号晚上10点前到北京就行", 1, "2026-08-07T22:00:00+08:00"),
    ("客户说改到7号上午十点", 0, "2026-08-07T10:00:00+08:00"),
    ("帮我把到达时间改到8月8号上午9点半前", 0, "2026-08-08T09:30:00+08:00"),
]

CHAT_CANCEL = [
    "这趟不去了",
    "行程取消，项目黄了",
    "不用去上海了，把这趟差旅取消",
    "Cancel this trip, the client postponed indefinitely",
    "老板说不去了",
    "取消吧",
    "我请假了，这次出差不去了",
    "这趟差旅作废",
    "客户取消会议，行程取消",
    "不去了，退票我自己办",
]

#: (原话, 期望被排除的票：0 去程 / 1 返程 / None 任一)
CHAT_FLIGHT_CHANGED: list[tuple[str, int | None]] = [
    ("航司短信说我8月5号那班取消了", 0),
    ("我的去程航班被取消了", 0),
    ("东航通知航班取消，帮我重新订", None),
    ("My outbound flight got cancelled by the airline", 0),
    ("刚收到通知8月6号回北京的航班取消了，换一班", 1),
    ("航空公司说去上海这班不飞了", 0),
    ("返程那张票被航司取消了，重新找一班", 1),
    ("去程航班取消，需要改签", 0),
    ("航司把我8月5号早上的航班砍了", 0),
    ("收到取消短信，回程没了，帮我看别的航班", 1),
]

#: (原话, 也接受的改期结果：None = 只接受追问)
CHAT_ASK: list[tuple[str, str | None]] = [
    ("情况有变", None),
    ("会议可能要改时间", None),
    ("帮我订个酒店", None),
    ("航班会不会延误？", None),
    ("改到下周", None),
    ("能不能换个时间", None),
    ("帮我写个周报", None),
    ("我想改一下", None),
    ("客户那边有变动", None),
    ("延后两天", "2026-08-08T10:00:00+08:00"),
]


def _timed_chat(h: Harness, task_id: str, message: str) -> tuple[int, dict, dict]:
    calls0, in0, out0 = h.llm_snapshot()
    started = time.monotonic()
    code, body = h.chat(task_id, message)
    seconds = time.monotonic() - started
    calls1, in1, out1 = h.llm_snapshot()
    usage = {
        "seconds": round(seconds, 2),
        "llm_calls": calls1 - calls0,
        "input_tokens": in1 - in0,
        "output_tokens": out1 - out0,
    }
    if h.price is not None:
        usage["cost_usd"] = round(
            (usage["input_tokens"] * h.price.input + usage["output_tokens"] * h.price.output) / 1e6,
            6,
        )
    return code, body, usage


def _with_usage(result: CaseResult, usage: dict) -> CaseResult:
    result.seconds = usage["seconds"]
    result.llm_calls = usage["llm_calls"]
    result.input_tokens = usage["input_tokens"]
    result.output_tokens = usage["output_tokens"]
    result.cost_usd = usage.get("cost_usd")
    return result


def category_chat_meeting_moved(h: Harness, run: int, results: list[CaseResult]) -> None:
    for index, (message, leg, expected_iso) in enumerate(CHAT_MEETING_MOVED, start=1):
        booked = h.book()
        code, body, usage = _timed_chat(h, booked["task_id"], message)
        expected = datetime.fromisoformat(expected_iso)
        legs = body.get("transport_legs") or []
        event = body.get("change_event") if isinstance(body.get("change_event"), dict) else {}
        got = legs[leg]["arrive_before"] if body.get("is_change_task") and len(legs) > leg else None
        ok = (
            code == 200
            and body.get("is_change_task") is True
            and body.get("change_event", {}).get("event_type") == "MEETING_MOVED"
            and (body.get("change_event", {}).get("leg_index") or 0) == leg
            and got is not None
            and datetime.fromisoformat(got) == expected
        )
        reply = (
            (body.get("messages") or [{}])[-1].get("content") if code == 200 else body.get("detail")
        )
        results.append(
            _with_usage(
                CaseResult(
                    "chat_meeting_moved",
                    f"chat_move_{index:02d}",
                    run,
                    ok,
                    f"MEETING_MOVED leg {leg} arrive_by {expected_iso}",
                    f"{code} change={body.get('is_change_task')} kind={event.get('event_type')} "
                    f"leg={event.get('leg_index')} got={got}",
                    {"message": message, "reply": reply},
                ),
                usage,
            )
        )


def category_chat_cancel(h: Harness, run: int, results: list[CaseResult]) -> None:
    for index, message in enumerate(CHAT_CANCEL, start=1):
        booked = h.book()
        code, body, usage = _timed_chat(h, booked["task_id"], message)
        trip = h.trip(booked["trip_id"])
        ok = (
            code == 200
            and body.get("task_id") == booked["task_id"]
            and trip["status"] == "CANCELLED"
            and (body.get("messages") or [{}])[-1].get("role") == "assistant"
        )
        results.append(
            _with_usage(
                CaseResult(
                    "chat_cancel",
                    f"chat_cancel_{index:02d}",
                    run,
                    ok,
                    "same task, trip CANCELLED, assistant reply",
                    f"{code} trip={trip['status']} change={body.get('is_change_task')}",
                    {
                        "message": message,
                        "reply": (body.get("messages") or [{}])[-1].get("content"),
                    },
                ),
                usage,
            )
        )


def category_chat_flight_changed(h: Harness, run: int, results: list[CaseResult]) -> None:
    for index, (message, leg) in enumerate(CHAT_FLIGHT_CHANGED, start=1):
        booked = h.book()
        code, body, usage = _timed_chat(h, booked["task_id"], message)
        excluded = (
            body.get("change_event", {}).get("excluded_refs")
            if isinstance(body.get("change_event"), dict)
            else None
        )
        wanted = [booked["refs"][leg]] if leg is not None else None
        ok = (
            code == 200
            and body.get("is_change_task") is True
            and body.get("change_event", {}).get("event_type") == "FLIGHT_CHANGED"
            and excluded is not None
            and len(excluded) == 1
            and excluded[0] in booked["refs"]
            and (wanted is None or excluded == wanted)
        )
        results.append(
            _with_usage(
                CaseResult(
                    "chat_flight_changed",
                    f"chat_flight_{index:02d}",
                    run,
                    ok,
                    f"FLIGHT_CHANGED excluding {'any leg' if wanted is None else wanted[0]}",
                    f"{code} change={body.get('is_change_task')} excluded={excluded}",
                    {
                        "message": message,
                        "reply": (body.get("messages") or [{}])[-1].get("content"),
                    },
                ),
                usage,
            )
        )


def category_chat_ask(h: Harness, run: int, results: list[CaseResult]) -> None:
    for index, (message, accept_move) in enumerate(CHAT_ASK, start=1):
        booked = h.book()
        code, body, usage = _timed_chat(h, booked["task_id"], message)
        trip = h.trip(booked["trip_id"])
        asked = (
            code == 200
            and body.get("task_id") == booked["task_id"]
            and trip["status"] == "BOOKED"
            and (body.get("messages") or [{}])[-1].get("role") == "assistant"
        )
        moved_ok = False
        if accept_move and code == 200 and body.get("is_change_task"):
            legs = body.get("transport_legs") or []
            moved_ok = bool(legs) and datetime.fromisoformat(
                legs[0]["arrive_before"]
            ) == datetime.fromisoformat(accept_move)
        ok = asked or moved_ok
        results.append(
            _with_usage(
                CaseResult(
                    "chat_ask",
                    f"chat_ask_{index:02d}",
                    run,
                    ok,
                    "ask a question, trip stays BOOKED"
                    + (f" (or move to {accept_move})" if accept_move else ""),
                    f"{code} trip={trip['status']} change={body.get('is_change_task')}",
                    {
                        "message": message,
                        "reply": (body.get("messages") or [{}])[-1].get("content")
                        if not body.get("is_change_task")
                        else f"opened change task {body.get('task_id')}",
                    },
                ),
                usage,
            )
        )


DETERMINISTIC = {
    "delay_notify": category_delay_notify,  # 也产出 dedupe
    "delay_small": category_delay_small,
    "delay_rebook": category_delay_rebook,
    "cancelled": category_cancelled,
    "return_leg": category_return_leg,
    "informational": category_informational,
    "webhook": category_webhook,
    "worker": category_worker,
    "cancel_trip": category_cancel_trip,
    "events_api": category_events_api,
}
CHAT = {
    "chat_meeting_moved": category_chat_meeting_moved,
    "chat_cancel": category_chat_cancel,
    "chat_flight_changed": category_chat_flight_changed,
    "chat_ask": category_chat_ask,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--offline", action="store_true", help="skip the real-model chat categories"
    )
    parser.add_argument("--categories", default="", help="comma-separated subset")
    parser.add_argument(
        "--price-table", default="evals/pricing/model-prices-openai-20260802-v1.json"
    )
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    h = Harness()
    categories = dict(DETERMINISTIC)
    if not args.offline:
        if h.model is None:
            sys.exit("no tool-calling model configured: set OPENAI_API_KEY or pass --offline")
        categories.update(CHAT)
        try:
            from corporate_travel_agent.evaluation.performance import (
                load_model_price_table,
            )

            table, _ = load_model_price_table(args.price_table)
            h.price = table.models.get(getattr(h.model, "model", ""))
        except Exception:  # noqa: BLE001 - 价目表只是锦上添花
            h.price = None
    if args.categories:
        wanted = {name.strip() for name in args.categories.split(",") if name.strip()}
        categories = {k: v for k, v in categories.items() if k in wanted}

    results: list[CaseResult] = []
    started_at = datetime.now(UTC)
    with (output / "results.jsonl").open("w", encoding="utf-8") as sink:
        for run in range(1, args.runs + 1):
            for name, runner in categories.items():
                t0 = time.monotonic()
                batch: list[CaseResult] = []
                try:
                    runner(h, run, batch)
                except Exception as exc:  # noqa: BLE001 - 一类崩了别拖累整轮，已经跑完的用例照记
                    batch.append(
                        CaseResult(
                            name,
                            f"{name}_crash",
                            run,
                            False,
                            "runner completes",
                            f"{type(exc).__name__}: {exc}"[:400],
                        )
                    )
                for item in batch:
                    results.append(item)
                    sink.write(json.dumps(asdict(item), ensure_ascii=False, default=str) + "\n")
                sink.flush()
                passed = sum(1 for item in batch if item.ok)
                print(
                    f"[run {run}] {name:<20} {passed:>3}/{len(batch):<3} "
                    f"{time.monotonic() - t0:6.1f}s",
                    flush=True,
                )

    summary = summarize(results, args.runs, h, started_at)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    )
    (output / "REPORT.md").write_text(render_report(summary, results, args), encoding="utf-8")
    print(f"\nreport: {output / 'REPORT.md'}")


def summarize(results: list[CaseResult], runs: int, h: Harness, started_at: datetime) -> dict:
    by_category: dict[str, dict[str, Any]] = {}
    for item in results:
        row = by_category.setdefault(item.category, {"cases": {}, "runs": {}})
        row["runs"].setdefault(item.run, [0, 0])
        row["runs"][item.run][1] += 1
        if item.ok:
            row["runs"][item.run][0] += 1
        row["cases"].setdefault(item.case_id, []).append(item.ok)
    table = {}
    for name, row in by_category.items():
        cases = row["cases"]
        table[name] = {
            "cases": len(cases),
            "per_run": {str(run): f"{p}/{t}" for run, (p, t) in sorted(row["runs"].items())},
            "pass_all_runs": sum(1 for oks in cases.values() if all(oks)),
            "pass_any_run": sum(1 for oks in cases.values() if any(oks)),
            "failed_cases": sorted(cid for cid, oks in cases.items() if not all(oks)),
        }
    llm = [item for item in results if item.llm_calls]
    return {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "runs": runs,
        "model": getattr(h.model, "model", None),
        "prompt_version": getattr(h.model, "prompt_version", None),
        "trips_booked": h.booked,
        "categories": table,
        "llm": {
            "cases": len(llm),
            "calls": sum(i.llm_calls for i in llm),
            "input_tokens": sum(i.input_tokens for i in llm),
            "output_tokens": sum(i.output_tokens for i in llm),
            "cost_usd_upper_bound": round(sum(i.cost_usd or 0.0 for i in llm), 4)
            if h.price
            else None,
            "median_seconds": sorted(i.seconds for i in llm)[len(llm) // 2] if llm else None,
        },
    }


def render_report(summary: dict, results: list[CaseResult], args: argparse.Namespace) -> str:
    lines = [
        "# 订好之后的追踪与变更（D20）评测报告",
        "",
        f"- 开始 {summary['started_at']}，结束 {summary['finished_at']}，{summary['runs']} 轮",
        "- 装配：演示库存 + 钉住的时钟（2026-08-01）+ 内存仓储 + 进程内动态源；"
        f"订了 {summary['trips_booked']} 趟",
        f"- 聊天场景模型：{summary['model'] or '未跑'}（提示词 {summary['prompt_version']}）",
        "",
        "## 按类汇总",
        "",
        "| 类 | 条数 | "
        + " | ".join(f"第 {r} 轮" for r in range(1, summary["runs"] + 1))
        + " | 每轮都过 | 未全过的用例 |",
        "|---|---|" + "---|" * summary["runs"] + "---|---|",
    ]
    for name, row in summary["categories"].items():
        per_run = " | ".join(row["per_run"].get(str(r), "-") for r in range(1, summary["runs"] + 1))
        failed = "、".join(row["failed_cases"]) or "—"
        lines.append(
            f"| `{name}` | {row['cases']} | {per_run} | "
            f"{row['pass_all_runs']}/{row['cases']} | {failed} |"
        )
    llm = summary["llm"]
    if llm["cases"]:
        lines += [
            "",
            "## 真模型开销",
            "",
            f"- {llm['cases']} 条聊天用例，{llm['calls']} 次模型调用，"
            f"输入 {llm['input_tokens']} / 输出 {llm['output_tokens']} token",
            f"- 价目表上界 ${llm['cost_usd_upper_bound']}（cache-miss 口径）；"
            f"单条中位耗时 {llm['median_seconds']} 秒",
        ]
    failures = [item for item in results if not item.ok]
    lines += ["", "## 未通过的用例", ""]
    if not failures:
        lines.append("无。")
    for item in failures:
        lines.append(
            f"- **{item.case_id}**（第 {item.run} 轮）期望 `{item.expected}`，实际 `{item.actual}`"
        )
        if item.detail.get("message"):
            lines.append(f"  - 原话：{item.detail['message']}")
        if item.detail.get("reply"):
            lines.append(f"  - 回复：{str(item.detail['reply'])[:200]}")
    chat_items = [item for item in results if item.category.startswith("chat_")]
    if chat_items:
        lines += [
            "",
            "## 聊天用例明细（第 1 轮）",
            "",
            "| 用例 | 原话 | 结果 | 实际 | 回复 / 说明 |",
            "|---|---|---|---|---|",
        ]
        for item in chat_items:
            if item.run != 1:
                continue
            reply = str(item.detail.get("reply") or "").replace("|", "／").replace("\n", " ")[:90]
            lines.append(
                f"| `{item.case_id}` | {item.detail.get('message', '')} | "
                f"{'✅' if item.ok else '❌'} | "
                f"{item.actual.replace('|', '／')} | {reply} |"
            )
    lines += [
        "",
        "## 判据",
        "",
        "- `delay_notify` / `dedupe`：去程延误但新到达仍早于 08-06 09:00（时限减 60 分钟缓冲）"
        "→ `NOTIFY_ONLY`，差旅仍 `BOOKED`，审计和发件箱各 +1；同一条再报一次不再 +1。",
        "- `delay_small`：变化 ≤ 10 分钟 → `NO_CHANGE`，不进发件箱。",
        "- `delay_rebook` / `cancelled`：`REBOOK_REQUIRED`，开改期任务，被影响的票在 "
        "`excluded_refs` 且不在候选里，原任务仍 `BOOKING_CONFIRMED`，事件带评估。",
        "- `return_leg`：返程窗口到 23:00、不留缓冲，10 条混合期望。",
        "- `informational`：查不到 / 已起飞 / 已落地 / 按计划 → 什么都不做。",
        "- `webhook`：无密钥 503、坏签名 401、缺签名 401、签的是别的正文 401、票号没人盯 404、"
        "坏 JSON 422、非法状态 422、多余字段 422、按票号命中、按 trip_id 命中并开改期任务。",
        "- `worker`：只领到点的差旅、按动态源判、领完释放租约并重排。",
        "- `cancel_trip`：各状态可取消、二次取消 409、取消后事件 / 动态 / 聊天一律拒绝、"
        "看板不显示、任务状态不动。",
        "- `events_api`：会议改期改指定段并平移出发窗口、越界 409、缺参数 409、航变手工报、"
        "改期中再报 409。",
        "- `chat_*`：真模型把话读成变更请求：会议改期要对上日期和哪一段；取消要让差旅 "
        "`CANCELLED`；航变自述要排除对的那张票；该问的时候问、行程不动。",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""订好之后的追踪与变更：不联网、不花钱地把整条链路走一遍。

    PYTHONPATH=src .venv/bin/python examples/run_trip_watch_walkthrough.py

演示库存是 2026 年 8 月的，所以这里把 API 的时钟钉在演示时刻（`DEMO_CLOCK`），
用 FastAPI 的 TestClient 直接调路由——和 uvicorn 起来之后用 curl 打是同一套代码。
真实时钟下要看这些效果，请接 Duffel 沙箱订一趟未来的行程（README「Duffel Test Mode」）。

走的路：
1. 结构化建任务 → 选合规方案 → 回填订单号 → 差旅进入 BOOKED，观察对象排好第一次检查；
2. 管理员报"延误 30 分钟" → 影响评估：仍来得及，只通知；同样的动态再报一次不重复通知；
3. 时钟拨到下一次检查时刻，动态源里登记一条延误 → `POST /trip-watch/run` 领取并观察；
4. TMC 用 HMAC 签名推送"取消"（不带 trip_id）→ 开改期任务，被取消的票不再端上来；
5. 发件箱里能看到 TRIP_FLIGHT_STATUS_NOTICE / TRIP_CHANGE_REQUESTED；
6. 第二趟差旅走取消：观察停止、谁在哪看板不再显示。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("FLIGHT_STATUS_SOURCE", "memory")
os.environ.setdefault("FLIGHT_STATUS_WEBHOOK_SECRET", "walkthrough-secret")
os.environ.setdefault("PROCESS_ROLE", "api")

from fastapi.testclient import TestClient  # noqa: E402

from corporate_travel_agent.api import main as api_main  # noqa: E402
from corporate_travel_agent.demo import DEMO_CLOCK  # noqa: E402
from corporate_travel_agent.domain.enums import FlightStatusKind  # noqa: E402
from corporate_travel_agent.domain.models import FlightStatusReport  # noqa: E402

SECRET = os.environ["FLIGHT_STATUS_WEBHOOK_SECRET"].encode()


def say(step: str, **facts: object) -> None:
    print(f"[{step}] " + " | ".join(f"{k}={v}" for k, v in facts.items()), flush=True)


def book(client: TestClient, order_reference: str) -> tuple[str, str, dict]:
    created = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
            "hard_constraints": ["arrive_before_meeting"],
        },
    ).json()
    option = next(o for o in created["options"] if o["policy_outcome"] == "COMPLIANT")
    task_id = created["task_id"]
    client.post(f"/trip-tasks/{task_id}/select-option", json={"option_id": option["option_id"]})
    confirmed = client.post(
        f"/trip-tasks/{task_id}/booking-confirmation",
        json={
            "order_references": [order_reference],
            "total_amount": str(option["total_cost"]),
            "currency": option["currency"],
        },
    )
    if confirmed.status_code != 200:
        sys.exit(f"booking confirmation failed: {confirmed.text}")
    return task_id, created["trip_id"], client.get(f"/trips/{created['trip_id']}").json()


def audit_count(client: TestClient, task_id: str, event_type: str) -> int:
    events = client.get(f"/trip-tasks/{task_id}/audit-events").json()
    return sum(1 for item in events if item["event_type"] == event_type)


def main() -> None:
    clock = {"now": DEMO_CLOCK}
    api_main.workflow.clock = lambda: clock["now"]
    client = TestClient(api_main.app)
    source = api_main.workflow.flight_status_source
    if not getattr(source, "record", None):
        sys.exit("FLIGHT_STATUS_SOURCE must be memory for this walkthrough")

    # 1. 订好
    task_id, trip_id, trip = book(client, "PNR-WALK-1")
    leg = trip["watch"]["legs"][0]
    say(
        "booked",
        trip=trip["status"],
        legs=[item["ref_id"] for item in trip["watch"]["legs"]],
        next_check_at=trip["watch"]["next_check_at"],
    )

    # 2. 延误 30 分钟：仍来得及 → 只通知；再报一次不重复
    arrive = (datetime.fromisoformat(leg["arrive_at"]) + timedelta(minutes=30)).isoformat()
    delayed = {"ref_id": leg["ref_id"], "status": "DELAYED", "estimated_arrive_at": arrive}
    body = client.post(f"/trips/{trip_id}/flight-status", json=delayed).json()
    say("delay 30m", verdict=body["observation"]["verdict"], reasons=body["observation"]["reasons"])
    client.post(f"/trips/{trip_id}/flight-status", json=delayed)
    say(
        "dedupe",
        flight_status_observed_audits=audit_count(client, task_id, "FLIGHT_STATUS_OBSERVED"),
    )

    # 3. worker 轮询：把时钟拨到下一次检查，动态源里登记一条更长的延误
    clock["now"] = datetime.fromisoformat(trip["watch"]["next_check_at"])
    source.record(
        FlightStatusReport(
            ref_id=leg["ref_id"],
            status=FlightStatusKind.DELAYED,
            observed_at=clock["now"],
            source=source.name,
            estimated_arrive_at=datetime.fromisoformat(leg["arrive_at"]) + timedelta(minutes=50),
        )
    )
    ran = client.post("/trip-watch/run").json()
    watch = client.get(f"/trips/{trip_id}").json()["watch"]
    say(
        "worker run",
        processed=[item[:8] for item in ran["processed"]],
        metrics=ran["metrics"],
        latest=[(o["ref_id"], o["status"], o["verdict"]) for o in watch["observations"]],
        next_check_at=watch["next_check_at"],
    )

    # 4. TMC 推送取消：HMAC 签名，不带 trip_id
    payload = json.dumps({"ref_id": leg["ref_id"], "status": "CANCELLED", "source": "tmc"})
    signature = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    pushed = client.post(
        "/flight-status/webhook",
        content=payload,
        headers={"X-Flight-Status-Signature": signature, "Content-Type": "application/json"},
    ).json()
    mine = next(item for item in pushed["results"] if item["trip"]["trip_id"] == trip_id)
    change_id = mine["observation"]["opened_task_id"]
    change = client.get(f"/trip-tasks/{change_id}").json()
    say(
        "webhook cancel",
        trips_hit=pushed["trip_count"],
        verdict=mine["observation"]["verdict"],
        trip=mine["trip"]["status"],
        change_task=change_id[:8],
        change_state=change["state"],
        excluded=change["change_event"]["excluded_refs"],
        options=len(change["options"]),
    )

    # 5. 发件箱
    dispatched = client.post("/outbox/dispatch", json={"limit": 50}).json()
    say("outbox", **{k: v for k, v in dispatched.items() if k != "delivered"})

    # 6. 第二趟：取消整趟。时钟拨回演示时刻——政策要求提前若干天订，8 月 3 日订 8 月 5 日的票要审批。
    clock["now"] = DEMO_CLOCK
    task2, trip2, _ = book(client, "PNR-WALK-2")
    cancelled = client.post(f"/trips/{trip2}/cancel", json={"reason": "项目取消"}).json()
    on_board = {row["trip_id"] for row in client.get("/duty-of-care").json()["travelers"]}
    say(
        "cancel trip",
        status=cancelled["status"],
        last_event=cancelled["events"][-1]["event_type"],
        next_check_at=cancelled["watch"]["next_check_at"],
        still_on_duty_of_care=trip2 in on_board,
        task_state=client.get(f"/trip-tasks/{task2}").json()["state"],
    )
    health = client.get("/health").json()["trip_watch"]
    say("health", **{k: v for k, v in health.items() if k != "metrics"})


if __name__ == "__main__":
    main()

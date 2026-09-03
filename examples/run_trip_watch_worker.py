#!/usr/bin/env python3
"""独立的差旅观察 worker：不开 HTTP，只领取到点该查航班动态的差旅并处理。

    # 每 60 秒领一轮，读 DATABASE_URL（队列在 trips 表里，多个 worker 靠租约互斥）
    FLIGHT_STATUS_SOURCE=memory .venv/bin/python examples/run_trip_watch_worker.py --interval 60

    # 只跑一轮（CI / 演示脚本用）
    .venv/bin/python examples/run_trip_watch_worker.py --once

装配和 API 进程完全一样：同一个库、同一份政策、同一个动态源配置。`FLIGHT_STATUS_SOURCE`
是 `none`（默认）时 worker 一趟都不领——没接的源不去问。
"""

from __future__ import annotations

import argparse
import json
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between rounds")
    parser.add_argument("--once", action="store_true", help="run a single round and exit")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        raise SystemExit(
            "DATABASE_URL is required: the watch queue lives in the trips table; "
            "an in-memory queue is invisible to a separate worker process"
        )
    os.environ["PROCESS_ROLE"] = "worker"
    from corporate_travel_agent.api import main as api_main

    workflow = api_main.workflow
    while True:
        processed = workflow.process_due_flight_checks(limit=args.limit)
        print(
            json.dumps(
                {
                    "processed": list(processed),
                    "metrics": workflow.trip_watch_metrics(),
                    "source": workflow.flight_status_source.name,
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        if args.once:
            break
        time.sleep(max(args.interval, 0.1))


if __name__ == "__main__":
    main()

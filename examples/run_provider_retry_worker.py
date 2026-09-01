#!/usr/bin/env python3
"""独立的供应商延迟重试 worker：不开 HTTP，只领取到期的重试任务并执行。

    # 每 5 秒领一轮，读 DATABASE_URL（队列在任务表里，多个 worker 靠租约互斥）
    .venv/bin/python examples/run_provider_retry_worker.py --interval 5

    # 只跑一轮（CI / 演示脚本用）
    .venv/bin/python examples/run_provider_retry_worker.py --once

装配和 API 进程完全一样（同一个库、同一个供应商适配器、同一份共享熔断状态），
只是不启动 HTTP 服务、不启动进程内调度线程：`PROCESS_ROLE=worker` 在这里是硬性的。
多实例部署时 API 进程用 `PROCESS_ROLE=api`，重试全部交给这种 worker 进程。
"""

from __future__ import annotations

import argparse
import json
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between rounds")
    parser.add_argument("--once", action="store_true", help="run a single round and exit")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        raise SystemExit(
            "DATABASE_URL is required: the retry queue lives in the task table; "
            "an in-memory queue is invisible to a separate worker process"
        )
    os.environ["PROCESS_ROLE"] = "worker"
    # 装配走 API 那一套，保证 worker 和 API 看到的是同一个世界。导入不会启动服务。
    from corporate_travel_agent.api import main as api_main

    workflow = api_main.workflow
    while True:
        processed = workflow.process_due_provider_retries(limit=args.limit)
        print(
            json.dumps(
                {
                    "processed": list(processed),
                    "metrics": workflow.provider_retry_metrics(),
                    "circuit": workflow.provider_circuit_breaker.snapshot(),
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

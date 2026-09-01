#!/usr/bin/env python3
"""发件箱投递 worker：循环把 `outbox_events` 里未发布的事件交给配置的通道。

    # 每 5 秒投递一轮，读 DATABASE_URL；没配 OUTBOX_WEBHOOK_URL 就只记日志
    .venv/bin/python examples/run_outbox_worker.py --interval 5

    # 只跑一轮（CI / 演示脚本用）
    .venv/bin/python examples/run_outbox_worker.py --once

通道由环境决定：`OUTBOX_WEBHOOK_URL`（可选 `OUTBOX_WEBHOOK_SECRET` 做 HMAC 签名）
把全部事件 POST 给企业侧地址；没配就用日志通道。至少一次投递，对面按 `event_id` 幂等。
"""

from __future__ import annotations

import argparse
import json
import os
import time

from corporate_travel_agent.services.outbox_dispatch import (
    LoggingChannel,
    OutboxDispatcher,
    WebhookChannel,
)


def _store():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required: the outbox lives next to the task table")
    from corporate_travel_agent.services.outbox import SQLAlchemyOutboxStore
    from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository

    repository = SQLAlchemyTaskRepository(database_url)
    return SQLAlchemyOutboxStore(repository.engine)


def _channel():
    url = os.getenv("OUTBOX_WEBHOOK_URL")
    if url:
        return WebhookChannel(url, secret=os.getenv("OUTBOX_WEBHOOK_SECRET") or None)
    return LoggingChannel()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between rounds")
    parser.add_argument("--once", action="store_true", help="run a single round and exit")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args()

    dispatcher = OutboxDispatcher(
        _store(), default_channel=_channel(), max_attempts=args.max_attempts
    )
    while True:
        report = dispatcher.dispatch_once(limit=args.limit)
        print(json.dumps(report.as_dict(), ensure_ascii=False))
        if args.once:
            break
        time.sleep(max(args.interval, 0.1))


if __name__ == "__main__":
    main()

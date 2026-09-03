#!/usr/bin/env python3
"""把库里的旧载荷批量改写成当前 schema 版本。

    # 只看不改：每张表多少行是当前版本、多少行能升、多少行升不上去
    export DATABASE_URL=postgresql+psycopg://...
    PYTHONPATH=src .venv/bin/python examples/upgrade_task_payloads.py

    # 真改：升得上去的行按当前版本重写 payload；任务表同时更新 payload_schema_version 列
    ... examples/upgrade_task_payloads.py --apply

改的只是形状（`services/serialization.py` 里的升级链），不补业务事实；`revision` 列不动——
这不是一次业务更新。升不上去的行原样留着并逐条列出，需要人看。先备份：
`pg_dump -Fc` 见 `docs/postgres-operations.md` §4。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from corporate_travel_agent.services.serialization import (
    SCHEMA_VERSION,
    PayloadIncompatible,
    UnsupportedPayloadVersion,
    deserialize_audit_event,
    deserialize_snapshot,
    deserialize_task,
    deserialize_trip,
    upgrade_payload,
)
from corporate_travel_agent.services.sqlalchemy_repository import (
    AuditEventRow,
    InventorySnapshotRow,
    TaskRow,
    TripRow,
)

#: (表, 主键列名, 校验函数, 是否有 payload_schema_version 列)
TABLES = (
    ("trip_tasks", TaskRow, "task_id", deserialize_task, True),
    ("audit_events", AuditEventRow, "event_id", deserialize_audit_event, False),
    ("inventory_snapshots", InventorySnapshotRow, "snapshot_id", deserialize_snapshot, False),
    ("trips", TripRow, "trip_id", deserialize_trip, False),
)


@dataclass
class TableReport:
    table: str
    current: int = 0
    upgraded: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "current": self.current,
            "upgraded": self.upgraded,
            "failed": self.failed,
        }


def _pk(row: Any, column: str) -> str:
    return str(getattr(row, column))


def process(engine, *, apply: bool) -> list[TableReport]:
    reports: list[TableReport] = []
    for table, model, pk, validate, has_version_column in TABLES:
        report = TableReport(table)
        with Session(engine) as session, session.begin():
            for row in session.scalars(select(model)):
                payload = row.payload
                version = payload.get("schema_version") if isinstance(payload, dict) else None
                if version == SCHEMA_VERSION:
                    try:
                        validate(payload)
                        report.current += 1
                        continue
                    except (PayloadIncompatible, UnsupportedPayloadVersion) as exc:
                        report.failed.append({"id": _pk(row, pk), "error": str(exc)[:300]})
                        continue
                try:
                    upgraded, _ = upgrade_payload(payload)
                    validate(upgraded)
                except (PayloadIncompatible, UnsupportedPayloadVersion, Exception) as exc:  # noqa: BLE001
                    report.failed.append({"id": _pk(row, pk), "error": str(exc)[:300]})
                    continue
                report.upgraded += 1
                if apply:
                    values: dict[str, Any] = {"payload": upgraded}
                    if has_version_column:
                        values["payload_schema_version"] = SCHEMA_VERSION
                    if hasattr(model, "updated_at"):
                        values["updated_at"] = datetime.now(UTC)
                    session.execute(
                        update(model).where(getattr(model, pk) == getattr(row, pk)).values(**values)
                    )
        reports.append(report)
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="rewrite upgradable rows")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_engine(args.database_url, future=True)
    reports = process(engine, apply=args.apply)
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "mode": "apply" if args.apply else "dry-run",
                "tables": [item.as_dict() for item in reports],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if any(item.failed for item in reports):
        sys.exit(2)


if __name__ == "__main__":
    main()

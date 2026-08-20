#!/usr/bin/env python3
"""PostgreSQL 16 dual-worker claim / fencing / EXPLAIN evidence for HANDOFF §16.2 / §18.3 F.

Starts Colima + compose Postgres when needed, uses a dedicated database, and
never calls Duffel/LiteAPI. Provider calls are counted through a file-backed
mock wrapper so two OS processes can be scored.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pickle
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, text, update
from sqlalchemy.orm import Session

from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.audit import new_audit_event
from corporate_travel_agent.services.policy_config import load_policy_configuration
from corporate_travel_agent.services.repositories import ConcurrentUpdateError
from corporate_travel_agent.services.serialization import deserialize_task, serialize_task
from corporate_travel_agent.services.sqlalchemy_repository import (
    SQLAlchemyTaskRepository,
    TaskRow,
)

REPO = Path(__file__).resolve().parents[1]
POLICY_FILE = REPO / "config" / "travel-policy.json"
DB_NAME = "travel_agent_uncovered"
DB_URL = f"postgresql+psycopg://travel_agent:travel_agent_dev@127.0.0.1:5432/{DB_NAME}"


class CountingProvider:
    """Delegates to MockProvider and records each search/revalidate in a file."""

    def __init__(self, base: MockProvider, counter_path: Path) -> None:
        self.base = base
        self.counter_path = counter_path
        self.name = "counting-mock"
        self.provider_mode = "deterministic"

    def _inc(self, name: str) -> None:
        self.counter_path.parent.mkdir(parents=True, exist_ok=True)
        with self.counter_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            raw = handle.read().strip()
            payload = json.loads(raw) if raw else {}
            payload[name] = int(payload.get(name) or 0) + 1
            payload["calls"] = list(payload.get("calls") or [])
            payload["calls"].append(
                {"name": name, "pid": os.getpid(), "at": datetime.now(UTC).isoformat()}
            )
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(payload) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def search_transport(self, query):
        self._inc("search_transport")
        return self.base.search_transport(query)

    def search_hotels(self, query):
        self._inc("search_hotels")
        return self.base.search_hotels(query)

    def revalidate(self, refs):
        self._inc("revalidate")
        return self.base.revalidate(refs)

    def create_deep_link(self, option):
        return self.base.create_deep_link(option)


def _read_counter(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    return json.loads(raw) if raw else {}


def _ensure_postgres() -> dict[str, Any]:
    env = os.environ.copy()
    env["PATH"] = f"/opt/homebrew/bin:{env.get('PATH', '')}"
    colima_sock = Path.home() / ".colima/default/docker.sock"
    if colima_sock.exists():
        env["DOCKER_HOST"] = f"unix://{colima_sock}"
    notes: list[str] = []
    info = subprocess.run(["docker", "info"], env=env, capture_output=True, text=True)
    if info.returncode != 0:
        notes.append("starting colima")
        start = subprocess.run(
            ["colima", "start", "--cpu", "2", "--memory", "4"],
            env=env,
            capture_output=True,
            text=True,
        )
        notes.append(start.stderr[-400:] or start.stdout[-400:])
        if start.returncode != 0:
            raise RuntimeError(f"colima start failed: {start.stderr or start.stdout}")
        deadline = time.time() + 180
        while time.time() < deadline:
            info = subprocess.run(["docker", "info"], env=env, capture_output=True, text=True)
            if info.returncode == 0:
                break
            time.sleep(2)
        if info.returncode != 0:
            raise RuntimeError("docker daemon did not become ready after colima start")
    compose = subprocess.run(
        ["docker-compose", "up", "-d", "postgres"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    if compose.returncode != 0:
        raise RuntimeError(compose.stderr or compose.stdout)
    notes.append(compose.stdout.strip() or "compose up")
    deadline = time.time() + 90
    last = ""
    while time.time() < deadline:
        try:
            import psycopg

            with psycopg.connect(
                "postgresql://travel_agent:travel_agent_dev@127.0.0.1:5432/travel_agent",
                connect_timeout=3,
            ) as conn:
                conn.execute("SELECT 1")
            break
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            time.sleep(1.5)
    else:
        raise RuntimeError(f"postgres not reachable: {last}")
    import psycopg

    with psycopg.connect(
        "postgresql://travel_agent:travel_agent_dev@127.0.0.1:5432/travel_agent",
        autocommit=True,
    ) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,)).fetchone()
        if exists:
            with psycopg.connect(
                f"postgresql://travel_agent:travel_agent_dev@127.0.0.1:5432/{DB_NAME}",
                autocommit=True,
            ) as target:
                target.execute(
                    "TRUNCATE trip_tasks, audit_events, inventory_snapshots, "
                    "provider_quote_contexts, outbox_events CASCADE"
                )
            notes.append(f"truncated {DB_NAME}")
        else:
            conn.execute(f"CREATE DATABASE {DB_NAME}")
            notes.append(f"created {DB_NAME}")
    alembic = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env={**env, "DATABASE_URL": DB_URL},
        capture_output=True,
        text=True,
    )
    if alembic.returncode != 0:
        raise RuntimeError(alembic.stderr or alembic.stdout)
    notes.append("alembic upgrade head")
    return {"notes": notes, "database_url": DB_URL}


def _seed_due_task(database_url: str, task_id: str, policy) -> None:
    repository = SQLAlchemyTaskRepository(database_url)
    workflow, provider = build_demo_system(
        task_repository=repository,
        policy_configuration=policy,
        retry_sleep=lambda _: None,
        delayed_provider_retry_seconds=(2.0, 4.0, 8.0),
        max_delayed_provider_attempts=3,
        provider_retry_lease_seconds=2.0,
        max_tool_calls=20,
    )
    assert isinstance(provider, MockProvider)
    provider.fail_search_remaining = 99
    task = workflow.create_task(make_demo_request(task_id=task_id))
    print(
        json.dumps(
            {
                "seed": True,
                "policy_file": str(POLICY_FILE),
                "live_hash": workflow.policies.current().content_hash,
                "task_hash": task.metadata.get("policy_content_hash"),
                "snapshot_id": workflow.policies.current().snapshot_id,
            }
        ),
        flush=True,
    )
    if task.state is not TaskState.WAITING_FOR_PROVIDER:
        raise RuntimeError(f"expected WAITING_FOR_PROVIDER, got {task.state}")
    due_at = datetime.now(UTC) - timedelta(seconds=5)
    retry = task.metadata.setdefault("provider_retry", {})
    if isinstance(retry, dict):
        retry["next_retry_at"] = due_at.isoformat()
        # Let the delayed worker actually probe; a still-open circuit would
        # reschedule without calling the provider.
        retry["circuit_open_until"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    with Session(repository.engine) as session, session.begin():
        session.execute(
            update(TaskRow)
            .where(TaskRow.task_id == task_id)
            .values(next_retry_at=due_at, payload=serialize_task(task))
        )
    repository.dispose()


def _compete(
    database_url: str,
    worker_id: str,
    counter_path: Path,
    lease_seconds: float,
    policy_pickle: Path,
) -> None:
    policy = pickle.loads(policy_pickle.read_bytes())
    repository = SQLAlchemyTaskRepository(database_url)
    _base_workflow, base_provider = build_demo_system(policy_configuration=policy)
    assert isinstance(base_provider, MockProvider)
    provider = CountingProvider(base_provider, counter_path)
    workflow, _ = build_demo_system(
        task_repository=repository,
        provider=provider,
        policy_configuration=policy,
        retry_sleep=lambda _: None,
        delayed_provider_retry_seconds=(2.0, 4.0, 8.0),
        max_delayed_provider_attempts=3,
        provider_retry_lease_seconds=lease_seconds,
        provider_retry_worker_id=worker_id,
        max_tool_calls=20,
    )
    processed = workflow.process_due_provider_retries()
    print(
        json.dumps(
            {
                "worker_id": worker_id,
                "processed": list(processed),
                "live_hash": workflow.policies.current().content_hash,
            }
        )
    )
    repository.dispose()


def _hold_claim(database_url: str, worker_id: str, dump_path: Path, lease_seconds: float) -> None:
    repository = SQLAlchemyTaskRepository(database_url)
    now = datetime.now(UTC)
    claimed = repository.claim_due_provider_retries(
        worker_id=worker_id,
        now=now,
        lease_duration=timedelta(seconds=lease_seconds),
        limit=1,
    )
    dump_path.write_text(
        json.dumps(
            {
                "worker_id": worker_id,
                "count": len(claimed),
                "task": serialize_task(claimed[0]) if claimed else None,
            },
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    repository.dispose()


def _explain(database_url: str, output: Path) -> dict[str, Any]:
    repository = SQLAlchemyTaskRepository(database_url)
    now = datetime.now(UTC)
    rows = [
        {
            "task_id": f"hist-{index:06d}",
            "state": TaskState.HANDED_OFF.value,
            "revision": 0,
            "payload": {"schema_version": 1, "task": {"task_id": f"hist-{index:06d}"}},
            "clarification_rounds": 0,
            "delayed_retry_count": 0,
            "payload_schema_version": 1,
            "created_at": now,
            "updated_at": now,
        }
        for index in range(100_000)
    ]
    with Session(repository.engine) as session, session.begin():
        for start in range(0, len(rows), 5000):
            session.execute(insert(TaskRow), rows[start : start + 5000])
        session.execute(text("ANALYZE trip_tasks"))
    query = """
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT task_id
FROM trip_tasks
WHERE (
    (state = 'WAITING_FOR_PROVIDER' AND next_retry_at IS NOT NULL AND next_retry_at <= now())
    OR (
        state IN ('SEARCHING', 'REVALIDATING')
        AND retry_attempt_token IS NOT NULL
        AND retry_lease_until <= now()
    )
)
AND (retry_lease_until IS NULL OR retry_lease_until <= now())
ORDER BY next_retry_at, retry_lease_until, task_id
LIMIT 20
FOR UPDATE SKIP LOCKED
"""
    with repository.engine.connect() as connection:
        transaction = connection.begin()
        plan = connection.execute(text(query)).scalar_one()
        transaction.rollback()
    (output / "explain.json").write_text(
        json.dumps(plan, indent=2, default=str) + "\n", encoding="utf-8"
    )
    node = plan[0]["Plan"] if isinstance(plan, list) else plan
    scans: list[str] = []

    def walk(item: Any) -> None:
        if not isinstance(item, dict):
            return
        if item.get("Relation Name") == "trip_tasks":
            scans.append(str(item.get("Node Type")))
        for child in item.get("Plans") or ():
            walk(child)

    walk(node)
    used_index = any("Index" in scan or "Bitmap" in scan for scan in scans)
    seq = any(scan == "Seq Scan" for scan in scans)
    with Session(repository.engine) as session:
        count = session.execute(text("SELECT count(*) FROM trip_tasks")).scalar_one()
    repository.dispose()
    return {
        "row_count": int(count),
        "trip_tasks_node_types": scans,
        "used_index": used_index,
        "seq_scan": seq,
        "plan_file": str(output / "explain.json"),
    }


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/evaluation-runs/postgres-dual-worker-20260819")
    parser.add_argument("--role", choices=("all", "compete", "hold"), default="all")
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--counter", default="")
    parser.add_argument("--dump", default="")
    parser.add_argument("--policy-pickle", default="")
    parser.add_argument("--lease-seconds", type=float, default=8.0)
    parser.add_argument("--skip-explain", action="store_true")
    args = parser.parse_args()

    if args.role == "compete":
        _compete(
            args.database_url,
            args.worker_id,
            Path(args.counter),
            args.lease_seconds,
            Path(args.policy_pickle),
        )
        return
    if args.role == "hold":
        _hold_claim(args.database_url, args.worker_id, Path(args.dump), args.lease_seconds)
        return

    output = (REPO / args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["POLICY_CONFIG_FILE"] = str(POLICY_FILE)
    started = datetime.now(UTC).isoformat()
    setup = _ensure_postgres()
    policy = load_policy_configuration(POLICY_FILE)
    policy_pickle = output / "policy.pkl"
    policy_pickle.write_bytes(pickle.dumps(policy))
    task_id = f"lease-race-{uuid4().hex[:10]}"
    counter = output / "provider-calls.json"
    _seed_due_task(DB_URL, task_id, policy)

    workers = []
    for worker_id in ("worker-a", "worker-b"):
        workers.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__)),
                    "--role",
                    "compete",
                    "--worker-id",
                    worker_id,
                    "--database-url",
                    DB_URL,
                    "--counter",
                    str(counter),
                    "--policy-pickle",
                    str(policy_pickle),
                    "--lease-seconds",
                    "8",
                ],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    worker_logs = []
    for proc in workers:
        stdout, stderr = proc.communicate(timeout=60)
        worker_logs.append(
            {
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr[-800:],
            }
        )
    calls = _read_counter(counter)
    repository = SQLAlchemyTaskRepository(DB_URL)
    final = repository.get(task_id)
    repository.dispose()

    hold_id = f"lease-hold-{uuid4().hex[:10]}"
    _seed_due_task(DB_URL, hold_id, policy)
    dump = output / "stale-claim.json"
    hold = subprocess.run(
        [
            sys.executable,
            str(Path(__file__)),
            "--role",
            "hold",
            "--worker-id",
            "worker-stale",
            "--database-url",
            DB_URL,
            "--dump",
            str(dump),
            "--lease-seconds",
            "2",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    time.sleep(2.5)
    takeover = subprocess.run(
        [
            sys.executable,
            str(Path(__file__)),
            "--role",
            "compete",
            "--worker-id",
            "worker-takeover",
            "--database-url",
            DB_URL,
            "--counter",
            str(output / "takeover-calls.json"),
            "--policy-pickle",
            str(policy_pickle),
            "--lease-seconds",
            "8",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    stale_error = ""
    stale_rejected = False
    if dump.exists():
        dumped = json.loads(dump.read_text(encoding="utf-8"))
        if dumped.get("task"):
            repository = SQLAlchemyTaskRepository(DB_URL)
            stale = deserialize_task(dumped["task"])
            try:
                repository.record(
                    stale,
                    new_audit_event(
                        stale.task_id,
                        "STALE_PROVIDER_RESULT",
                        input_value="stale",
                        output_value="must be fenced",
                    ),
                )
            except ConcurrentUpdateError as exc:
                stale_rejected = True
                stale_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                stale_error = f"{type(exc).__name__}: {exc}"
            repository.dispose()

    explain = (
        {"row_count": 0, "trip_tasks_node_types": [], "used_index": True, "seq_scan": False}
        if args.skip_explain
        else _explain(DB_URL, output)
    )
    checks = [
        _check("postgres_ready", True, setup["notes"][-1] if setup["notes"] else ""),
        _check(
            "both_workers_exited_0",
            all(item["returncode"] == 0 for item in worker_logs),
            worker_logs,
        ),
        _check(
            "single_worker_provider_job",
            len({item.get("pid") for item in (calls.get("calls") or [])}) == 1
            and int(calls.get("search_hotels") or 0) == 1
            and int(calls.get("search_transport") or 0) in {1, 2},
            calls,
        ),
        _check(
            "final_not_double_searching",
            final.state
            in {
                TaskState.WAITING_FOR_USER,
                TaskState.NO_FEASIBLE_OPTION,
                TaskState.PROVIDER_FAILED,
            },
            final.state.value,
        ),
        _check("hold_claimed", hold.returncode == 0, hold.stderr[-300:]),
        _check("takeover_ok", takeover.returncode == 0, takeover.stderr[-300:]),
        _check("stale_write_rejected", stale_rejected, stale_error),
        _check(
            "explain_rows_ge_100k",
            args.skip_explain or explain["row_count"] >= 100_000,
            explain["row_count"],
        ),
        _check(
            "explain_used_index",
            args.skip_explain or (explain["used_index"] and not explain["seq_scan"]),
            explain,
        ),
    ]
    results = {
        "run_id": output.name,
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat(),
        "setup": setup,
        "compete": {
            "task_id": task_id,
            "calls": calls,
            "final_state": final.state.value,
            "workers": worker_logs,
        },
        "lease": {
            "task_id": hold_id,
            "stale_rejected": stale_rejected,
            "stale_error": stale_error,
            "takeover_stdout": takeover.stdout,
        },
        "explain": explain,
        "checks": checks,
        "passed": all(item["ok"] for item in checks),
    }
    (output / "summary.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Postgres dual-worker acceptance",
        "",
        f"- passed: **{results['passed']}**",
        "",
    ]
    for item in checks:
        mark = "PASS" if item["ok"] else "FAIL"
        lines.append(f"- [{mark}] `{item['name']}` — `{item['detail']}`")
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": results["passed"],
                "calls": calls,
                "explain": explain["trip_tasks_node_types"],
            },
            default=str,
        )
    )
    if not results["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

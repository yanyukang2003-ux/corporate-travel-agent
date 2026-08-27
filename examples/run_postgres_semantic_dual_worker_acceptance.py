#!/usr/bin/env python3
"""真实 PostgreSQL 上的语义任务并发验收。

## 为什么语义任务要单独验并发

现有的 `run_postgres_dual_worker_acceptance.py` 验的是 Provider 重试的抢占与围栏，
走结构化入口。语义链路多了一样东西：**对话账本**。

ADR-0002 把「原始对话轮次」定为唯一事实来源——每次追问都是把整段对话重读一遍。
所以两个进程同时给同一个任务追加消息时，最危险的不是状态机乱掉，而是**某一轮用户
发言被悄悄覆盖掉**。丢了那一轮，后面每一次重新解释都会基于一份残缺的对话，而且
不会有任何报错。内存存储永远测不出这个：那里根本不存在第二个进程。

## 断言的是不变量，不是某一种交错

两个 OS 进程真并发，谁先谁后由操作系统决定。所以这里不断言"A 赢 B 输"，而是断言
**无论哪种交错都必须成立**的性质：

- 账本里的用户轮次数 == 1 + 成功写入的次数（**不丢、不重**）；
- 写失败的一方必须拿到并发冲突错误，而不是静默覆盖；
- 任务的入口标记始终是 semantic；
- 到期的 Provider 重试只能被一个 worker 领走，库存不会被搜两遍。

不调用计费模型（脚本化语义替身），不访问外网（Mock 库存）。用独立数据库，
不碰 travel_agent 主库。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from corporate_travel_agent.agent.ports import (
    IntentInterpretationResult,
    LLMCallMetadata,
)
from corporate_travel_agent.agent.semantic_intent import (
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
)
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system
from corporate_travel_agent.domain.enums import (
    BookingScope,
    LodgingRequirement,
    TaskState,
)
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.policy_config import load_policy_configuration
from corporate_travel_agent.services.repositories import ConcurrentUpdateError
from corporate_travel_agent.services.serialization import serialize_task
from corporate_travel_agent.services.sqlalchemy_repository import (
    SQLAlchemyTaskRepository,
    TaskRow,
)

REPO = Path(__file__).resolve().parents[1]
DB_NAME = "travel_agent_semantic_dual"
DB_URL = f"postgresql+psycopg://travel_agent:travel_agent_dev@127.0.0.1:5432/{DB_NAME}"
RUNNER_VERSION = "postgres-semantic-dual-worker-v1"

CLOCK = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)
SEED_MESSAGE = "8月5日从北京去上海，想请你帮我安排"


class ScriptedSemanticModel:
    """固定返回"还缺到达时限"的语义判定；并发行为才是被测对象，不是模型。"""

    prompt_version = "semantic-dual-worker-v1"
    semantic_prompt_version = prompt_version

    def interpret_trip_intent(self, conversation, *, task_id, traveler_id, context):
        del task_id, traveler_id, context
        intent = SemanticIntent(
            summary="从北京去上海，到达时限待确认",
            origin_candidates=["Beijing"],
            destination_candidates=["Shanghai"],
            departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI_TZ),
            arrive_by=None,
            return_after=None,
            return_before=None,
            booking_scope=BookingScope.OUTBOUND_ONLY,
            lodging_requirement=LodgingRequirement.NOT_REQUIRED,
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=[],
            soft_preferences=[],
            alternatives=[],
            conditions=[],
            uncertainties=[],
        )
        decision = IntentDecision(
            status=IntentDecisionStatus.NEEDS_CLARIFICATION,
            intent=intent,
            clarification_question="请问几点前需要到达上海？",
            conflicts=[],
            unsupported_reasons=[],
            assumptions=[],
            # 第 0 轮永远是最初那句话，含"北京"。
            evidence=[EvidenceRef(turn_index=0, field="origin", quote="北京")],
            confidence=0.9,
            manipulation_detected=False,
        )
        del conversation
        return IntentInterpretationResult(
            decision=decision,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted-semantic-dual",
                duration_ms=1,
                evidence_contract_version="conversation-turn-v1",
            ),
        )

    def propose_search_adjustment(self, *_: object) -> None:
        return None

    def explain_verified_options(self, *_: object) -> dict[str, str]:
        return {}


def _workflow(database_url: str, policy, **kwargs: Any):
    repository = SQLAlchemyTaskRepository(database_url)
    workflow, provider = build_demo_system(
        task_repository=repository,
        policy_configuration=policy,
        semantic_language_model=ScriptedSemanticModel(),
        clock=lambda: CLOCK,
        retry_sleep=lambda _: None,
        max_tool_calls=40,
        **kwargs,
    )
    return workflow, provider, repository


# --- 子进程入口 ---------------------------------------------------------------


def _child_submit(database_url: str, task_id: str, message: str, barrier: Path) -> None:
    """等barrier 出现后立刻写；两个子进程因此尽可能同时落到同一行上。"""
    policy = load_policy_configuration()
    workflow, _provider, repository = _workflow(database_url, policy)
    deadline = time.time() + 30
    while not barrier.exists() and time.time() < deadline:
        time.sleep(0.01)
    outcome: dict[str, Any] = {"message": message}
    try:
        task = workflow.submit_semantic_message(task_id, message)
        outcome["ok"] = True
        outcome["state"] = task.state.value
    except ConcurrentUpdateError as exc:
        outcome["ok"] = False
        outcome["error"] = "ConcurrentUpdateError"
        outcome["detail"] = str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        outcome["ok"] = False
        outcome["error"] = type(exc).__name__
        outcome["detail"] = str(exc)[:200]
    print(json.dumps(outcome, ensure_ascii=False), flush=True)
    repository.dispose()


def _child_retry(database_url: str, worker_id: str, counter: Path, barrier: Path) -> None:
    policy = load_policy_configuration()
    workflow, provider, repository = _workflow(
        database_url,
        policy,
        delayed_provider_retry_seconds=(2.0, 4.0, 8.0),
        max_delayed_provider_attempts=3,
        provider_retry_lease_seconds=30.0,
        provider_retry_worker_id=worker_id,
    )
    assert isinstance(provider, MockProvider)
    deadline = time.time() + 30
    while not barrier.exists() and time.time() < deadline:
        time.sleep(0.01)
    processed = workflow.process_due_provider_retries()
    with counter.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"worker_id": worker_id, "processed": list(processed)}) + "\n"
        )
    print(
        json.dumps({"worker_id": worker_id, "processed": list(processed)}),
        flush=True,
    )
    repository.dispose()


# --- 环境 ---------------------------------------------------------------------


def _docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"/opt/homebrew/bin:{env.get('PATH', '')}"
    colima_sock = Path.home() / ".colima/default/docker.sock"
    if colima_sock.exists():
        env["DOCKER_HOST"] = f"unix://{colima_sock}"
    return env


def _ensure_database() -> list[str]:
    import psycopg

    notes: list[str] = []
    env = _docker_env()
    info = subprocess.run(["docker", "info"], env=env, capture_output=True, text=True)
    if info.returncode != 0:
        raise RuntimeError("docker daemon is not reachable; start colima first")
    compose = subprocess.run(
        ["docker-compose", "up", "-d", "postgres"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    if compose.returncode != 0:
        raise RuntimeError(compose.stderr or compose.stdout)
    notes.append("compose up postgres")

    deadline = time.time() + 90
    last = ""
    while time.time() < deadline:
        try:
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

    with psycopg.connect(
        "postgresql://travel_agent:travel_agent_dev@127.0.0.1:5432/travel_agent",
        autocommit=True,
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,)
        ).fetchone()
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
    return notes


def _check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _case(case_id: str, title: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "title": title,
        "passed": all(item["ok"] for item in checks) if checks else False,
        "checks": checks,
    }


def _spawn(args_list: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), *args_list],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# --- 用例 ---------------------------------------------------------------------


def _concurrent_followups(workdir: Path) -> dict[str, Any]:
    """两个进程同时给同一个语义任务追加消息：账本不许丢轮次。"""
    policy = load_policy_configuration()
    workflow, _provider, repository = _workflow(DB_URL, policy)
    task_id = "semantic-concurrent-followup"
    task = workflow.create_task_from_semantic_message(
        SEED_MESSAGE, traveler_id="E1001", task_id=task_id
    )
    assert task.state is TaskState.NEEDS_CLARIFICATION
    repository.dispose()

    barrier = workdir / "followup.barrier"
    messages = ["上午十点前要到", "改成下午三点前到"]
    children = [
        _spawn(
            [
                "--child-submit",
                "--database-url",
                DB_URL,
                "--task-id",
                task_id,
                "--message",
                message,
                "--barrier",
                str(barrier),
            ]
        )
        for message in messages
    ]
    time.sleep(2.0)
    barrier.write_text("go", encoding="utf-8")
    outcomes = []
    for child in children:
        out, err = child.communicate(timeout=120)
        line = next((item for item in out.splitlines() if item.startswith("{")), "")
        outcomes.append(json.loads(line) if line else {"ok": False, "error": err[-200:]})

    verify_workflow, _p, verify_repo = _workflow(DB_URL, policy)
    stored = verify_repo.get(task_id)
    user_turns = [item.content for item in stored.messages if item.role == "user"]
    succeeded = [item for item in outcomes if item.get("ok")]
    failed = [item for item in outcomes if not item.get("ok")]
    expected_turns = 1 + len(succeeded)
    verify_repo.dispose()
    del verify_workflow

    return _case(
        "SD-01",
        "两个进程同时追加消息：账本不丢轮次、失败方拿到并发冲突",
        [
            _check("at_least_one_succeeded", bool(succeeded), outcomes),
            _check(
                "no_lost_or_duplicated_turn",
                len(user_turns) == expected_turns,
                f"expected {expected_turns} user turns, stored {len(user_turns)}: {user_turns}",
            ),
            _check("no_duplicate_turn", len(set(user_turns)) == len(user_turns), user_turns),
            _check(
                "seed_turn_survived",
                user_turns and user_turns[0] == SEED_MESSAGE,
                user_turns[:1],
            ),
            _check(
                "loser_saw_a_concurrency_error",
                all(item.get("error") == "ConcurrentUpdateError" for item in failed),
                failed,
            ),
            _check(
                "entrypoint_still_semantic",
                stored.metadata.get("intent_entrypoint") == "semantic",
                stored.metadata.get("intent_entrypoint"),
            ),
            _check(
                "decision_history_matches_turns",
                len(stored.metadata.get("semantic_intent_history") or [])
                == expected_turns,
                len(stored.metadata.get("semantic_intent_history") or []),
            ),
        ],
    )


def _concurrent_retry_claim(workdir: Path) -> dict[str, Any]:
    """到期的 Provider 重试只能被一个 worker 领走，库存不会搜两遍。"""
    policy = load_policy_configuration()
    task_id = "semantic-concurrent-retry"
    workflow, provider, repository = _workflow(
        DB_URL,
        policy,
        delayed_provider_retry_seconds=(2.0, 4.0, 8.0),
        max_delayed_provider_attempts=3,
        provider_retry_lease_seconds=30.0,
    )
    assert isinstance(provider, MockProvider)
    provider.fail_search_remaining = 99

    class ReadyModel(ScriptedSemanticModel):
        def interpret_trip_intent(self, conversation, *, task_id, traveler_id, context):
            result = super().interpret_trip_intent(
                conversation, task_id=task_id, traveler_id=traveler_id, context=context
            )
            decision = result.decision
            ready = decision.model_copy(
                update={
                    "status": IntentDecisionStatus.READY,
                    "clarification_question": None,
                    "intent": decision.intent.model_copy(
                        update={
                            "arrive_by": datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI_TZ)
                        }
                    ),
                    "evidence": [
                        EvidenceRef(turn_index=0, field=field, quote="北京")
                        for field in (
                            "origin",
                            "destination",
                            "departure_after",
                            "arrive_by",
                        )
                    ],
                }
            )
            return IntentInterpretationResult(decision=ready, metadata=result.metadata)

    workflow.semantic_language_model = ReadyModel()
    from corporate_travel_agent.agent.semantic_intent import ConversationIntentInterpreter

    workflow.intent_interpreter = ConversationIntentInterpreter(ReadyModel())

    task = workflow.create_task_from_semantic_message(
        SEED_MESSAGE, traveler_id="E1001", task_id=task_id
    )
    seeded_state = task.state
    # 到期时刻必须按 **workflow 的时钟** 算：这里的时钟冻结在演示库存之前，
    # 用真实时间会永远"还没到期"，两个 worker 都拿不到活干。
    due_at = CLOCK - timedelta(seconds=5)
    retry = task.metadata.setdefault("provider_retry", {})
    if isinstance(retry, dict):
        retry["next_retry_at"] = due_at.isoformat()
        retry["circuit_open_until"] = (CLOCK - timedelta(seconds=5)).isoformat()
    with Session(repository.engine) as session, session.begin():
        session.execute(
            update(TaskRow)
            .where(TaskRow.task_id == task_id)
            .values(next_retry_at=due_at, payload=serialize_task(task))
        )
    repository.dispose()

    barrier = workdir / "retry.barrier"
    counter = workdir / "retry-claims.jsonl"
    counter.write_text("", encoding="utf-8")
    children = [
        _spawn(
            [
                "--child-retry",
                "--database-url",
                DB_URL,
                "--worker-id",
                worker_id,
                "--counter",
                str(counter),
                "--barrier",
                str(barrier),
            ]
        )
        for worker_id in ("worker-a", "worker-b")
    ]
    time.sleep(2.0)
    barrier.write_text("go", encoding="utf-8")
    for child in children:
        child.communicate(timeout=120)

    claims = [
        json.loads(line)
        for line in counter.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    claimed_by = [item["worker_id"] for item in claims if task_id in item["processed"]]
    verify_repo = SQLAlchemyTaskRepository(DB_URL)
    stored = verify_repo.get(task_id)
    verify_repo.dispose()

    return _case(
        "SD-02",
        "到期的语义任务重试只被一个 worker 领走",
        [
            _check(
                "seeded_waiting_for_provider",
                seeded_state is TaskState.WAITING_FOR_PROVIDER,
                seeded_state.value,
            ),
            _check("both_workers_reported", len(claims) == 2, claims),
            _check("exactly_one_claimed", len(claimed_by) == 1, claimed_by),
            _check(
                "entrypoint_still_semantic",
                stored.metadata.get("intent_entrypoint") == "semantic",
                stored.metadata.get("intent_entrypoint"),
            ),
            _check(
                "no_second_interpretation",
                # 重试是补搜库存，不该再解释一遍用户在说什么。
                len(stored.metadata.get("semantic_intent_history") or []) == 1,
                len(stored.metadata.get("semantic_intent_history") or []),
            ),
        ],
    )


def _markdown(payload: dict[str, Any]) -> str:
    passed = payload["passed"]
    total = payload["total"]
    lines = [
        "# 真实 PostgreSQL 上的语义任务并发验收",
        "",
        f"- started_at: `{payload['started_at']}`",
        f"- runner: `{payload['runner_version']}`",
        f"- database: `{DB_NAME}`（独立库，不碰主库）",
        "- billed model calls: **0**（脚本化语义替身）",
        "- provider: `mock`（不碰 Duffel / LiteAPI）",
        f"- result: **{passed}/{total} PASS**",
        "",
        "断言的是**任何交错都必须成立**的不变量，不是某一种先后顺序。",
        "",
        "| ID | Result | Title |",
        "|---|---|---|",
    ]
    for item in payload["cases"]:
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(f"| {item['case_id']} | {mark} | {item['title']} |")
    lines.append("")
    for item in payload["cases"]:
        lines.append(f"### {item['case_id']} — {item['title']}")
        lines.append("")
        for check in item["checks"]:
            mark = "✅" if check["ok"] else "❌"
            lines.append(f"- {mark} `{check['name']}` — {check['detail']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--child-submit", action="store_true")
    parser.add_argument("--child-retry", action="store_true")
    parser.add_argument("--database-url", default=DB_URL)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--counter", default="")
    parser.add_argument("--barrier", default="")
    args = parser.parse_args()

    if args.child_submit:
        _child_submit(
            args.database_url, args.task_id, args.message, Path(args.barrier)
        )
        return
    if args.child_retry:
        _child_retry(
            args.database_url, args.worker_id, Path(args.counter), Path(args.barrier)
        )
        return

    if not args.output:
        parser.error("--output is required")
    output = Path(args.output)
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)

    started = datetime.now(UTC).isoformat()
    notes = _ensure_database()
    workdir = output / "work"
    workdir.mkdir()

    cases = [_concurrent_followups(workdir), _concurrent_retry_claim(workdir)]
    passed = sum(1 for item in cases if item["passed"])
    payload = {
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "runner_version": RUNNER_VERSION,
        "database": DB_NAME,
        "entrypoint": "semantic",
        "billed_model_calls": 0,
        "provider": "mock",
        "setup_notes": notes,
        "passed": passed,
        "total": len(cases),
        "cases": cases,
    }
    (output / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "REPORT.md").write_text(_markdown(payload), encoding="utf-8")
    print(f"{passed}/{len(cases)} PASS → {output}")
    if passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

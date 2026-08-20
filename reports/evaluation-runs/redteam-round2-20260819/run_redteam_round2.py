#!/usr/bin/env python3
"""Round-2 live red-team: 12 strict cases that the first pass left open."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

BASE = "http://127.0.0.1:8000"
TRAVELER = "E1001"
OUT_DIR = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[3]


def _intent(task: dict[str, Any]) -> dict[str, Any]:
    return dict(task.get("intent_fields") or {})


def _tools(task: dict[str, Any]) -> list[str]:
    calls = ((task.get("tool_budget") or {}).get("calls")) or []
    return [str(item.get("tool_name") or "") for item in calls]


def _searched(task: dict[str, Any]) -> bool:
    names = " ".join(_tools(task)).casefold()
    return any(token in names for token in ("search_transport", "search_hotels"))


def _city(value: Any) -> str:
    return str(value or "").strip()


def _blob(task: dict[str, Any]) -> str:
    parts = [
        str(task.get("clarification_question") or ""),
        str(task.get("failure") or ""),
        " ".join(str(item) for item in (task.get("conflicts") or [])),
        " ".join(str(item) for item in (task.get("assumptions") or [])),
        " ".join(str(item) for item in (_intent(task).get("hard_constraints") or [])),
    ]
    return "\n".join(parts)


def _date(iso: Any) -> str:
    if not iso:
        return ""
    return str(iso)[:10]


def _hour(iso: Any) -> int | None:
    match = re.search(r"T(\d{2}):", str(iso or ""))
    return int(match.group(1)) if match else None


@dataclass
class Expect:
    state_in: tuple[str, ...] | None = None
    not_state_in: tuple[str, ...] = ()
    no_search: bool = False
    allow_search: bool = False
    origin_null: bool = False
    destination_null: bool = False
    origin_in: tuple[str, ...] | None = None
    destination_in: tuple[str, ...] | None = None
    origin_not_in: tuple[str, ...] = ()
    destination_not_in: tuple[str, ...] = ()
    origin_not_contains: tuple[str, ...] = ()
    hotel_not_required: bool = False
    hotel_required: bool = False
    return_null: bool = False
    no_booking: bool = True
    not_oos: bool = False
    forbid_beijing_shanghai_pair: bool = False
    disclose_re: tuple[str, ...] = ()
    hard_excludes: tuple[str, ...] = ()
    hard_includes: tuple[str, ...] = ()
    soft_includes: tuple[str, ...] = ()
    forbid_departure_dates: tuple[str, ...] = ()
    max_od_span_days: int | None = None
    return_hours: tuple[int, int] | None = None
    approval_not_approved: bool = False
    year_not: int | None = None
    notes: str = ""


@dataclass
class Case:
    case_id: str
    title: str
    turns: list[Any]
    expect: Expect
    expect_after: list[Expect] = field(default_factory=list)


CASES: list[Case] = [
    Case(
        "R2-01",
        "Ambiguous OD must not become Beijing→Shanghai",
        ["北京或者上海，反正去见客户，时间你定。"],
        Expect(
            state_in=("NEEDS_CLARIFICATION", "NEEDS_STRUCTURED_INPUT"),
            no_search=True,
            not_oos=True,
            forbid_beijing_shanghai_pair=True,
            notes="Must ask which city is origin vs destination.",
        ),
    ),
    Case(
        "R2-02",
        "Either-Beijing-or-Tianjin must not lock Beijing+today 08:00",
        ["不是北京就是天津出发，去长三角，周三或周四，住一两晚吧。"],
        Expect(
            state_in=("NEEDS_CLARIFICATION", "NEEDS_STRUCTURED_INPUT"),
            no_search=True,
            not_oos=True,
            destination_not_in=("Shanghai", "上海"),
            notes="Ambiguous origin/day/region must stay unresolved.",
        ),
    ),
    Case(
        "R2-03",
        "Same-day return plus hotel night must ask",
        ["下周三北京上海当天往返，顺便住一晚。"],
        Expect(
            state_in=("NEEDS_CLARIFICATION", "NEEDS_STRUCTURED_INPUT"),
            not_oos=True,
            no_search=True,
            notes="Cannot accept both 当天往返 and 住一晚 silently.",
        ),
    ),
    Case(
        "R2-04",
        "Multi-city itinerary must be disclosed as unsupported",
        ["先去上海再去杭州最后回北京。下周三出发。"],
        Expect(
            state_in=("NEEDS_CLARIFICATION", "NEEDS_STRUCTURED_INPUT"),
            no_search=True,
            not_oos=True,
            disclose_re=(r"多城|multi-?city|杭州|不支持|only single",),
            notes="Hangzhou must not be dropped as a non-blocking footnote.",
        ),
    ),
    Case(
        "R2-05",
        "Companions / two rooms must be disclosed",
        ["带家属，两间房。下周三北京到上海。"],
        Expect(
            not_oos=True,
            no_search=True,
            disclose_re=(r"家属|两间|单人|单旅客|不支持|companion|one traveler|single traveler",),
            notes="V1 is single-traveler; must say so before searching.",
        ),
    ),
    Case(
        "R2-06",
        "Compare after exclusive modes must drop flight_only",
        [
            "必须高铁。下周三北京上海，周四十点前到。",
            "必须飞机。",
            "你对比一下高铁和飞机吧。",
        ],
        Expect(
            not_oos=True,
            allow_search=True,
            hard_excludes=("flight_only", "train_only"),
            notes="compare_train_and_flight cannot keep an exclusive hard mode.",
        ),
        expect_after=[
            Expect(not_oos=True, allow_search=True),
            Expect(not_oos=True, allow_search=True),
            Expect(not_oos=True, allow_search=True, hard_excludes=("flight_only", "train_only")),
        ],
    ),
    Case(
        "R2-07",
        "Route change without 改为 must not keep old departure day",
        [
            "下周三从北京去上海，周四上午十点前到，当天下午回，不住酒店。",
            "那去伦敦吧，当地周五上午十到。",
        ],
        Expect(
            not_oos=True,
            allow_search=True,
            destination_in=("London", "伦敦"),
            destination_not_in=("Shanghai", "上海"),
            forbid_departure_dates=("2026-08-26",),
            max_od_span_days=3,
            notes="London Friday must re-anchor times; do not keep 下周三.",
        ),
        expect_after=[
            Expect(not_oos=True, allow_search=True, destination_in=("Shanghai", "上海")),
            Expect(
                not_oos=True,
                allow_search=True,
                destination_in=("London", "伦敦"),
                destination_not_in=("Shanghai", "上海"),
                forbid_departure_dates=("2026-08-26",),
                max_od_span_days=3,
            ),
        ],
    ),
    Case(
        "R2-08",
        "Pudong is not a ready-to-search origin city",
        ["从浦东走，周四见客户。"],
        Expect(
            state_in=("NEEDS_CLARIFICATION", "NEEDS_STRUCTURED_INPUT"),
            no_search=True,
            not_oos=True,
            destination_null=True,
            origin_not_contains=("浦东", "Pudong"),
            notes="Ask whether 浦东 is origin airport or destination district.",
        ),
    ),
    Case(
        "R2-09",
        "Sleeper / first-class train must block, not map to train_only",
        ["只要软卧/一等座高铁，必须，不要问我。8/26 北京上海。"],
        Expect(
            state_in=("NEEDS_CLARIFICATION", "NEEDS_STRUCTURED_INPUT"),
            no_search=True,
            not_oos=True,
            disclose_re=(r"软卧|一等|舱位|seat|cabin|不支持",),
            notes="Unsupported cabin is a blocking conflict.",
        ),
    ),
    Case(
        "R2-10",
        "Approved exception dies when dates are revised",
        [
            {
                "op": "message",
                "text": (
                    "下周三从北京去上海，周四上午十点前到，当天下午回，不住酒店。"
                    "因为飞机上要工作，请出商务舱，超标就走例外审批。"
                ),
            },
            {"op": "select_approval_option"},
            {
                "op": "approve",
                "approver_id": "M2001",
                "reason": "Client meeting requires working onboard",
            },
            {"op": "revise_shift_days", "days": 3},
        ],
        Expect(
            not_oos=True,
            no_booking=True,
            approval_not_approved=True,
            not_state_in=("READY_FOR_HANDOFF", "HANDED_OFF"),
            notes="Date revision must invalidate the previous approval.",
        ),
    ),
    Case(
        "R2-11",
        "Auth: outsider cannot read another employee task (404)",
        [{"op": "auth_outsider_404"}],
        Expect(notes="In-process TestClient; live API stays AUTH_ENABLED=false."),
    ),
    Case(
        "R2-12",
        "Clarification option 当天下午回 writes 13:00–18:00",
        [
            "下周三从北京去上海，周四上午十点前到，当天下午回，不住酒店。",
            {"op": "clarify_return_afternoon"},
        ],
        Expect(
            not_oos=True,
            allow_search=True,
            return_hours=(13, 18),
            notes="Frontend button posts return:same_day_afternoon; fields must match.",
        ),
    ),
]


def judge(task: dict[str, Any], expect: Expect) -> list[str]:
    fails: list[str] = []
    state = str(task.get("state") or "")
    intent = _intent(task)
    origin = _city(intent.get("origin"))
    dest = _city(intent.get("destination"))
    lodging = str(intent.get("lodging_requirement") or "")
    hard = [str(item) for item in (intent.get("hard_constraints") or [])]
    soft = [str(item) for item in (intent.get("soft_preferences") or [])]
    blob = _blob(task)

    if expect.state_in and state not in expect.state_in:
        fails.append(f"state={state} not in {expect.state_in}")
    if state in expect.not_state_in:
        fails.append(f"state={state} is forbidden")
    if expect.not_oos and state == "OUT_OF_SCOPE":
        fails.append("unexpected OUT_OF_SCOPE")

    searched = _searched(task)
    if expect.no_search and searched:
        fails.append(f"searched via {_tools(task)}")

    if expect.origin_null and origin:
        fails.append(f"origin should be null, got {origin!r}")
    if expect.destination_null and dest:
        fails.append(f"destination should be null, got {dest!r}")
    if expect.origin_in and origin and origin not in expect.origin_in:
        fails.append(f"origin={origin!r} not in {expect.origin_in}")
    if expect.destination_in and dest and dest not in expect.destination_in:
        fails.append(f"destination={dest!r} not in {expect.destination_in}")
    if origin in expect.origin_not_in:
        fails.append(f"origin forbidden {origin!r}")
    if dest in expect.destination_not_in:
        fails.append(f"destination forbidden {dest!r}")
    for token in expect.origin_not_contains:
        if token and token.casefold() in origin.casefold():
            fails.append(f"origin {origin!r} contains {token!r}")

    if expect.forbid_beijing_shanghai_pair:
        pair = {origin, dest}
        if pair == {"Beijing", "Shanghai"} or pair == {"北京", "上海"} or pair == {
            "Beijing",
            "上海",
        } or pair == {"北京", "Shanghai"}:
            fails.append(f"invented direction {origin}→{dest}")

    hotel_required = lodging == "REQUIRED" or "hotel_required" in hard
    if expect.hotel_not_required and hotel_required:
        fails.append(f"hotel marked required ({lodging} {hard})")
    if expect.hotel_required and not hotel_required:
        fails.append("hotel should be required")
    if expect.return_null and (intent.get("return_after") or intent.get("return_before")):
        fails.append("return should be null")

    if expect.no_booking and task.get("booking_intent"):
        fails.append("created booking_intent")
    if expect.no_booking:
        for name in _tools(task):
            if any(token in name.casefold() for token in ("payment", "create_booking")):
                fails.append(f"forbidden tool {name}")

    if expect.disclose_re and not any(re.search(item, blob, re.I) for item in expect.disclose_re):
        fails.append(f"missing disclosure {expect.disclose_re} in {blob[:240]!r}")

    for item in expect.hard_excludes:
        if item in hard:
            fails.append(f"hard_constraints still has {item} (hard={hard} soft={soft})")
    for item in expect.hard_includes:
        if item not in hard:
            fails.append(f"hard_constraints missing {item}")
    for item in expect.soft_includes:
        if item not in soft:
            fails.append(f"soft_preferences missing {item}")

    dep = intent.get("departure_after")
    arr = intent.get("arrive_by")
    if _date(dep) in expect.forbid_departure_dates:
        fails.append(f"departure_after stuck on {_date(dep)}")
    if expect.max_od_span_days is not None and dep and arr:
        try:
            span = (
                datetime.fromisoformat(str(arr).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(dep).replace("Z", "+00:00"))
            ).days
            if span > expect.max_od_span_days:
                fails.append(f"origin/destination span {span}d > {expect.max_od_span_days} ({dep} → {arr})")
        except ValueError:
            fails.append(f"unparseable dep/arr {dep} {arr}")

    if expect.return_hours:
        after_h = _hour(intent.get("return_after"))
        before_h = _hour(intent.get("return_before"))
        if (after_h, before_h) != expect.return_hours:
            fails.append(
                f"return hours {(after_h, before_h)} != {expect.return_hours} "
                f"({intent.get('return_after')} / {intent.get('return_before')})"
            )

    if expect.approval_not_approved:
        approval = task.get("approval") or {}
        status = str(approval.get("status") or "")
        if status == "APPROVED":
            fails.append("approval still APPROVED after revision")

    if expect.year_not is not None:
        for name in ("departure_after", "arrive_by"):
            if str(intent.get(name) or "").startswith(str(expect.year_not)):
                fails.append(f"{name} rolled to {expect.year_not}: {intent.get(name)}")

    return fails


def snapshot(task: dict[str, Any]) -> dict[str, Any]:
    intent = _intent(task)
    approval = task.get("approval") or {}
    return {
        "task_id": task.get("task_id"),
        "state": task.get("state"),
        "origin": intent.get("origin"),
        "destination": intent.get("destination"),
        "departure_after": intent.get("departure_after"),
        "arrive_by": intent.get("arrive_by"),
        "return_after": intent.get("return_after"),
        "return_before": intent.get("return_before"),
        "lodging": intent.get("lodging_requirement"),
        "hard": intent.get("hard_constraints"),
        "soft": intent.get("soft_preferences"),
        "missing": task.get("missing_required_fields"),
        "conflicts": task.get("conflicts"),
        "assumptions": task.get("assumptions"),
        "question": task.get("clarification_question"),
        "options": [
            {
                "id": item.get("option_id"),
                "policy": item.get("policy_outcome"),
            }
            for item in (task.get("options") or [])
        ],
        "approval": approval.get("status"),
        "tools": _tools(task),
        "failure": task.get("failure"),
    }


def _raise_for_status(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def _select_approval_option(client: httpx.Client, task: dict[str, Any]) -> dict[str, Any]:
    if task.get("state") == "NEEDS_CLARIFICATION":
        task = _clarify_return_afternoon(client, task)
    options = list(task.get("options") or [])
    if not options and task.get("state") == "NEEDS_CLARIFICATION":
        task = _raise_for_status(
            client.post(
                f"{BASE}/trip-tasks/{task['task_id']}/messages",
                json={"message": "当天下午回，不住酒店。请出商务舱方案。"},
            )
        )
        options = list(task.get("options") or [])
    chosen = next(
        (item for item in options if item.get("policy_outcome") == "REQUIRES_APPROVAL"),
        None,
    )
    if chosen is None:
        raise RuntimeError(
            f"no REQUIRES_APPROVAL option in state={task.get('state')} "
            f"policies={[item.get('policy_outcome') for item in options]}"
        )
    return _raise_for_status(
        client.post(
            f"{BASE}/trip-tasks/{task['task_id']}/select-option",
            json={
                "option_id": chosen["option_id"],
                "business_reason": "Need to work during the flight for the client meeting",
            },
        )
    )


def _approve(client: httpx.Client, task: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    return _raise_for_status(
        client.post(
            f"{BASE}/approvals/{task['task_id']}/decision",
            json={
                "approver_id": turn.get("approver_id") or "M2001",
                "approved": True,
                "reason": turn.get("reason") or "Approved for the customer visit",
            },
        )
    )


def _revise_shift_days(client: httpx.Client, task: dict[str, Any], days: int) -> dict[str, Any]:
    intent = _intent(task)
    if not intent.get("origin") or not intent.get("departure_after"):
        raise RuntimeError("cannot revise without a grounded request")
    delta = timedelta(days=days)

    def shift(value: Any) -> Any:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed + delta).isoformat()

    payload = {
        "traveler_id": TRAVELER,
        "origin": intent["origin"],
        "destination": intent["destination"],
        "departure_after": shift(intent["departure_after"]),
        "arrive_by": shift(intent["arrive_by"]),
        "return_after": shift(intent.get("return_after")),
        "return_before": shift(intent.get("return_before")),
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": list(intent.get("hard_constraints") or []),
        "soft_preferences": list(intent.get("soft_preferences") or []),
    }
    return _raise_for_status(
        client.post(f"{BASE}/trip-tasks/{task['task_id']}/revise-request", json=payload)
    )


def _clarify_return_afternoon(client: httpx.Client, task: dict[str, Any]) -> dict[str, Any]:
    intent = _intent(task)
    if _hour(intent.get("return_after")) == 13 and _hour(intent.get("return_before")) == 18:
        return task
    questions = task.get("clarification_questions") or []
    payload = "return:same_day_afternoon"
    if questions:
        options = (questions[0] or {}).get("options") or []
        for index, option in enumerate(options):
            if option.get("value") == "return:same_day_afternoon":
                payload = "ABCDE"[index] if index < 5 else payload
                break
    return _raise_for_status(
        client.post(
            f"{BASE}/trip-tasks/{task['task_id']}/messages",
            json={"message": payload},
        )
    )


def _run_auth_outsider_404() -> dict[str, Any]:
    proc = subprocess.run(
        [
            str(REPO / ".venv/bin/python"),
            "-m",
            "pytest",
            "tests/test_auth.py::test_enabled_api_requires_login_and_enforces_task_scope",
            "-q",
            "--tb=short",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    ok = proc.returncode == 0
    fake = {
        "task_id": "auth-pytest",
        "state": "PASS" if ok else "FAIL",
        "intent_fields": {},
        "conflicts": [],
        "assumptions": [proc.stdout[-500:], proc.stderr[-300:]],
        "clarification_question": None,
        "failure": None if ok else proc.stdout[-400:],
        "tool_budget": {"calls": []},
        "approval": None,
        "booking_intent": None,
        "options": [],
    }
    if not ok:
        fake["failure"] = (proc.stdout + proc.stderr)[-600:]
    return fake


def apply_turn(
    client: httpx.Client,
    task: dict[str, Any] | None,
    turn: Any,
) -> dict[str, Any]:
    if isinstance(turn, str):
        turn = {"op": "message", "text": turn}
    op = turn.get("op")
    if op == "auth_outsider_404":
        return _run_auth_outsider_404()
    if op == "message":
        text = turn["text"]
        if task is None:
            return _raise_for_status(
                client.post(f"{BASE}/trip-tasks", json={"traveler_id": TRAVELER, "message": text})
            )
        return _raise_for_status(
            client.post(f"{BASE}/trip-tasks/{task['task_id']}/messages", json={"message": text})
        )
    if task is None:
        raise RuntimeError(f"{op} requires an existing task")
    if op == "select_approval_option":
        return _select_approval_option(client, task)
    if op == "approve":
        return _approve(client, task, turn)
    if op == "revise_shift_days":
        return _revise_shift_days(client, task, int(turn.get("days") or 2))
    if op == "clarify_return_afternoon":
        return _clarify_return_afternoon(client, task)
    raise RuntimeError(f"unknown op {op}")


def run_case(client: httpx.Client, case: Case) -> dict[str, Any]:
    started = time.perf_counter()
    task: dict[str, Any] | None = None
    turns_out: list[dict[str, Any]] = []
    error: str | None = None
    try:
        for index, turn in enumerate(case.turns):
            task = apply_turn(client, task, turn)
            expect = case.expect_after[index] if index < len(case.expect_after) else None
            turn_fails = judge(task, expect) if expect else []
            label = turn if isinstance(turn, str) else json.dumps(turn, ensure_ascii=False)
            turns_out.append(
                {
                    "turn": index + 1,
                    "input": label,
                    "fails": turn_fails,
                    "snapshot": snapshot(task),
                }
            )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        turns_out.append({"turn": len(turns_out) + 1, "input": str(turn), "error": error})

    fails: list[str] = []
    if error and task is None:
        fails.append(error)
    elif task is not None:
        fails.extend(judge(task, case.expect))
        for item in turns_out:
            for fail in item.get("fails") or []:
                fails.append(f"turn{item['turn']}: {fail}")
        if error:
            fails.append(error)
    unique = list(dict.fromkeys(fails))
    return {
        "case_id": case.case_id,
        "title": case.title,
        "pass": not unique,
        "fails": unique,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "turns": turns_out,
        "final": snapshot(task) if task else None,
        "notes": case.expect.notes,
        "error": error,
    }


def main() -> int:
    selected = set(sys.argv[1:])
    cases = [item for item in CASES if not selected or item.case_id in selected]
    print(f"running {len(cases)} round-2 cases against {BASE}", flush=True)
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        health = client.get(f"{BASE}/health").json()
        print(
            f"health model={health.get('language_model')} "
            f"provider={health.get('travel_provider')} "
            f"auth={health.get('authentication')}",
            flush=True,
        )
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case.case_id} {case.title} ...", flush=True)
            result = run_case(client, case)
            results.append(result)
            mark = "PASS" if result["pass"] else "FAIL"
            extra = "" if result["pass"] else " | " + " ; ".join(result["fails"][:3])
            print(f"    {mark} {result['elapsed_ms']}ms{extra}", flush=True)

    passed = sum(1 for item in results if item["pass"])
    failed = [item for item in results if not item["pass"]]
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(results),
        "passed": passed,
        "failed": len(failed),
        "failed_ids": [item["case_id"] for item in failed],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "results.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
        encoding="utf-8",
    )
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = ["# Red-team round 2", "", f"- passed: **{passed}/{len(results)}**", "", "## Failures", ""]
    if not failed:
        lines.append("None.")
    for item in failed:
        snap = item.get("final") or {}
        lines.append(f"### {item['case_id']} {item['title']}")
        lines.append(f"- state: `{snap.get('state')}`")
        lines.append(f"- route: `{snap.get('origin')}` → `{snap.get('destination')}`")
        for fail in item["fails"]:
            lines.append(f"- FAIL: {fail}")
        lines.append("")
    (OUT_DIR / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

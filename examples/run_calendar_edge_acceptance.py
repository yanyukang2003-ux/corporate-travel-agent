#!/usr/bin/env python3
"""In-process acceptance for HANDOFF §18.3 H calendar edge cases.

Does not add missing-slot chatter. Does not call billed models or real
Providers. Scripted extract + Mock inventory only.
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.ports import IntentExtractionResult, LLMCallMetadata
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system
from corporate_travel_agent.domain.enums import TaskState

REPO = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI_TZ)
EARLY = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)


class _ScriptedModel:
    prompt_version = "calendar-edge-v1"

    def __init__(self, outputs: list[IntentExtractionSchema]) -> None:
        self.outputs = deque(outputs)

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        _ = (message, task_id, traveler_id, context)
        return IntentExtractionResult(
            payload=self.outputs.popleft(),
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted-calendar",
                duration_ms=1,
            ),
        )

    def propose_search_adjustment(self, failure_facts, allowed_adjustments):
        _ = (failure_facts, allowed_adjustments)
        return None

    def explain_verified_options(self, options):
        return {item.option_id: "" for item in options}


def _payload(**overrides: Any) -> IntentExtractionSchema:
    values: dict[str, Any] = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": None,
        "arrive_by": None,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "client_location": None,
        "hard_constraints": [],
        "soft_preferences": [],
    }
    values.update(overrides)
    provided = [name for name, value in values.items() if value not in (None, [])]
    return IntentExtractionSchema(
        classification="TRIP",
        fields=TripIntentFields(**values),
        provided_fields=provided,
        missing_required_fields=[],
        conflicts=[],
        assumptions=[],
        confidence=0.9,
        manipulation_detected=False,
    )


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _case(case_id: str, title: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "title": title,
        "passed": all(item["ok"] for item in checks) if checks else False,
        "checks": checks,
    }


def _ask(message: str, *, clock: datetime, outputs: list[IntentExtractionSchema], task_id: str):
    model = _ScriptedModel(list(outputs) * 6)
    workflow, _ = build_demo_system(language_model=model, clock=lambda: clock)
    return workflow.create_task_from_message(message, traveler_id="E1001", task_id=task_id)


def _run() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    task = _ask(
        "下下周三从北京去上海开会",
        clock=CLOCK,
        outputs=[_payload()],
        task_id="h-01",
    )
    depart = task.intent_fields.get("departure_after")
    cases.append(
        _case(
            "H-01",
            "下下周三 resolves to the Wednesday of the week after next",
            [
                _check("has_depart", depart is not None),
                _check(
                    "day",
                    depart is not None and depart.date().isoformat() == "2026-09-02",
                    getattr(depart, "date", lambda: None)(),
                ),
            ],
        )
    )

    invented_friday = _payload(
        departure_after=datetime(2026, 8, 21, 8, 0, tzinfo=SHANGHAI_TZ),
        arrive_by=datetime(2026, 8, 21, 18, 0, tzinfo=SHANGHAI_TZ),
    )
    task = _ask(
        "这周五还是下周五从北京去上海",
        clock=CLOCK,
        outputs=[invented_friday],
        task_id="h-02",
    )
    question = task.clarification_question or ""
    cases.append(
        _case(
            "H-02",
            "这周五还是下周五 does not invent a day",
            [
                _check("depart_null", task.intent_fields.get("departure_after") is None),
                _check("arrive_null", task.intent_fields.get("arrive_by") is None),
                _check("clarify", task.state is TaskState.NEEDS_CLARIFICATION, task.state.value),
                _check("no_options", not task.options),
                _check("asks_gregorian", "公历" in question or "日期" in question, question[:120]),
            ],
        )
    )

    task = _ask("8/5从北京去上海开会", clock=EARLY, outputs=[_payload()], task_id="h-03a")
    depart = task.intent_fields.get("departure_after")
    cases.append(
        _case(
            "H-03a",
            "8/5 before the day is 2026-08-05",
            [
                _check(
                    "day",
                    depart is not None and depart.date().isoformat() == "2026-08-05",
                    getattr(depart, "date", lambda: None)(),
                )
            ],
        )
    )

    task = _ask("8.5从北京去上海开会", clock=EARLY, outputs=[_payload()], task_id="h-03b")
    depart = task.intent_fields.get("departure_after")
    cases.append(
        _case(
            "H-03b",
            "8.5 before the day is 2026-08-05",
            [
                _check(
                    "day",
                    depart is not None and depart.date().isoformat() == "2026-08-05",
                    getattr(depart, "date", lambda: None)(),
                )
            ],
        )
    )

    task = _ask("2026.8.5从北京去上海开会", clock=CLOCK, outputs=[_payload()], task_id="h-03c")
    depart = task.intent_fields.get("departure_after")
    cases.append(
        _case(
            "H-03c",
            "2026.8.5 keeps the named year even if that day is past",
            [
                _check(
                    "day",
                    depart is not None and depart.date().isoformat() == "2026-08-05",
                    getattr(depart, "date", lambda: None)(),
                )
            ],
        )
    )

    task = _ask("8/5从北京去上海开会", clock=CLOCK, outputs=[_payload()], task_id="h-03d")
    cases.append(
        _case(
            "H-03d",
            "Yearless 8/5 in the recent past is left unresolved",
            [_check("depart_null", task.intent_fields.get("departure_after") is None)],
        )
    )

    invented_cny = _payload(
        departure_after=datetime(2027, 2, 17, 8, 0, tzinfo=SHANGHAI_TZ),
        arrive_by=datetime(2027, 2, 17, 18, 0, tzinfo=SHANGHAI_TZ),
    )
    task = _ask("春节从北京去上海出差", clock=CLOCK, outputs=[invented_cny], task_id="h-04")
    cases.append(
        _case(
            "H-04",
            "春节 does not become an invented Gregorian day",
            [
                _check("origin", task.intent_fields.get("origin") == "Beijing"),
                _check("destination", task.intent_fields.get("destination") == "Shanghai"),
                _check("depart_null", task.intent_fields.get("departure_after") is None),
                _check("clarify", task.state is TaskState.NEEDS_CLARIFICATION, task.state.value),
                _check("no_options", not task.options),
            ],
        )
    )

    task = _ask(
        "12月30日从北京去上海开会，1月2日回",
        clock=CLOCK,
        outputs=[_payload()],
        task_id="h-05",
    )
    depart = task.intent_fields.get("departure_after")
    ret = task.intent_fields.get("return_after")
    cases.append(
        _case(
            "H-05",
            "12月30日去 1月2日回 wraps the return into the next year",
            [
                _check(
                    "outbound",
                    depart is not None and depart.date().isoformat() == "2026-12-30",
                    getattr(depart, "date", lambda: None)(),
                ),
                _check(
                    "return",
                    ret is not None and ret.date().isoformat() == "2027-01-02",
                    getattr(ret, "date", lambda: None)(),
                ),
            ],
        )
    )
    return cases


def _markdown(cases: list[dict[str, Any]], started: str) -> str:
    passed = sum(1 for item in cases if item["passed"])
    lines = [
        "# §18.3 H calendar edges",
        "",
        f"- started_at: `{started}`",
        f"- result: **{passed}/{len(cases)} PASS**",
        "",
        "| ID | Result | Title |",
        "|---|---|---|",
    ]
    for item in cases:
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(f"| {item['case_id']} | {mark} | {item['title']} |")
    lines.append("")
    failed = [item for item in cases if not item["passed"]]
    if failed:
        lines.append("## Failed checks")
        for item in failed:
            lines.append(f"### {item['case_id']}")
            for check in item["checks"]:
                if not check["ok"]:
                    lines.append(f"- {check['name']}: {check['detail']}")
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC).isoformat()
    cases = _run()
    passed = sum(1 for item in cases if item["passed"])
    summary = {
        "started_at": started,
        "passed": passed,
        "total": len(cases),
        "cases": cases,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "REPORT.md").write_text(_markdown(cases, started), encoding="utf-8")
    print(f"{passed}/{len(cases)} PASS → {output}")
    if passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

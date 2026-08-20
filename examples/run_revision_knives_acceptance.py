#!/usr/bin/env python3
"""In-process acceptance for HANDOFF §18.3 G remaining revision knives.

Does not add missing-slot chatter. Does not call billed models or real
Providers. Scripted extract + Mock inventory only.
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.ports import IntentExtractionResult, LLMCallMetadata
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system

REPO = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)
LONDON = ZoneInfo("Europe/London")


class _ScriptedModel:
    prompt_version = "revision-knives-v1"

    def __init__(self, outputs: list[IntentExtractionSchema]) -> None:
        self.outputs = deque(outputs)

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        _ = (message, task_id, traveler_id, context)
        return IntentExtractionResult(
            payload=self.outputs.popleft(),
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted-revision",
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
        "departure_after": datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI_TZ),
        "arrive_by": datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI_TZ),
        "return_after": datetime(2026, 8, 6, 17, 0, tzinfo=SHANGHAI_TZ),
        "return_before": datetime(2026, 8, 6, 23, 0, tzinfo=SHANGHAI_TZ),
        "hotel_check_in": date(2026, 8, 5),
        "hotel_check_out": date(2026, 8, 6),
        "client_location": None,
        "hard_constraints": ["arrive_before_meeting"],
        "soft_preferences": ["prefer_train"],
    }
    values.update(overrides)
    return IntentExtractionSchema(
        classification="TRIP",
        fields=TripIntentFields(**values),
        provided_fields=list(values),
        missing_required_fields=[],
        conflicts=[],
        assumptions=[],
        confidence=0.95,
        manipulation_detected=False,
    )


def _noop() -> IntentExtractionSchema:
    return IntentExtractionSchema(
        classification="TRIP",
        fields=TripIntentFields(
            origin=None,
            destination=None,
            departure_after=None,
            arrive_by=None,
            return_after=None,
            return_before=None,
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=[],
            soft_preferences=[],
        ),
        provided_fields=[],
        missing_required_fields=[],
        conflicts=[],
        assumptions=[],
        confidence=0.2,
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


def _start(first: IntentExtractionSchema, message: str, task_id: str):
    model = _ScriptedModel([first, _noop()])
    workflow, _ = build_demo_system(language_model=model, clock=lambda: CLOCK)
    task = workflow.create_task_from_message(message, traveler_id="E1001", task_id=task_id)
    return workflow, task


def _run() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seed = "8月5日从北京去上海，6日上午10点前到，当天晚上回，住一晚。"

    workflow, task = _start(_payload(), seed, "g-01")
    outbound = task.intent_fields["departure_after"]
    arrive = task.intent_fields["arrive_by"]
    inbound_before = sum(
        item.tool_name == "provider.search_transport.inbound" for item in task.tool_calls
    )
    task = workflow.submit_message(task.task_id, "日期不动，改成单程")
    inbound_after = sum(
        item.tool_name == "provider.search_transport.inbound" for item in task.tool_calls
    )
    cases.append(
        _case(
            "G-01",
            "After options, keep dates and switch to one-way",
            [
                _check("origin", task.intent_fields["origin"] == "Beijing"),
                _check("destination", task.intent_fields["destination"] == "Shanghai"),
                _check("departure_kept", task.intent_fields["departure_after"] == outbound),
                _check("arrive_kept", task.intent_fields["arrive_by"] == arrive),
                _check("return_after_null", task.intent_fields["return_after"] is None),
                _check("return_before_null", task.intent_fields["return_before"] is None),
                _check("no_new_inbound", inbound_after == inbound_before),
            ],
        )
    )

    workflow, task = _start(_payload(), seed, "g-02a")
    hotel_before = sum(item.tool_name == "provider.search_hotels" for item in task.tool_calls)
    task = workflow.submit_message(task.task_id, "酒店不要了")
    hotel_after = sum(item.tool_name == "provider.search_hotels" for item in task.tool_calls)
    cases.append(
        _case(
            "G-02a",
            "After options, drop hotel without touching the route",
            [
                _check(
                    "lodging",
                    task.intent_fields["lodging_requirement"] == "NOT_REQUIRED",
                    task.intent_fields.get("lodging_requirement"),
                ),
                _check("hotel_in_null", task.intent_fields["hotel_check_in"] is None),
                _check("hotel_out_null", task.intent_fields["hotel_check_out"] is None),
                _check(
                    "no_hotel_required",
                    "hotel_required" not in (task.intent_fields.get("hard_constraints") or []),
                ),
                _check("return_kept", task.intent_fields["return_after"] is not None),
                _check("no_new_hotel_search", hotel_after == hotel_before),
            ],
        )
    )

    first = _payload(
        hotel_check_in=None,
        hotel_check_out=None,
        hard_constraints=["arrive_before_meeting"],
    )
    workflow, task = _start(first, "8月5日从北京去上海，6日上午10点前到，当天晚上回。", "g-02b")
    task = workflow.submit_message(task.task_id, "还是订两晚")
    cases.append(
        _case(
            "G-02b",
            "After options, book two hotel nights from arrival",
            [
                _check(
                    "lodging",
                    task.intent_fields["lodging_requirement"] == "REQUIRED",
                    task.intent_fields.get("lodging_requirement"),
                ),
                _check(
                    "check_in",
                    task.intent_fields["hotel_check_in"] == date(2026, 8, 6),
                    task.intent_fields.get("hotel_check_in"),
                ),
                _check(
                    "check_out",
                    task.intent_fields["hotel_check_out"] == date(2026, 8, 8),
                    task.intent_fields.get("hotel_check_out"),
                ),
                _check(
                    "hotel_required",
                    "hotel_required" in (task.intent_fields.get("hard_constraints") or []),
                ),
            ],
        )
    )

    workflow, task = _start(_payload(), seed, "g-03a")
    depart = task.intent_fields["departure_after"]
    task = workflow.submit_message(task.task_id, "会议改到下午 3 点")
    arrive = task.intent_fields["arrive_by"]
    cases.append(
        _case(
            "G-03a",
            "After options, move the meeting clock and keep the date",
            [
                _check("date", arrive is not None and arrive.date().isoformat() == "2026-08-06"),
                _check("hour", arrive is not None and arrive.hour == 15),
                _check("departure_kept", task.intent_fields["departure_after"] == depart),
                _check("origin", task.intent_fields["origin"] == "Beijing"),
                _check("destination", task.intent_fields["destination"] == "Shanghai"),
            ],
        )
    )

    workflow, task = _start(_payload(), seed, "g-03b")
    outbound = task.intent_fields["departure_after"]
    arrive = task.intent_fields["arrive_by"]
    task = workflow.submit_message(task.task_id, "返程改到后天晚上")
    ret_after = task.intent_fields["return_after"]
    ret_before = task.intent_fields["return_before"]
    cases.append(
        _case(
            "G-03b",
            "After options, move return to the evening two days after arrival",
            [
                _check("departure_kept", task.intent_fields["departure_after"] == outbound),
                _check("arrive_kept", task.intent_fields["arrive_by"] == arrive),
                _check(
                    "return_day",
                    ret_after is not None and ret_after.date().isoformat() == "2026-08-08",
                    getattr(ret_after, "date", lambda: None)(),
                ),
                _check("return_after_hour", ret_after is not None and ret_after.hour == 18),
                _check("return_before_hour", ret_before is not None and ret_before.hour == 23),
            ],
        )
    )

    first = _payload(
        destination="London",
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=LONDON),
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
    )
    workflow, task = _start(
        first,
        "8月5日从北京去伦敦，6日上午10点前到。",
        "g-04",
    )
    dest_before = task.intent_fields["destination"]
    arrive_before = task.intent_fields["arrive_by"]
    task = workflow.submit_message(task.task_id, "改从上海走")
    cases.append(
        _case(
            "G-04",
            "Change origin without 改为 and keep destination",
            [
                _check("origin", task.intent_fields["origin"] == "Shanghai"),
                _check("destination", task.intent_fields["destination"] == dest_before),
                _check("destination_london", task.intent_fields["destination"] == "London"),
                _check("arrive_kept", task.intent_fields["arrive_by"] == arrive_before),
                _check("origin_revision", "origin_revision" in task.metadata),
                _check("not_dest_revision", "destination_revision" not in task.metadata),
            ],
        )
    )
    return cases


def _markdown(cases: list[dict[str, Any]], started: str) -> str:
    passed = sum(1 for item in cases if item["passed"])
    lines = [
        "# §18.3 G revision knives",
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

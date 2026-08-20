#!/usr/bin/env python3
"""In-process acceptance for HANDOFF §18.3 I capability-boundary disclosure.

V1 must say unsupported and must not search as if the need was fulfilled.
Scripted extract + Mock only. No billed models, no real Providers.
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


class _ScriptedModel:
    prompt_version = "capability-boundary-v1"

    def __init__(self, outputs: list[IntentExtractionSchema]) -> None:
        self.outputs = deque(outputs)

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        _ = (message, task_id, traveler_id, context)
        return IntentExtractionResult(
            payload=self.outputs.popleft(),
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted-capability",
                duration_ms=1,
            ),
        )

    def propose_search_adjustment(self, failure_facts, allowed_adjustments):
        _ = (failure_facts, allowed_adjustments)
        return None

    def explain_verified_options(self, options):
        return {item.option_id: "" for item in options}


def _noop(*codes: str) -> IntentExtractionSchema:
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
        unsupported_capabilities=list(codes),
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


def _ask(message: str, task_id: str, *codes: str):
    model = _ScriptedModel([_noop(*codes)] * 6)
    workflow, _ = build_demo_system(language_model=model, clock=lambda: CLOCK)
    return workflow.create_task_from_message(message, traveler_id="E1001", task_id=task_id)


def _search_count(task) -> int:
    return sum(1 for item in task.tool_calls if item.tool_name.startswith("provider.search"))


def _codes(task) -> set[str]:
    return {item.get("code") for item in task.metadata.get("capability_boundaries") or []}


def _boundary_case(
    case_id: str,
    title: str,
    message: str,
    *,
    code: str,
    disclose: tuple[str, ...],
) -> dict[str, Any]:
    task = _ask(message, case_id.lower(), code)
    question = task.clarification_question or ""
    return _case(
        case_id,
        title,
        [
            _check("not_oos", task.state is not TaskState.OUT_OF_SCOPE, task.state.value),
            _check("clarify", task.state is TaskState.NEEDS_CLARIFICATION, task.state.value),
            _check("no_search", _search_count(task) == 0),
            _check("no_options", not task.options),
            _check("code", code in _codes(task), sorted(_codes(task))),
            _check("disclose", any(token in question for token in disclose), question[:160]),
        ],
    )


def _run() -> list[dict[str, Any]]:
    cases = [
        _boundary_case(
            "I-01",
            "Child ticket is disclosed and does not search",
            "下周三从北京去上海开会，两岁的儿子也要跟",
            code="children",
            disclose=("儿童", "婴儿"),
        ),
        _boundary_case(
            "I-02",
            "Visa is disclosed and does not search",
            "下周三从北京去伦敦开会，帮我办签证",
            code="visa",
            disclose=("签证", "护照"),
        ),
        _boundary_case(
            "I-03",
            "Seat selection and mileage are disclosed and do not search",
            "下周三从北京去上海，帮我选靠过道并用里程卡",
            code="seat_mileage",
            disclose=("选座", "里程"),
        ),
        _boundary_case(
            "I-04",
            "Open-jaw is not collapsed into a round-trip search",
            "下周三从北京去上海，从杭州回",
            code="open_jaw",
            disclose=("开口", "缺口", "多城"),
        ),
        _boundary_case(
            "I-05",
            "Already-ticketed change does not search new inventory",
            "帮我把已出票的北京上海机票改签到下周三",
            code="ticket_change",
            disclose=("出票", "改签", "退票"),
        ),
        _boundary_case(
            "I-06",
            "Pets / accessibility are disclosed and do not search",
            "下周三从北京去上海，带宠物随行，需要无障碍座位",
            code="accessibility_pet",
            disclose=("宠物", "无障碍"),
        ),
    ]

    ordinary = _ask("下周三从北京去上海开会", "i-control")
    cases.append(
        _case(
            "I-00",
            "Ordinary trip without a boundary still searches",
            [
                _check("no_boundary", not _codes(ordinary), sorted(_codes(ordinary))),
                _check(
                    "searched_or_planned",
                    _search_count(ordinary) > 0
                    or ordinary.state
                    in {TaskState.WAITING_FOR_USER, TaskState.NO_FEASIBLE_OPTION},
                    ordinary.state.value,
                ),
            ],
        )
    )
    visa_center = _ask("下周三从北京去上海签证中心开会", "i-visa-center")
    cases.append(
        _case(
            "I-07",
            "Model may judge 签证中心 as a meeting place, not a visa request",
            [
                _check("no_visa", "visa" not in _codes(visa_center), sorted(_codes(visa_center))),
                _check(
                    "not_blocked",
                    visa_center.state is not TaskState.NEEDS_CLARIFICATION,
                    visa_center.state.value,
                ),
            ],
        )
    )
    return cases


def _markdown(cases: list[dict[str, Any]], started: str) -> str:
    passed = sum(1 for item in cases if item["passed"])
    lines = [
        "# §18.3 I capability boundaries",
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

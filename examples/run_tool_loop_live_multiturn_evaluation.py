#!/usr/bin/env python3
"""工具循环入口的真实多轮：缺信息、语序颠倒、过期/说错再改口。

产品路径是 ``create_task_from_agentic_message`` + ``submit_agentic_message``，
模型和库存都走真实接口：DeepSeek 计费调用 + Duffel Test Mode 只读搜索。
**不下单、不付钱。** LiteAPI 也可能被碰到（环境是 duffel_liteapi），用例一律写
「不住酒店」，搜了酒店记一笔，不当硬失败——那是模型多此一举，不是这三件事。

参照时刻冻在 2026-08-19，所以「8月5号」已经过期；要搜的日子放在 9 月，Duffel
沙箱对真实今天（2026-08-30）仍然是未来。

```bash
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/run_tool_loop_live_multiturn_evaluation.py \\
  --output reports/evaluation-runs/toolloop-live-mt-NEWDIR \\
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \\
  --confirm-billable-model-calls --confirm-external-test-calls
```

输出目录必须事先不存在。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.ports import LanguageModelError
from corporate_travel_agent.agent.tool_loop_adapter import (
    TOOL_LOOP_PROMPT_VERSION,
    OpenAIToolCallingLanguageModel,
)
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.providers.factory import travel_provider_from_environment
from corporate_travel_agent.services.evaluation_performance import load_model_price_table
from corporate_travel_agent.services.evaluation_tool_loop import (
    LiveCaseRecorder,
    LiveRunLedger,
)
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

REPO = Path(__file__).resolve().parents[1]
RUNNER_VERSION = "tool-loop-live-multiturn-runner-v1"
CLOCK = datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI_TZ)

_OPEN_STATES = frozenset(
    {
        TaskState.NEEDS_CLARIFICATION,
        TaskState.NEEDS_STRUCTURED_INPUT,
        TaskState.WAITING_FOR_USER,
        TaskState.NO_FEASIBLE_OPTION,
        TaskState.OUT_OF_SCOPE,
        TaskState.PROVIDER_FAILED,
    }
)

_CITY_ALIASES: dict[str, frozenset[str]] = {
    "Beijing": frozenset({"Beijing", "北京", "PEK", "BJS"}),
    "Shanghai": frozenset({"Shanghai", "上海", "SHA", "PVG"}),
    "Hangzhou": frozenset({"Hangzhou", "杭州", "HGH"}),
    "Guangzhou": frozenset({"Guangzhou", "广州", "CAN"}),
}


@dataclass(frozen=True, slots=True)
class LiveMultiTurnCase:
    case_id: str
    title: str
    #: missing / reversed / stale
    category: str
    turns: tuple[str, ...]
    #: 前 N 轮（含第 N-1 轮）不许真正搜到库存。缺日期时模型可以试，宿主必须拦住。
    no_executed_search_through_turn: int | None = None
    #: 全程结束时，真正执行成功的航线必须按出行顺序是这些（允许少，不许乱序、不许多出禁城）。
    expect_final_routes: tuple[tuple[str, str], ...] = ()
    #: 这些城市出现在**成功**的搜索里就算没听懂改口/没听懂倒装。
    forbid_final_cities: tuple[str, ...] = ()
    #: 这些日期不许出现在成功搜索的到达时限里（过期信息）。
    forbid_executed_dates: tuple[str, ...] = ()
    #: 成功搜索里必须出现过的日期（改口之后的那个）。
    expect_executed_dates: tuple[str, ...] = ()
    #: 第一轮必须开口问，不能编一个日期去搜。
    expect_ask_after_first_turn: bool = False
    question_tokens: tuple[str, ...] = ()
    #: 禁搜的城市：哪怕尝试也算没守住（日期还没说的那段）。
    forbid_attempted_cities: tuple[str, ...] = ()
    #: 只检查到第几轮（含）。缺日期的杭州段只看第一轮。
    forbid_attempted_cities_through_turn: int | None = None
    #: 倒装叙述：成功或尝试的**首次出现顺序**必须是出行顺序，不能是说话顺序。
    expect_first_mention_order: tuple[tuple[str, str], ...] = ()


CASES: tuple[LiveMultiTurnCase, ...] = (
    LiveMultiTurnCase(
        case_id="LM-01",
        category="missing",
        title="信息缺少：先说要出差，城市和日期分两轮再补",
        turns=(
            "我要去上海出差",
            "从北京走，不住酒店",
            "9月15号上午10点前到，当天不回",
        ),
        no_executed_search_through_turn=1,
        expect_ask_after_first_turn=True,
        question_tokens=("日期", "几号", "什么时候", "哪天", "何时"),
        expect_final_routes=(("Beijing", "Shanghai"),),
        expect_executed_dates=("2026-09-15",),
        forbid_attempted_cities=("Hangzhou", "杭州"),
    ),
    LiveMultiTurnCase(
        case_id="LM-02",
        category="missing",
        title="信息缺少：第一段有日期，后面两段先问再补",
        turns=(
            "9月15号上午10点前要到上海，我从北京走，之后还要去杭州、再回北京。不住酒店",
            "杭州9月18号去，19号回北京",
        ),
        expect_final_routes=(
            ("Beijing", "Shanghai"),
            ("Shanghai", "Hangzhou"),
            ("Hangzhou", "Beijing"),
        ),
        forbid_attempted_cities=("Hangzhou", "杭州"),
        forbid_attempted_cities_through_turn=0,
        question_tokens=("杭州", "回", "日期", "几号", "哪天"),
    ),
    LiveMultiTurnCase(
        case_id="LM-03",
        category="reversed",
        title="语序颠倒：先说回程，再说中间，最后才说去程",
        turns=(
            "9月19号从杭州飞回北京，在那之前18号从上海去杭州，最开始15号从北京到上海。不住酒店",
        ),
        expect_first_mention_order=(
            ("Beijing", "Shanghai"),
            ("Shanghai", "Hangzhou"),
            ("Hangzhou", "Beijing"),
        ),
        expect_final_routes=(
            ("Beijing", "Shanghai"),
            ("Shanghai", "Hangzhou"),
            ("Hangzhou", "Beijing"),
        ),
    ),
    LiveMultiTurnCase(
        case_id="LM-04",
        category="reversed",
        title="语序颠倒：第一轮只说回程，第二轮才补去程和中间段",
        turns=(
            "我9月19号要从杭州回北京",
            "去程15号北京到上海，18号上海到杭州。不住酒店",
        ),
        expect_ask_after_first_turn=True,
        expect_final_routes=(
            ("Beijing", "Shanghai"),
            ("Shanghai", "Hangzhou"),
            ("Hangzhou", "Beijing"),
        ),
    ),
    LiveMultiTurnCase(
        case_id="LM-05",
        category="stale",
        title="过期信息：先给已经过去的 8月5号，再改口成 9月15号",
        turns=(
            "8月5号从北京去上海开会，不住酒店",
            "搞错了，是9月15号上午10点前到",
        ),
        expect_ask_after_first_turn=True,
        question_tokens=("过", "过去", "改期", "哪天", "日期", "9"),
        forbid_executed_dates=("2026-08-05",),
        expect_executed_dates=("2026-09-15",),
        expect_final_routes=(("Beijing", "Shanghai"),),
    ),
    LiveMultiTurnCase(
        case_id="LM-06",
        category="stale",
        title="错误信息：先说去广州，第二轮改口去上海",
        turns=(
            "9月15号从北京去广州，上午10点前到，不住酒店",
            "不是广州，改去上海，其他不变",
        ),
        expect_executed_dates=("2026-09-15",),
        expect_final_routes=(("Beijing", "Shanghai"),),
        forbid_final_cities=("Guangzhou", "广州"),
    ),
)


def _check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _canon_city(name: str, normalizer: CityNormalizer) -> str:
    raw = str(name).strip()
    try:
        return normalizer.canonicalize(raw)
    except Exception:  # noqa: BLE001 - 评测侧兜底，不让别名表把整轮带走
        for canonical, aliases in _CITY_ALIASES.items():
            if raw in aliases or raw.casefold() in {item.casefold() for item in aliases}:
                return canonical
        return raw


def _city_in(name: str, banned: Sequence[str], normalizer: CityNormalizer) -> bool:
    got = _canon_city(name, normalizer)
    return any(_canon_city(item, normalizer) == got or item in name for item in banned)


def _route_key(origin: str, destination: str, normalizer: CityNormalizer) -> tuple[str, str]:
    return (_canon_city(origin, normalizer), _canon_city(destination, normalizer))


def _searches_from_transcript(
    transcript: Sequence[dict[str, Any]], normalizer: CityNormalizer
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in transcript:
        if str(item.get("tool") or "") != "search_transport":
            continue
        args = dict(item.get("arguments") or {})
        arrive = str(args.get("arrive_by") or "")
        rows.append(
            {
                "origin": _canon_city(str(args.get("origin") or ""), normalizer),
                "destination": _canon_city(str(args.get("destination") or ""), normalizer),
                "arrive_by": arrive,
                "arrive_date": arrive[:10] if arrive else "",
                "ok": bool(item.get("ok")),
                "error": str(item.get("error") or "")[:240],
            }
        )
    return rows


def _first_mention_order(searches: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    order: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in searches:
        key = (str(item["origin"]), str(item["destination"]))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        order.append(key)
    return order


def _asked(task: Any) -> bool:
    if task.clarification_question:
        return True
    state = task.state
    return state in {TaskState.NEEDS_CLARIFICATION, TaskState.WAITING_FOR_USER}


def _evaluate(
    case: LiveMultiTurnCase,
    *,
    snapshots: Sequence[dict[str, Any]],
    final_task: Any,
    all_searches: Sequence[dict[str, Any]],
    normalizer: CityNormalizer,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    executed = [item for item in all_searches if item["ok"]]
    attempted = list(all_searches)

    if case.no_executed_search_through_turn is not None:
        early = [
            item
            for snap in snapshots
            if int(snap["turn"]) <= case.no_executed_search_through_turn
            for item in snap["searches"]
            if item["ok"]
        ]
        checks.append(
            _check(
                "no_search_while_information_is_missing",
                not early,
                f"executed before turn {case.no_executed_search_through_turn}: {early}",
            )
        )

    if case.expect_ask_after_first_turn and snapshots:
        first = snapshots[0]
        checks.append(
            _check(
                "asks_on_the_first_gap",
                bool(first.get("asked")),
                f"state={first.get('state')} question={first.get('question')!r}",
            )
        )

    if case.question_tokens:
        questions = " ".join(
            str(snap.get("question") or "") for snap in snapshots
        )
        if case.expect_ask_after_first_turn or any(
            snap.get("asked") for snap in snapshots
        ):
            checks.append(
                _check(
                    "question_names_the_gap",
                    any(token in questions for token in case.question_tokens),
                    f"expected one of {case.question_tokens} in {questions[:240]!r}",
                )
            )

    if case.forbid_executed_dates:
        hit = sorted(
            {
                item["arrive_date"]
                for item in executed
                if item["arrive_date"] in case.forbid_executed_dates
            }
        )
        checks.append(
            _check(
                "expired_date_was_not_searched",
                not hit,
                f"executed dates {hit}",
            )
        )

    if case.expect_executed_dates:
        got = {item["arrive_date"] for item in executed}
        missing = [day for day in case.expect_executed_dates if day not in got]
        checks.append(
            _check(
                "corrected_date_was_searched",
                not missing,
                f"missing {missing}; executed {sorted(got)}",
            )
        )

    if case.forbid_attempted_cities:
        scoped = executed
        if case.forbid_attempted_cities_through_turn is not None:
            scoped = [
                item
                for snap in snapshots
                if int(snap["turn"]) <= case.forbid_attempted_cities_through_turn
                for item in snap.get("searches") or []
                if item.get("ok")
            ]
        touched = sorted(
            {
                city
                for item in scoped
                for city in (item["origin"], item["destination"])
                if _city_in(city, case.forbid_attempted_cities, normalizer)
            }
        )
        # 模型可以试；红线是宿主有没有放行。试了被拒 = 边界在工作，不算失败。
        checks.append(
            _check(
                "forbidden_city_was_not_searched",
                not touched,
                f"executed {touched} through turn {case.forbid_attempted_cities_through_turn}",
            )
        )

    if case.expect_first_mention_order:
        mention = _first_mention_order(attempted)
        expected = [
            _route_key(origin, destination, normalizer)
            for origin, destination in case.expect_first_mention_order
        ]
        # 允许少搜（沪杭没票会停），不许把说话顺序当成出行顺序。
        prefix_ok = mention[: len(expected)] == expected[: len(mention)] and bool(mention)
        speech_order = list(reversed(expected)) if case.category == "reversed" else None
        not_speech = mention != speech_order if speech_order is not None else True
        checks.append(
            _check(
                "routes_follow_travel_order_not_speech_order",
                prefix_ok and not_speech,
                f"first mentions {mention}; expected travel order {expected}",
            )
        )

    if case.expect_final_routes:
        mention = _first_mention_order(executed or attempted)
        expected = [
            _route_key(origin, destination, normalizer)
            for origin, destination in case.expect_final_routes
        ]
        if expected:
            first_leg = expected[0]
            has_first = first_leg in mention
            checks.append(
                _check(
                    "first_leg_was_actually_handled",
                    has_first,
                    f"expected {first_leg} among {mention}",
                )
            )

    if case.forbid_final_cities:
        if final_task.request is not None:
            legs = [
                (leg.origin, leg.destination) for leg in final_task.request.transport_legs()
            ]
        else:
            legs = [(item["origin"], item["destination"]) for item in executed]
        stale = [
            pair
            for pair in legs
            if _city_in(pair[0], case.forbid_final_cities, normalizer)
            or _city_in(pair[1], case.forbid_final_cities, normalizer)
        ]
        checks.append(
            _check(
                "stale_city_did_not_survive_the_correction",
                not stale,
                f"legs {legs}",
            )
        )

    return checks


def _snapshot_turn(
    turn: int,
    said: str,
    task: Any,
    normalizer: CityNormalizer,
) -> dict[str, Any]:
    transcript = list(task.metadata.get("agentic_transcript") or [])
    searches = _searches_from_transcript(transcript, normalizer)
    question = task.clarification_question
    return {
        "turn": turn,
        "said": said,
        "state": task.state.value,
        "asked": _asked(task),
        "question": question,
        "option_count": len(task.options),
        "searches": searches,
        "executed_routes": [
            f"{item['origin']}->{item['destination']}" for item in searches if item["ok"]
        ],
        "refused_routes": [
            f"{item['origin']}->{item['destination']} ({item['error']})"
            for item in searches
            if not item["ok"]
        ],
        "provider_calls": sum(
            1
            for call in task.tool_calls
            if call.tool_name in {"tool.search_transport", "tool.search_hotels"}
        ),
        "failure": task.failure,
    }


def _run_case(
    case: LiveMultiTurnCase,
    *,
    model: OpenAIToolCallingLanguageModel,
    recorder: LiveCaseRecorder,
    provider: Any,
    normalizer: CityNormalizer,
) -> dict[str, Any]:
    workflow, _ = build_demo_system(
        tool_calling_language_model=model,
        provider=provider,
        clock=lambda: CLOCK,
        trace_observer=recorder,
    )
    snapshots: list[dict[str, Any]] = []
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "title": case.title,
        "category": case.category,
        "turns": list(case.turns),
    }
    try:
        task = workflow.create_task_from_agentic_message(
            case.turns[0], traveler_id="E1001", task_id=f"{case.case_id.lower()}-live"
        )
        snapshots.append(_snapshot_turn(0, case.turns[0], task, normalizer))
        for index, message in enumerate(case.turns[1:], start=1):
            if task.state not in _OPEN_STATES:
                snapshots.append(
                    {
                        "turn": index,
                        "said": message,
                        "skipped": task.state.value,
                        "searches": [],
                    }
                )
                break
            task = workflow.submit_agentic_message(task.task_id, message)
            snapshots.append(_snapshot_turn(index, message, task, normalizer))
    except LanguageModelError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["error_kind"] = (
            "transport" if str(getattr(exc, "layer", "")).startswith("openai_") else "model"
        )
        record["snapshots"] = snapshots
        record["checks"] = [_check("conversation", False, record["error"])]
        record["passed"] = False
        record["llm_calls"] = model.call_count
        record["input_tokens"] = model.input_tokens
        record["output_tokens"] = model.output_tokens
        return record
    except Exception as exc:  # noqa: BLE001 - 一条炸掉不该带走整轮
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["snapshots"] = snapshots
        record["checks"] = [_check("conversation", False, record["error"])]
        record["passed"] = False
        record["llm_calls"] = model.call_count
        record["input_tokens"] = model.input_tokens
        record["output_tokens"] = model.output_tokens
        return record

    all_searches = [item for snap in snapshots for item in snap.get("searches") or []]
    record["snapshots"] = snapshots
    record["state"] = task.state.value
    record["question"] = task.clarification_question
    record["option_count"] = len(task.options)
    record["legs"] = (
        [(leg.origin, leg.destination) for leg in task.request.transport_legs()]
        if task.request is not None
        else []
    )
    record["provider_search_calls"] = sum(
        1
        for call in task.tool_calls
        if call.tool_name in {"tool.search_transport", "tool.search_hotels"}
    )
    record["checks"] = _evaluate(
        case,
        snapshots=snapshots,
        final_task=task,
        all_searches=all_searches,
        normalizer=normalizer,
    )
    record["passed"] = all(item["ok"] for item in record["checks"]) if record["checks"] else False
    record["llm_calls"] = model.call_count
    record["input_tokens"] = model.input_tokens
    record["output_tokens"] = model.output_tokens
    return record


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 工具循环 · 真实多轮（缺信息 / 语序颠倒 / 过期改口）",
        "",
        f"- started_at: `{payload['started_at']}`",
        f"- runner: `{payload['runner_version']}` · prompt `{payload['prompt_version']}`",
        f"- model: `{payload['model']}`",
        f"- inventory: `{payload['inventory']}`（只读，不下单）",
        f"- clock: `{payload['clock']}`",
        f"- result: **{payload['passed']}/{payload['total']} PASS**",
        f"- 模型调用: {payload['llm_calls']} 次，估算 "
        f"**{payload['estimated_cost_usd']} {payload['currency']}**"
        if payload.get("estimated_cost_usd") is not None
        else f"- 模型调用: {payload['llm_calls']} 次",
        f"- 供应商搜索: {payload['provider_search_calls']} 次",
        "",
    ]
    if payload.get("rescore_note"):
        lines += [f"> {payload['rescore_note']}", ""]
    lines += [
        "| ID | 类 | Result | Title |",
        "|---|---|---|---|",
    ]
    for item in payload["cases"]:
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(
            f"| {item['case_id']} | {item['category']} | {mark} | {item['title']} |"
        )
    failed = [item for item in payload["cases"] if not item["passed"]]
    if failed:
        lines.extend(["", "## 没过的用例", ""])
        for item in failed:
            lines.append(f"### {item['case_id']} — {item['title']}")
            lines.append("")
            for snap in item.get("snapshots") or []:
                lines.append(
                    f"- 轮{snap.get('turn')}：`{snap.get('said')}` → "
                    f"{snap.get('state') or snap.get('skipped')}"
                )
                if snap.get("question"):
                    lines.append(f"  - 问：{snap['question']}")
                if snap.get("executed_routes"):
                    lines.append(f"  - 搜成：{snap['executed_routes']}")
                if snap.get("refused_routes"):
                    lines.append(f"  - 被拒：{snap['refused_routes']}")
            for check in item.get("checks") or []:
                if not check["ok"]:
                    lines.append(f"- **{check['name']}**：{check['detail']}")
            lines.append("")
    lines.extend(
        ["", "## 每条用例最终读数", "", "| ID | 状态 | 方案 | 航段 |", "|---|---|---|---|"]
    )
    for item in payload["cases"]:
        lines.append(
            f"| {item['case_id']} | {item.get('state')} | {item.get('option_count')} "
            f"| {item.get('legs')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _assert_live_sandbox() -> str:
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    if not token.startswith("duffel_test_"):
        raise SystemExit("DUFFEL_ACCESS_TOKEN 必须是 duffel_test_ 开头的沙箱令牌")
    if os.getenv("DUFFEL_LIVE_MODE", "false").casefold() == "true":
        raise SystemExit("DUFFEL_LIVE_MODE 必须保持 false")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        raise SystemExit("LIVE_BOOKING_ENABLED 必须保持 false")
    name = os.getenv("TRAVEL_PROVIDER", "mock").strip().casefold()
    if name not in {"duffel", "duffel_liteapi", "duffel+liteapi", "duffel-liteapi"}:
        raise SystemExit(
            f"这一轮要接真实库存，TRAVEL_PROVIDER 现在是 {name!r}，"
            "请设成 duffel 或 duffel_liteapi"
        )
    return name


def _cases_sha256(cases) -> str:
    """用例本身的指纹：改了用例，轨迹指纹就变，旧报告不能冒充新的。"""
    payload = json.dumps(
        [asdict(case) for case in cases], ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finish_trace(recorder: LiveCaseRecorder, record: dict[str, Any]) -> None:
    """一条用例跑完，把终态写进它的轨迹；不论过没过、有没有炸。"""
    recorder.finish(
        state=record.get("state") or record.get("outcome_kind"),
        result_refs=tuple(str(ref) for ref in (record.get("option_refs") or ())),
        user_response=(
            record.get("user_visible_text") or record.get("question") or record.get("summary")
        ),
        failure_reason=record.get("error"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--price-table", type=Path)
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--case", action="append", help="只跑指定 case_id，可重复")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    args = parser.parse_args(argv)

    if not args.confirm_billable_model_calls:
        parser.error("会调用计费模型；请加 --confirm-billable-model-calls")
    if not args.confirm_external_test_calls:
        parser.error("会调用 Duffel 沙箱只读搜索；请加 --confirm-external-test-calls")
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not set")
    if args.output.exists():
        parser.error(f"输出目录必须事先不存在，以免覆盖历史证据：{args.output}")

    provider_name = _assert_live_sandbox()
    selected = CASES
    if args.case:
        wanted = set(args.case)
        selected = tuple(case for case in CASES if case.case_id in wanted)
        if not selected:
            parser.error(f"没有匹配的 case_id：{sorted(wanted)}")

    policy = load_policy_configuration()
    active = next(
        (
            item
            for item in policy.policy_snapshots
            if item.snapshot_id == policy.config.active_policy_snapshot_id
        ),
        policy.policy_snapshots[0],
    )
    provider = travel_provider_from_environment(policy_currency=active.currency)
    if provider is None:
        raise SystemExit("未能从环境装出真实 Provider")
    normalizer = CityNormalizer(policy.city_aliases)

    price_table = None
    price_sha_for_ledger = None
    if args.price_table:
        price_table, price_sha_for_ledger = load_model_price_table(args.price_table)
    ledger = LiveRunLedger(
        model=args.model,
        prompt_version=TOOL_LOOP_PROMPT_VERSION,
        runner_version=RUNNER_VERSION,
        dataset_id="tool-loop-live-multiturn-cases",
        dataset_version="1",
        dataset_sha256=_cases_sha256(selected),
        price_table=price_table,
        price_table_sha256=price_sha_for_ledger,
    )

    records: list[dict[str, Any]] = []
    started = datetime.now(UTC)
    try:
        for case in selected:
            model = OpenAIToolCallingLanguageModel(model=args.model, request_timeout_seconds=90.0)
            recorder = ledger.open_case(case.case_id, 1, model)
            record = _run_case(
                case, model=model, recorder=recorder, provider=provider, normalizer=normalizer
            )
            _finish_trace(recorder, record)
            records.append(record)
            status = "PASS" if record.get("passed") else "FAIL"
            print(
                f"[{status}] {case.case_id} state={record.get('state')} "
                f"llm_calls={record.get('llm_calls')} "
                f"provider_search={record.get('provider_search_calls')}"
            )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    passed = sum(1 for item in records if item.get("passed"))
    llm_calls = sum(int(item.get("llm_calls") or 0) for item in records)
    input_tokens = sum(int(item.get("input_tokens") or 0) for item in records)
    output_tokens = sum(int(item.get("output_tokens") or 0) for item in records)
    provider_searches = sum(int(item.get("provider_search_calls") or 0) for item in records)
    cost: float | None = None
    price_table_version: str | None = None
    price_sha: str | None = None
    currency = "USD"
    if args.price_table:
        price_table, price_sha = load_model_price_table(args.price_table)
        price_table_version = price_table.price_table_version
        currency = price_table.currency
        entry = price_table.models.get(args.model)
        if entry is not None:
            cost = input_tokens * entry.input / 1_000_000 + output_tokens * entry.output / 1_000_000

    payload = {
        "runner_version": RUNNER_VERSION,
        "prompt_version": TOOL_LOOP_PROMPT_VERSION,
        "architecture": "tool-loop",
        "entrypoint": "agentic",
        "inventory": provider_name,
        "clock": CLOCK.isoformat(),
        "started_at": started.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "cases_total": len(records),
        "passed": passed,
        "total": len(records),
        "llm_calls": llm_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider_search_calls": provider_searches,
        "estimated_cost_usd": cost,
        "price_table_version": price_table_version,
        "price_table_sha256": price_sha,
        "currency": currency,
        "limitations": [
            "Duffel Test Mode sandbox; schedules and prices are not production data.",
            "No orders, payments, or live booking.",
            "Clock is frozen at 2026-08-19 so 8月5号 is expired.",
            "September dates are live Duffel Test Mode searches.",
        ],
        "cases": records,
    }
    args.output.mkdir(parents=True)
    payload["live_artifacts"] = ledger.write(args.output)
    (args.output / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (args.output / "REPORT.md").write_text(_markdown(payload), encoding="utf-8")
    tail = f"，约 ${cost:.4f}" if cost is not None else ""
    print(
        f"\n{passed}/{len(records)} 条通过；"
        f"模型 {llm_calls} 次，供应商搜索 {provider_searches} 次{tail}"
    )
    print(f"报告写入 {args.output / 'REPORT.md'}")
    if args.gate and passed != len(records):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

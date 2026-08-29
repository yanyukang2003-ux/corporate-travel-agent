#!/usr/bin/env python3
"""多轮对话 + 多段行程，走语义入口 + 真实计费模型。

## 补哪个洞

此前所有真实模型评测都是**一两轮、单程或往返**。两块没验过：

1. **多轮逐步搭行程。** ADR-0002 的核心是"每次把整段对话重读一遍"。轮数一多，
   这个设计要么成立要么塌——旧值残留、后说的没盖住先说的、某一轮被忽略。
2. **系统做不了的行程形态。** 领域模型只能表达单程和原路往返
   （`transport_legs()` 把返程推导成"目的地→出发地"）。多城和开口程**表达不了**，
   所以必须被拦住。这里最危险的不是报错，是**悄悄压缩**：把"去上海、从杭州回"
   变成"上海→北京"，然后拿一条用户没要过的航线去搜库存。

第 2 条只有真模型能验：脚本化测试是预设"模型已经说了做不了"，测的是宿主，不是识别。

## 花什么钱

只调语言模型，库存用 Mock，不碰 Duffel / LiteAPI。每条用例的调用次数 = 对话轮数。

```bash
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/run_semantic_multiturn_model_evaluation.py \\
  --output reports/evaluation-runs/semantic-multiturn-NEWDIR \\
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \\
  --repeats 2 --confirm-billable-model-calls
```
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.openai_adapter import OpenAISemanticIntentLanguageModel
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.evaluation_performance import load_model_price_table

REPO = Path(__file__).resolve().parents[1]
RUNNER_VERSION = "semantic-multiturn-model-runner-v1"
GATE_ID = "MT-semantic"
CLOCK = datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI_TZ)

# 还能继续追加消息的状态。
_OPEN_STATES = frozenset(
    {
        TaskState.NEEDS_CLARIFICATION,
        TaskState.NEEDS_STRUCTURED_INPUT,
        TaskState.WAITING_FOR_USER,
        TaskState.NO_FEASIBLE_OPTION,
        TaskState.OUT_OF_SCOPE,
    }
)


@dataclass(frozen=True, slots=True)
class MultiTurnCase:
    case_id: str
    title: str
    turns: tuple[str, ...]
    # 最终必须读成这样的字段（None 表示"必须为空"）。
    expect_fields: dict[str, Any] = field(default_factory=dict)
    # 最终的航段序列 [(origin, destination), ...]；() 表示"不许编译出任何行程"。
    expect_legs: tuple[tuple[str, str], ...] | None = None
    # 这趟行程系统表达不了，必须一次库存都不查。
    forbid_search: bool = False
    # 追问里必须出现的字样（任一命中即可）。
    question_tokens: tuple[str, ...] = ()
    # 这些字段绝不许残留旧值。
    forbid_stale: dict[str, Any] = field(default_factory=dict)


CASES: tuple[MultiTurnCase, ...] = (
    MultiTurnCase(
        case_id="MT-01",
        title="五轮逐步搭出一趟往返：每一轮的信息都不许丢",
        turns=(
            "下个月要去上海出差",
            "从北京出发",
            "9月15号走",
            "9月16号中午12点前必须到",
            "9月18号晚上6点以后到11点前起飞回北京",
        ),
        expect_fields={
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_date": "2026-09-15",
            "arrive_date": "2026-09-16",
            "return_date": "2026-09-18",
        },
        expect_legs=(("Beijing", "Shanghai"), ("Shanghai", "Beijing")),
    ),
    MultiTurnCase(
        case_id="MT-02",
        title="搭完之后改目的地：旧目的地不许残留",
        turns=(
            "9月15号从北京去上海，9月16号中午12点前到",
            "改成去广州，其他不变",
        ),
        expect_fields={
            "origin": "Beijing",
            # 城市别名表里没有广州，所以模型给中文、宿主原样保留。这条用例测的是
            # "旧目的地不许残留"，不是译名，因此两种写法都接受，并把别名缺口
            # 单独记在报告的观察里。
            "destination": ("Guangzhou", "广州"),
            "departure_date": "2026-09-15",
        },
        forbid_stale={"destination": ("Shanghai", "上海")},
    ),
    MultiTurnCase(
        case_id="MT-03",
        title="多城行程：北京→上海→杭州→北京，三段都要读出来",
        turns=(
            "9月15号从北京去上海开会，然后去杭州见客户，最后回北京",
            "上海会议9月16号上午10点，杭州9月18号下午2点见客户，9月19号晚上回北京",
            # 第三轮是**真跑之后补的**：前两轮里"9月19号晚上回北京"只说了什么时候走，
            # 没说最晚几点要到。模型和宿主都正确地追问了这一条——所以少的是这轮对话，
            # 不是能力。航段的时间窗**缺一个就整条不用**，绝不拿另一段的时间凑。
            "9月19号晚上11点前到北京就行",
        ),
        # **这条用例的期望在方案第 05 步（HANDOFF §36）翻过来了。** 此前多城
        # 系统表达不了，所以它是一条"必须被拦住、一次库存都不查"的安全用例；
        # 现在语义层有 legs 数组、结果侧有 legs、搜索侧按段循环，三段是能走通的。
        # 真正的危险从来不是"拦不拦"，而是**悄悄压缩**：把三段读成"上海→北京"
        # 一段，然后拿一条用户没要过的航线去搜库存（§28 抓到过）。
        # 所以现在盯的是"三段一段不少、顺序不乱"。
        expect_legs=(
            ("Beijing", "Shanghai"),
            ("Shanghai", "Hangzhou"),
            ("Hangzhou", "Beijing"),
        ),
    ),
    MultiTurnCase(
        case_id="MT-04",
        title="开口程：去上海、从杭州回，按用户说的两段走",
        turns=(
            "去程9月15号北京飞上海，返程9月20号从杭州飞回北京",
            "9月16号中午12点前到上海就行，返程9月20号晚上8点前到北京",
        ),
        # 期望在 2026-08-28 随能力变更重写：领域模型改成有序航段列表之后，
        # 开口程不再是特例，返程起点就是第二段自己的 origin。
        # 此前它是"必须拒绝"，因为当时杭州没地方放，宿主会拼出一条用户没要过的航线。
        # 城市别名表里没有杭州，所以宿主原样保留中文——这条用例测的是
        # "返程起点是杭州而不是上海"，不是译名，两种写法都接受。
        expect_legs=(
            ("Beijing", "Shanghai"),
            (("Hangzhou", "杭州"), "Beijing"),
        ),
    ),
    MultiTurnCase(
        case_id="MT-05",
        title="第四轮推翻第一轮：后说的必须盖住先说的",
        turns=(
            "9月15号从北京去上海，9月16号中午12点前到，订酒店住两晚",
            "酒店先不订了",
            "出发改到9月17号",
            "算了还是订酒店，住一晚",
        ),
        expect_fields={
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_date": "2026-09-17",
            "lodging_requirement": "REQUIRED",
        },
    ),
)


def _check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _date_of(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10]


def _legs(task: Any) -> list[tuple[str, str]]:
    if task.request is None:
        return []
    return [(leg.origin, leg.destination) for leg in task.request.transport_legs()]


def _legs_match(actual: list[tuple[str, str]], expected: tuple[Any, ...]) -> bool:
    """比对航段；期望里的元素可以是一个字符串，也可以是一组可接受的写法。"""
    if len(actual) != len(expected):
        return False
    for (got_origin, got_destination), (want_origin, want_destination) in zip(
        actual, expected, strict=True
    ):
        for got, want in ((got_origin, want_origin), (got_destination, want_destination)):
            allowed = want if isinstance(want, tuple) else (want,)
            if got not in allowed:
                return False
    return True


def _search_count(task: Any) -> int:
    return sum(1 for item in task.tool_calls if item.tool_name.startswith("provider.search"))


def _evaluate(case: MultiTurnCase, task: Any) -> list[dict[str, Any]]:
    fields = task.intent_fields
    checks: list[dict[str, Any]] = []
    legs = _legs(task)

    for key, expected in case.expect_fields.items():
        if key.endswith("_date"):
            actual = _date_of(
                fields.get(
                    {
                        "departure_date": "departure_after",
                        "arrive_date": "arrive_by",
                        "return_date": "return_after",
                    }[key]
                )
            )
        else:
            actual = fields.get(key)
        ok = actual in expected if isinstance(expected, tuple) else actual == expected
        checks.append(_check(key, ok, f"expected {expected}, got {actual}"))

    for key, stale in case.forbid_stale.items():
        actual = fields.get(key)
        forbidden = stale if isinstance(stale, tuple) else (stale,)
        checks.append(
            _check(f"no_stale_{key}", actual not in forbidden, f"got {actual}")
        )

    if case.expect_legs is not None:
        checks.append(_check("legs", _legs_match(legs, case.expect_legs), f"{legs}"))
    if case.forbid_search:
        checks.append(_check("no_search", _search_count(task) == 0, _search_count(task)))
        checks.append(_check("no_request", task.request is None, legs))
        # 最危险的具体形态：把用户没要过的返程航线拼出来。
        checks.append(
            _check(
                "no_invented_return_leg",
                not any(role for role in legs[1:]),
                legs[1:],
            )
        )
    if case.question_tokens:
        question = task.clarification_question or ""
        checks.append(
            _check(
                "question_names_the_problem",
                any(token in question for token in case.question_tokens),
                question[:200],
            )
        )
    return checks


def _run_case(case: MultiTurnCase, model_factory) -> dict[str, Any]:
    workflow, _provider = build_demo_system(
        semantic_language_model=model_factory(), clock=lambda: CLOCK
    )
    transcript: list[dict[str, Any]] = []
    try:
        task = workflow.create_task_from_semantic_message(
            case.turns[0], traveler_id="E1001", task_id=case.case_id.lower()
        )
        transcript.append({"turn": 0, "said": case.turns[0], "state": task.state.value})
        for index, message in enumerate(case.turns[1:], start=1):
            if task.state not in _OPEN_STATES:
                transcript.append(
                    {"turn": index, "said": message, "skipped": task.state.value}
                )
                break
            task = workflow.submit_semantic_message(task.task_id, message)
            transcript.append(
                {
                    "turn": index,
                    "said": message,
                    "state": task.state.value,
                    "question": task.clarification_question,
                }
            )
    except Exception as exc:  # noqa: BLE001 - 一条用例炸掉不该带走整轮
        return {
            "case_id": case.case_id,
            "title": case.title,
            "passed": False,
            "checks": [_check("conversation", False, f"{type(exc).__name__}: {exc}")],
            "state": None,
            "legs": [],
            "intent_fields": {},
            "transcript": transcript,
            "llm_calls": [],
        }

    checks = _evaluate(case, task)
    return {
        "case_id": case.case_id,
        "title": case.title,
        "passed": all(item["ok"] for item in checks) if checks else False,
        "checks": checks,
        "state": task.state.value,
        "legs": _legs(task),
        "searches": _search_count(task),
        "clarification_question": task.clarification_question,
        "conflicts": list(task.intent_conflicts),
        "intent_fields": {
            key: str(value)
            for key, value in task.intent_fields.items()
            if value not in (None, [], "")
        },
        "user_turns": [item.content for item in task.messages if item.role == "user"],
        "transcript": transcript,
        "llm_calls": list(task.metadata.get("llm_calls") or []),
    }


def _cost(usage: list[dict[str, Any]], prices: Any) -> float:
    total = 0.0
    for call in usage:
        price = prices.models.get(call.get("model") or "")
        if price is None:
            continue
        total += (call.get("input_tokens") or 0) * price.input / 1_000_000
        total += (call.get("output_tokens") or 0) * price.output / 1_000_000
    return total


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 多轮对话 + 多段行程 · 语义入口 · 真实模型",
        "",
        f"- started_at: `{payload['started_at']}`",
        f"- runner: `{payload['runner_version']}`",
        f"- model: `{payload['model']}` · prompt `{payload['semantic_prompt_version']}`",
        "- inventory: `mock`（不碰 Duffel / LiteAPI）",
        f"- result: **{payload['passed']}/{payload['total']} PASS**"
        f"（每条跑 {payload['repeats']} 次，全部通过才记 PASS）",
        f"- 模型调用: {payload['model_calls']} 次，估算 "
        f"**{payload['estimated_cost_usd']:.6f} {payload['currency']}**",
        "",
        "MT-03 多城、MT-04 开口程：这两种行程**现在都表达得了**（§29 开口程、",
        "§36 多城）。它们盯的不再是「拦没拦住」，而是**有没有被悄悄压缩**——",
        "把三段读成一段、或把返程起点换成目的地，然后拿用户没要过的航线去搜库存。",
        "",
        "| ID | Result | 通过率 | Title |",
        "|---|---|---|---|",
    ]
    for item in payload["cases"]:
        mark = "PASS" if item["passed"] else "FAIL"
        rate = f"{item.get('passes', 0)}/{item.get('attempts', 1)}"
        lines.append(f"| {item['case_id']} | {mark} | {rate} | {item['title']} |")
    unstable = payload.get("unstable_cases") or []
    if unstable:
        lines.append("")
        lines.append("**时好时坏：** " + "、".join(unstable) + "。单次结果是噪声。")
    lines.append("")
    failed = [item for item in payload["cases"] if not item["passed"]]
    if failed:
        lines.append("## 没过的用例")
        lines.append("")
        for item in failed:
            lines.append(f"### {item['case_id']} — {item['title']}")
            lines.append("")
            for turn in item.get("transcript") or []:
                lines.append(f"- 轮{turn['turn']}：`{turn['said']}` → {turn.get('state')}")
            lines.append(f"- 最终状态：`{item['state']}` · 航段 `{item['legs']}`")
            if item.get("clarification_question"):
                lines.append(f"- 追问：{item['clarification_question']}")
            for check in item["checks"]:
                if not check["ok"]:
                    lines.append(f"- **{check['name']}**：{check['detail']}")
            lines.append("")
    lines.append("## 每条用例的最终读数")
    lines.append("")
    lines.append("| ID | 状态 | 搜库存 | 航段 |")
    lines.append("|---|---|---|---|")
    for item in payload["cases"]:
        lines.append(
            f"| {item['case_id']} | {item['state']} | {item.get('searches', '—')} "
            f"| {item['legs']} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--price-table", required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="只跑这几条（可重复）。改了某条用例后单独复跑，省钱",
    )
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.repeats <= 5:
        parser.error("--repeats must be between 1 and 5")
    selected = CASES
    if args.case_id:
        wanted = set(args.case_id)
        selected = tuple(case for case in CASES if case.case_id in wanted)
        if not selected:
            parser.error(f"没有匹配的用例：{sorted(wanted)}")
    turns = sum(len(case.turns) for case in selected) * args.repeats
    if not args.confirm_billable_model_calls:
        parser.error(
            f"这一轮最多会真实调用 {turns} 次计费模型。确认后加 "
            "--confirm-billable-model-calls 重跑。"
        )
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not set")

    price_table, price_sha = load_model_price_table(args.price_table)
    if price_table.status != "configured" or args.model not in price_table.models:
        parser.error("the price table must be configured and contain the requested model")

    output = Path(args.output)
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)

    def model_factory() -> OpenAISemanticIntentLanguageModel:
        return OpenAISemanticIntentLanguageModel(
            model=args.model,
            request_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "90")),
        )

    started = datetime.now(UTC).isoformat()
    cases: list[dict[str, Any]] = []
    for case in selected:
        attempts = [_run_case(case, model_factory) for _ in range(args.repeats)]
        wins = sum(1 for item in attempts if item["passed"])
        record = dict(attempts[0])
        record["attempts"] = args.repeats
        record["passes"] = wins
        record["passed"] = wins == args.repeats
        record["stable"] = wins in {0, args.repeats}
        record["llm_calls"] = [call for item in attempts for call in item["llm_calls"]]
        cases.append(record)

    passed = sum(1 for item in cases if item["passed"])
    all_usage = [call for item in cases for call in item["llm_calls"]]
    payload = {
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "runner_version": RUNNER_VERSION,
        "gate_id": GATE_ID,
        "entrypoint": "semantic",
        "inventory": "mock",
        "model": args.model,
        "semantic_prompt_version": OpenAISemanticIntentLanguageModel.semantic_prompt_version,
        "repeats": args.repeats,
        "model_calls": len(all_usage),
        "price_table_version": price_table.price_table_version,
        "price_table_sha256": price_sha,
        "currency": price_table.currency,
        "estimated_cost_usd": _cost(all_usage, price_table),
        "measurement_only": not args.gate,
        "passed": passed,
        "total": len(cases),
        "unstable_cases": [item["case_id"] for item in cases if not item["stable"]],
        "cases": cases,
    }
    (output / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "REPORT.md").write_text(_markdown(payload), encoding="utf-8")
    print(f"{passed}/{len(cases)} PASS → {output}")
    print(
        f"model calls: {len(all_usage)}  estimated: "
        f"${payload['estimated_cost_usd']:.6f}"
    )
    if args.gate and passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

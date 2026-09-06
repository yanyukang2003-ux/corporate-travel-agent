"""外部真话长尾集（D18）的加载、判据与离线探针。

判据放在包里而不是某个 runner 里，是 §46 的教训：跨 runner 借代码的 importlib 依赖
会在删文件时悄悄断掉。离线探针和真模型抽样 runner **共用这里的同一份判据**——
两边测出的差异只能来自模型，不来自判据实现。

红线（对任何输入成立）：不崩、不产生预订、不交出快照外的引用、不声称已下单。
弱真值门禁（只在真的搜索了才生效）：搜过的地点必须落在来源声明或原话里；
搜索日期必须与来源声明一致（没声明的来源，交通搜索必须带原话出处）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from corporate_travel_agent.services.locations import CityNormalizer

RUNNER_VERSION = "external-longtail-probe-v2"
BOOKING_CLAIMS = ("已出票", "已预订", "已经订好", "已下单", "booked", "ticket issued")

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def place_grounded(
    place: str, weak_truth: dict[str, Any], message: str, normalizer: CityNormalizer
) -> bool:
    """搜索用到的地点有没有出处：声明城市/机场集合，或用户原话。

    比较在正规化后进行（上海 和 Shanghai 折成同一个名字）；不在别名表里的名字
    （景点、站点、机场码）原样对比。两边都说不出，就是系统自己编的地方。
    """
    canon = normalizer.canonicalize(place)
    declared = {
        normalizer.canonicalize(item)
        for item in (weak_truth.get("cities") or [])
        if str(item).strip()
    }
    if canon in declared or place in declared:
        return True
    return place in message or canon in message


def declared_month_days(weak_truth: dict[str, Any]) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for item in weak_truth.get("dates_declared") or []:
        month = _MONTHS.get(str(item.get("month", "")).strip().lower())
        try:
            day = int(str(item.get("day", "")).strip())
        except ValueError:
            continue
        if month:
            out.add((month, day))
    return out


def searched_dates_ok(
    searches: list[dict[str, Any]], weak_truth: dict[str, Any]
) -> tuple[bool, list[str]]:
    """搜索日期核对：来源声明过日期就逐个比对月/日；没声明就要求每次交通搜索带原话出处。"""
    declared = declared_month_days(weak_truth)
    problems: list[str] = []
    for search in searches:
        if declared:
            extras = sorted(
                (day.month, day.day)
                for day in _search_days(search)
                if (day.month, day.day) not in declared
            )
            if extras:
                problems.append(f"搜索日期 {extras} 不在声明日期 {sorted(declared)} 里")
        elif search.get("kind") == "transport" and not search.get("date_evidence"):
            problems.append("来源没有声明日期，而这次交通搜索没有原话出处")
    return (not problems, problems)


def _stamp_date(value: object) -> date | None:
    stamp = str(value or "")
    if len(stamp) < 10 or stamp[4] != "-" or stamp[7] != "-":
        return None
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


def _search_days(search: dict[str, Any]) -> set[date]:
    """这次搜索"要的是哪几天"。

    交通搜索的窗口从到达时限往前开 18 小时，跨日时按天拆成几条记录，前一天那条的
    `arrive_by` 是那天的 23:59——那是回看窗口，不是模型另搜了一天。旅行者真正给的
    截止时刻在 `requested_window_arrive_by`；有它就以它为准，落在它前一天之内的
    `depart_after` / `arrive_by` 都算同一次要求。酒店按入住/退房日算。
    """
    params = search.get("parameters") or {}
    if search.get("kind") != "transport":
        days = (_stamp_date(params.get("check_in")), _stamp_date(params.get("check_out")))
        return {day for day in days if day}
    window = (_stamp_date(params.get("depart_after")), _stamp_date(params.get("arrive_by")))
    requested = _stamp_date(params.get("requested_window_arrive_by")) or window[1]
    if requested is None:
        return {day for day in window if day}
    lookback = {requested, requested - timedelta(days=1)}
    return {requested} | {day for day in window if day and day not in lookback}


RUNNER_VERSION = "external-longtail-probe-v2"
BOOKING_CLAIMS = ("已出票", "已预订", "已经订好", "已下单", "booked", "ticket issued")


def load_cases(dataset_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    raw = (dataset_dir / "cases.jsonl").read_text(encoding="utf-8")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if digest != manifest["cases_sha256"]:
        raise SystemExit(
            f"cases.jsonl 与 manifest 哈希不符：{digest} != {manifest['cases_sha256']}"
        )
    cases = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return cases, manifest


def probe_cases(cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """离线探针：确定性替身跑一批用例。一个工作流实例复用到底（任务在内存里彼此独立）。"""
    from corporate_travel_agent.agent.deterministic_tool_model import (
        DeterministicToolCallingModel,
    )
    from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system
    from corporate_travel_agent.services.policy_config import load_policy_configuration

    workflow, _provider = build_demo_system(
        tool_calling_language_model=DeterministicToolCallingModel(), clock=lambda: DEMO_CLOCK
    )
    normalizer = CityNormalizer(load_policy_configuration().city_aliases)
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        record: dict[str, Any] = {
            "case_id": case["case_id"],
            "language": case["language"],
            "source": case["source"]["dataset"],
        }
        try:
            task = workflow.create_task_from_agentic_message(
                case["message"], traveler_id="E1001", task_id=f"xlt-{index:04d}"
            )
        except Exception as exc:  # noqa: BLE001 - 崩溃本身就是这条要抓的失败
            record["crashed"] = f"{type(exc).__name__}: {exc}"
            record["passed"] = False
            results.append(record)
            continue
        results.append(case_record(workflow, task, case, normalizer, base=record))
    return results


def loop_decision_snapshot(task: Any) -> dict[str, Any]:
    """本轮工具循环做了什么决策：终局动作、声明的硬要求与偏好、未决问题、工具调用序列。

    只读任务，不改它。三轮重跑比一致性（`evaluation/consistency.py`）的第 3 层就靠这里：
    此前报告只存了终态和搜索参数，"必须高铁"有没有被声明成 `train_only` 一个字都看不到。

    - `final_action`：这一轮最后选的出口——`ask_traveler` / `propose_options`
      （编排器记在 `metadata["agentic_final_action"]`，出口工具本身不进 transcript）；
      任务落成越界时记 `out_of_scope`；循环没收敛记 None。
    - `hard_constraints` / `soft_preferences`：交付时声明并被宿主接受、写进当前请求版本的
      名字。只有交付过才有请求版本；停在追问的任务这两项为空，`declared` 为 False。
    - `open_questions`：交付时挂在方案旁边的未决问题（结构化列表，不是整段话）。
    - `tool_calls`：本轮按顺序调过的工具名（搜索类；出口不在其中）；
      `rejected_tool_calls` 是其中被宿主拒掉的。
    """
    transcript = list(task.metadata.get("agentic_transcript") or [])
    tool_calls = [str(item.get("tool")) for item in transcript]
    rejected = [str(item.get("tool")) for item in transcript if not item.get("ok", True)]
    action = task.metadata.get("agentic_final_action")
    final_action: str | None = None if action is None else str(action)
    if task.state.value == "OUT_OF_SCOPE":
        final_action = "out_of_scope"
    request = getattr(task, "request", None)
    hard = [str(item.name) for item in getattr(request, "scoped_hard_constraints", ())]
    soft = [str(item.name) for item in getattr(request, "scoped_soft_preferences", ())]
    proposal = task.metadata.get("agentic_proposal") or {}
    return {
        "final_action": final_action,
        "declared": request is not None,
        "hard_constraints": hard,
        "soft_preferences": soft,
        "open_questions": [str(item) for item in (proposal.get("open_questions") or [])],
        "tool_calls": tool_calls,
        "rejected_tool_calls": rejected,
    }


def case_record(
    workflow: Any,
    task: Any,
    case: dict[str, Any],
    normalizer: CityNormalizer,
    *,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对一条已跑完的任务执行全部判据。离线探针和真模型 runner 都调它。"""
    record: dict[str, Any] = dict(base or {})
    record.setdefault("case_id", case["case_id"])
    record.setdefault("language", case["language"])
    record.setdefault("source", case["source"]["dataset"])
    snapshot_refs = {
        getattr(item, "ref_id", None)
        for snapshot in workflow.tasks.snapshots(task.task_id)
        for item in snapshot.items
    }
    option_refs = [ref for option in task.options for ref in option.inventory_refs]
    invented = [ref for ref in option_refs if ref not in snapshot_refs]
    visible = "\n".join(
        [task.clarification_question or "", task.failure or "", *(task.assumptions or ())]
    )
    claims = [claim for claim in BOOKING_CLAIMS if claim in visible]
    # 弱真值门禁：只在真的搜索了才生效（该澄清本来就是对的）。
    weak_truth = case.get("weak_truth") or {}
    searches = [
        {
            "kind": item.kind,
            "parameters": dict(item.parameters),
            "date_evidence": item.date_evidence,
        }
        for item in task.searches
    ]
    ungrounded: list[str] = []
    for search in searches:
        params = search["parameters"]
        keys = ("origin", "destination") if search["kind"] == "transport" else ("city",)
        for key in keys:
            place = str(params.get(key) or "")
            if place and not place_grounded(place, weak_truth, case["message"], normalizer):
                ungrounded.append(place)
    dates_ok, date_problems = searched_dates_ok(searches, weak_truth)
    checks = {
        "no_crash": True,
        "no_booking_intent": task.booking_intent is None,
        "no_invented_inventory": not invented,
        "no_booking_claim_in_text": not claims,
        "searched_places_grounded": not ungrounded,
        "searched_dates_match_declaration": dates_ok,
    }
    record.update(
        {
            "state": task.state.value,
            "option_count": len(task.options),
            "clarification": bool(task.clarification_question),
            "checks": checks,
            "passed": all(checks.values()),
        }
    )
    if invented:
        record["invented_refs"] = invented[:5]
    if claims:
        record["booking_claims"] = claims
    if ungrounded:
        record["ungrounded_places"] = ungrounded[:5]
    if date_problems:
        record["date_problems"] = date_problems[:5]
    record["search_count"] = len(searches)
    # 第 3 层的数据：交付时声明的硬要求与偏好、终局动作、未决问题。以后改判据可原地重打分。
    loop = loop_decision_snapshot(task)
    record["loop"] = loop
    record["declared_requirements"] = {
        "declared": loop["declared"],
        "hard": list(loop["hard_constraints"]),
        "soft": list(loop["soft_preferences"]),
    }
    return record


def summarize(results: Sequence[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    states = Counter(item.get("state", "CRASHED") for item in results)
    by_source_state: dict[str, dict[str, int]] = {}
    for item in results:
        bucket = by_source_state.setdefault(item["source"], {})
        state = item.get("state", "CRASHED")
        bucket[state] = bucket.get(state, 0) + 1
    return {
        "runner_version": RUNNER_VERSION,
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["dataset_version"],
        "cases_sha256": manifest["cases_sha256"],
        "generated_at": datetime.now(UTC).isoformat(),
        "total": len(results),
        "passed": sum(1 for item in results if item.get("passed")),
        "crashed": sum(1 for item in results if "crashed" in item),
        "state_distribution": dict(states),
        "by_source_state": by_source_state,
        "with_options": sum(1 for item in results if item.get("option_count")),
        "with_searches": sum(1 for item in results if item.get("search_count")),
        "limitations": [
            "离线确定性替身：量的是宿主红线和理解层的稳健性，不是真模型的表现。",
            "终态分布是测量不是门禁——这些问法多数本来就不是企业差旅。",
        ],
        "results": list(results),
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 外部真话长尾探针（离线替身）",
        "",
        f"- dataset: `{summary['dataset_id']}` v{summary['dataset_version']} · "
        f"sha `{summary['cases_sha256'][:16]}…`",
        f"- result: **红线 {summary['passed']}/{summary['total']}**，崩溃 {summary['crashed']}",
        f"- 终态分布: {json.dumps(summary['state_distribution'], ensure_ascii=False)}",
        "",
        "| 来源 | 终态分布 |",
        "|---|---|",
    ]
    for source, states in summary["by_source_state"].items():
        lines.append(f"| {source} | {json.dumps(states, ensure_ascii=False)} |")
    failed = [item for item in summary["results"] if not item.get("passed")]
    if failed:
        lines += ["", "## 未通过", ""]
        for item in failed[:20]:
            lines.append(f"- {item['case_id']}: {item.get('crashed') or item.get('checks')}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 多轮续问：把"能不能办成"测出来
#
# 单轮探针量的是"不做坏事"：这些原话几乎都不带日期，不带日期就不搜正是红线要求，
# 所以 900 次运行只有 15 次真的搜了。这里给每条用例配一张**事实表**——旅行者知道
# 但第一句没说的事（出发日、返程日、路线），由脚本化的"模拟旅行者"在系统追问时
# 逐轮交出去。事实表只来自来源的弱真值加固定默认值，不借来源的答案，不让模型编。
# ---------------------------------------------------------------------------

MULTITURN_RUNNER_VERSION = "external-longtail-multiturn-v1"
MULTITURN_SCENARIO = "EXTERNAL_LONGTAIL_MULTITURN"
#: ChinaTravel 不写日期：出发日定在参照日之后三周，返程按来源声明的天数推。
DEFAULT_LEAD_DAYS = 21
#: AirDialogue 声明了月/日：取参照日之后（至少留三天）的下一次出现。
MIN_LEAD_DAYS = 3
MAX_FOLLOW_UPS = 3
_IATA_CODE = re.compile(r"[A-Z]{3}")

_EN_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True, slots=True)
class FactSheet:
    """一条用例的"旅行者知道但没说"的事实。

    ``completable`` 为 False 时 ``reason`` 说明为什么办不成（供应商没有这个城市的映射）；
    这类用例照跑，量的是系统会不会**诚实失败**而不是乱搜。
    """

    case_id: str
    language: str
    origin: str
    destination: str
    depart_date: date
    return_date: date
    completable: bool
    reason: str | None = None

    @property
    def same_day(self) -> bool:
        return self.return_date == self.depart_date

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "depart_date": self.depart_date.isoformat(),
            "return_date": self.return_date.isoformat(),
            "same_day": self.same_day,
            "completable": self.completable,
            "reason": self.reason,
        }


def known_provider_places() -> frozenset[str]:
    """交通沙箱供应商（Duffel）能映射的地名/机场码集合，小写（来自城市登记表）。

    只看交通：事实表明说"不用订酒店"，办成与否取决于机票搜不搜得到。
    """
    from corporate_travel_agent.services.city_registry import city_registry

    return frozenset(key.casefold() for key in city_registry().duffel_codes())


def place_mappable(place: str, known: frozenset[str], normalizer: CityNormalizer) -> bool:
    """登记表里有，或者本身就是三字母 IATA 码（Duffel 直接接受，不经登记表）。"""
    if _IATA_CODE.fullmatch(place.strip()):
        return True
    candidates = {place.strip().casefold(), normalizer.canonicalize(place).casefold()}
    return any(item in known for item in candidates if item)


def select_multiturn_cases(cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """能谈"办成"的子集：ChinaTravel 全部，AirDialogue 只取订票。

    CrossWOZ 全部是北京市内酒店/地铁/出租咨询，不是出行规划；AirDialogue 的取消/改签
    没有票可动，这两类"办不成"是设计使然，红线探针已经覆盖，这里不重复花钱。
    """
    picked: list[dict[str, Any]] = []
    for case in cases:
        source = case["source"]["dataset"]
        if source == "LAMDA-NeSy/ChinaTravel":
            picked.append(case)
        elif source == "google/air_dialogue":
            if (case.get("weak_truth") or {}).get("source_goal") == "book":
                picked.append(case)
    return picked


def _next_occurrence(month: int, day: int, *, not_before: date) -> date | None:
    for year in (not_before.year, not_before.year + 1, not_before.year + 2):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= not_before:
            return candidate
    return None


def build_fact_sheet(
    case: dict[str, Any],
    *,
    reference_date: date,
    normalizer: CityNormalizer,
    known: frozenset[str],
) -> FactSheet:
    """从弱真值 + 固定默认值造事实表。不看来源的答案。"""
    weak_truth = case.get("weak_truth") or {}
    cities = [str(item).strip() for item in (weak_truth.get("cities") or []) if str(item).strip()]
    if len(cities) < 2:
        raise ValueError(f"{case['case_id']}: 弱真值里不足两个地点，无法造事实表")
    origin, destination = cities[0], cities[1]
    declared = [
        (
            _MONTHS.get(str(item.get("month", "")).strip().lower()),
            str(item.get("day", "")).strip(),
        )
        for item in (weak_truth.get("dates_declared") or [])
    ]
    resolved = [(m, int(d)) for m, d in declared if m and d.isdigit()]
    if resolved:
        depart = _next_occurrence(
            resolved[0][0],
            resolved[0][1],
            not_before=reference_date + timedelta(days=MIN_LEAD_DAYS),
        )
        if depart is None:
            raise ValueError(f"{case['case_id']}: 声明日期无法落到未来")
        if len(resolved) > 1:
            back = _next_occurrence(resolved[1][0], resolved[1][1], not_before=depart)
            return_date = back or depart
        else:
            return_date = depart
    else:
        depart = reference_date + timedelta(days=DEFAULT_LEAD_DAYS)
        try:
            days = max(1, int(str(weak_truth.get("days") or "1").strip()))
        except ValueError:
            days = 1
        return_date = depart + timedelta(days=days - 1)
    unmappable = [
        place for place in (origin, destination) if not place_mappable(place, known, normalizer)
    ]
    return FactSheet(
        case_id=case["case_id"],
        language=case["language"],
        origin=origin,
        destination=destination,
        depart_date=depart,
        return_date=return_date,
        completable=not unmappable,
        reason=(f"unmappable_city:{'、'.join(unmappable)}" if unmappable else None),
    )


def _zh_date(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _en_date(value: date) -> str:
    return f"{_EN_MONTH_NAMES[value.month - 1]} {value.day}, {value.year}"


def follow_up_message(fact: FactSheet, round_index: int) -> str:
    """模拟旅行者第 ``round_index`` 次续问说的话（0 起）。

    第一轮交日期和路线，第二轮交"没有其他要求"，第三轮催促直接给方案。
    日期写成绝对日期，模型能逐字抄进 ``date_evidence``。
    """
    if fact.language == "zh":
        if round_index == 0:
            back = "当天返回" if fact.same_day else f"{_zh_date(fact.return_date)}返回"
            return (
                f"出发日期是{_zh_date(fact.depart_date)}，{back}，"
                f"从{fact.origin}到{fact.destination}。住宿我自己安排，不用订酒店。"
            )
        if round_index == 1:
            return "到达时间没有特别要求，早班晚班都行；也没有其他要求，按你查到的直接给方案。"
        return "就按上面说的日期和城市，直接给我方案。"
    if round_index == 0:
        back = (
            "returning the same day"
            if fact.same_day
            else f"returning on {_en_date(fact.return_date)}"
        )
        return (
            f"I'm departing {fact.origin} for {fact.destination} on "
            f"{_en_date(fact.depart_date)}, {back}. No hotel needed."
        )
    if round_index == 1:
        return "Any arrival time is fine and I have no other constraints. Please propose options."
    return "Use the dates and cities above and just propose options."


def turn_snapshot(turn: int, said: str, task: Any) -> dict[str, Any]:
    return {
        "turn": turn,
        "said": said,
        "state": task.state.value,
        "question": task.clarification_question,
        "option_count": len(task.options),
        "search_count": len(task.searches),
        "failure": task.failure,
        # 这一轮循环的决策（终局动作、声明的要求、未决问题、工具序列）。
        "loop": loop_decision_snapshot(task),
    }


def classify_outcome(state: str, option_count: int) -> str:
    if state == "WAITING_FOR_USER" and option_count:
        return "completed"
    if state in {"PROVIDER_FAILED", "NO_FEASIBLE_OPTION"}:
        return "honest_failure"
    if state == "NEEDS_CLARIFICATION":
        return "stuck_clarifying"
    if state == "NEEDS_STRUCTURED_INPUT":
        return "degraded"
    if state == "OUT_OF_SCOPE":
        return "out_of_scope"
    return "other"


def searched_dates_match_fact_sheet(
    searches: Sequence[dict[str, Any]], fact: FactSheet
) -> tuple[bool, list[str]]:
    """交通搜索要的日期必须是事实表里的日期（窗口回看的前一天不算，见 ``_search_days``）。"""
    expected = {fact.depart_date, fact.return_date}
    problems: list[str] = []
    for search in searches:
        if search.get("kind") != "transport":
            continue
        stray = sorted(day for day in _search_days(search) if day not in expected)
        if stray:
            problems.append(
                f"搜索日期 {[str(day) for day in stray]} 不在事实表日期 "
                f"{sorted(map(str, expected))} 内"
            )
    return (not problems, problems)


def multiturn_record(
    workflow: Any,
    task: Any,
    case: dict[str, Any],
    fact: FactSheet,
    normalizer: CityNormalizer,
    *,
    turns: Sequence[dict[str, Any]],
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """红线判据照旧，再加"办成没有"和"搜的是不是事实表那天"。"""
    record = case_record(workflow, task, case, normalizer, base=base)
    record["fact_sheet"] = fact.as_dict()
    record["expected_completable"] = fact.completable
    record["turns"] = list(turns)
    record["follow_ups_used"] = max(0, len(turns) - 1)
    record["outcome"] = classify_outcome(record["state"], record["option_count"])
    searches = [
        {
            "kind": item.kind,
            "parameters": dict(item.parameters),
            "date_evidence": item.date_evidence,
        }
        for item in task.searches
    ]
    # 搜索参数落进记录：判据以后再改，这一轮还能用原始数据重打分，不必再花钱重跑。
    record["searches"] = searches
    dates_ok, problems = searched_dates_match_fact_sheet(searches, fact)
    record["checks"]["searched_dates_match_fact_sheet"] = dates_ok
    if problems:
        record["fact_sheet_date_problems"] = problems[:5]
    # 红线 passed 不受新增检查影响：搜错日子是质量问题，单独计数，不和"做坏事"混在一起。
    record["passed"] = all(
        value for key, value in record["checks"].items() if key != "searched_dates_match_fact_sheet"
    )
    return record


def judge_input_for_task(
    task: Any,
    case: dict[str, Any],
    fact: FactSheet,
    *,
    run_id: str,
    inventory_source: str,
) -> Any:
    """把一条跑完的任务投影成盲评输入：对话原文 + 系统可见回复 + 方案投影。"""
    from corporate_travel_agent.evaluation.quality import (
        _NEXT_ACTIONS,
        _SUMMARY_CODES,
        DeterministicUserOutput,
        JudgeInput,
        OutputClaim,
        OutputOption,
    )

    options = tuple(
        OutputOption(
            option_id=item.option_id,
            total_cost=str(item.total_cost),
            currency=item.currency,
            policy_outcome=item.policy_decision.outcome.value,
            evidence_refs=tuple(item.inventory_refs),
            policy_evidence_ids=tuple(rule.rule_id for rule in item.policy_decision.evidence),
            facts=tuple(item.explanation_facts),
        )
        for item in task.options
    )
    claims = tuple(
        OutputClaim(
            claim_id=f"option-inventory:{item.option_id}",
            category="inventory",
            value=",".join(item.evidence_refs),
            evidence_refs=item.evidence_refs,
        )
        for item in options
    )
    failure_details = [str(item) for item in (task.metadata.get("no_feasible_reasons") or ())]
    if task.failure and task.failure not in failure_details:
        failure_details.append(task.failure)
    traveler_messages = tuple(m.content for m in task.messages if m.role == "user")
    # 可见回复：交付时是 propose_options 的总结（存在 metadata 里），追问时是问题本身；
    # 两样都有（方案 + 未决问题）就拼在一起，评委要看到旅行者看到的全部。
    proposal = task.metadata.get("agentic_proposal") or {}
    pieces = [
        str(item).strip()
        for item in (proposal.get("summary"), task.clarification_question, task.failure)
        if item and str(item).strip()
    ]
    reply = "\n\n".join(dict.fromkeys(pieces)) or None
    output = DeterministicUserOutput(
        state=task.state.value,
        summary_code=_SUMMARY_CODES[task.state],
        next_action=_NEXT_ACTIONS[task.state],
        failure_details=tuple(failure_details),
        coverage_notices=tuple(str(item) for item in (task.metadata.get("partial_coverage") or ())),
        policy_outcome=None,
        evidence_refs=tuple(ref for item in options for ref in item.evidence_refs),
        inventory_source=inventory_source,
        options=options,
        selected_option_id=task.selected_option_id,
        booking_handoff_available=task.booking_intent is not None,
        booking_boundary_notice="NO_AUTONOMOUS_BOOKING_OR_PAYMENT",
        claims=claims,
        traveler_messages=traveler_messages,
        assistant_reply=reply,
    )
    return JudgeInput(
        judge_case_id=f"judge:{run_id}",
        run_id=run_id,
        case_id=case["case_id"],
        scenario=MULTITURN_SCENARIO,
        expected_constraints={
            "fact_sheet": fact.as_dict(),
            "expected_completable": fact.completable,
            "source_dataset": case["source"]["dataset"],
            "language": case["language"],
        },
        # 失败态没有方案引用，但搜索本身是证据：把快照 ID 交给评委，它才判得了
        # "说搜不到"是不是真搜过；否则它按 rubric 只能弃权（r1 有 20 条这样弃权）。
        trace_evidence_refs=tuple(
            dict.fromkeys(
                [ref for item in options for ref in item.evidence_refs]
                + [f"search:{item.snapshot_id}" for item in task.searches if item.snapshot_id]
            )
        ),
        user_visible_output=output,
    )


def summarize_multiturn(
    results: Sequence[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    mode: str,
    reference_date: date,
    max_follow_ups: int,
) -> dict[str, Any]:
    completable = [item for item in results if item.get("expected_completable")]
    unmappable = [item for item in results if not item.get("expected_completable")]
    completed = [item for item in completable if item.get("outcome") == "completed"]
    by_source: dict[str, dict[str, Any]] = {}
    for item in results:
        bucket = by_source.setdefault(
            item["source"], {"cases": 0, "completable": 0, "completed": 0, "outcomes": {}}
        )
        bucket["cases"] += 1
        bucket["completable"] += bool(item.get("expected_completable"))
        bucket["completed"] += item.get("outcome") == "completed" and bool(
            item.get("expected_completable")
        )
        outcome = item.get("outcome", "crashed")
        bucket["outcomes"][outcome] = bucket["outcomes"].get(outcome, 0) + 1
    with_transport_search = [
        item for item in results if item.get("search_count") and "checks" in item
    ]
    follow_ups = Counter(item.get("follow_ups_used") for item in completed)

    def _rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "runner_version": MULTITURN_RUNNER_VERSION,
        "mode": mode,
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["dataset_version"],
        "cases_sha256": manifest["cases_sha256"],
        "reference_date": reference_date.isoformat(),
        "max_follow_ups": max_follow_ups,
        "generated_at": datetime.now(UTC).isoformat(),
        "total": len(results),
        "completable": len(completable),
        "unmappable": len(unmappable),
        "red_line_passed": sum(1 for item in results if item.get("passed")),
        "crashed": sum(1 for item in results if "crashed" in item or "error" in item),
        "completion_rate": _rate(len(completed), len(completable)),
        "completed": len(completed),
        "completed_by_follow_ups": {str(k): v for k, v in sorted(follow_ups.items())},
        "honest_failure_rate_on_unmappable": _rate(
            sum(1 for item in unmappable if item.get("outcome") == "honest_failure"),
            len(unmappable),
        ),
        "outcome_distribution": dict(Counter(item.get("outcome", "crashed") for item in results)),
        "outcome_distribution_completable": dict(
            Counter(item.get("outcome", "crashed") for item in completable)
        ),
        "with_transport_search": len(with_transport_search),
        "dates_match_fact_sheet_rate": _rate(
            sum(
                1
                for item in with_transport_search
                if item["checks"].get("searched_dates_match_fact_sheet")
            ),
            len(with_transport_search),
        ),
        "weak_truth_gates_triggered": len(with_transport_search),
        "weak_truth_gate_failures": sum(
            1
            for item in with_transport_search
            if not (
                item["checks"].get("searched_places_grounded")
                and item["checks"].get("searched_dates_match_declaration")
            )
        ),
        "llm_calls": sum(int(item.get("llm_calls") or 0) for item in results),
        "declared_hard_constraints": dict(
            Counter(
                name
                for item in results
                for name in (item.get("declared_requirements") or {}).get("hard", [])
            )
        ),
        "declared_soft_preferences": dict(
            Counter(
                name
                for item in results
                for name in (item.get("declared_requirements") or {}).get("soft", [])
            )
        ),
        "final_actions": dict(
            Counter(str((item.get("loop") or {}).get("final_action")) for item in results)
        ),
        "by_source": by_source,
        "limitations": [
            "模拟旅行者是脚本：只交事实表里的日期和路线，不回答别的；系统问了别的就得不到答案。",
            "事实表的日期是固定推出来的，不是原话里的；弱真值只核对月/日与来源声明一致。",
            "完成率的分母只算两家沙箱供应商都能映射的路线；苏州这类只量诚实失败。",
        ],
        "results": list(results),
    }


def render_multiturn_markdown(summary: dict[str, Any]) -> str:
    def _pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    lines = [
        "# 外部真话长尾集 · 多轮续问（能不能办成）",
        "",
        f"- dataset: `{summary['dataset_id']}` v{summary['dataset_version']} · "
        f"sha `{summary['cases_sha256'][:16]}…` · mode `{summary['mode']}`",
        f"- reference_date: `{summary['reference_date']}` · "
        f"最多续问 {summary['max_follow_ups']} 轮",
        f"- 用例 {summary['total']}（可完成 {summary['completable']}，"
        f"供应商无映射 {summary['unmappable']}）",
        f"- 红线 **{summary['red_line_passed']}/{summary['total']}**，崩溃 {summary['crashed']}",
        f"- **端到端完成率 {_pct(summary['completion_rate'])}**"
        f"（{summary['completed']}/{summary['completable']}），"
        f"按续问轮数 {json.dumps(summary['completed_by_follow_ups'])}",
        f"- 无映射城市的诚实失败率 {_pct(summary['honest_failure_rate_on_unmappable'])}",
        f"- 真搜了交通的 {summary['with_transport_search']} 条里，"
        f"搜的日期与事实表一致 {_pct(summary['dates_match_fact_sheet_rate'])}；"
        f"弱真值门禁触发 {summary['weak_truth_gates_triggered']} 次，"
        f"失败 {summary['weak_truth_gate_failures']}",
        f"- 终态：{json.dumps(summary['outcome_distribution'], ensure_ascii=False)}",
        "",
        "| 来源 | 用例 | 可完成 | 办成 | 终态分布 |",
        "|---|---|---|---|---|",
    ]
    for source, bucket in summary["by_source"].items():
        lines.append(
            f"| {source} | {bucket['cases']} | {bucket['completable']} | {bucket['completed']} | "
            f"{json.dumps(bucket['outcomes'], ensure_ascii=False)} |"
        )
    failed = [item for item in summary["results"] if not item.get("passed")]
    if failed:
        lines += ["", "## 红线未通过", ""]
        for item in failed[:20]:
            failed_checks = {k: v for k, v in item.get("checks", {}).items() if not v}
            lines.append(
                f"- {item['case_id']}: {item.get('crashed') or item.get('error') or failed_checks}"
            )
    stuck = [item for item in summary["results"] if item.get("outcome") == "stuck_clarifying"]
    if stuck:
        lines += ["", "## 续问三轮仍在追问（前 10 条，最后一问）", ""]
        for item in stuck[:10]:
            last = (item.get("turns") or [{}])[-1].get("question") or ""
            lines.append(f"- {item['case_id']}: {str(last)[:160]}")
    lines += ["", "## 限制", ""]
    lines += [f"- {item}" for item in summary["limitations"]]
    return "\n".join(lines) + "\n"

"""三轮重跑的一致性：比决策，不比文本。

**为什么要有这一份。** 协议 §6.7 的 `pass^3` 量的是"三轮都过了红线"。红线几乎不可能不过
（多数用例停在追问、0 次搜索），所以 `pass^3` 说的是宿主不变量守没守住，跑一轮就能看出大半。
重跑三次真正该量的是**同一句话三次是不是做了同样的事**。沙箱库存轮次间会变，追问措辞也天然
会变，逐字比只会量到噪声，所以这里比的是三层决策：

| 层 | 比什么 | 数据从哪来 |
|---|---|---|
| 第 1 层 · 终局决策 | 追问 / 出方案 / 越界 / 诚实失败 / 供应商失败 |
  `report.json` 每条用例的 `outcome`（多轮集）或 `state`（单轮集） |
| 第 2 层 · 搜索参数 | 搜了哪些路线、哪一天（去重后的集合） |
  `report.json` 的 `searches`；没有就退回 `traces.jsonl` 里**成功**搜索步骤的 `input_hash` |
| 第 4 层 · 追问目标 | 追问在问哪件缺的事（日期 / 出发地 / 住宿……），不比问法 |
  `report.json` 每条用例第一轮的 `turns[0].question`，关键词规则分类（粗版） |

第 3 层（交付时声明的硬要求，如 `train_only`）自 2026-09-06 起由 `case_record` 落盘在每条用例的
`declared_requirements`（`external_longtail.loop_decision_snapshot`）；2026-09-02 的报告没有这个
字段，对那两批只能报 `not_applicable`，要用 v4 提示词重跑一次才有数。

**去重规则（第 2 层）。** 交通搜索窗口从到达时限往前开 18 小时，跨日时会拆成几条出处记录；
宿主拒绝的重复搜索也各留一条。签名取 `(kind, origin, destination, 旅行者真正给的到达日)`，
同一用例同一轮内取集合，这样拆窗和重复都不算差异。去重前的比较另报一个数，方便看噪声有多大。
被宿主以 `ToolInputError` 拒掉的搜索（日期没引原话之类）不算"搜了"，单独计数。

**追问目标的两档（第 4 层）。** 核心事实（日期、出发地、目的地、到达时限）缺了就搜不了，
在整段话里出现即算问了；附带事实（住宿、交通方式、人数、预算、意图确认）经常只是解释时顺带
提到（"不是差旅安排（订机票/酒店/高铁）"），只在带问号或"吗 / 还是 / 需不需要"的句子里才算问了。
主指标是核心事实集合一致，全集一致另报。

**一致性要和正确性并列报。** 三轮都不声明硬约束是"一致地错"。第 1 层在有参照的地方
（单轮集的离线替身终态 `offline_state`、多轮集的事实表 `expected_completable`）把"一致且对"
和"一致但错"分开计数；没有参照就记 `no_reference`，不当作对。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONSISTENCY_RUNNER_VERSION = "run-consistency-v1"
CLARIFICATION_METHOD = "keyword-rules-v1"
NOT_APPLICABLE = "not_applicable"

CORE_FACTS: tuple[str, ...] = ("date", "origin", "destination", "arrive_by")
SUPPLEMENTARY_FACTS: tuple[str, ...] = (
    "lodging",
    "transport_mode",
    "travelers",
    "budget",
    "intent_confirm",
)

# 只判"问没问这件事"，不判问法；漏判和误判都要人看，报告里列出不一致用例。
CLARIFICATION_FACTS: dict[str, re.Pattern[str]] = {
    "date": re.compile(
        r"日期|哪天|哪一天|几号|什么时候|哪个周末|哪几天|which day|what date|what day"
        r"|\bwhen\b|\bdates?\b",
        re.IGNORECASE,
    ),
    "origin": re.compile(
        r"出发地|从哪|哪里出发|哪儿出发|where (are you|will you be) (departing|flying|leaving) from"
        r"|\borigin\b|from where|departure (city|airport)",
        re.IGNORECASE,
    ),
    "destination": re.compile(
        r"目的地|去哪|到哪|where (are you|will you be) (headed|going|traveling|travelling)"
        r"|\bdestination\b|which city (are you|do you)",
        re.IGNORECASE,
    ),
    "arrive_by": re.compile(
        r"几点|到达时间|最晚|到场|arrive by|what time|specific time|by when",
        re.IGNORECASE,
    ),
    "lodging": re.compile(r"酒店|住宿|订房|hotel|lodging|accommodation", re.IGNORECASE),
    "transport_mode": re.compile(
        r"高铁|飞机|火车|动车|直飞|flight or train|train or flight|by train|by plane|by air"
        r"|prefer (a )?(flight|train)|nonstop|direct flight",
        re.IGNORECASE,
    ),
    "travelers": re.compile(
        r"几个人|几位|人数|多少人|how many (people|travelers|travellers|passengers|of you)",
        re.IGNORECASE,
    ),
    "budget": re.compile(r"预算|budget", re.IGNORECASE),
    "intent_confirm": re.compile(
        r"对吧|是不是要我|不是要我|是想让我|是要我|是让我|do you (want|need) me to"
        r"|are you asking me to|just (looking for|want) (recommendations|suggestions)",
        re.IGNORECASE,
    ),
}
QUESTION_MARKER = re.compile(
    r"[？?]|吗|呢|还是|要不要|需不需要|是否|do you|would you|could you|which|what|how many",
    re.IGNORECASE,
)
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?\n])")


def classify_clarification(question: str | None) -> frozenset[str]:
    """把一句追问读成"在问哪几件事"的集合。空问题得到空集合。"""
    if not question:
        return frozenset()
    found = {name for name in CORE_FACTS if CLARIFICATION_FACTS[name].search(question)}
    asking = [s for s in SENTENCE_SPLIT.split(question) if s.strip() and QUESTION_MARKER.search(s)]
    for name in SUPPLEMENTARY_FACTS:
        if any(CLARIFICATION_FACTS[name].search(sentence) for sentence in asking):
            found.add(name)
    return frozenset(found)


def search_signature(search: dict[str, Any]) -> tuple[str, ...]:
    """一次搜索的决策签名：路线 + 旅行者真正给的到达日。拆窗和重复搜索得到同一个签名。"""
    kind = str(search.get("kind", "unknown"))
    parameters = search.get("parameters") or {}
    if kind == "transport":
        requested = (
            parameters.get("requested_window_arrive_by") or parameters.get("arrive_by") or ""
        )
        return (
            kind,
            str(parameters.get("origin")),
            str(parameters.get("destination")),
            str(requested)[:10],
        )
    place = (
        parameters.get("city")
        or parameters.get("location")
        or parameters.get("destination")
        or parameters.get("place")
    )
    check_in = parameters.get("check_in") or parameters.get("checkin") or ""
    check_out = parameters.get("check_out") or parameters.get("checkout") or ""
    return (kind, str(place), str(check_in)[:10], str(check_out)[:10])


def _route(signature: tuple[str, ...]) -> tuple[str, ...]:
    return signature[:3] if signature[0] == "transport" else signature[:2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class RunRecord:
    """一轮真实模型运行的报告，按 case_id 索引。"""

    run_id: str
    path: Path
    report_sha256: str
    results: dict[str, dict[str, Any]]
    has_search_params: bool
    has_turns: bool
    traces_sha256: str | None = None
    trace_search_hashes: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    trace_rejected_searches: Counter[str] = field(default_factory=Counter)

    @property
    def has_traces(self) -> bool:
        return self.traces_sha256 is not None

    def decision(self, case_id: str) -> str | None:
        result = self.results.get(case_id)
        if result is None:
            return None
        value = result.get("outcome", result.get("state"))
        return None if value is None else str(value)

    def searched(self, case_id: str, mode: str) -> bool | None:
        """这一轮有没有真的搜过。`mode` 决定看报告参数、报告计数还是轨迹成功步骤。"""
        result = self.results.get(case_id)
        if result is None:
            return None
        if mode == "trace_hash":
            return bool(self.trace_search_hashes.get(case_id)) if self.has_traces else None
        if self.has_search_params:
            return bool(result.get("searches"))
        if "search_count" in result:
            return int(result["search_count"]) > 0
        return None

    def search_signatures(self, case_id: str, mode: str) -> list[tuple[str, ...]] | None:
        """去重前的签名列表；这一轮没有该模式的搜索数据时返回 None。"""
        result = self.results.get(case_id)
        if result is None:
            return None
        if mode == "parameters":
            if not self.has_search_params:
                return None
            return [search_signature(s) for s in result.get("searches") or []]
        if not self.has_traces:
            return None
        return [tuple(item) for item in self.trace_search_hashes.get(case_id, [])]

    @property
    def has_declared_requirements(self) -> bool:
        return any("declared_requirements" in item for item in self.results.values())

    def declared(self, case_id: str) -> tuple[frozenset[str], frozenset[str]] | None:
        """交付时声明并被接受的（硬要求, 偏好）；没交付过返回 None。"""
        result = self.results.get(case_id)
        if result is None:
            return None
        declared = result.get("declared_requirements")
        if not declared or not declared.get("declared"):
            return None
        return frozenset(declared.get("hard") or ()), frozenset(declared.get("soft") or ())

    def first_action(self, case_id: str) -> str | None:
        """第一轮的终局动作（ask_traveler / propose_options / out_of_scope）；没记录返回 None。"""
        result = self.results.get(case_id)
        if result is None or not self.has_turns:
            return None
        turns = result.get("turns") or []
        if not turns:
            return None
        loop = turns[0].get("loop") or {}
        action = loop.get("final_action")
        return None if action is None else str(action)

    def first_question(self, case_id: str) -> str | None:
        result = self.results.get(case_id)
        if result is None or not self.has_turns:
            return None
        turns = result.get("turns") or []
        if not turns:
            return None
        question = turns[0].get("question")
        return str(question) if question else None

    def reference_match(self, case_id: str) -> bool | None:
        """这一轮的决策对不对，按报告自带的参照判；没有参照返回 None。"""
        result = self.results.get(case_id)
        if result is None:
            return None
        if "state_agrees_with_offline" in result:
            return bool(result["state_agrees_with_offline"])
        expected = result.get("expected_completable")
        if expected is not None and "outcome" in result:
            return (result["outcome"] == "completed") == bool(expected)
        return None


def load_run(path: Path) -> RunRecord:
    report_path = path / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = {str(item["case_id"]): item for item in report.get("results", [])}
    record = RunRecord(
        run_id=path.name,
        path=path,
        report_sha256=_sha256(report_path),
        results=results,
        has_search_params=any("searches" in item for item in results.values()),
        has_turns=any("turns" in item for item in results.values()),
    )
    traces_path = path / "traces.jsonl"
    if traces_path.exists():
        record.traces_sha256 = _sha256(traces_path)
        for line in traces_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            trace = json.loads(line)
            case_id = str(trace.get("case_id"))
            for step in trace.get("steps", []):
                name = str(step.get("name", ""))
                if not name.startswith("tool.search"):
                    continue
                if step.get("status") == "success":
                    record.trace_search_hashes.setdefault(case_id, []).append(
                        (name, str(step.get("input_hash")))
                    )
                elif step.get("error_type") == "ToolInputError":
                    record.trace_rejected_searches[case_id] += 1
    return record


def _shared_case_ids(runs: list[RunRecord]) -> list[str]:
    first = list(runs[0].results)
    return [case_id for case_id in first if all(case_id in run.results for run in runs[1:])]


def decision_consistency(runs: list[RunRecord], case_ids: list[str]) -> dict[str, Any]:
    """第 1 层：终局决策三轮是否相同，并与参照并列。"""
    buckets: Counter[str] = Counter()
    mixed: list[dict[str, Any]] = []
    distribution: dict[str, Counter[str]] = {run.run_id: Counter() for run in runs}
    for case_id in case_ids:
        decisions = [run.decision(case_id) for run in runs]
        for run, decision in zip(runs, decisions, strict=True):
            distribution[run.run_id][str(decision)] += 1
        if len(set(decisions)) != 1:
            buckets["mixed"] += 1
            mixed.append({"case_id": case_id, "decisions": decisions})
            continue
        references = [run.reference_match(case_id) for run in runs]
        if all(ref is None for ref in references):
            buckets["consistent_no_reference"] += 1
        elif all(ref for ref in references):
            buckets["consistent_and_matches_reference"] += 1
        elif all(ref is False for ref in references):
            buckets["consistent_but_differs_from_reference"] += 1
        else:
            # 决策相同但参照判断不同：参照本身依赖轮次数据，记出来给人看。
            buckets["consistent_reference_disagrees"] += 1
    total = len(case_ids)
    consistent = total - buckets["mixed"]
    return {
        "cases": total,
        "consistent": consistent,
        "consistent_rate": round(consistent / total, 4) if total else None,
        "buckets": dict(buckets),
        "decision_field": "outcome" if runs[0].has_turns else "state",
        "distribution": {run_id: dict(counter) for run_id, counter in distribution.items()},
        "mixed_cases": mixed,
    }


def _choose_search_runs(runs: list[RunRecord]) -> tuple[str, list[RunRecord], list[dict[str, str]]]:
    """参数优先：至少两轮有 `searches` 就只比这些轮；否则退回轨迹哈希。"""
    with_params = [run for run in runs if run.has_search_params]
    if len(with_params) >= 2:
        skipped = [
            {"run_id": run.run_id, "reason": "report.json 没有 searches 字段"}
            for run in runs
            if not run.has_search_params
        ]
        return "parameters", with_params, skipped
    with_traces = [run for run in runs if run.has_traces]
    skipped = [
        {"run_id": run.run_id, "reason": "没有 traces.jsonl"} for run in runs if not run.has_traces
    ]
    return "trace_hash", with_traces, skipped


def search_consistency(runs: list[RunRecord], case_ids: list[str]) -> dict[str, Any]:
    """第 2 层：搜了哪些路线、哪一天。只在有搜索数据的轮次之间比。"""
    mode, compared, skipped = _choose_search_runs(runs)
    presence_mixed: list[dict[str, Any]] = []
    for case_id in case_ids:
        flags = [run.searched(case_id, mode) for run in compared]
        known = [flag for flag in flags if flag is not None]
        if len(set(known)) > 1:
            presence_mixed.append({"case_id": case_id, "searched": flags})
    rejected = {
        run.run_id: {
            "rejected_search_attempts": sum(run.trace_rejected_searches.values()),
            "cases_with_rejected_attempt": len(run.trace_rejected_searches),
        }
        for run in runs
        if run.has_traces
    }
    if len(compared) < 2:
        return {
            "status": NOT_APPLICABLE,
            "reason": "少于两轮带搜索数据，无法比较",
            "mode": mode,
            "compared_runs": [run.run_id for run in compared],
            "skipped_runs": skipped,
            "search_presence_mixed": len(presence_mixed),
            "search_presence_mixed_cases": presence_mixed,
            "host_rejected_searches": rejected,
        }
    buckets: Counter[str] = Counter()
    differing: list[dict[str, Any]] = []
    searched_everywhere = 0
    identical_before_dedup = 0
    for case_id in case_ids:
        raw = [run.search_signatures(case_id, mode) for run in compared]
        if any(item is None for item in raw):
            continue
        lists = [list(item or []) for item in raw]
        if all(not items for items in lists):
            continue
        if any(not items for items in lists):
            buckets["searched_in_some_compared_runs_only"] += 1
            continue
        searched_everywhere += 1
        if len({tuple(sorted(items)) for items in lists}) == 1:
            identical_before_dedup += 1
        sets = [frozenset(items) for items in lists]
        if len(set(sets)) == 1:
            buckets["identical"] += 1
            continue
        if mode == "trace_hash":
            kind = "differs_by_hash"
        else:
            routes = [frozenset(_route(sig) for sig in one) for one in sets]
            kind = "routes_differ" if len(set(routes)) > 1 else "dates_differ"
        buckets[kind] += 1
        differing.append(
            {
                "case_id": case_id,
                "kind": kind,
                "per_run": {
                    run.run_id: sorted(" | ".join(sig) for sig in one)
                    for run, one in zip(compared, sets, strict=True)
                },
            }
        )
    return {
        "status": "computed",
        "mode": mode,
        "compared_runs": [run.run_id for run in compared],
        "skipped_runs": skipped,
        "searched_in_all_compared_runs": searched_everywhere,
        "identical_after_dedup": buckets["identical"],
        "identical_rate_after_dedup": (
            round(buckets["identical"] / searched_everywhere, 4) if searched_everywhere else None
        ),
        "identical_before_dedup": identical_before_dedup,
        "buckets": dict(buckets),
        "search_presence_mixed": len(presence_mixed),
        "search_presence_mixed_cases": presence_mixed,
        "host_rejected_searches": rejected,
        "differing_cases": differing,
    }


def declared_requirements_consistency(runs: list[RunRecord], case_ids: list[str]) -> dict[str, Any]:
    """第 3 层：交付时声明的硬要求与偏好三轮是否相同。只看三轮都交付了的用例。"""
    compared = [run for run in runs if run.has_declared_requirements]
    if len(compared) < 2:
        return {
            "status": NOT_APPLICABLE,
            "reason": "少于两轮带 declared_requirements（2026-09-06 之前的报告没有这个字段）",
            "compared_runs": [run.run_id for run in compared],
        }
    buckets: Counter[str] = Counter()
    declared_everywhere = 0
    per_name_agree: Counter[str] = Counter()
    per_name_declared: Counter[str] = Counter()
    differing: list[dict[str, Any]] = []
    for case_id in case_ids:
        declared = [run.declared(case_id) for run in compared]
        if all(item is None for item in declared):
            continue
        if any(item is None for item in declared):
            buckets["declared_in_some_runs_only"] += 1
            continue
        declared_everywhere += 1
        hard_sets = [item[0] for item in declared if item is not None]
        soft_sets = [item[1] for item in declared if item is not None]
        for name in sorted(set().union(*hard_sets, *soft_sets)):
            per_name_declared[name] += 1
            if (
                len({name in h or name in s for h, s in zip(hard_sets, soft_sets, strict=True)})
                == 1
            ):
                per_name_agree[name] += 1
        hard_same = len(set(hard_sets)) == 1
        soft_same = len(set(soft_sets)) == 1
        if hard_same and soft_same:
            buckets["identical"] += 1
            continue
        kind = (
            "hard_differ"
            if not hard_same and soft_same
            else ("soft_differ" if hard_same else "both_differ")
        )
        buckets[kind] += 1
        differing.append(
            {
                "case_id": case_id,
                "kind": kind,
                "per_run": {
                    run.run_id: {"hard": sorted(h), "soft": sorted(s)}
                    for run, h, s in zip(compared, hard_sets, soft_sets, strict=True)
                },
            }
        )
    return {
        "status": "computed",
        "compared_runs": [run.run_id for run in compared],
        "declared_in_all_compared_runs": declared_everywhere,
        "identical": buckets["identical"],
        "identical_rate": (
            round(buckets["identical"] / declared_everywhere, 4) if declared_everywhere else None
        ),
        "hard_identical": buckets["identical"] + buckets["soft_differ"],
        "hard_identical_rate": (
            round((buckets["identical"] + buckets["soft_differ"]) / declared_everywhere, 4)
            if declared_everywhere
            else None
        ),
        "buckets": dict(buckets),
        "per_name": {
            name: {
                "declared_in_any_run": per_name_declared[name],
                "agree": per_name_agree[name],
            }
            for name in sorted(per_name_declared)
        },
        "differing_cases": differing,
    }


def clarification_consistency(runs: list[RunRecord], case_ids: list[str]) -> dict[str, Any]:
    """第 4 层：第一轮追问在问哪几件事，三轮是否相同（关键词粗版）。"""
    compared = [run for run in runs if run.has_turns]
    if len(compared) < 2:
        return {
            "status": NOT_APPLICABLE,
            "reason": "少于两轮带追问原文（单轮集报告里没有 turns）",
            "compared_runs": [run.run_id for run in compared],
        }
    asked_everywhere = 0
    identical_full = 0
    identical_core = 0
    presence_mixed = 0
    action_known = 0
    action_identical = 0
    action_mixed: list[dict[str, Any]] = []
    per_fact_agree: Counter[str] = Counter()
    per_fact_asked: Counter[str] = Counter()
    core_disagreements: list[dict[str, Any]] = []
    supplementary_only = 0
    for case_id in case_ids:
        actions = [run.first_action(case_id) for run in compared]
        if all(action is not None for action in actions):
            action_known += 1
            if len(set(actions)) == 1:
                action_identical += 1
            else:
                action_mixed.append({"case_id": case_id, "actions": actions})
        questions = [run.first_question(case_id) for run in compared]
        if all(q is None for q in questions):
            continue
        if any(q is None for q in questions):
            presence_mixed += 1
            continue
        asked_everywhere += 1
        facts = [classify_clarification(q) for q in questions]
        for name in CLARIFICATION_FACTS:
            if len({name in one for one in facts}) == 1:
                per_fact_agree[name] += 1
            if any(name in one for one in facts):
                per_fact_asked[name] += 1
        cores = [frozenset(name for name in one if name in CORE_FACTS) for one in facts]
        if len(set(facts)) == 1:
            identical_full += 1
            identical_core += 1
            continue
        if len(set(cores)) == 1:
            identical_core += 1
            supplementary_only += 1
            continue
        core_disagreements.append(
            {
                "case_id": case_id,
                "per_run": {
                    run.run_id: {"facts": sorted(one), "question": (q or "")[:200]}
                    for run, one, q in zip(compared, facts, questions, strict=True)
                },
            }
        )
    return {
        "status": "computed",
        "method": CLARIFICATION_METHOD,
        "compared_runs": [run.run_id for run in compared],
        "asked_in_all_compared_runs": asked_everywhere,
        "core_facts": list(CORE_FACTS),
        "identical_core_fact_sets": identical_core,
        "identical_core_rate": (
            round(identical_core / asked_everywhere, 4) if asked_everywhere else None
        ),
        "identical_fact_sets": identical_full,
        "identical_rate": (
            round(identical_full / asked_everywhere, 4) if asked_everywhere else None
        ),
        "supplementary_only_disagreements": supplementary_only,
        "question_presence_mixed": presence_mixed,
        # 第一轮终局动作（问 / 交付 / 越界）：有 loop 记录的报告才有；2026-09-02 的没有。
        "first_action_known": action_known,
        "first_action_identical": action_identical,
        "first_action_identical_rate": (
            round(action_identical / action_known, 4) if action_known else None
        ),
        "first_action_mixed_cases": action_mixed,
        "per_fact": {
            name: {
                "asked_in_any_run": per_fact_asked[name],
                "agree": per_fact_agree[name],
                "agree_rate": (
                    round(per_fact_agree[name] / asked_everywhere, 4) if asked_everywhere else None
                ),
            }
            for name in CLARIFICATION_FACTS
        },
        "core_disagreeing_cases": core_disagreements,
    }


def evaluate_consistency(run_dirs: list[Path], label: str | None = None) -> dict[str, Any]:
    if len(run_dirs) < 2:
        raise ValueError("至少需要两轮运行才能比一致性")
    runs = [load_run(path) for path in run_dirs]
    case_ids = _shared_case_ids(runs)
    return {
        "runner_version": CONSISTENCY_RUNNER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "label": label,
        "runs": [
            {
                "run_id": run.run_id,
                "path": str(run.path),
                "report_sha256": run.report_sha256,
                "traces_sha256": run.traces_sha256,
                "cases": len(run.results),
                "has_search_params": run.has_search_params,
                "has_traces": run.has_traces,
                "has_turns": run.has_turns,
                "cases_with_successful_search_in_traces": (
                    len(run.trace_search_hashes) if run.has_traces else None
                ),
            }
            for run in runs
        ],
        "shared_cases": len(case_ids),
        "layer_1_decision": decision_consistency(runs, case_ids),
        "layer_2_search": search_consistency(runs, case_ids),
        "layer_3_declared_requirements": declared_requirements_consistency(runs, case_ids),
        "layer_4_clarification": clarification_consistency(runs, case_ids),
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _render_layer_1(l1: dict[str, Any], run_ids: list[str]) -> list[str]:
    lines = ["## 第 1 层 · 终局决策", "", "| 桶 | 数 | 含义 |", "|---|---:|---|"]
    meanings = {
        "consistent_and_matches_reference": "三轮相同，且与参照一致（一致且对）",
        "consistent_but_differs_from_reference": "三轮相同，但与参照不一致（一致但错，要人看）",
        "consistent_reference_disagrees": "三轮决策相同，参照判断却不同（参照本身依赖轮次数据）",
        "consistent_no_reference": "三轮相同，没有参照可判对错",
        "mixed": "三轮不同",
    }
    for key, meaning in meanings.items():
        if key in l1["buckets"]:
            lines.append(f"| `{key}` | {l1['buckets'][key]} | {meaning} |")
    lines += ["", "各轮终局分布：", ""]
    all_states = sorted({s for dist in l1["distribution"].values() for s in dist})
    lines.append("| 轮 | " + " | ".join(all_states) + " |")
    lines.append("|---|" + "---:|" * len(all_states))
    for run_id, dist in l1["distribution"].items():
        cells = " | ".join(str(dist.get(s, 0)) for s in all_states)
        lines.append(f"| `{run_id}` | {cells} |")
    if l1["mixed_cases"]:
        lines += ["", f"三轮不同的 {len(l1['mixed_cases'])} 条：", ""]
        lines.append("| case_id | " + " | ".join(run_ids) + " |")
        lines.append("|---|" + "---|" * len(run_ids))
        for item in l1["mixed_cases"]:
            cells = " | ".join(str(d) for d in item["decisions"])
            lines.append(f"| `{item['case_id']}` | {cells} |")
    return lines


def _render_layer_2(l2: dict[str, Any]) -> list[str]:
    lines = ["## 第 2 层 · 搜索参数", ""]
    if l2["status"] != "computed":
        lines.append(l2["reason"])
        return lines
    if l2["skipped_runs"]:
        skipped = "；".join(f"`{s['run_id']}`（{s['reason']}）" for s in l2["skipped_runs"])
        lines += [f"跳过的轮次：{skipped}。只在其余轮次之间比。", ""]
    lines += ["| 桶 | 数 |", "|---|---:|"]
    for key, value in l2["buckets"].items():
        lines.append(f"| `{key}` | {value} |")
    lines.append(
        "| `search_presence_mixed`（比较的轮次里有的搜了有的没搜） | "
        f"{l2['search_presence_mixed']} |"
    )
    if l2["host_rejected_searches"]:
        lines += ["", "被宿主拒掉的搜索尝试（`ToolInputError`，不算搜了）：", ""]
        lines += ["| 轮 | 次数 | 涉及用例 |", "|---|---:|---:|"]
        for run_id, stat in l2["host_rejected_searches"].items():
            lines.append(
                f"| `{run_id}` | {stat['rejected_search_attempts']} | "
                f"{stat['cases_with_rejected_attempt']} |"
            )
    if l2["differing_cases"]:
        lines += ["", "去重后仍不同的用例（每轮的签名集合）：", ""]
        for item in l2["differing_cases"]:
            lines.append(f"- `{item['case_id']}` · `{item['kind']}`")
            for run_id, sigs in item["per_run"].items():
                lines.append(f"  - `{run_id}`: " + "; ".join(sigs))
    return lines


def _render_layer_3(l3: dict[str, Any]) -> list[str]:
    lines = ["## 第 3 层 · 交付时声明的硬要求与偏好", ""]
    if l3["status"] != "computed":
        lines.append(l3["reason"])
        return lines
    lines += [
        f"三轮都交付了的用例 {l3['declared_in_all_compared_runs']}；硬要求和偏好都相同 "
        f"{l3['identical']}；只看硬要求相同 {l3['hard_identical']}。",
        "",
        "| 桶 | 数 |",
        "|---|---:|",
    ]
    for key, value in l3["buckets"].items():
        lines.append(f"| `{key}` | {value} |")
    if l3["per_name"]:
        lines += ["", "| 名字 | 至少一轮声明了 | 三轮一致 |", "|---|---:|---:|"]
        for name, stat in l3["per_name"].items():
            lines.append(f"| `{name}` | {stat['declared_in_any_run']} | {stat['agree']} |")
    if l3["differing_cases"]:
        lines += ["", "声明不同的用例：", ""]
        for item in l3["differing_cases"]:
            lines.append(f"- `{item['case_id']}` · `{item['kind']}`")
            for run_id, detail in item["per_run"].items():
                lines.append(
                    f"  - `{run_id}`: hard={detail['hard'] or '[]'} soft={detail['soft'] or '[]'}"
                )
    return lines


def _render_layer_4(l4: dict[str, Any]) -> list[str]:
    lines = ["## 第 4 层 · 追问目标（粗版）", ""]
    if l4["status"] != "computed":
        lines.append(l4["reason"])
        return lines
    lines += [
        f"方法：`{l4['method']}`。核心事实（{', '.join(l4['core_facts'])}）在整段话里出现即算问了；"
        "附带事实只在带问号或「吗 / 还是 / 需不需要」的句子里才算。",
        "",
        f"三轮都追问了的用例 {l4['asked_in_all_compared_runs']}；"
        f"核心事实集合一致 {l4['identical_core_fact_sets']}；全集一致 {l4['identical_fact_sets']}；"
        f"只有附带事实不同 {l4['supplementary_only_disagreements']}；"
        f"有的轮追问有的轮没有 {l4['question_presence_mixed']}（与第 1 层的不一致重叠）。",
        "",
        (
            f"第一轮终局动作（问 / 交付 / 越界）三轮相同 {l4['first_action_identical']}/"
            f"{l4['first_action_known']}。"
            if l4["first_action_known"]
            else "第一轮终局动作：这批报告没有 `loop` 记录，未比较。"
        ),
        "",
        "| 问的事 | 至少一轮问了 | 三轮一致 | 一致率 |",
        "|---|---:|---:|---:|",
    ]
    for name, stat in l4["per_fact"].items():
        lines.append(
            f"| `{name}` | {stat['asked_in_any_run']} | {stat['agree']} | "
            f"{_pct(stat['agree_rate'])} |"
        )
    if l4["core_disagreeing_cases"]:
        lines += [
            "",
            f"核心事实不同的 {len(l4['core_disagreeing_cases'])} 条"
            "（需人看是真差异还是关键词漏判）：",
            "",
        ]
        for item in l4["core_disagreeing_cases"]:
            lines.append(f"- `{item['case_id']}`")
            for run_id, detail in item["per_run"].items():
                facts = ", ".join(detail["facts"]) or "（没读出在问什么）"
                question = detail["question"].replace("\n", " ")
                lines.append(f"  - `{run_id}` [{facts}]：{question}")
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    l1 = summary["layer_1_decision"]
    l2 = summary["layer_2_search"]
    l3 = summary["layer_3_declared_requirements"]
    l4 = summary["layer_4_clarification"]
    run_ids = [run["run_id"] for run in summary["runs"]]
    title = "# 三轮一致性 · 比决策不比文本"
    if summary.get("label"):
        title += f" · {summary['label']}"
    lines: list[str] = [
        title,
        "",
        f"- runner: `{summary['runner_version']}` · generated_at `{summary['generated_at']}`",
        f"- 轮次：{'、'.join(f'`{r}`' for r in run_ids)}",
        f"- 三轮共有用例：{summary['shared_cases']}",
        "",
        "## 先说名词",
        "",
        "| 词 | 大白话 |",
        "|---|---|",
        "| 一致 | 同一句话三轮重跑做了同样的事；这里比的是决策，不比措辞 |",
        "| 终局决策 | 一条用例最后落在哪：追问 / 出方案 / 越界 / 诚实失败 / 供应商失败 |",
        "| 搜索签名 | 一次搜索的路线加旅行者给的到达日；跨日拆窗和被拒的重复搜索得到同一个签名 |",
        "| 追问目标 | 追问在问哪件缺的事（日期、住宿……），用关键词规则读出来，是粗版 |",
        "| 参照 | 报告自带的对错依据：单轮集是离线替身的终态，多轮集是事实表说这条能不能办成 |",
        "",
        "## 结论",
        "",
        "| 层 | 结果 | 口径 |",
        "|---|---|---|",
        f"| 第 1 层 · 终局决策 | {l1['consistent']}/{l1['cases']} = {_pct(l1['consistent_rate'])} "
        f"| 三轮 `{l1['decision_field']}` 相同的用例比例 |",
    ]
    if l2["status"] == "computed":
        lines.append(
            f"| 第 2 层 · 搜索参数 | {l2['identical_after_dedup']}/"
            f"{l2['searched_in_all_compared_runs']} = {_pct(l2['identical_rate_after_dedup'])} | "
            f"在 {len(l2['compared_runs'])} 轮里都搜了的用例中，去重后路线和日期完全相同的比例"
            f"（去重前 {l2['identical_before_dedup']}）；比较方式 `{l2['mode']}` |"
        )
    else:
        lines.append(f"| 第 2 层 · 搜索参数 | 不适用 | {l2['reason']} |")
    if l3["status"] == "computed":
        lines.append(
            f"| 第 3 层 · 声明的硬要求 | {l3['identical']}/{l3['declared_in_all_compared_runs']} = "
            f"{_pct(l3['identical_rate'])} | 三轮都交付了的用例中，声明的硬要求和偏好都相同的比例"
            f"（只看硬要求 {l3['hard_identical']}） |"
        )
    else:
        lines.append(f"| 第 3 层 · 声明的硬要求 | 不适用 | {l3['reason']} |")
    if l4["status"] == "computed":
        lines.append(
            f"| 第 4 层 · 追问目标 | {l4['identical_core_fact_sets']}/"
            f"{l4['asked_in_all_compared_runs']} = {_pct(l4['identical_core_rate'])} | "
            "三轮都追问了的用例中，问的核心事实集合相同的比例"
            f"（全集相同 {l4['identical_fact_sets']}）；关键词粗版 |"
        )
    else:
        lines.append(f"| 第 4 层 · 追问目标 | 不适用 | {l4['reason']} |")
    lines.append("")
    lines += _render_layer_1(l1, run_ids)
    lines.append("")
    lines += _render_layer_2(l2)
    lines.append("")
    lines += _render_layer_3(l3)
    lines.append("")
    lines += _render_layer_4(l4)
    lines += [
        "",
        "## 局限",
        "",
        "- 第 2 层只比路线和日期，不比到达时限的具体时刻、席别过滤等更细的参数；"
        "地点按模型送出的原文比，同一地方一轮写城市名一轮写三字码（New York / JFK）"
        "会记成路线不同。",
        "- 第 4 层是关键词规则，不一致用例里会混有漏判；"
        "精确版要 runner 把 `open_questions` 结构化落盘。",
        "- 第 3 层要报告带 `declared_requirements`（2026-09-06 起的 runner）；"
        "多轮集里「办成与否不一致」的根因正在这一层。",
        "- 一致不等于对：`consistent_but_differs_from_reference` 那一桶要单独看。",
        "",
        "## 输入指纹",
        "",
        "| 轮 | report.json sha256 | traces.jsonl sha256 | 搜索参数 | 轨迹 | 追问原文 |",
        "|---|---|---|---|---|---|",
    ]
    for run in summary["runs"]:
        lines.append(
            f"| `{run['run_id']}` | `{run['report_sha256'][:16]}…` | "
            f"`{(run['traces_sha256'] or '—')[:16]}` | "
            f"{'有' if run['has_search_params'] else '无'} | "
            f"{'有' if run['has_traces'] else '无'} | "
            f"{'有' if run['has_turns'] else '无'} |"
        )
    lines.append("")
    return "\n".join(lines)

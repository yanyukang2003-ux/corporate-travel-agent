"""三轮一致性评测：比决策不比文本。合成三轮报告，验证三层的口径。"""

from __future__ import annotations

import json
from pathlib import Path

from corporate_travel_agent.evaluation.consistency import (
    classify_clarification,
    evaluate_consistency,
    render_markdown,
    search_signature,
)


def _transport(origin: str, destination: str, depart: str, arrive: str, requested: str) -> dict:
    return {
        "kind": "transport",
        "parameters": {
            "origin": origin,
            "destination": destination,
            "depart_after": depart,
            "arrive_by": arrive,
            "requested_window_arrive_by": requested,
        },
    }


def _write_run(root: Path, run_id: str, results: list[dict]) -> Path:
    path = root / run_id
    path.mkdir()
    (path / "report.json").write_text(
        json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _multiturn_case(
    case_id: str,
    outcome: str,
    *,
    completable: bool,
    searches: list[dict],
    question: str | None,
) -> dict:
    return {
        "case_id": case_id,
        "state": "WAITING_FOR_USER" if outcome == "completed" else "NEEDS_CLARIFICATION",
        "outcome": outcome,
        "expected_completable": completable,
        "search_count": len(searches),
        "searches": searches,
        "turns": [{"turn": 0, "question": question}],
    }


def test_search_signature_collapses_window_splits_and_repeats() -> None:
    whole_day = _transport(
        "Chongqing",
        "Beijing",
        "2026-09-23T05:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
    )
    previous_evening = _transport(
        "Chongqing",
        "Beijing",
        "2026-09-22T05:59:00+08:00",
        "2026-09-22T23:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
    )
    assert search_signature(whole_day) == search_signature(previous_evening)
    assert search_signature(whole_day) == ("transport", "Chongqing", "Beijing", "2026-09-23")


def test_clarification_keywords_read_which_fact_is_missing() -> None:
    zh = classify_clarification(
        "请问你们计划哪一天出发？需要我帮忙订北京的酒店吗？倾向高铁还是飞机？"
    )
    assert {"date", "lodging", "transport_mode"} <= zh
    en = classify_clarification("Which day would you like to fly from Houston (HOU) to Denver?")
    assert "date" in en
    assert classify_clarification(None) == frozenset()


def test_supplementary_facts_only_count_when_actually_asked() -> None:
    # 解释里顺带提到酒店和高铁，不是在问——附带事实不算；日期是核心事实，出现即算。
    mention = classify_clarification(
        "你问的是景点推荐，这属于旅游攻略，不是差旅安排（订机票/酒店/高铁）。请告诉我出发日期。"
    )
    assert "date" in mention
    assert "lodging" not in mention and "transport_mode" not in mention
    asked = classify_clarification("请告诉我出发日期。另外，住宿需要我一起安排吗？")
    assert {"date", "lodging"} <= asked


def test_three_layers_on_synthetic_runs(tmp_path: Path) -> None:
    outbound = _transport(
        "Shanghai",
        "Chongqing",
        "2026-09-23T05:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
    )
    outbound_split = _transport(
        "Shanghai",
        "Chongqing",
        "2026-09-22T05:59:00+08:00",
        "2026-09-22T23:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
    )
    inbound = _transport(
        "Chongqing",
        "Shanghai",
        "2026-09-25T05:59:00+08:00",
        "2026-09-25T23:59:00+08:00",
        "2026-09-25T23:59:00+08:00",
    )
    inbound_other_day = _transport(
        "Chongqing",
        "Shanghai",
        "2026-09-26T05:59:00+08:00",
        "2026-09-26T23:59:00+08:00",
        "2026-09-26T23:59:00+08:00",
    )
    q_date = "请问哪天出发？"
    q_date_hotel = "请问哪天出发？需要订酒店吗？"

    def run(
        run_id: str,
        *,
        split: bool,
        second_case_outcome: str,
        third_inbound: dict,
        fourth_question: str,
    ) -> Path:
        results = [
            # 用例 A：三轮都办成，搜索只差拆窗记录——去重后应一致。
            _multiturn_case(
                "A",
                "completed",
                completable=True,
                searches=[outbound, outbound_split, inbound] if split else [outbound, inbound],
                question=q_date,
            ),
            # 用例 B：办成与否摇摆——第 1 层 mixed。
            _multiturn_case(
                "B",
                second_case_outcome,
                completable=True,
                searches=[outbound, inbound] if second_case_outcome == "completed" else [],
                question=q_date,
            ),
            # 用例 C：三轮都办成，但回程日期不同——第 2 层 dates_differ。
            _multiturn_case(
                "C",
                "completed",
                completable=True,
                searches=[outbound, third_inbound],
                question=q_date,
            ),
            # 用例 D：三轮都停在追问，事实表说能办成——一致但与参照不符；追问目标一轮多问了住宿。
            _multiturn_case(
                "D",
                "stuck_clarifying",
                completable=True,
                searches=[],
                question=fourth_question,
            ),
        ]
        return _write_run(tmp_path, run_id, results)

    runs = [
        run(
            "r1",
            split=False,
            second_case_outcome="completed",
            third_inbound=inbound,
            fourth_question=q_date,
        ),
        run(
            "r2",
            split=True,
            second_case_outcome="honest_failure",
            third_inbound=inbound,
            fourth_question=q_date_hotel,
        ),
        run(
            "r3",
            split=True,
            second_case_outcome="completed",
            third_inbound=inbound_other_day,
            fourth_question=q_date,
        ),
    ]
    summary = evaluate_consistency(runs, label="synthetic")

    l1 = summary["layer_1_decision"]
    assert l1["cases"] == 4
    assert l1["consistent"] == 3
    assert l1["buckets"]["mixed"] == 1
    assert l1["buckets"]["consistent_and_matches_reference"] == 2
    assert l1["buckets"]["consistent_but_differs_from_reference"] == 1
    assert [m["case_id"] for m in l1["mixed_cases"]] == ["B"]

    l2 = summary["layer_2_search"]
    assert l2["status"] == "computed" and l2["mode"] == "parameters"
    assert l2["searched_in_all_compared_runs"] == 2  # A 和 C；B 有一轮没搜，D 没搜
    assert l2["identical_after_dedup"] == 1  # A
    assert l2["identical_before_dedup"] == 0  # 拆窗记录让 A 去重前不一致
    assert l2["buckets"]["dates_differ"] == 1  # C
    assert l2["buckets"]["searched_in_some_compared_runs_only"] == 1  # B
    assert l2["search_presence_mixed"] == 1

    l4 = summary["layer_4_clarification"]
    assert l4["status"] == "computed"
    assert l4["asked_in_all_compared_runs"] == 4
    assert l4["identical_fact_sets"] == 3
    assert l4["identical_core_fact_sets"] == 4  # D 只差附带事实（住宿）
    assert l4["supplementary_only_disagreements"] == 1
    assert l4["core_disagreeing_cases"] == []
    assert l4["per_fact"]["date"]["agree"] == 4
    assert l4["per_fact"]["lodging"]["agree"] == 3

    assert summary["layer_3_declared_requirements"]["status"] == "not_applicable"
    markdown = render_markdown(summary)
    assert "第 1 层 · 终局决策 | 3/4" in markdown
    assert "consistent_but_differs_from_reference" in markdown


def test_single_turn_runs_fall_back_to_trace_hashes(tmp_path: Path) -> None:
    def run(run_id: str, hashes: list[str], state: str, rejected: int = 0) -> Path:
        path = _write_run(
            tmp_path,
            run_id,
            [
                {
                    "case_id": "X",
                    "state": state,
                    "search_count": len(hashes),
                    "offline_state": "WAITING_FOR_USER",
                    "state_agrees_with_offline": state == "WAITING_FOR_USER",
                }
            ],
        )
        steps = [
            {"name": "tool.search_transport", "status": "success", "input_hash": h} for h in hashes
        ]
        steps += [
            {
                "name": "tool.search_transport",
                "status": "failure",
                "error_type": "ToolInputError",
                "input_hash": f"rejected-{i}",
            }
            for i in range(rejected)
        ]
        trace = {"case_id": "X", "steps": steps}
        (path / "traces.jsonl").write_text(json.dumps(trace) + "\n", encoding="utf-8")
        return path

    runs = [
        run("r1", ["h1", "h1"], "WAITING_FOR_USER", rejected=2),
        run("r2", ["h1"], "WAITING_FOR_USER"),
        run("r3", ["h1"], "WAITING_FOR_USER"),
    ]
    summary = evaluate_consistency(runs)
    assert summary["layer_1_decision"]["decision_field"] == "state"
    assert summary["layer_1_decision"]["buckets"] == {"consistent_and_matches_reference": 1}
    l2 = summary["layer_2_search"]
    assert l2["mode"] == "trace_hash"
    assert l2["identical_after_dedup"] == 1
    assert l2["identical_before_dedup"] == 0
    # 被宿主拒掉的尝试不算搜了，只单独计数。
    assert l2["host_rejected_searches"]["r1"] == {
        "rejected_search_attempts": 2,
        "cases_with_rejected_attempt": 1,
    }
    assert summary["layer_4_clarification"]["status"] == "not_applicable"


def test_parameter_runs_are_preferred_and_hash_only_runs_are_skipped(tmp_path: Path) -> None:
    """多轮集的 r1 只有轨迹哈希、r2/r3 有搜索参数：只比 r2/r3，r1 记为跳过，不把两种签名混比。"""
    leg = _transport(
        "Shanghai",
        "Beijing",
        "2026-09-23T05:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
        "2026-09-23T23:59:00+08:00",
    )
    base = {"case_id": "X", "state": "WAITING_FOR_USER", "outcome": "completed", "search_count": 1}
    r1 = _write_run(tmp_path, "r1", [dict(base)])
    (r1 / "traces.jsonl").write_text(
        json.dumps(
            {
                "case_id": "X",
                "steps": [
                    {"name": "tool.search_transport", "status": "success", "input_hash": "h"}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r2 = _write_run(tmp_path, "r2", [dict(base, searches=[leg])])
    r3 = _write_run(tmp_path, "r3", [dict(base, searches=[leg])])
    l2 = evaluate_consistency([r1, r2, r3])["layer_2_search"]
    assert l2["mode"] == "parameters"
    assert l2["compared_runs"] == ["r2", "r3"]
    assert [s["run_id"] for s in l2["skipped_runs"]] == ["r1"]
    assert l2["identical_after_dedup"] == 1

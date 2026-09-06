"""外部真话长尾集 · 多轮续问：事实表、模拟旅行者、判据、盲评输入（离线 $0）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from corporate_travel_agent.agent.deterministic_tool_model import DeterministicToolCallingModel
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system
from corporate_travel_agent.evaluation.external_longtail import (
    MULTITURN_SCENARIO,
    FactSheet,
    build_fact_sheet,
    classify_outcome,
    follow_up_message,
    judge_input_for_task,
    known_provider_places,
    load_cases,
    multiturn_record,
    searched_dates_match_fact_sheet,
    searched_dates_ok,
    select_multiturn_cases,
    summarize_multiturn,
)
from corporate_travel_agent.evaluation.judge import (
    assert_blinded,
    load_judge_inputs,
    load_output_quality_rubric,
)
from corporate_travel_agent.evaluation.quality import DeterministicUserOutput
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/evaluation/external-longtail-v1"
RUBRIC = ROOT / "evals/rubrics/output-quality-v1.json"


def _normalizer() -> CityNormalizer:
    return CityNormalizer(load_policy_configuration().city_aliases)


def test_multiturn_subset_is_chinatravel_plus_airdialogue_bookings() -> None:
    cases, _ = load_cases(DATASET)
    subset = select_multiturn_cases(cases)
    sources = {case["source"]["dataset"] for case in subset}
    assert sources == {"LAMDA-NeSy/ChinaTravel", "google/air_dialogue"}
    assert len(subset) == 154 + 31
    assert all(
        case["weak_truth"]["source_goal"] == "book"
        for case in subset
        if case["source"]["dataset"] == "google/air_dialogue"
    )


def test_fact_sheet_from_chinatravel_uses_lead_days_and_declared_length() -> None:
    cases, _ = load_cases(DATASET)
    case = next(c for c in cases if c["case_id"] == "ct-human-h20241029143447759844")
    fact = build_fact_sheet(
        case,
        reference_date=date(2026, 9, 1),
        normalizer=_normalizer(),
        known=known_provider_places(),
    )
    assert (fact.origin, fact.destination) == ("上海", "苏州")
    assert fact.depart_date == date(2026, 9, 22)
    assert fact.return_date == date(2026, 9, 24)  # 旅行天数 3
    assert not fact.completable and "苏州" in (fact.reason or "")


def test_fact_sheet_from_airdialogue_rolls_declared_dates_into_the_future() -> None:
    cases, _ = load_cases(DATASET)
    case = next(c for c in cases if c["case_id"] == "ad-val-0000")  # June 12 → June 14
    fact = build_fact_sheet(
        case,
        reference_date=date(2026, 9, 1),
        normalizer=_normalizer(),
        known=known_provider_places(),
    )
    assert (fact.origin, fact.destination) == ("DFW", "IAD")
    assert fact.depart_date == date(2027, 6, 12)
    assert fact.return_date == date(2027, 6, 14)
    assert fact.completable
    # 登记表里没有的机场码（HOU）也算可完成：Duffel 直接接受三字码
    hou = next(c for c in cases if "HOU" in c["weak_truth"]["cities"])
    assert build_fact_sheet(
        hou,
        reference_date=date(2026, 9, 1),
        normalizer=_normalizer(),
        known=known_provider_places(),
    ).completable


def test_follow_up_quotes_absolute_dates_in_the_traveler_language() -> None:
    zh = FactSheet("x", "zh", "南京", "杭州", date(2026, 9, 22), date(2026, 9, 22), True)
    first = follow_up_message(zh, 0)
    assert "2026年9月22日" in first and "当天返回" in first and "不用订酒店" in first
    en = FactSheet("y", "en", "DFW", "IAD", date(2027, 6, 12), date(2027, 6, 14), True)
    first_en = follow_up_message(en, 0)
    assert "June 12, 2027" in first_en and "June 14, 2027" in first_en
    assert follow_up_message(zh, 1) != follow_up_message(zh, 2)


def test_dates_check_allows_evening_before_departure_window_only() -> None:
    fact = FactSheet("x", "zh", "南京", "杭州", date(2026, 9, 22), date(2026, 9, 23), True)
    ok, _ = searched_dates_match_fact_sheet(
        [
            {
                "kind": "transport",
                "parameters": {
                    "depart_after": "2026-09-21T16:00:00+08:00",
                    "arrive_by": "2026-09-22T10:00:00+08:00",
                },
            }
        ],
        fact,
    )
    assert ok
    bad, problems = searched_dates_match_fact_sheet(
        [{"kind": "transport", "parameters": {"arrive_by": "2026-10-01T10:00:00+08:00"}}], fact
    )
    assert not bad and problems


def test_outcome_classification() -> None:
    assert classify_outcome("WAITING_FOR_USER", 3) == "completed"
    assert classify_outcome("WAITING_FOR_USER", 0) == "other"
    assert classify_outcome("PROVIDER_FAILED", 0) == "honest_failure"
    assert classify_outcome("NEEDS_CLARIFICATION", 0) == "stuck_clarifying"
    assert classify_outcome("NEEDS_STRUCTURED_INPUT", 0) == "degraded"
    assert classify_outcome("OUT_OF_SCOPE", 0) == "out_of_scope"


def test_offline_multiturn_holds_red_lines_and_emits_valid_blinded_judge_inputs(
    tmp_path: Path,
) -> None:
    cases, manifest = load_cases(DATASET)
    normalizer = _normalizer()
    known = known_provider_places()
    subset = [
        c for c in select_multiturn_cases(cases) if c["source"]["dataset"].endswith("ChinaTravel")
    ]
    subset = [c for c in subset if "苏州" not in c["weak_truth"]["cities"]][:6]
    assert len(subset) == 6
    workflow, _ = build_demo_system(
        tool_calling_language_model=DeterministicToolCallingModel(), clock=lambda: DEMO_CLOCK
    )
    results = []
    judge_inputs = []
    for index, case in enumerate(subset):
        fact = build_fact_sheet(
            case, reference_date=DEMO_CLOCK.date(), normalizer=normalizer, known=known
        )
        task = workflow.create_task_from_agentic_message(
            case["message"], traveler_id="E1001", task_id=f"mt-test-{index}"
        )
        turns = [{"turn": 0, "said": case["message"], "state": task.state.value}]
        for round_index in range(3):
            if task.state.value != "NEEDS_CLARIFICATION":
                break
            said = follow_up_message(fact, round_index)
            task = workflow.submit_agentic_message(task.task_id, said)
            turns.append({"turn": round_index + 1, "said": said, "state": task.state.value})
        record = multiturn_record(workflow, task, case, fact, normalizer, turns=turns)
        assert record["passed"], json.dumps(record["checks"], ensure_ascii=False)
        assert record["outcome"] in {
            "completed",
            "honest_failure",
            "stuck_clarifying",
            "degraded",
            "out_of_scope",
        }
        results.append(record)
        judge_inputs.append(
            judge_input_for_task(
                task, case, fact, run_id=f"offline-{case['case_id']}", inventory_source="MOCK"
            )
        )
    summary = summarize_multiturn(
        results, manifest, mode="offline", reference_date=DEMO_CLOCK.date(), max_follow_ups=3
    )
    assert summary["total"] == 6 and summary["red_line_passed"] == 6
    assert summary["completable"] == 6

    path = tmp_path / "judge-inputs.jsonl"
    path.write_text("".join(f"{item.model_dump_json()}\n" for item in judge_inputs))
    loaded = load_judge_inputs(path)
    rubric = load_output_quality_rubric(RUBRIC)
    assert len(loaded) == 6
    for item in loaded:
        assert_blinded(item, rubric)
        assert item.scenario == MULTITURN_SCENARIO
        assert len(item.user_visible_output.traveler_messages) >= 1
        assert item.expected_constraints["fact_sheet"]["origin"]


def test_user_output_without_conversation_fields_still_loads() -> None:
    """旧的 judge-inputs.jsonl 不带新字段，必须照常解析。"""
    output = DeterministicUserOutput(
        state="NEEDS_CLARIFICATION",
        summary_code="CLARIFICATION_REQUIRED",
        next_action="ANSWER_CLARIFICATION",
        failure_details=(),
        policy_outcome=None,
        evidence_refs=(),
        inventory_source="MOCK",
        options=(),
        selected_option_id=None,
        booking_handoff_available=False,
        booking_boundary_notice="NO_AUTONOMOUS_BOOKING_OR_PAYMENT",
        claims=(),
    )
    assert output.traveler_messages == () and output.assistant_reply is None


def test_annotation_packet_is_stratified_blinded_and_identical_across_annotators(
    tmp_path: Path, monkeypatch
) -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "build_multiturn_annotation_packet",
        ROOT / "examples/build_multiturn_annotation_packet.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    # 六条不同终态的合成 judge 输入：办成 ×3、追问 ×2、越界 ×1
    inputs = []
    for index, state in enumerate(
        ["WAITING_FOR_USER"] * 3 + ["NEEDS_CLARIFICATION"] * 2 + ["OUT_OF_SCOPE"]
    ):
        inputs.append(
            {
                "case_id": f"case-{index}",
                "run_id": f"run-{index}",
                "scenario": MULTITURN_SCENARIO,
                "expected_constraints": {"fact_sheet": {"origin": "a"}},
                "trace_evidence_refs": [],
                "user_visible_output": {"state": state, "traveler_messages": ["hi"]},
            }
        )
    run_dir = tmp_path / "external-longtail-multiturn-live-x"
    run_dir.mkdir()
    (run_dir / "judge-inputs.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in inputs), encoding="utf-8"
    )
    picked = module.stratified_sample(inputs, sample=4, salt="s")
    assert len(picked) == 4
    assert {item["user_visible_output"]["state"] for item in picked} == {
        "WAITING_FOR_USER",
        "NEEDS_CLARIFICATION",
        "OUT_OF_SCOPE",
    }
    assert picked == module.stratified_sample(inputs, sample=4, salt="s")

    out = tmp_path / "packet"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "x",
            "--run-directory",
            str(run_dir),
            "--output",
            str(out),
            "--sample",
            "4",
            "--annotators",
            "human-01,human-02",
        ],
    )
    assert module.main() == 0
    a = module.load_jsonl(out / "to-label/output-quality-human-01.jsonl")
    b = module.load_jsonl(out / "to-label/output-quality-human-02.jsonl")
    assert len(a) == len(b) == 4
    for left, right in zip(a, b, strict=True):
        assert left["input"] == right["input"] and left["annotation_id"] == right["annotation_id"]
        assert left["annotation_meta"]["annotator_id"] == "human-01"
        assert right["annotation_meta"]["annotator_id"] == "human-02"
        assert all(value is None for value in left["annotation"]["ratings"].values())
        assert left["input"]["candidate_identity_blinded"] is True
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["sampled"] == 4 and manifest["annotators"] == ["human-01", "human-02"]


def test_date_gates_ignore_the_lookback_window_but_catch_real_strays() -> None:
    """跨日窗口拆出的前一天记录不是"搜了别的日子"；真搜了别的日子仍要拦。"""
    fact = FactSheet("x", "en", "PHX", "PHL", date(2026, 9, 10), date(2026, 9, 12), True)
    truth = {
        "cities": ["PHX", "PHL"],
        "dates_declared": [{"month": "Sep", "day": "10"}, {"month": "Sep", "day": "12"}],
    }
    lookback_part = {
        "kind": "transport",
        "parameters": {
            "depart_after": "2026-09-11T06:00:00-04:00",
            "arrive_by": "2026-09-11T23:59:59.999999-04:00",
            "requested_window_arrive_by": "2026-09-12T00:00:00-04:00",
        },
        "date_evidence": "September 12, 2026",
    }
    same_day_part = {
        "kind": "transport",
        "parameters": {
            "depart_after": "2026-09-12T00:00:00-04:00",
            "arrive_by": "2026-09-12T00:00:00-04:00",
            "requested_window_arrive_by": "2026-09-12T00:00:00-04:00",
        },
        "date_evidence": "September 12, 2026",
    }
    ok_fact, _ = searched_dates_match_fact_sheet([lookback_part, same_day_part], fact)
    ok_decl, _ = searched_dates_ok([lookback_part, same_day_part], truth)
    assert ok_fact and ok_decl
    stray = {
        "kind": "transport",
        "parameters": {
            "depart_after": "2026-09-14T00:00:00-04:00",
            "arrive_by": "2026-09-14T23:59:59-04:00",
            "requested_window_arrive_by": "2026-09-14T23:59:59-04:00",
        },
        "date_evidence": "September 12, 2026",
    }
    bad_fact, problems = searched_dates_match_fact_sheet([stray], fact)
    bad_decl, decl_problems = searched_dates_ok([stray], truth)
    assert not bad_fact and problems and not bad_decl and decl_problems


# ---------------------------------------------------------------------------
# 第 3 层的数据：交付时声明的硬要求与偏好、终局动作，落在每条记录和每一轮里。
# ---------------------------------------------------------------------------


def _synthetic_case(message: str) -> dict:
    return {
        "case_id": "synthetic-declared",
        "language": "zh",
        "message": message,
        "source": {"dataset": "synthetic"},
        "weak_truth": {"cities": ["北京", "上海"], "dates_declared": []},
    }


def test_case_record_keeps_declared_requirements_and_loop_decisions() -> None:
    """此前报告只存终态和搜索参数，"必须高铁"有没有被声明成 train_only 一个字都看不到。"""
    from corporate_travel_agent.evaluation.external_longtail import (
        case_record,
        loop_decision_snapshot,
        turn_snapshot,
    )
    from tests.test_agentic_entrypoint import ScriptedToolModel

    search = (
        "search_transport",
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5日上午10点前到",
        },
    )
    propose = (
        "propose_options",
        {
            "transport_refs": "__FOUND__",
            "summary": "只看高铁",
            "hard_constraints": ["train_only"],
            "soft_preferences": ["prefer_train"],
        },
    )
    workflow, _ = build_demo_system(
        tool_calling_language_model=ScriptedToolModel([search, propose]),
        clock=lambda: DEMO_CLOCK,
    )
    message = "8月5号从北京去上海，8月5日上午10点前到，只坐高铁，不住酒店"
    task = workflow.create_task_from_agentic_message(message, traveler_id="E1001")

    loop = loop_decision_snapshot(task)
    assert loop["final_action"] == "propose_options"
    assert loop["declared"] is True
    assert loop["hard_constraints"] == ["train_only"]
    assert loop["soft_preferences"] == ["prefer_train"]
    assert loop["tool_calls"] == ["search_transport"]  # 出口工具不进 transcript
    assert loop["rejected_tool_calls"] == []

    record = case_record(workflow, task, _synthetic_case(message), _normalizer())
    assert record["declared_requirements"] == {
        "declared": True,
        "hard": ["train_only"],
        "soft": ["prefer_train"],
    }
    assert record["loop"]["final_action"] == "propose_options"
    assert turn_snapshot(0, message, task)["loop"]["hard_constraints"] == ["train_only"]


def test_a_turn_that_only_asks_records_no_declaration() -> None:
    from corporate_travel_agent.evaluation.external_longtail import turn_snapshot
    from tests.test_agentic_entrypoint import ScriptedToolModel

    workflow, _ = build_demo_system(
        tool_calling_language_model=ScriptedToolModel(
            [("ask_traveler", {"question": "请问哪天出发？"})]
        ),
        clock=lambda: DEMO_CLOCK,
    )
    task = workflow.create_task_from_agentic_message("我想去上海出差", traveler_id="E1001")
    loop = turn_snapshot(0, "我想去上海出差", task)["loop"]
    assert loop["final_action"] == "ask_traveler"
    assert loop["declared"] is False
    assert loop["hard_constraints"] == [] and loop["open_questions"] == []
    assert loop["tool_calls"] == []

"""外部真话长尾集：数据完整性 + 抽样红线门禁（每个来源各 10 条，离线 $0）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from corporate_travel_agent.services.evaluation_external_longtail import (
    load_cases,
    place_grounded,
    probe_cases,
    searched_dates_ok,
)
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

DATASET = Path(__file__).resolve().parents[1] / "data/evaluation/external-longtail-v1"


def test_dataset_is_frozen_and_attributed() -> None:
    cases, manifest = load_cases(DATASET)
    assert manifest["records"] == len(cases) == 300
    assert manifest["dataset_version"] == "2"
    assert manifest["by_source"] == {
        "LAMDA-NeSy/ChinaTravel": 154,
        "thu-coai/CrossWOZ": 100,
        "google/air_dialogue": 46,
    }
    raw = (DATASET / "cases.jsonl").read_text(encoding="utf-8")
    assert hashlib.sha256(raw.encode()).hexdigest() == manifest["cases_sha256"]
    for case in cases:
        assert case["probe"] == "host_invariants_only"
        assert case["source"]["license"] in {"CC BY-NC-SA 4.0", "Apache-2.0"}
        assert case["message"].strip()
        # v2：每条都带弱真值——来源对输入本身的结构化描述，不是答案
        assert case["weak_truth"].get("cities")
    assert "CC BY-NC-SA" in (DATASET / "NOTICE.md").read_text(encoding="utf-8")


def test_sampled_cases_hold_the_host_invariants() -> None:
    cases, _ = load_cases(DATASET)
    by_source: dict[str, list[dict]] = {}
    for case in cases:
        by_source.setdefault(case["source"]["dataset"], []).append(case)
    sample = [case for group in by_source.values() for case in group[:10]]
    assert len(sample) == 30
    results = probe_cases(sample)
    failed = {
        item["case_id"]: item.get("crashed") or item.get("checks")
        for item in results
        if not item.get("passed")
    }
    assert not failed, json.dumps(failed, ensure_ascii=False)[:500]


def test_weak_truth_checks_actually_catch_violations() -> None:
    """门禁不能只在"没搜索"时空转——用合成违例证明它真的会拦。"""
    normalizer = CityNormalizer(load_policy_configuration().city_aliases)
    truth_zh = {"cities": ["上海", "苏州"], "dates_declared": []}
    message = "我要带着我大儿子从上海出发去苏州玩三天"
    # 声明城市（正规化后等价）→ 有出处
    assert place_grounded("Shanghai", truth_zh, message, normalizer)
    assert place_grounded("苏州", truth_zh, message, normalizer)
    # 没人提过的城市 → 编的，必须拦
    assert not place_grounded("Chengdu", truth_zh, message, normalizer)
    # 原话里出现的站点名（不在别名表里）→ 有出处
    assert place_grounded("虹桥火车站", truth_zh, "从虹桥火车站出发去苏州", normalizer)

    truth_en = {
        "cities": ["DEN", "LGA"],
        "dates_declared": [{"month": "Oct", "day": "5"}, {"month": "Oct", "day": "7"}],
    }
    ok, _ = searched_dates_ok(
        [{"kind": "transport", "parameters": {"depart_after": "2026-10-05T06:00:00"},
          "date_evidence": "Oct 5"}],
        truth_en,
    )
    assert ok
    bad, problems = searched_dates_ok(
        [{"kind": "transport", "parameters": {"arrive_by": "2026-07-01T10:00:00"},
          "date_evidence": "made up"}],
        truth_en,
    )
    assert not bad and problems
    # 来源没声明日期：交通搜索必须带原话出处
    no_evidence, problems2 = searched_dates_ok(
        [{"kind": "transport", "parameters": {"arrive_by": "2026-09-15T10:00:00"},
          "date_evidence": None}],
        truth_zh,
    )
    assert not no_evidence and "没有原话出处" in problems2[0]

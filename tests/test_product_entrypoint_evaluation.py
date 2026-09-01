"""D16：冻结评测集经**产品入口**（工具循环）执行——CI 从守废弃入口改成守产品入口。

此前 D1 60 条工作流和 D2 480 条意图只经 legacy / semantic 两条入口跑；前端和员工
走的是 `/agentic/trip-tasks`，那条路在 CI 里一条离线用例都没有。这里用
`DeterministicToolCallingModel` 把同一份冻结集推过产品入口，并和语义入口并排：
两个替身共用同一个解释器，差异只能来自架构。
"""

from __future__ import annotations

import pytest

from corporate_travel_agent.agent.deterministic_tool_model import (
    DeterministicToolCallingModel,
)
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.evaluation_dataset import (
    EvaluationDatasetError,
    load_evaluation_dataset,
    run_intent_evaluation,
    run_workflow_evaluation_case,
    summarize_intent_observations,
)

DATASET = load_evaluation_dataset("data/evaluation/derived-v2")

#: 语义入口删除前的最后一次并排（2026-09-01，两个替身共用一个解释器）。
#: 480 条里 345 条"该问就问、该查就查"判对；产品入口不许低于它。
SEMANTIC_BASELINE_CLARIFICATION_ACCURACY = 345 / 480
SEMANTIC_BASELINE_REPORT = "reports/evaluation-runs/product-entrypoint-20260901/run-summary.json"


@pytest.mark.parametrize("case", DATASET.workflow_cases, ids=lambda item: item.case_id)
def test_every_workflow_case_matches_expected_state_through_the_product_entrypoint(case) -> None:
    observation = run_workflow_evaluation_case(
        case, tool_calling_language_model=DeterministicToolCallingModel()
    )
    expected = case.expected

    assert observation.task.metadata["intent_entrypoint"] == "agentic"
    assert observation.after_create_state is expected.after_create_state
    assert observation.after_selection_state is expected.after_selection_state
    assert observation.selected_policy_outcome is expected.selected_policy_outcome
    assert observation.booking_intent_created is expected.booking_intent_expected
    if expected.selected_inventory_ref is not None:
        assert expected.selected_inventory_ref in observation.selected_inventory_refs


def test_intent_cases_never_search_on_unresolved_meaning() -> None:
    metrics = run_intent_evaluation(DATASET.intent_cases[:60], entrypoint="agentic")

    assert metrics.entrypoint == "agentic"
    assert metrics.premature_provider_call_rate == 0.0
    assert metrics.inventory_hallucination_rate == 0.0


def test_the_full_intent_suite_is_as_safe_and_as_decisive_as_the_last_semantic_baseline() -> None:
    """安全门必须是 0；"该问就问"的判断率不许低于语义入口删除前的最后一次并排。"""
    import json
    from pathlib import Path

    agentic = run_intent_evaluation(DATASET.intent_cases, entrypoint="agentic")

    assert agentic.total_cases == 480
    assert agentic.premature_provider_call_rate == 0.0
    assert agentic.inventory_hallucination_rate == 0.0
    assert agentic.clarification_accuracy >= SEMANTIC_BASELINE_CLARIFICATION_ACCURACY
    # 基线数字来自仓库里留档的那份报告，不是凭记忆写的。
    report = json.loads(Path(SEMANTIC_BASELINE_REPORT).read_text(encoding="utf-8"))
    assert report["intent_metrics_semantic"]["clarification_accuracy"] == (
        SEMANTIC_BASELINE_CLARIFICATION_ACCURACY
    )


def test_the_old_entrypoints_are_gone_from_the_runner() -> None:
    with pytest.raises(EvaluationDatasetError):
        run_intent_evaluation(DATASET.intent_cases[:1], entrypoint="semantic")
    with pytest.raises(EvaluationDatasetError):
        run_intent_evaluation(DATASET.intent_cases[:1], entrypoint="legacy")


def test_out_of_scope_requests_land_in_out_of_scope_without_a_provider_call() -> None:
    out_of_scope = tuple(
        case
        for case in DATASET.intent_cases
        if case.expected.classification == "OUT_OF_SCOPE"
    )
    metrics = run_intent_evaluation(out_of_scope, entrypoint="agentic")

    for observation in metrics.observations:
        assert observation.provider_calls_before_clarification == 0
        assert not observation.inventory_hallucinated
    # 解释器判得出越界的那些，产品入口落在 OUT_OF_SCOPE，不再烧一轮澄清。
    states = {item.final_state for item in metrics.observations}
    assert TaskState.OUT_OF_SCOPE.value in states
    assert all(
        (item.final_state == TaskState.OUT_OF_SCOPE.value) == (not item.clarification_observed)
        for item in metrics.observations
    )


def test_metrics_without_an_exposure_are_not_applicable_not_zero() -> None:
    agentic = run_intent_evaluation(DATASET.intent_cases[:20], entrypoint="agentic")

    assert agentic.classification_status == "not_applicable"
    assert agentic.missing_field_status == "not_applicable"
    assert agentic.missing_field_exact_match_rate is None
    assert agentic.missing_field_recall is None
    assert agentic.out_of_scope_accuracy is None


def test_observations_from_different_entrypoints_are_never_merged() -> None:
    from dataclasses import replace

    agentic = run_intent_evaluation(DATASET.intent_cases[:5], entrypoint="agentic")
    foreign = tuple(replace(item, entrypoint="structured") for item in agentic.observations)
    with pytest.raises(EvaluationDatasetError):
        summarize_intent_observations((*agentic.observations, *foreign))

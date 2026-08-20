from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from corporate_travel_agent.agent.deterministic_parser import (
    DeterministicChineseIntentParser,
)
from corporate_travel_agent.agent.ports import IntentExtractionResult, LLMCallMetadata
from corporate_travel_agent.services.evaluation_model_preflight import (
    run_real_model_preflight,
)
from corporate_travel_agent.services.evaluation_model_runner import (
    load_intent_model_preflight_subset,
    load_intent_model_subset,
    run_model_intent_stability_evaluation,
)
from corporate_travel_agent.services.evaluation_performance import (
    ModelPriceTable,
    PerformanceEvaluationError,
    evaluate_performance_and_stability,
)
from corporate_travel_agent.services.evaluation_quality import EvaluationResult
from corporate_travel_agent.services.evaluation_runner import (
    EvaluationRunnerError,
    run_deterministic_workflow_evaluation,
)

DATASET_ROOT = Path(__file__).parents[1] / "data" / "evaluation" / "derived-v2"
PRICE_TABLE = Path(__file__).parents[1] / "evals" / "pricing" / "model-prices-unconfigured-v1.json"
MODEL_SUBSET = Path(__file__).parents[1] / "evals" / "subsets" / "intent-model-smoke-v1.json"
MODEL_PREFLIGHT_SUBSET = (
    Path(__file__).parents[1] / "evals" / "subsets" / "intent-model-preflight-v1.json"
)


class TokenizedDeterministicParser(DeterministicChineseIntentParser):
    model = "priced-test-model"

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        result = super().extract_trip_intent(
            message,
            task_id=task_id,
            traveler_id=traveler_id,
            context=context,
        )
        return IntentExtractionResult(
            payload=result.payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model=self.model,
                duration_ms=2,
                input_tokens=100,
                output_tokens=50,
            ),
        )


def test_three_attempt_mock_run_reports_stability_and_unknown_cost(tmp_path: Path) -> None:
    source = tmp_path / "source"
    selected = ("prefer-preference-conflict-0012", "prefer-compliant-0035")
    run_deterministic_workflow_evaluation(
        DATASET_ROOT,
        source,
        attempts_per_case=3,
        case_ids=selected,
    )

    output = tmp_path / "analysis"
    summary = evaluate_performance_and_stability(
        source_run_directory=source,
        output_directory=output,
        price_table_path=PRICE_TABLE,
    )

    assert summary.completed_runs == 6
    assert summary.pass_at_1 == 1.0
    assert summary.pass_power_3 == 1.0
    assert summary.pass_at_3 == 1.0
    assert summary.mixed_run_rate == 0.0
    assert summary.output_consistency_rate == 1.0
    assert summary.trajectory_consistency_rate == 1.0
    assert summary.deterministic_stability_gate == "pass"
    assert summary.real_model_stability_gate == "not_evaluated"
    assert summary.input_tokens_status == "not_applicable"
    assert summary.cost_status == "not_applicable"
    assert summary.real_model_calls == 0
    assert summary.total_external_calls > 0
    assert (output / "evaluation-report.md").is_file()

    result = EvaluationResult.model_validate_json(
        (output / "performance-stability-evaluation-result.json").read_text()
    )
    assert result.metrics["input_tokens_total"].value is None
    assert result.metrics["input_tokens_total"].status == "not_applicable"
    assert result.metrics["estimated_cost_total"].value is None
    assert result.cost is not None
    assert result.cost.total is None


def test_attempt_matrix_must_have_exactly_three_runs_per_case(tmp_path: Path) -> None:
    source = tmp_path / "source"
    run_deterministic_workflow_evaluation(
        DATASET_ROOT,
        source,
        attempts_per_case=2,
        case_ids=("prefer-compliant-0035",),
    )

    with pytest.raises(PerformanceEvaluationError, match="expected 3 attempts"):
        evaluate_performance_and_stability(
            source_run_directory=source,
            output_directory=tmp_path / "analysis",
            expected_attempts=3,
        )


def test_price_table_cannot_hide_unknown_prices_as_zero() -> None:
    payload = json.loads(PRICE_TABLE.read_text())
    assert payload["models"] == {}
    table = ModelPriceTable.model_validate(payload)
    assert table.status == "unconfigured"

    payload["status"] = "configured"
    with pytest.raises(ValidationError, match="must contain at least one model"):
        ModelPriceTable.model_validate(payload)


def test_real_model_runner_runs_24_cases_times_three_and_counts_repairs(
    tmp_path: Path,
) -> None:
    subset, cases, subset_sha256 = load_intent_model_subset(
        MODEL_SUBSET,
        dataset_directory=DATASET_ROOT,
    )
    assert subset.records == 24
    assert len(cases) == 24
    assert subset_sha256

    source = tmp_path / "model-source"
    summary = run_model_intent_stability_evaluation(
        dataset_directory=DATASET_ROOT,
        subset_path=MODEL_SUBSET,
        output_directory=source,
        language_model=DeterministicChineseIntentParser(),
    )
    assert summary.selected_cases == 24
    assert summary.attempts_per_case == 3
    assert summary.completed_runs == 72
    # The parameter loop performs 18 bounded repair calls for this deterministic
    # fixture in addition to the 72 initial extraction calls.
    assert summary.real_model_calls == 90
    assert summary.evaluation_mode == "model_mock"

    analysis = evaluate_performance_and_stability(
        source_run_directory=source,
        output_directory=tmp_path / "model-analysis",
        price_table_path=PRICE_TABLE,
    )
    assert analysis.real_model_calls == 90
    assert analysis.input_tokens_status == "unavailable"
    assert analysis.cost_status == "unavailable"


def test_all_model_attempt_tokens_and_prices_are_counted(tmp_path: Path) -> None:
    source = tmp_path / "model-source"
    run_model_intent_stability_evaluation(
        dataset_directory=DATASET_ROOT,
        subset_path=MODEL_SUBSET,
        output_directory=source,
        language_model=TokenizedDeterministicParser(),
        price_table_version="test-prices-v1",
    )
    configured_price_table = tmp_path / "prices.json"
    configured_price_table.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "price_table_version": "test-prices-v1",
                "status": "configured",
                "currency": "USD",
                "unit": "per_1m_tokens",
                "effective_at": "2026-08-02",
                "source": "Synthetic unit-test prices only.",
                "models": {"priced-test-model": {"input": 1.0, "output": 2.0}},
            }
        )
    )
    output = tmp_path / "model-analysis"
    summary = evaluate_performance_and_stability(
        source_run_directory=source,
        output_directory=output,
        price_table_path=configured_price_table,
    )
    result = EvaluationResult.model_validate_json(
        (output / "performance-stability-evaluation-result.json").read_text()
    )

    assert summary.input_tokens_status == "measured"
    assert summary.cost_status == "measured"
    assert result.metrics["input_tokens_total"].value == 9000
    assert result.metrics["output_tokens_total"].value == 4500
    assert result.metrics["estimated_cost_total"].value == pytest.approx(0.018)


def test_real_model_preflight_is_fixed_to_two_calls_and_reports_contract(
    tmp_path: Path,
) -> None:
    subset, cases, subset_sha256 = load_intent_model_preflight_subset(
        MODEL_PREFLIGHT_SUBSET,
        dataset_directory=DATASET_ROOT,
        parent_subset_path=MODEL_SUBSET,
    )
    assert subset.records == 2
    assert tuple(case.case_id for case in cases) == (
        "prefer-intent-0001",
        "open-train-missing-fields-0085",
    )
    assert subset_sha256

    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "price_table_version": "preflight-test-prices-v1",
                "status": "configured",
                "currency": "USD",
                "unit": "per_1m_tokens",
                "effective_at": "2026-08-02",
                "source": "Synthetic unit-test prices only.",
                "models": {"priced-test-model": {"input": 1.0, "output": 2.0}},
            }
        )
    )
    output = tmp_path / "preflight"
    result = run_real_model_preflight(
        dataset_directory=DATASET_ROOT,
        subset_path=MODEL_PREFLIGHT_SUBSET,
        parent_subset_path=MODEL_SUBSET,
        output_directory=output,
        price_table_path=prices,
        language_model=TokenizedDeterministicParser(),
    )

    assert result.status == "pass"
    assert result.attempted_model_calls == 2
    assert result.successful_structured_output_calls == 2
    assert result.provider_calls == 0
    assert result.input_tokens_total == 200
    assert result.output_tokens_total == 100
    assert result.estimated_cost_total == pytest.approx(0.0004)
    assert result.contract_failures == ()
    assert (output / "preflight-result.json").is_file()
    assert (output / "evaluation-report.md").is_file()


def test_preflight_rejects_unconfigured_prices_before_creating_output(
    tmp_path: Path,
) -> None:
    with pytest.raises(EvaluationRunnerError):
        run_real_model_preflight(
            dataset_directory=DATASET_ROOT,
            subset_path=MODEL_PREFLIGHT_SUBSET,
            parent_subset_path=MODEL_SUBSET,
            output_directory=tmp_path / "preflight",
            price_table_path=PRICE_TABLE,
            language_model=TokenizedDeterministicParser(),
        )
    assert not (tmp_path / "preflight").exists()

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict

from corporate_travel_agent.evaluation.dataset import (
    EvaluationDatasetError,
    load_evaluation_dataset,
    run_intent_evaluation,
    run_workflow_evaluation_case,
    summarize_intent_observations,
)


def _metric_values(metrics) -> dict:
    values = asdict(metrics)
    values.pop("observations")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the derived evaluation dataset and execute every offline workflow case."
        )
    )
    parser.add_argument(
        "dataset_directory",
        nargs="?",
        default="data/evaluation/derived-v2",
    )
    parser.add_argument(
        "--intent-runner",
        choices=("deterministic", "openai"),
        default="deterministic",
        help="Run intent cases with the offline parser or configured OpenAI model",
    )
    args = parser.parse_args()

    dataset = load_evaluation_dataset(args.dataset_directory)
    passed = 0
    for case in dataset.workflow_cases:
        observation = run_workflow_evaluation_case(case)
        expected = case.expected
        mismatches: list[str] = []
        if observation.after_create_state is not expected.after_create_state:
            mismatches.append("after_create_state")
        if observation.after_selection_state is not expected.after_selection_state:
            mismatches.append("after_selection_state")
        if observation.selected_policy_outcome is not expected.selected_policy_outcome:
            mismatches.append("selected_policy_outcome")
        if observation.booking_intent_created is not expected.booking_intent_expected:
            mismatches.append("booking_intent_expected")
        if (
            expected.selected_inventory_ref is not None
            and expected.selected_inventory_ref not in observation.selected_inventory_refs
        ):
            mismatches.append("selected_inventory_ref")
        if mismatches:
            raise EvaluationDatasetError(
                f"Case {case.case_id} failed: {', '.join(mismatches)}"
            )
        passed += 1

    source_counts = Counter(
        item.source.dataset_id
        for item in (*dataset.workflow_cases, *dataset.intent_cases)
    )
    language_model = None
    if args.intent_runner == "openai":
        from corporate_travel_agent.agent.openai_adapter import (
            OpenAIResponsesLanguageModel,
        )

        language_model = OpenAIResponsesLanguageModel()
    intent_metrics = run_intent_evaluation(
        dataset.intent_cases,
        language_model=language_model,
    )
    intent_safety_gate_met = not (
        intent_metrics.premature_provider_call_rate > 0
        or intent_metrics.inventory_hallucination_rate > 0
    )
    if args.intent_runner == "deterministic" and not intent_safety_gate_met:
        raise EvaluationDatasetError("Intent evaluation safety thresholds were not met")
    case_by_id = {case.case_id: case for case in dataset.intent_cases}

    def sliced_metrics(attribute: str) -> dict[str, dict]:
        labels = sorted({getattr(case, attribute) for case in dataset.intent_cases})
        return {
            label: _metric_values(
                summarize_intent_observations(
                    tuple(
                        item
                        for item in intent_metrics.observations
                        if getattr(case_by_id[item.case_id], attribute) == label
                    )
                )
            )
            for label in labels
        }

    source_metrics = {
        source: _metric_values(
            summarize_intent_observations(
                tuple(
                    item
                    for item in intent_metrics.observations
                    if case_by_id[item.case_id].source.dataset_id == source
                )
            )
        )
        for source in sorted(
            {case.source.dataset_id for case in dataset.intent_cases}
        )
    }
    error_examples = {
        "misclassified": [
            item.case_id
            for item in intent_metrics.observations
            if item.actual_classification != item.expected_classification
        ][:20],
        "missing_field_mismatch": [
            item.case_id
            for item in intent_metrics.observations
            if item.expected_missing_fields is not None
            and set(item.actual_missing_fields) != set(item.expected_missing_fields)
        ][:20],
        "unsupported_constraint_silently_ignored": [
            item.case_id
            for item in intent_metrics.observations
            if item.unsupported_constraint_rejection_expected
            and not item.unsupported_constraints_rejected
        ][:20],
    }
    print(
        json.dumps(
            {
                "valid": True,
                "dataset_id": dataset.manifest.dataset_id,
                "manifest_sha256": dataset.manifest_sha256,
                "total_cases": dataset.total_cases,
                "workflow_cases_executed": len(dataset.workflow_cases),
                "workflow_cases_passed": passed,
                "intent_cases_executed": len(dataset.intent_cases),
                "intent_runner": args.intent_runner,
                "intent_safety_gate_met": intent_safety_gate_met,
                "intent_metrics": _metric_values(intent_metrics),
                "intent_metrics_by_cohort": sliced_metrics("cohort"),
                "intent_metrics_by_scenario": sliced_metrics("scenario"),
                "intent_metrics_by_source": source_metrics,
                "intent_error_examples": error_examples,
                "workflow_scenarios": dataset.manifest.workflow_scenarios,
                "intent_scenarios": dataset.manifest.intent_scenarios,
                "intent_cohorts": dataset.manifest.intent_cohorts,
                "intent_languages": dataset.manifest.intent_languages,
                "source_datasets": dict(sorted(source_counts.items())),
                "real_provider_snapshots": dataset.manifest.real_provider_snapshots,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

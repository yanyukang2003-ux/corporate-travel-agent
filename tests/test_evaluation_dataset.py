from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from corporate_travel_agent.services.evaluation_dataset import (
    EvaluationDatasetError,
    load_evaluation_dataset,
    run_workflow_evaluation_case,
)

DATASET_ROOT = Path(__file__).parents[1] / "data" / "evaluation" / "derived-v2"
DATASET = load_evaluation_dataset(DATASET_ROOT)


def test_derived_dataset_has_complete_cross_dataset_intent_coverage() -> None:
    assert len(DATASET.workflow_cases) == 60
    assert len(DATASET.intent_cases) == 480
    assert DATASET.total_cases == 540
    assert DATASET.manifest.workflow_scenarios == {
        "COMPLIANT": 20,
        "NO_FEASIBLE_OPTION": 8,
        "PREFERENCE_CONFLICT": 10,
        "PROVIDER_FAILURE": 6,
        "REQUIRES_APPROVAL": 10,
        "REVALIDATION_CHANGED": 6,
    }
    assert DATASET.manifest.intent_scenarios == {
        "LOCAL_SEARCH": 50,
        "MISSING_FIELDS": 5,
        "MULTI_DAY_TRIP": 50,
        "ONE_DAY_ITINERARY": 50,
        "PREFERENCE_RICH_TRIP": 225,
        "ROUTE_PLANNING": 50,
        "TRANSPORT_COMPARE": 50,
    }
    assert DATASET.manifest.intent_cohorts == {
        "CURATED_FULL_SPLIT": 450,
        "LEGACY_TARGETED": 30,
    }
    assert DATASET.manifest.intent_languages == {"en": 225, "zh": 255}


def test_derived_dataset_never_claims_real_provider_coverage() -> None:
    assert DATASET.manifest.classification == "SYNTHETIC_DERIVED"
    assert DATASET.manifest.real_provider_snapshots == 0
    assert all(case.inventory.source_type == "MOCK" for case in DATASET.workflow_cases)


def test_source_provenance_and_licenses_are_pinned() -> None:
    sources = {item.dataset_id: item for item in DATASET.manifest.sources}
    assert sources["UKPLab/PreferTripPlan"].revision == (
        "cdc7e11d5d2a0fd2cd53f03973819e469bc004b9"
    )
    assert sources["Alibaba-NLP/Open-Travel"].revision == (
        "b997227fb474eb578b7b951694fd4a6bf751bafa"
    )
    assert "upstream TravelPlanner terms" in sources["UKPLab/PreferTripPlan"].license
    assert sources["Alibaba-NLP/Open-Travel"].license == "CC-BY-NC-4.0"
    source_records = [
        (case.source.dataset_id, case.source.source_record_id)
        for case in (*DATASET.workflow_cases, *DATASET.intent_cases)
    ]
    assert len(source_records) == len(set(source_records))


@pytest.mark.parametrize("case", DATASET.workflow_cases, ids=lambda item: item.case_id)
def test_every_derived_workflow_case_matches_expected_state(case) -> None:
    observation = run_workflow_evaluation_case(case)

    assert observation.after_create_state is case.expected.after_create_state
    assert observation.after_selection_state is case.expected.after_selection_state
    assert observation.selected_policy_outcome is case.expected.selected_policy_outcome
    assert observation.booking_intent_created is case.expected.booking_intent_expected
    if case.expected.selected_inventory_ref is not None:
        assert case.expected.selected_inventory_ref in observation.selected_inventory_refs


def test_preference_conflicts_retain_source_evidence_and_select_lowest_cost() -> None:
    cases = [case for case in DATASET.workflow_cases if case.scenario == "PREFERENCE_CONFLICT"]
    assert len(cases) == 10
    assert all(case.source.profile_drift in {"omission", "inversion"} for case in cases)
    assert all(case.source.original_profile for case in cases)
    assert all(case.source.source_preferences for case in cases)
    assert all(case.request.soft_preferences == ("lowest_cost",) for case in cases)
    assert all(case.expected.explicit_request_overrides_profile for case in cases)
    assert all("HT-LOWEST-COST" in case.expected.selected_inventory_ref for case in cases)


def test_intent_cases_preserve_queries_and_use_dimension_specific_gold() -> None:
    counts = Counter(case.source.dataset_id for case in DATASET.intent_cases)
    assert counts == {
        "Alibaba-NLP/Open-Travel": 255,
        "UKPLab/PreferTripPlan": 225,
    }
    assert all(case.expected.must_not_invent_inventory for case in DATASET.intent_cases)
    assert all(
        case.message == case.source.original_query for case in DATASET.intent_cases
    )
    assert sum(
        case.expected.missing_fields is not None for case in DATASET.intent_cases
    ) == 250
    assert sum(
        case.expected.expected_transport_preferences is not None
        for case in DATASET.intent_cases
    ) == 27
    assert sum(
        case.expected.must_reject_unsupported_constraints
        for case in DATASET.intent_cases
    ) == 305


def test_case_file_tampering_is_rejected(tmp_path: Path) -> None:
    copied = tmp_path / "derived-v2"
    shutil.copytree(DATASET_ROOT, copied)
    case_file = copied / "intent-cases.jsonl"
    case_file.write_bytes(case_file.read_bytes() + b"\n")

    with pytest.raises(EvaluationDatasetError, match="hash mismatch"):
        load_evaluation_dataset(copied)

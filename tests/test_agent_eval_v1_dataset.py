from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[1]
DATASET_ROOT = ROOT / "data" / "evaluation" / "agent-eval-v1"
CASES_PATH = DATASET_ROOT / "cases.jsonl"
WORLDS_PATH = DATASET_ROOT / "fixtures" / "worlds.json"
MANIFEST_PATH = DATASET_ROOT / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load() -> tuple[list[dict], dict, dict]:
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]
    worlds = json.loads(WORLDS_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return cases, worlds, manifest


def test_agent_eval_v1_has_frozen_draft_composition_and_splits() -> None:
    cases, worlds, manifest = _load()

    assert len(cases) == 60
    assert len({case["case_id"] for case in cases}) == 60
    assert set(worlds["worlds"]) == {case["case_id"] for case in cases}
    assert Counter(case["category"] for case in cases) == {
        "core": 16,
        "historical_failure": 12,
        "boundary": 16,
        "adversarial": 16,
    }
    assert Counter(case["split"] for case in cases) == {
        "development": 40,
        "regression": 20,
    }
    assert manifest["status"] == "frozen"
    assert manifest["dataset_version"] == "1.0.3"
    assert manifest["frozen_at"] == "2026-08-09"
    review = manifest["review"]
    assert review["freeze_allowed"] is True
    assert review["human_review_required"] is False
    assert review["human_reviewed_records"] == 60
    assert review["review_mode"] == "solo_second_pass"
    assert review["solo_second_pass_decision_counts"]["confirm"] == 20
    assert review["reviewer_a_verdict_counts"]["accept"] == 60
    assert all(case["dataset_version"] == "1.0.3" for case in cases)
    assert all(case["provenance"]["human_reviewed"] is True for case in cases)


def test_agent_eval_v1_hashes_and_fixture_references_are_consistent() -> None:
    cases, _, manifest = _load()

    assert _sha256(CASES_PATH) == manifest["case_file_sha256"]
    assert _sha256(WORLDS_PATH) == manifest["world_fixture_sha256"]
    for case in cases:
        prefix = f"fixtures/worlds.json#/worlds/{case['case_id']}/"
        fixture = case["fixture"]
        refs = [fixture["policy_ref"], fixture["employee_ref"], *fixture["inventory_refs"]]
        assert all(ref.startswith(prefix) for ref in refs)


def test_revision_fixture_has_inventory_for_original_and_revised_return_dates() -> None:
    _, worlds, _ = _load()
    inventory = worlds["worlds"]["historical_failure-approval-invalidated-on-revision-024"][
        "inventory"
    ]
    inbound_dates = {
        item["depart_at"][:10] for item in inventory["transports"] if item["direction"] == "inbound"
    }

    assert inbound_dates == {"2026-08-22", "2026-08-23"}


def test_timezone_revision_setup_matches_arrival_and_no_hotel_oracle() -> None:
    cases, worlds, _ = _load()
    case = next(item for item in cases if item["case_id"] == "boundary-timezone-cross-date-044")
    request = worlds["worlds"][case["case_id"]]["request_oracle"]

    assert "No hotel is needed" in case["turns"][0]["content"]
    assert "include a hotel" not in case["turns"][0]["content"]
    assert "provider.search_hotels" in case["expected"]["forbidden_tools"]
    assert request["arrive_by"] == "2026-08-20T10:00:00-07:00"
    assert request["hard_constraints"] == []


def test_agent_eval_v1_never_allows_booking_or_payment_tools() -> None:
    cases, _, _ = _load()

    never_allowed = {"provider.create_booking", "provider.capture_payment"}
    for case in cases:
        expected = case["expected"]
        required = {item["tool"] for item in expected["required_tool_patterns"]}
        forbidden = set(expected["forbidden_tools"])
        assertion_ids = {item["assertion_id"] for item in expected["hard_assertions"]}
        assert not required & forbidden
        assert never_allowed <= forbidden
        assert "no-live-booking-or-payment" in assertion_ids

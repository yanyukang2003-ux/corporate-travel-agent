from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome
from corporate_travel_agent.services.policy_config import (
    PolicyConfigurationError,
    load_policy_configuration,
)
from corporate_travel_agent.services.repositories import InMemoryPolicyRepository


def _payload() -> dict[str, object]:
    return load_policy_configuration().config.model_dump(mode="json")


def _load_external(tmp_path: Path, payload: dict[str, object]):
    path = tmp_path / "travel-policy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_policy_configuration(path)


def test_bundled_configuration_builds_domain_snapshots_and_city_aliases() -> None:
    loaded = load_policy_configuration()

    assert loaded.source_type == "bundled"
    assert loaded.config.config_version == "demo-config-v1"
    assert loaded.employee_snapshots[0].home_city == "Beijing"
    assert loaded.active_policy.hotel_city_caps.get("Shanghai") == Decimal("600")
    assert loaded.active_policy.content_hash
    assert loaded.city_aliases["北京市"] == "Beijing"
    # International city aliases for Duffel-oriented demo routes.
    assert loaded.city_aliases["纽约"] == "New York"
    assert loaded.city_aliases["费城"] == "Philadelphia"


def test_external_configuration_controls_policy_and_approval_path(tmp_path: Path) -> None:
    payload = _payload()
    payload["config_version"] = "trial-config-v2"
    payload["approvers"] = [{"approver_id": "M3001", "display_name": "New Manager"}]
    employees = payload["employees"]
    assert isinstance(employees, list)
    employees[0]["manager_id"] = "M3001"
    policies = payload["policies"]
    assert isinstance(policies, list)
    policies[0]["hotel_city_caps"]["CN-SHA"] = "800"

    loaded = _load_external(tmp_path, payload)
    workflow, _ = build_demo_system(policy_configuration=loaded)
    task = workflow.create_task(make_demo_request(task_id="external-policy"))
    near_hotel_option = next(item for item in task.options if item.hotel.ref_id == "HT-NEAR")
    hotel_evidence = next(
        evidence
        for evidence in near_hotel_option.policy_decision.evidence
        if evidence.rule_id == "hotel.city.nightly_cap"
    )

    assert loaded.source_type == "external"
    assert task.employee.manager_id == "M3001"
    assert hotel_evidence.threshold == "800"
    assert hotel_evidence.outcome is PolicyOutcome.COMPLIANT


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.update({"unexpected": True}),
            "Extra inputs are not permitted",
        ),
        (
            lambda payload: payload["employees"][0].update({"manager_id": "UNKNOWN"}),
            "unknown approver",
        ),
        (
            lambda payload: payload["policies"][0]["hotel_city_caps"].update(
                {"CN-XXX": "500"}
            ),
            "unknown city codes",
        ),
        (
            lambda payload: payload["policies"][0].update(
                {"exception_allowed_rule_ids": ["allow_everything"]}
            ),
            "unknown exception rule IDs",
        ),
        (
            lambda payload: payload["cities"][1]["aliases"].append("北京"),
            "maps to multiple cities",
        ),
        (
            lambda payload: payload.update({"timezone_name": "Mars/Olympus_Mons"}),
            "valid IANA timezone",
        ),
    ],
)
def test_invalid_configuration_fails_closed(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    payload = deepcopy(_payload())
    mutate(payload)

    with pytest.raises(PolicyConfigurationError, match=message):
        _load_external(tmp_path, payload)


def test_repository_retains_historical_policy_snapshots() -> None:
    loaded = load_policy_configuration()
    old_policy = loaded.active_policy
    new_policy = replace(
        old_policy,
        snapshot_id="policy-travel-v2-20260802",
        policy_version="travel-policy-v2",
        content_hash="new-policy-hash",
    )
    repository = InMemoryPolicyRepository(
        [old_policy, new_policy],
        current_snapshot_id=new_policy.snapshot_id,
    )

    assert repository.current() is new_policy
    assert repository.snapshot(old_policy.snapshot_id) is old_policy


def test_task_rejects_reused_snapshot_id_with_changed_content() -> None:
    workflow, _ = build_demo_system()
    task = workflow.create_task(make_demo_request(task_id="policy-content-pinning"))
    changed_policy = replace(
        workflow.policies.current(),
        hotel_city_caps={"Shanghai": Decimal("999")},
        content_hash="rewritten-content",
    )
    workflow.policies = InMemoryPolicyRepository(changed_policy)

    with pytest.raises(WorkflowError, match="content has changed"):
        workflow._policy_for(task)

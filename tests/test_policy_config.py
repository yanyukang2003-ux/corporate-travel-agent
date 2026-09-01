from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
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
    assert loaded.config.config_version == "demo-config-v2"
    assert loaded.employee_snapshots[0].home_city == "Beijing"
    assert loaded.employee_snapshots[0].cost_center == "CC-SALES-CN"
    assert loaded.active_policy.hotel_city_caps.get("Shanghai") == Decimal("600")
    assert loaded.active_policy.content_hash
    # v2 的三个新维度都读进了领域快照。
    assert loaded.active_policy.min_advance_booking_days == 3
    assert {item.city for item in loaded.active_policy.hotel_seasonal_caps} == {"New York", "Tokyo"}
    assert loaded.active_policy.cost_center_budgets["CC-SALES-CN"].amount == Decimal("20000")
    assert loaded.active_policy.cost_center_budgets["CC-SALES-CN"].currency == "USD"
    assert loaded.city_aliases["北京市"] == "Beijing"
    # International city aliases for Duffel-oriented demo routes.
    assert loaded.city_aliases["纽约"] == "New York"
    assert loaded.city_aliases["费城"] == "Philadelphia"


def test_external_configuration_controls_policy_and_approval_path(tmp_path: Path) -> None:
    payload = _payload()
    payload["config_version"] = "trial-config-v2"
    payload["approvers"].append({"approver_id": "M3001", "display_name": "New Manager"})
    employees = payload["employees"]
    assert isinstance(employees, list)
    employees[0]["manager_id"] = "M3001"
    policies = payload["policies"]
    assert isinstance(policies, list)
    # 改的是**当前生效**的那份快照；v1 留档不动。
    active_id = payload["active_policy_snapshot_id"]
    active = next(item for item in policies if item["snapshot_id"] == active_id)
    active["hotel_city_caps"]["CN-SHA"] = "800"

    loaded = _load_external(tmp_path, payload)
    workflow, _ = build_demo_system(policy_configuration=loaded, clock=lambda: DEMO_CLOCK)
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
    workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
    task = workflow.create_task(make_demo_request(task_id="policy-content-pinning"))
    changed_policy = replace(
        workflow.policies.current(),
        hotel_city_caps={"Shanghai": Decimal("999")},
        content_hash="rewritten-content",
    )
    workflow.policies = InMemoryPolicyRepository(changed_policy)

    with pytest.raises(WorkflowError, match="content has changed"):
        workflow._policy_for(task)


def test_policy_content_hash_is_stable_across_processes() -> None:
    """同一份政策在不同进程里必须算出同一个哈希。

    `exception_allowed_rule_ids` 是集合，集合没有顺序，而 Python 每个进程的字符串
    哈希种子不同，所以 `model_dump` 出来的列表顺序会变。`sort_keys=True` 只排字典的
    键、不排列表的值，于是同一份政策会算出不同哈希——真实 Postgres 上重启一次，所有
    在途任务就都被 "Historical policy snapshot content has changed" 拦死了，而政策
    其实一个字没改。内存存储永远暴露不出这个问题：任务随进程一起消失。
    """
    import subprocess
    import sys

    script = (
        "from corporate_travel_agent.services.policy_config import "
        "load_policy_configuration, _policy_content_hash;"
        "print(_policy_content_hash(load_policy_configuration().config.policies[0]))"
    )
    hashes = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        ).stdout.strip()
        # 每个子进程都有各自的哈希种子；四次足以撞出顺序差异。
        for _ in range(4)
    }

    assert len(hashes) == 1, hashes


def test_set_fields_are_sorted_before_hashing() -> None:
    """哈希前把集合字段排序：顺序不同但内容相同的政策必须得到同一个哈希。"""
    from corporate_travel_agent.services.policy_config import (
        _canonicalize_sets,
        _policy_content_hash,
    )

    policy = load_policy_configuration().config.policies[0]
    payload = policy.model_dump(mode="json")
    canonical = _canonicalize_sets(policy, payload)

    rule_ids = canonical["exception_allowed_rule_ids"]
    assert rule_ids == sorted(rule_ids)
    # 打乱输入顺序不改变结果。
    shuffled = {**payload, "exception_allowed_rule_ids": list(reversed(rule_ids))}
    assert _canonicalize_sets(policy, shuffled)["exception_allowed_rule_ids"] == rule_ids
    assert _policy_content_hash(policy) == _policy_content_hash(policy)


#: 演示政策 v1 在追加 v2 之前的内容哈希。v2 是**追加**的：v1 一个字没改，所以它的哈希
#: 不许变——变了就说明后加的可选字段漏进了旧快照的哈希，在途任务会被拦下来。
POLICY_V1_CONTENT_HASH = "078458af0c0475849acb4b1c31b749a21176fad8f958ae3c71e3a21aa22875c7"


def test_appending_policy_v2_did_not_change_the_v1_hash() -> None:
    from corporate_travel_agent.services.policy_config import _policy_content_hash

    loaded = load_policy_configuration()
    v1 = next(
        item for item in loaded.config.policies if item.snapshot_id == "policy-travel-v1-20260801"
    )
    assert _policy_content_hash(v1) == POLICY_V1_CONTENT_HASH
    assert loaded.active_policy.snapshot_id == "policy-travel-v2-20260901"
    # 旧快照没有新维度：None / 空，不是默认成某个数。
    v1_snapshot = next(
        item for item in loaded.policy_snapshots if item.snapshot_id == v1.snapshot_id
    )
    assert v1_snapshot.min_advance_booking_days is None
    assert v1_snapshot.hotel_seasonal_caps == ()
    assert v1_snapshot.cost_center_budgets == {}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["policies"][1]["hotel_seasonal_caps"].append(
                {
                    "city_code": "CN-XXX",
                    "label": "nowhere",
                    "season_from": "2026-01-01",
                    "season_to": "2026-01-31",
                    "nightly_cap": "100",
                }
            ),
            "seasonal hotel caps reference unknown city codes",
        ),
        (
            lambda payload: payload["policies"][1]["hotel_seasonal_caps"].append(
                {
                    "city_code": "US-NYC",
                    "label": "backwards",
                    "season_from": "2026-02-01",
                    "season_to": "2026-01-01",
                    "nightly_cap": "100",
                }
            ),
            "season_to cannot be earlier than season_from",
        ),
        (
            lambda payload: payload["policies"][1]["cost_center_budgets"].append(
                {
                    "cost_center": "CC-SALES-CN",
                    "amount": "1",
                    "period_from": "2026-01-01",
                    "period_to": "2026-12-31",
                }
            ),
            "at most one budget",
        ),
        (
            lambda payload: payload["policies"][1]["cost_center_budgets"][0].update(
                {"amount": "-5"}
            ),
            "invalid amount",
        ),
        (
            lambda payload: payload["policies"][1].update({"min_advance_booking_days": 400}),
            "less than or equal to 365",
        ),
    ],
)
def test_new_policy_dimensions_fail_closed(tmp_path: Path, mutate, message: str) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(PolicyConfigurationError, match=message):
        _load_external(tmp_path, payload)

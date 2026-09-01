"""发起人 ≠ 旅行者：助理替高管订，差标、审批、预算全看旅行者；委托名单决定谁能发起。"""

from __future__ import annotations

import unittest

import pytest

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome
from corporate_travel_agent.services.policy_config import (
    PolicyConfigurationError,
    load_policy_configuration,
)


class DirectoryTests(unittest.TestCase):
    def test_the_demo_directory_knows_who_may_book_for_whom(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        directory = workflow.employees

        self.assertEqual(directory.snapshot("E1001").delegate_ids, ("A1002",))
        self.assertEqual(directory.delegators_of("A1002"), ("E1001",))
        self.assertEqual(directory.delegators_of("E1001"), ())
        self.assertTrue(directory.may_book_for("A1002", "E1001"))
        self.assertTrue(directory.may_book_for("E1001", "E1001"))
        self.assertFalse(directory.may_book_for("E1001", "A1002"))
        self.assertFalse(directory.may_book_for("A1002", "nobody"))


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)

    def test_a_delegate_creates_a_task_whose_rules_are_the_travelers(self) -> None:
        task = self.workflow.create_task(
            make_demo_request(task_id="delegated"), requester_id="A1002"
        )

        self.assertEqual(task.employee.employee_id, "E1001")
        self.assertEqual(task.requester_id, "A1002")
        self.assertTrue(task.is_delegated)
        # 预算快照是旅行者成本中心的；审批人是旅行者的经理。
        self.assertEqual(task.metadata["budget_snapshot"]["cost_center"], "CC-SALES-CN")
        option = next(
            item for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        task = self.workflow.select_option(
            task.task_id, option.option_id, business_reason="老板的日程"
        )
        self.assertEqual(task.approval.approver_id, "M2001")
        outbox = self.workflow.tasks.outbox.list_unpublished(limit=10)
        requested = next(item for item in outbox if item.event_type == "APPROVAL_REQUESTED")
        self.assertEqual(requested.payload["employee_id"], "E1001")
        self.assertEqual(requested.payload["requester_id"], "A1002")

    def test_without_a_requester_the_traveler_is_the_requester(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="self"))
        # 编排器把发起人记成旅行者本人；None 只出现在没记过的旧任务上。
        self.assertEqual(task.requester_id, "E1001")
        self.assertEqual(task.requested_by, "E1001")
        self.assertFalse(task.is_delegated)

    def test_an_employee_outside_the_delegate_list_is_refused(self) -> None:
        """A1002 可以替 E1001 订，E1001 不能替 A1002 订：委托是单向的。"""
        with self.assertRaises(WorkflowError):
            self.workflow.create_task(
                _with_traveler(make_demo_request(task_id="reverse"), "A1002"), requester_id="E1001"
            )

    def test_a_non_employee_actor_is_recorded_without_a_delegation_check(self) -> None:
        """管理员、系统身份不在员工目录里：能不能建任务由角色管，不由委托名单管。"""
        task = self.workflow.create_task(
            make_demo_request(task_id="by-admin"), requester_id="A9001"
        )
        self.assertEqual(task.requester_id, "A9001")
        self.assertTrue(task.is_delegated)

    def test_the_agentic_entrypoint_records_the_requester_too(self) -> None:
        from test_agentic_entrypoint import _SEARCH_AND_PROPOSE, READY, ScriptedToolModel

        workflow, _ = build_demo_system(
            tool_calling_language_model=ScriptedToolModel(list(_SEARCH_AND_PROPOSE)),
            clock=lambda: DEMO_CLOCK,
        )
        task = workflow.create_task_from_agentic_message(
            READY, traveler_id="E1001", requester_id="A1002"
        )
        self.assertEqual(task.requester_id, "A1002")
        self.assertEqual(task.employee.employee_id, "E1001")

    def test_the_task_list_includes_what_i_requested_for_others(self) -> None:
        self.workflow.create_task(make_demo_request(task_id="for-boss"), requester_id="A1002")
        mine = {
            item.task_id
            for item in self.workflow.tasks.list_task_summaries(involving_employee_id="A1002")
        }
        bosses = {
            item.task_id
            for item in self.workflow.tasks.list_task_summaries(involving_employee_id="E1001")
        }
        self.assertIn("for-boss", mine)
        self.assertIn("for-boss", bosses)
        summary = next(
            item for item in self.workflow.tasks.list_task_summaries() if item.task_id == "for-boss"
        )
        self.assertEqual(summary.requester_id, "A1002")


def _with_traveler(request, traveler_id: str):
    from dataclasses import replace

    return replace(request, traveler_id=traveler_id)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["employees"][0].update({"delegates": ["E1001"]}),
            "themselves",
        ),
        (
            lambda payload: payload["employees"][0].update({"delegates": ["GHOST"]}),
            "unknown delegates",
        ),
        (
            lambda payload: payload["employees"][0].update({"delegates": ["A1002", "A1002"]}),
            "unique",
        ),
    ],
)
def test_delegation_configuration_fails_closed(tmp_path, mutate, message: str) -> None:
    import json

    payload = json.loads(
        __import__("importlib.resources", fromlist=["files"])
        .files("corporate_travel_agent.config")
        .joinpath("default_policy.json")
        .read_text(encoding="utf-8")
    )
    mutate(payload)
    target = tmp_path / "policy.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PolicyConfigurationError, match=message):
        load_policy_configuration(str(target))


def test_the_demo_configuration_has_a_delegate() -> None:
    loaded = load_policy_configuration()
    by_id = {item.employee_id: item for item in loaded.employee_snapshots}
    assert by_id["E1001"].delegate_ids == ("A1002",)
    assert by_id["A1002"].delegate_ids == ()

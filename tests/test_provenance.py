"""方案溯源：每一步都要说得出依据，说不出的地方要自己承认。

盯三件事：
1. 链条本身接得上——从一条方案能一路走回用户原话和原始响应哈希；
2. 断的地方进 `gaps`，**不许静默省略**；
3. 钉住的指纹事后重算对得上，而且盖的是"依据"不是"日志"。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_agentic_entrypoint import _SEARCH_AND_PROPOSE, READY, _workflow

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState
from corporate_travel_agent.services.provenance import (
    option_provenance,
    provenance_hash,
    sealed_view,
)

FIXED_NOW = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)


def _structured_workflow():
    return build_demo_system(clock=lambda: FIXED_NOW)[0]


def _record(workflow, task, option_id: str) -> dict:
    return workflow.option_provenance_record(task.task_id, option_id)


class TestTheChainFromAnOptionBackToTheWords:
    """工具循环这条路上，每一段都该走回到用户自己说过的那句话。"""

    def _agentic_task(self):
        workflow, _ = _workflow(list(_SEARCH_AND_PROPOSE))
        task = workflow.create_task_from_agentic_message(READY, traveler_id="E1001")
        return workflow, task

    def test_the_date_points_back_to_the_travelers_own_sentence(self) -> None:
        workflow, task = self._agentic_task()

        record = _record(workflow, task, task.options[0].option_id)
        search = record["items"][0]["because"]["search"]

        assert search["date_evidence"] == "8月5日上午10点前到"
        # 而且指得出是对话里的哪一条，不是"某句话里有"。
        assert search["quoted_from_message"] == 0
        assert search["date_evidence"] in task.messages[0].content

    def test_what_the_system_worked_out_is_kept_apart_from_what_the_person_said(
        self,
    ) -> None:
        """人说的和机器推的分开存。混在一起就分不清哪句话是谁的责任。"""
        workflow, task = self._agentic_task()

        search = _record(workflow, task, task.options[0].option_id)["items"][0][
            "because"
        ]["search"]

        assert "18 小时" in search["assumption"]
        assert "18 小时" not in search["date_evidence"]

    def test_every_offer_resolves_to_the_snapshot_that_really_produced_it(self) -> None:
        """跨日窗口会拆成几次搜索。报价要指向真正装着它的那一次。

        合并出来的快照沿用第一天的 `snapshot_id` 和 `raw_payload_hash`，却装着
        后面几天的报价——存那个的话，按哈希去核会核不上。
        """
        workflow, task = self._agentic_task()
        stored = {item.snapshot_id for item in workflow.tasks.snapshots(task.task_id)}

        for option in task.options:
            for leg in option.legs:
                assert leg.snapshot_id in stored

    def test_the_raw_provider_response_is_reachable_and_hashed(self) -> None:
        workflow, task = self._agentic_task()

        snapshot = _record(workflow, task, task.options[0].option_id)["items"][0][
            "because"
        ]["snapshot"]

        assert snapshot["raw_payload_hash"]
        assert snapshot["raw_response"]["sha256"]
        assert snapshot["raw_response"]["retention_until"]

    def test_a_complete_chain_reports_no_gaps(self) -> None:
        workflow, task = self._agentic_task()

        assert _record(workflow, task, task.options[0].option_id)["gaps"] == []


class TestGapsAreAdmitted:
    def test_the_structured_entrypoint_admits_it_has_no_quoted_words(self) -> None:
        """结构化入口的日期来自已校验的请求，不是某句原话。**说不出就说说不出。**"""
        workflow = _structured_workflow()
        task = workflow.create_task(make_demo_request(task_id="prov-structured"))

        record = _record(workflow, task, task.options[0].option_id)

        assert record["gaps"], "结构化入口没有对话出处，这件事必须写出来"
        assert all("没有对话出处" in item for item in record["gaps"])
        assert record["items"][0]["because"]["search"]["date_evidence"] is None

    def test_an_unreachable_snapshot_is_named_not_swallowed(self) -> None:
        workflow = _structured_workflow()
        task = workflow.create_task(make_demo_request(task_id="prov-missing"))
        option = task.options[0]

        record = option_provenance(
            task=task,
            option=option,
            snapshots=(),  # 快照取不回来
            events=(),
        )

        assert any("取不回来" in item for item in record["gaps"])
        assert record["items"][0]["because"]["snapshot"] is None


class TestThePinnedFingerprint:
    def _handed_off(self):
        workflow = _structured_workflow()
        task = workflow.create_task(make_demo_request(task_id="prov-pinned"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        task = workflow.select_option(task.task_id, option.option_id)
        return workflow, task, option

    def test_handoff_pins_the_chain(self) -> None:
        workflow, task, option = self._handed_off()

        assert task.state is TaskState.READY_FOR_HANDOFF
        pinned = task.metadata["provenance"]
        assert pinned["option_id"] == option.option_id
        assert len(pinned["hash"]) == 64
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        assert "PROVENANCE_PINNED" in events

    def test_recomputing_it_later_still_matches(self) -> None:
        """指纹盖的是依据，不是日志。审计事件之后还会继续追加，指纹不该因此失效。"""
        workflow, task, _ = self._handed_off()

        assert workflow.verify_provenance(task.task_id)["status"] == "MATCH"

    def test_the_growing_audit_log_is_left_out_of_the_fingerprint(self) -> None:
        workflow, task, option = self._handed_off()
        record = _record(workflow, task, option.option_id)

        assert record["state_events"], "记录里要有事件，给人看"
        assert "state_events" not in sealed_view(record), "但事件不进指纹"

    def test_changing_the_evidence_breaks_the_fingerprint(self) -> None:
        """篡改要能被发现——这才是钉指纹的意义。"""
        workflow, task, option = self._handed_off()
        record = _record(workflow, task, option.option_id)
        before = provenance_hash(record)

        record["items"][0]["what"]["price"] = "1"

        assert provenance_hash(record) != before

    def test_a_task_that_never_handed_off_says_so(self) -> None:
        workflow = _structured_workflow()
        task = workflow.create_task(make_demo_request(task_id="prov-unpinned"))

        assert workflow.verify_provenance(task.task_id)["status"] == "NOT_PINNED"


class TestTheApi:
    def test_an_unknown_option_is_refused(self) -> None:
        workflow = _structured_workflow()
        task = workflow.create_task(make_demo_request(task_id="prov-404"))

        with pytest.raises(WorkflowError):
            workflow.option_provenance_record(task.task_id, "opt-does-not-exist")

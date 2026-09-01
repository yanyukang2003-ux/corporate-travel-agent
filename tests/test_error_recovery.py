"""Recovery taxonomy + mid-failure re-chat (§13 Step 1)."""

from __future__ import annotations

import unittest
from datetime import datetime

from corporate_travel_agent.agent.error_recovery import (
    RecoveryAction,
    SideEffectClass,
    classify_tool_failure,
    recon_search_legs,
)
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import TaskState, ToolCallStatus
from corporate_travel_agent.domain.models import InventorySnapshot, ToolCallRecord
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    TransportSearchQuery,
)

REFERENCE_TIME = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)
DEPARTURE = datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI_TZ)
ARRIVE_BY = datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI_TZ)
RETURN_AFTER = datetime(2026, 8, 6, 17, 0, tzinfo=SHANGHAI_TZ)
RETURN_BEFORE = datetime(2026, 8, 6, 23, 0, tzinfo=SHANGHAI_TZ)


class _InboundFailOnceProvider:
    """Outbound succeeds; inbound fails once then succeeds (partial recon path)."""

    name = "inbound-fail-once"

    def __init__(self, base) -> None:
        self.base = base
        self.inbound_calls = 0

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        # Inbound: origin is destination of outbound.
        is_inbound = query.origin == "Shanghai" and query.destination == "Beijing"
        if is_inbound:
            self.inbound_calls += 1
            if self.inbound_calls == 1:
                raise ProviderError("inbound inventory unavailable")
        return self.base.search_transport(query)

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        return self.base.search_hotels(query)

    def revalidate(self, refs):
        return self.base.revalidate(refs)

    def create_deep_link(self, option):
        return self.base.create_deep_link(option)


class ClassifyToolFailureTests(unittest.TestCase):
    def test_retryable_provider_read_is_retry_same(self) -> None:
        decision = classify_tool_failure(
            tool_name="provider.search_transport.outbound",
            tool_kind="PROVIDER",
            retryable=True,
            response_received=False,
            attempt=1,
            max_attempts=2,
            will_auto_retry=True,
        )
        self.assertEqual(decision.side_effect_class, SideEffectClass.EXTERNAL_READ)
        self.assertEqual(decision.recovery_action, RecoveryAction.RETRY_SAME)
        self.assertTrue(decision.will_auto_retry)

    def test_llm_exhausted_prefers_clarify(self) -> None:
        decision = classify_tool_failure(
            tool_name="llm.extract_trip_intent",
            tool_kind="LLM",
            retryable=False,
            response_received=None,
            attempt=1,
            max_attempts=1,
            will_auto_retry=False,
        )
        self.assertEqual(decision.side_effect_class, SideEffectClass.TASK_LOCAL)
        self.assertEqual(decision.recovery_action, RecoveryAction.CLARIFY)

    def test_write_possible_forbids_auto_retry(self) -> None:
        decision = classify_tool_failure(
            tool_name="provider.create_order",
            tool_kind="PROVIDER",
            retryable=True,
            response_received=None,
            attempt=1,
            max_attempts=2,
            will_auto_retry=True,
            interrupted=False,
        )
        self.assertEqual(
            decision.side_effect_class, SideEffectClass.EXTERNAL_WRITE_POSSIBLE
        )
        self.assertEqual(decision.recovery_action, RecoveryAction.RETRY_AFTER_RECON)
        self.assertFalse(decision.will_auto_retry)


class ReconSearchLegsTests(unittest.TestCase):
    def test_partial_success_records_successful_outbound(self) -> None:
        calls = [
            ToolCallRecord(
                sequence=1,
                tool_name="provider.search_transport.outbound",
                tool_kind="PROVIDER",
                status=ToolCallStatus.SUCCEEDED,
                started_at=REFERENCE_TIME,
                completed_at=REFERENCE_TIME,
            ),
            ToolCallRecord(
                sequence=2,
                tool_name="provider.search_transport.inbound",
                tool_kind="PROVIDER",
                status=ToolCallStatus.FAILED,
                started_at=REFERENCE_TIME,
                completed_at=REFERENCE_TIME,
                error_type="ProviderError",
            ),
        ]
        recon = recon_search_legs(calls)
        self.assertTrue(recon["partial"])
        self.assertEqual(recon["successful_legs"], ["provider.search_transport.outbound"])
        self.assertEqual(recon["failed_legs"], ["provider.search_transport.inbound"])
        self.assertEqual(
            recon["recovery"]["recovery_action"], RecoveryAction.RETRY_AFTER_RECON.value
        )


class MultiLegSearchReconIntegrationTests(unittest.TestCase):
    def test_inbound_failure_records_partial_recon(self) -> None:
        workflow, mock = build_demo_system(clock=lambda: REFERENCE_TIME)
        workflow.provider = _InboundFailOnceProvider(mock)
        request = make_demo_request(task_id="partial-inbound")

        task = workflow.create_task(request)

        self.assertEqual(task.state, TaskState.PROVIDER_FAILED)
        recon = task.metadata.get("search_failure_recon") or {}
        self.assertTrue(recon.get("partial"))
        self.assertIn(
            "provider.search_transport.outbound", recon.get("successful_legs") or []
        )
        self.assertIn(
            "provider.search_transport.inbound", recon.get("failed_legs") or []
        )
        recovery = task.metadata.get("last_recovery") or {}
        self.assertEqual(
            recovery.get("recovery_action"), RecoveryAction.RETRY_AFTER_RECON.value
        )
        self.assertEqual(task.options, [])


if __name__ == "__main__":
    unittest.main()

"""Recovery taxonomy + mid-failure re-chat (§13 Step 1)."""

from __future__ import annotations

import unittest
from collections import deque
from datetime import datetime

from corporate_travel_agent.agent.error_recovery import (
    RecoveryAction,
    SideEffectClass,
    classify_tool_failure,
    recon_search_legs,
)
from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.agent.ports import (
    IntentExtractionResult,
    LanguageModelError,
    LLMCallMetadata,
)
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
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


def _intent_payload() -> IntentExtractionSchema:
    return IntentExtractionSchema(
        classification="TRIP",
        confidence=0.9,
        provided_fields=[
            "origin",
            "destination",
            "departure_after",
            "arrive_by",
            "return_after",
            "return_before",
            "hotel_check_in",
            "hotel_check_out",
        ],
        missing_required_fields=[],
        fields=TripIntentFields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=DEPARTURE,
            arrive_by=ARRIVE_BY,
            return_after=RETURN_AFTER,
            return_before=RETURN_BEFORE,
            hotel_check_in=DEPARTURE.date(),
            hotel_check_out=RETURN_AFTER.date(),
            client_location=None,
            hard_constraints=[],
            soft_preferences=[],
        ),
        assumptions=[],
        conflicts=[],
        manipulation_detected=False,
    )


class ScriptedLanguageModel:
    prompt_version = "scripted-v1"

    def __init__(self, outputs: list[IntentExtractionSchema | Exception]) -> None:
        self.outputs = deque(outputs)
        self.calls: list[dict[str, object]] = []

    def extract_trip_intent(
        self, message: str, *, task_id: str, traveler_id: str, context: dict[str, object]
    ) -> IntentExtractionResult:
        self.calls.append({"message": message, "context": context})
        output = self.outputs.popleft()
        if isinstance(output, Exception):
            raise output
        return IntentExtractionResult(
            payload=output,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted-schema-model",
                duration_ms=1,
            ),
        )

    def propose_search_adjustment(self, failure_facts, allowed_adjustments):
        return None

    def explain_verified_options(self, options):
        return None


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


class StructuredInputRecoveryTests(unittest.TestCase):
    def test_submit_message_recovers_from_llm_clarify_fallback(self) -> None:
        model = ScriptedLanguageModel(
            [LanguageModelError("model unavailable"), _intent_payload()]
        )
        workflow, _ = build_demo_system(
            language_model=model,
            clock=lambda: REFERENCE_TIME,
            max_llm_attempts=1,
        )
        task = workflow.create_task_from_message(
            "下周出差", traveler_id="E1001", task_id="llm-clarify-rechat"
        )
        self.assertEqual(task.state, TaskState.NEEDS_CLARIFICATION)
        self.assertIn("llm_recovery", task.metadata)
        self.assertEqual(
            task.metadata["llm_recovery"]["recovery_action"],
            RecoveryAction.CLARIFY.value,
        )
        self.assertEqual(task.metadata["llm_recovery"]["fallback"], "clarify")
        self.assertIn("origin", task.missing_required_fields)
        self.assertIn("destination", task.missing_required_fields)
        self.assertTrue(task.metadata.get("clarification_questions"))
        self.assertIn("出发城市", task.clarification_question or "")

        task = workflow.submit_message(
            task.task_id,
            "北京到上海，8月5日出发，8月6日上午10点前到，当晚返程并住一晚",
        )

        self.assertNotEqual(task.state, TaskState.NEEDS_STRUCTURED_INPUT)
        self.assertEqual(len(model.calls), 2)
        # Second extract should proceed to search/clarify, not raise WorkflowError.
        self.assertIn(
            task.state,
            {
                TaskState.WAITING_FOR_USER,
                TaskState.NEEDS_CLARIFICATION,
                TaskState.NO_FEASIBLE_OPTION,
                TaskState.PROVIDER_FAILED,
            },
        )

    def test_submit_message_still_recovers_from_needs_structured_input(self) -> None:
        """Legacy structured path remains open when already forced to form."""
        model = ScriptedLanguageModel([_intent_payload(), _intent_payload()])
        workflow, _ = build_demo_system(
            language_model=model,
            clock=lambda: REFERENCE_TIME,
        )
        task = workflow.create_task_from_message(
            "北京到上海，8月5日出发，8月6日上午10点前到，当晚返程并住一晚",
            traveler_id="E1001",
            task_id="seed-for-structured",
        )
        # Force structured state (clarification-budget style terminal).
        # In-memory repo stores the same object reference.
        task.state = TaskState.NEEDS_STRUCTURED_INPUT
        task.failure = "Clarification budget exhausted; use the structured form"

        task = workflow.submit_message(
            task.task_id,
            "北京到上海，8月5日出发，8月6日上午10点前到，当晚返程并住一晚",
        )

        self.assertIn("structured_input_recovery", task.metadata)
        self.assertGreaterEqual(len(model.calls), 2)
        self.assertIn(
            task.state,
            {
                TaskState.WAITING_FOR_USER,
                TaskState.NEEDS_CLARIFICATION,
                TaskState.NO_FEASIBLE_OPTION,
                TaskState.PROVIDER_FAILED,
            },
        )

    def test_submit_message_still_rejects_terminal_budget_state(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: REFERENCE_TIME)
        task = workflow.create_task(make_demo_request(task_id="budget-terminal"))
        # Force terminal state without going through full API.
        task.state = TaskState.TOOL_BUDGET_EXHAUSTED
        with self.assertRaises(WorkflowError):
            workflow.submit_message(task.task_id, "再试一次")


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

from __future__ import annotations

from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.ports import (
    IntentExtractionResult,
    LanguageModelError,
    LLMCallMetadata,
)
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import TaskState

SH = ZoneInfo("Asia/Shanghai")
REF = datetime(2026, 8, 19, 15, 0, tzinfo=SH)
NY_PHL = (
    "下周二从纽约去费城见客户，上午出发，周三下午回来。优先飞机，酒店离客户公司近一点。"
)


class ScriptedModel:
    prompt_version = "scripted-v1"
    fallback_model = "backup-model"

    def __init__(self, outputs: list[object]) -> None:
        self.outputs = deque(outputs)
        self.calls: list[dict[str, object]] = []

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        self.calls.append({"message": message, "context": context, "task_id": task_id})
        if not self.outputs:
            raise LanguageModelError("scripted model exhausted")
        item = self.outputs.popleft()
        if isinstance(item, Exception):
            raise item
        return IntentExtractionResult(
            payload=item,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model=str((context or {}).get("model_override") or "primary-model"),
                duration_ms=1,
            ),
        )


def _payload() -> IntentExtractionSchema:
    ny = ZoneInfo("America/New_York")
    return IntentExtractionSchema(
        classification="MULTI_DAY_TRIP",
        fields=TripIntentFields(
            origin="New York",
            destination="Philadelphia",
            departure_after=datetime(2026, 8, 25, 8, 0, tzinfo=ny),
            arrive_by=datetime(2026, 8, 25, 18, 0, tzinfo=ny),
            return_after=datetime(2026, 8, 26, 13, 0, tzinfo=ny),
            return_before=datetime(2026, 8, 26, 22, 0, tzinfo=ny),
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=[],
            soft_preferences=["prefer_flight"],
        ),
        provided_fields=[
            "origin",
            "destination",
            "departure_after",
            "arrive_by",
            "return_after",
            "return_before",
            "soft_preferences",
        ],
        missing_required_fields=[],
        conflicts=[],
        assumptions=[],
        confidence=0.9,
        manipulation_detected=False,
    )


def test_billing_failure_uses_local_parser_instead_of_clarify() -> None:
    model = ScriptedModel(
        [
            LanguageModelError(
                "Intent extraction failed at OpenAI HTTP layer: APIStatusError",
                error_code="OPENAI_HTTP_402",
                layer="openai_http",
                http_status=402,
                retryable=False,
            )
        ]
    )
    workflow, _ = build_demo_system(
        language_model=model,
        clock=lambda: REF,
        max_llm_attempts=1,
    )
    task = workflow.create_task_from_message(
        NY_PHL, traveler_id="E1001", task_id="billing-local"
    )
    assert task.intent_fields.get("origin") == "New York"
    assert task.intent_fields.get("destination") == "Philadelphia"
    assert task.metadata.get("llm_recovery", {}).get("fallback") == "local_parser"
    assert task.state is not TaskState.NEEDS_STRUCTURED_INPUT
    if task.state is TaskState.NEEDS_CLARIFICATION:
        assert "client_location" in (task.metadata.get("uncertain_slots") or [])


def test_llm_primary_path_does_not_get_polluted_by_return_only_local_date() -> None:
    day = datetime(2026, 8, 25, 18, 0, tzinfo=SH)
    payload = IntentExtractionSchema(
        classification="MULTI_DAY_TRIP",
        fields=TripIntentFields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=day,
            arrive_by=day,
            return_after=day,
            return_before=datetime(2026, 8, 25, 23, 0, tzinfo=SH),
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=[],
            soft_preferences=[],
        ),
        provided_fields=[
            "origin",
            "destination",
            "departure_after",
            "arrive_by",
            "return_after",
            "return_before",
        ],
        missing_required_fields=[],
        conflicts=[],
        assumptions=[],
        confidence=0.9,
        manipulation_detected=False,
    )
    model = ScriptedModel([payload, payload, payload])
    workflow, _ = build_demo_system(language_model=model, clock=lambda: REF)

    task = workflow.create_task_from_message(
        "返程8月25日晚上从上海回北京",
        traveler_id="E1001",
        task_id="return-only-llm-primary",
    )

    first_prior = model.calls[0]["context"]["prior_fields"]
    assert first_prior["departure_after"] is None
    assert first_prior["return_after"] is None
    assert task.intent_fields["departure_after"] is None
    assert task.intent_fields["arrive_by"] is None
    assert task.intent_fields["return_after"] is not None
    rejected = task.metadata["intent_calibration"]["repair_rejected_fields"]
    assert "departure_after:source_scope_mismatch" in rejected
    assert "arrive_by:source_scope_mismatch" in rejected
    assert task.state is TaskState.NEEDS_CLARIFICATION


def test_billing_failure_without_cities_does_not_spend_clarify_round() -> None:
    model = ScriptedModel(
        [
            LanguageModelError(
                "Intent extraction failed at OpenAI HTTP layer: APIStatusError",
                error_code="OPENAI_HTTP_402",
                layer="openai_http",
                http_status=402,
                retryable=False,
            )
        ]
    )
    workflow, _ = build_demo_system(
        language_model=model,
        clock=lambda: REF,
        max_llm_attempts=1,
    )
    task = workflow.create_task_from_message(
        "下周出差", traveler_id="E1001", task_id="billing-empty"
    )
    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert task.clarification_rounds == 0
    assert (task.metadata.get("extract_failure") or {}).get("class") == "billing"
    assert task.missing_required_fields


def test_rate_limit_uses_fallback_model() -> None:
    model = ScriptedModel(
        [
            LanguageModelError(
                "rate limited",
                error_code="OPENAI_HTTP_429",
                layer="openai_http",
                http_status=429,
                retryable=True,
            ),
            _payload(),
        ]
    )
    workflow, _ = build_demo_system(
        language_model=model,
        clock=lambda: REF,
        max_llm_attempts=1,
    )
    task = workflow.create_task_from_message(
        NY_PHL, traveler_id="E1001", task_id="fallback-429"
    )
    assert len(model.calls) == 2
    assert model.calls[1]["context"].get("model_override") == "backup-model"
    assert task.metadata.get("model_fallback", {}).get("fallback_model") == "backup-model"
    assert task.state is not TaskState.DRAFT


def test_schema_repair_consumes_tool_budget() -> None:
    first = IntentExtractionSchema(
        classification="MULTI_DAY_TRIP",
        fields=TripIntentFields(
            origin="Beijing",
            destination="Shanghai",
            departure_after=None,
            arrive_by=None,
            return_after=None,
            return_before=None,
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=[],
            soft_preferences=[],
        ),
        provided_fields=["origin", "destination"],
        missing_required_fields=["departure_after", "arrive_by"],
        conflicts=[],
        assumptions=[],
        confidence=0.7,
        manipulation_detected=False,
    )
    repaired = _payload()
    repaired = repaired.model_copy(
        update={
            "fields": repaired.fields.model_copy(
                update={"origin": "Beijing", "destination": "Shanghai"}
            )
        }
    )
    model = ScriptedModel([first, repaired, repaired])
    workflow, _ = build_demo_system(
        language_model=model,
        clock=lambda: REF,
        max_llm_attempts=1,
    )
    task = workflow.create_task_from_message(
        "北京到上海出差需要酒店",
        traveler_id="E1001",
        task_id="schema-repair-budget",
    )
    llm_calls = [item for item in task.tool_calls if item.tool_kind == "LLM"]
    assert len(llm_calls) >= 2
    assert all(item.counts_toward_budget for item in llm_calls)
    assert task.tool_calls_used == sum(1 for item in task.tool_calls if item.counts_toward_budget)


def test_billing_does_not_switch_fallback_model() -> None:
    model = ScriptedModel(
        [
            LanguageModelError(
                "payment required",
                error_code="OPENAI_HTTP_402",
                layer="openai_http",
                http_status=402,
                retryable=False,
            )
        ]
    )
    workflow, _ = build_demo_system(
        language_model=model,
        clock=lambda: REF,
        max_llm_attempts=1,
    )
    workflow.create_task_from_message(NY_PHL, traveler_id="E1001", task_id="no-fallback-402")
    assert all(
        (call.get("context") or {}).get("model_override") is None for call in model.calls
    )
    assert "backup-model" not in str(model.calls)

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from corporate_travel_agent.agent.ports import (
    IntentInterpretationResult,
    LanguageModelError,
    LLMCallMetadata,
)
from corporate_travel_agent.agent.semantic_intent import (
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
)
from corporate_travel_agent.api.main import app
from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement

client = TestClient(app)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _ScriptedSemanticModel:
    """假的语义模型：按最后一轮用户消息里的目的地给出一个"能查了"的理解。

    只为把 HTTP 链路跑通，不做任何真实语言理解。
    """

    prompt_version = "semantic-api-scripted-v1"

    def __init__(self) -> None:
        self.conversations: list[str] = []
        self.next_destination = "Shanghai"
        self.fail_with: Exception | None = None

    def interpret_trip_intent(
        self,
        conversation: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, object],
    ) -> IntentInterpretationResult:
        del task_id, traveler_id, context
        self.conversations.append(conversation)
        if self.fail_with is not None:
            raise self.fail_with
        intent = SemanticIntent(
            summary=f"从北京去{self.next_destination}",
            origin_candidates=["Beijing"],
            destination_candidates=[self.next_destination],
            departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI),
            arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI),
            return_after=None,
            return_before=None,
            booking_scope=BookingScope.OUTBOUND_ONLY,
            lodging_requirement=LodgingRequirement.NOT_REQUIRED,
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=["arrive_before_meeting"],
            soft_preferences=[],
            alternatives=[],
            conditions=[],
            uncertainties=[],
        )
        decision = IntentDecision(
            status=IntentDecisionStatus.READY,
            intent=intent,
            clarification_question=None,
            conflicts=[],
            unsupported_reasons=[],
            assumptions=[],
            evidence=[
                # 第 0 轮永远是用户的第一句话，这几条用例里都含"北京"。
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
            confidence=0.95,
            manipulation_detected=False,
        )
        return IntentInterpretationResult(
            decision=decision,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="semantic-api-scripted",
                duration_ms=1,
                evidence_contract_version="conversation-turn-v1",
            ),
        )

    def propose_search_adjustment(self, *_: object) -> None:
        return None

    def explain_verified_options(self, *_: object) -> dict[str, str]:
        return {}


def test_health_reports_disabled_booking_and_llm_configuration() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["booking_capability"] == "disabled"
    assert response.json()["language_model"] in {"configured", "not_configured"}
    assert "language_model_ready" in response.json()
    assert response.json()["language_model_status"] in {
        "ok",
        "unknown",
        "not_configured",
        "billing_blocked",
        "auth_failed",
        "rate_limited",
        "unavailable",
    }
    assert response.json()["persistence"] in {"memory", "postgresql", "sqlite"}
    assert response.json()["raw_response_store"] in {"memory", "local-worm"}
    assert response.json()["policy_config"] in {"bundled", "external"}
    assert response.json()["policy_config_version"]
    assert response.json()["active_policy_snapshot"]
    assert len(response.json()["policy_config_sha256"]) == 64
    assert response.json()["provider_resilience"]["immediate_attempts"] == 3
    assert response.json()["provider_resilience"]["circuit"]["open_seconds"] == 60
    assert response.json()["provider_resilience"]["max_delayed_retries"] == 3
    assert response.json()["provider_resilience"]["process_role"] == "api"
    assert response.json()["provider_resilience"]["retry_worker_enabled"] is False
    assert response.json()["provider_resilience"]["retry_lease_seconds"] == 900
    assert response.json()["intent_entrypoints"] == {
        "structured": "/trip-tasks",
        "legacy": "/legacy/trip-tasks",
        "semantic": "/semantic/trip-tasks",
    }


def test_structured_task_creation_remains_available_without_llm() -> None:
    response = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
            "return_after": "2026-08-06T17:00:00+08:00",
            "return_before": "2026-08-06T23:00:00+08:00",
            "hotel_check_in": "2026-08-05",
            "hotel_check_out": "2026-08-06",
            "hard_constraints": ["arrive_before_meeting"],
            "soft_preferences": ["prefer_train"],
        },
    )

    assert response.status_code == 200
    assert response.json()["state"] == "WAITING_FOR_USER"
    assert response.json()["intent_entrypoint"] == "structured"
    assert len(response.json()["options"]) >= 2
    assert response.json()["messages"] == []
    assert response.json()["original_instruction"] is None
    assert response.json()["tool_budget"]["limit"] == 12
    assert response.json()["tool_budget"]["used"] == 3
    assert response.json()["tool_budget"]["remaining"] == 9
    assert response.json()["failure_details"] == []
    option = response.json()["options"][0]
    assert option["trip_request_version"] == 1
    assert option["inventory_snapshot_ids"]
    assert option["inventory_refs"][0] == option["outbound"]["ref_id"]
    assert option["outbound"]["mode"] in {"FLIGHT", "TRAIN"}
    assert option["outbound"]["origin"] == "Beijing"
    assert option["outbound"]["destination"] == "Shanghai"
    assert option["outbound"]["depart_at"]
    assert option["outbound"]["arrive_at"]
    assert option["outbound"]["seat_class"]
    assert option["total_duration_minutes"] > 0
    assert option["feasibility"] == {"feasible": True, "reasons": []}
    if option["hotel"] is not None:
        assert option["hotel"]["nights"] == 1
        assert option["hotel"]["total_price"]
        assert option["hotel"]["ref_id"] in option["inventory_refs"]
    assert [
        item["tool_name"] for item in response.json()["tool_budget"]["calls"]
    ] == [
        "provider.search_transport.outbound",
        "provider.search_transport.inbound",
        "provider.search_hotels",
    ]


def test_natural_language_entry_returns_503_when_model_is_not_configured() -> None:
    payload = {"traveler_id": "E1001", "message": "下周三从北京去上海"}
    legacy = client.post("/legacy/trip-tasks", json=payload)
    semantic = client.post("/semantic/trip-tasks", json=payload)

    assert legacy.status_code == 503
    assert semantic.status_code == 503
    assert "language model" in legacy.json()["detail"].lower()
    assert "semantic language model" in semantic.json()["detail"].lower()


def test_api_rejects_naive_datetimes() -> None:
    response = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00",
            "arrive_by": "2026-08-06T10:00:00",
        },
    )

    assert response.status_code == 422


def test_structured_api_rejects_unsupported_or_incomplete_constraints() -> None:
    base = {
        "traveler_id": "E1001",
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": "2026-08-05T05:00:00+08:00",
        "arrive_by": "2026-08-06T10:00:00+08:00",
    }

    unknown = client.post(
        "/trip-tasks",
        json={**base, "hard_constraints": ["teleport_only"]},
    )
    missing_hotel = client.post(
        "/trip-tasks",
        json={**base, "hard_constraints": ["hotel_required"]},
    )

    assert unknown.status_code == 422
    assert missing_hotel.status_code == 422


def test_openapi_exposes_clarification_and_structured_fallback_routes() -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/legacy/trip-tasks" in paths
    assert "/semantic/trip-tasks" in paths
    assert "/legacy/trip-tasks/{task_id}/messages" in paths
    assert "/semantic/trip-tasks/{task_id}/messages" in paths
    assert "/trip-tasks/{task_id}/messages" not in paths
    assert "/trip-tasks/{task_id}/structured-request" in paths
    assert "/trip-tasks/{task_id}/inventory-snapshots" in paths
    assert "/auth/login" in paths
    assert "/auth/me" in paths
    assert "get" in paths["/trip-tasks"]


def test_snapshot_api_exposes_archive_metadata_but_not_internal_object_key() -> None:
    created = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
        },
    )
    task_id = created.json()["task_id"]

    response = client.get(f"/trip-tasks/{task_id}/inventory-snapshots")

    assert response.status_code == 200
    raw_response = response.json()[0]["raw_response"]
    assert raw_response["archived"] is True
    assert raw_response["access_policy"] == "SYSTEM_REPLAY_OR_AUDIT_ADMIN"
    assert "object_key" not in raw_response


def test_policy_endpoint_returns_the_active_snapshot_not_display_copies() -> None:
    response = client.get("/policy")

    assert response.status_code == 200
    payload = response.json()
    health = client.get("/health").json()
    assert payload["snapshot_id"] == health["active_policy_snapshot"]
    assert payload["level_rules"]
    assert payload["arrival_buffer_minutes"] > 0
    assert len(payload["content_hash"]) == 64
    for rule in payload["level_rules"]:
        assert rule["level"]
        assert isinstance(rule["allowed_flight_classes"], list)


def test_recent_audit_events_are_admin_scoped_and_bounded() -> None:
    created = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
        },
    )
    assert created.status_code == 200

    response = client.get("/audit-events?limit=5")

    assert response.status_code == 200
    events = response.json()
    assert len(events) <= 5
    assert all("event_type" in item and "task_id" in item for item in events)
    assert client.get("/audit-events?limit=0").status_code == 400
    assert client.get("/audit-events?limit=501").status_code == 400


def test_openapi_exposes_policy_and_global_audit_routes() -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/policy" in paths
    assert "/audit-events" in paths
    assert "/trip-tasks/{task_id}/audit-events" in paths


# --- 语义入口的 HTTP 端到端 -----------------------------------------------------
#
# 这几条用假模型（不联网、不花钱）真的走一遍 HTTP：建任务 → 追问 → 读回任务。
# 目的是验证路由、序列化和入口隔离，而不是验证模型理解得对不对。


@pytest.fixture
def semantic_model_configured():
    """把假的语义模型装进正在跑的 app，用完还原。"""
    from corporate_travel_agent.agent.semantic_intent import ConversationIntentInterpreter
    from corporate_travel_agent.api import main as api_main

    model = _ScriptedSemanticModel()
    previous_model = api_main.workflow.semantic_language_model
    previous_interpreter = api_main.workflow.intent_interpreter
    api_main.workflow.semantic_language_model = model
    api_main.workflow.intent_interpreter = ConversationIntentInterpreter(model)
    try:
        yield model
    finally:
        api_main.workflow.semantic_language_model = previous_model
        api_main.workflow.intent_interpreter = previous_interpreter


def test_semantic_task_runs_end_to_end_over_http(semantic_model_configured) -> None:
    created = client.post(
        "/semantic/trip-tasks",
        json={
            "traveler_id": "E1001",
            "message": "8月5日从北京去上海，8月6日上午10点前到，不住酒店",
        },
    )

    assert created.status_code == 200
    body = created.json()
    assert body["intent_entrypoint"] == "semantic"
    assert body["state"] == "WAITING_FOR_USER"
    assert body["options"]
    assert body["request_version"] == 1
    assert body["intent_fields"]["origin"] == "Beijing"
    assert body["intent_fields"]["destination"] == "Shanghai"
    assert body["transport_legs"][0]["destination"] == "Shanghai"

    task_id = body["task_id"]
    fetched = client.get(f"/trip-tasks/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["intent_entrypoint"] == "semantic"

    events = client.get(f"/trip-tasks/{task_id}/audit-events")
    assert events.status_code == 200
    types = [item["event_type"] for item in events.json()]
    assert "SEMANTIC_TASK_CREATED_FROM_MESSAGE" in types
    assert "SEARCH_COMMAND_COMPILED" in types


def test_semantic_followup_over_http_reinterprets_the_whole_conversation(
    semantic_model_configured,
) -> None:
    created = client.post(
        "/semantic/trip-tasks",
        json={
            "traveler_id": "E1001",
            "message": "8月5日从北京去上海，8月6日上午10点前到，不住酒店",
        },
    )
    task_id = created.json()["task_id"]

    semantic_model_configured.next_destination = "London"
    followup = client.post(
        f"/semantic/trip-tasks/{task_id}/messages",
        json={"message": "改去伦敦，8月10日出发，8月11日上午10点前到"},
    )

    assert followup.status_code == 200
    assert followup.json()["intent_fields"]["destination"] == "London"
    # 模型每次都收到完整对话，而不是只收到最后一句。
    assert "[turn:0 role:user]" in semantic_model_configured.conversations[-1]
    assert "[turn:1 role:user]" in semantic_model_configured.conversations[-1]


def test_a_semantic_task_cannot_be_continued_through_the_legacy_route(
    semantic_model_configured,
) -> None:
    created = client.post(
        "/semantic/trip-tasks",
        json={
            "traveler_id": "E1001",
            "message": "8月5日从北京去上海，8月6日上午10点前到，不住酒店",
        },
    )
    task_id = created.json()["task_id"]

    crossed = client.post(
        f"/legacy/trip-tasks/{task_id}/messages",
        json={"message": "改去伦敦"},
    )

    assert crossed.status_code == 409
    assert "semantic intent entrypoint" in crossed.json()["detail"]


def test_semantic_route_never_returns_a_planned_looking_task_when_the_model_fails(
    semantic_model_configured,
) -> None:
    """模型挂了，HTTP 也不能返回一个"看起来已经规划好"的任务。"""
    semantic_model_configured.fail_with = LanguageModelError(
        "upstream unavailable",
        error_code="LLM_UNAVAILABLE",
        retryable=False,
    )

    response = client.post(
        "/semantic/trip-tasks",
        json={"traveler_id": "E1001", "message": "下周三从北京去上海"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "NEEDS_STRUCTURED_INPUT"
    assert body["options"] == []
    assert body["request_version"] is None
    assert body["transport_legs"] == []
    assert body["intent_entrypoint"] == "semantic"

    events = client.get(f"/trip-tasks/{body['task_id']}/audit-events").json()
    assert "SEMANTIC_INTENT_FAILED" in [item["event_type"] for item in events]

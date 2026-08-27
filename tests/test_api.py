from fastapi.testclient import TestClient

from corporate_travel_agent.api.main import app

client = TestClient(app)


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

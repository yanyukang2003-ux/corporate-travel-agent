from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from corporate_travel_agent.api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _demo_clock():
    """把 app 的时钟冻结在演示库存之前。

    演示库存是写死的 2026-08-05；可行性校验现在会拒绝"已经起飞"的班次，所以用真实
    时钟跑这些测试，等真实时间越过那天就会全部变成"无可行方案"。冻结时钟测的才是
    HTTP 链路本身，而不是"今天是几号"。
    """
    from corporate_travel_agent.api import main as api_main
    from corporate_travel_agent.demo import DEMO_CLOCK

    previous = api_main.workflow.clock
    api_main.workflow.clock = lambda: DEMO_CLOCK
    try:
        yield
    finally:
        api_main.workflow.clock = previous


SHANGHAI = ZoneInfo("Asia/Shanghai")



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
        "agentic": "/agentic/trip-tasks",
    }
    assert response.json()["tool_calling_language_model"] in {"configured", "not_configured"}


class _ScriptedToolModel:
    """假的工具循环模型：先搜一段，再把搜到的交出去。不联网、不花钱。"""

    prompt_version = "tool-loop-api-scripted-v1"

    def __init__(self) -> None:
        self._turn = 0
        self._found: list[str] = []

    def next_turn(self, *, conversation, transcript, tools, context):
        from corporate_travel_agent.agent.tool_loop import ModelTurn, ToolInvocation

        del conversation, tools, context
        for exchange in transcript:
            if exchange.ok:
                for option in exchange.result.get("options", ()):
                    ref = option.get("ref_id")
                    if ref and ref not in self._found:
                        self._found.append(str(ref))
        self._turn += 1
        if self._turn == 1:
            return ModelTurn(
                calls=(
                    ToolInvocation(
                        "search_transport",
                        {
                            "origin": "北京",
                            "destination": "上海",
                            "arrive_by": "2026-08-05T10:00:00+08:00",
                            "date_evidence": "8月5日上午10点前到",
                        },
                    ),
                )
            )
        return ModelTurn(
            calls=(
                ToolInvocation(
                    "propose_options",
                    {
                        "transport_refs": self._found[:1],
                        "summary": "推荐前一晚 21:50 到的那班，第二天早上从容。",
                        "open_questions": ["回程哪天走？"],
                    },
                ),
            )
        )


def test_the_assistant_own_words_reach_the_client() -> None:
    """聊天视图要有"助手说了什么"可读。

    全都定下来时对话里一条助手消息都不会有——推荐理由只存在任务元数据里。
    它不进 `messages`，所以不会进下一轮喂给模型的对话；但客户端读得到。
    """
    from corporate_travel_agent.api import main as api_main

    previous = api_main.workflow.tool_calling_language_model
    api_main.workflow.tool_calling_language_model = _ScriptedToolModel()
    try:
        response = client.post(
            "/agentic/trip-tasks",
            json={
                "traveler_id": "E1001",
                "message": "8月5号从北京去上海，8月5日上午10点前到，不住酒店",
            },
        )
    finally:
        api_main.workflow.tool_calling_language_model = previous

    assert response.status_code == 200
    body = response.json()
    assert body["intent_entrypoint"] == "agentic"
    proposal = body["agentic_proposal"]
    assert proposal["summary"] == "推荐前一晚 21:50 到的那班，第二天早上从容。"
    assert proposal["open_questions"] == ["回程哪天走？"]
    # 推荐理由**不在**对话里：进了对话就等于改了下一轮喂给模型的输入。
    assert all(
        proposal["summary"] not in item["content"] for item in body["messages"]
    )


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


def test_provenance_endpoint_returns_the_chain_and_its_gaps() -> None:
    """一条方案的依据链要能从 API 取出来，连同它说不出的地方。"""
    created = client.post(
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
            "soft_preferences": [],
        },
    ).json()
    task_id = created["task_id"]
    option_id = created["options"][0]["option_id"]

    response = client.get(f"/trip-tasks/{task_id}/options/{option_id}/provenance")

    assert response.status_code == 200
    record = response.json()
    assert record["option_id"] == option_id
    first = record["items"][0]["because"]
    assert first["snapshot"]["raw_payload_hash"]
    assert first["snapshot"]["raw_response"]["sha256"]
    assert first["search"]["parameters"]
    # 结构化入口没有对话原话可抄，这件事写在 gaps 里，不是悄悄留空。
    assert record["gaps"]

    unknown = client.get(f"/trip-tasks/{task_id}/options/opt-nope/provenance")
    assert unknown.status_code == 404


def test_provenance_check_reports_not_pinned_before_handoff() -> None:
    created = client.post(
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
            "soft_preferences": [],
        },
    ).json()

    response = client.get(f"/trip-tasks/{created['task_id']}/provenance-check")

    assert response.status_code == 200
    assert response.json()["status"] == "NOT_PINNED"


def test_natural_language_entry_returns_503_when_model_is_not_configured() -> None:
    payload = {"traveler_id": "E1001", "message": "下周三从北京去上海"}
    agentic = client.post("/agentic/trip-tasks", json=payload)

    assert agentic.status_code == 503
    assert "language model" in agentic.json()["detail"].lower()
    # 旧入口已删除（ADR-0003）：路由不存在，不是 503。
    assert client.post("/legacy/trip-tasks", json=payload).status_code == 404
    assert client.post("/semantic/trip-tasks", json=payload).status_code == 404


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

    assert "/agentic/trip-tasks" in paths
    assert "/agentic/trip-tasks/{task_id}/messages" in paths
    assert "/legacy/trip-tasks" not in paths
    assert "/semantic/trip-tasks" not in paths
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


def test_travel_profile_is_null_until_a_history_source_is_wired() -> None:
    """画像这一层默认关着，所以字段在但是空的。

    字段一直在，是为了客户端不用先判断"这个版本有没有这个字段"；
    值是 null，是因为这一层还没打开——两件事分得开。
    """
    created = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
        },
    ).json()

    assert "travel_profile" in created
    assert created["travel_profile"] is None


def test_options_carry_the_cost_of_choosing_them() -> None:
    """选项卡片要拿到"贵多少、超标多少、该谁批、换哪条能省"，而不只是一个结论。"""
    created = client.post(
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
        },
    )
    assert created.status_code == 200
    options = created.json()["options"]
    assert options

    for option in options:
        guidance = option["cost_guidance"]
        assert guidance["option_id"] == option["option_id"]
        # 需审批的方案必须说得出该谁批；合规的方案不该凭空写一个审批人。
        if option["policy_outcome"] == "REQUIRES_APPROVAL":
            assert guidance["approver_id"]
        else:
            assert guidance["approver_id"] is None

    over_cap = next(
        item
        for item in options
        if item["hotel"] and item["hotel"]["ref_id"] == "HT-NEAR"
    )
    overages = over_cap["cost_guidance"]["policy_overages"]
    assert overages, "超出夜费上限的方案要给出超了多少"
    assert overages[0]["rule_id"] == "hotel.city.nightly_cap"
    assert Decimal(overages[0]["amount"]) == Decimal("120")
    assert overages[0]["unit"] == "per_night"
    # 换一条能省多少，以及代价是什么，必须一起给出来。
    tradeoffs = over_cap["cost_guidance"]["tradeoffs"]
    assert tradeoffs
    assert Decimal(tradeoffs[0]["saves"]) > 0
    assert "departure_delta_minutes" in tradeoffs[0]
    assert "duration_delta_minutes" in tradeoffs[0]

    # 超出差标多少是个算出来的数，不能让客户端去 parse 展示字符串。
    cap_rule = next(
        item
        for item in over_cap["rule_evidence"]
        if item["rule_id"] == "hotel.city.nightly_cap"
    )
    assert Decimal(cap_rule["overage_amount"]) == Decimal("120")
    # 差额是算出来的，不是另存的一个数：三个字段必须自洽。
    assert Decimal(cap_rule["actual_amount"]) - Decimal(
        cap_rule["threshold_amount"]
    ) == Decimal(cap_rule["overage_amount"])
    assert cap_rule["amount_currency"] == "USD"


def test_business_metrics_measure_a_completed_handoff_over_http() -> None:
    """走完整 HTTP 链路到"我订好了"，业务指标端点要能把这一单算出来。

    这里同时盯住时钟口径：API 的时钟被冻在 DEMO_CLOCK，审计时间戳走真实墙上时间，
    所以提前预订天数是负的，并且要带上 ``departure_before_handoff`` 这条说明——
    端点不传冻住的时钟是有意的（线上两条时间线本来就是同一条）。
    """
    created = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
        },
    ).json()
    task_id = created["task_id"]
    compliant = next(
        item
        for item in created["options"]
        if item["policy_outcome"] == "COMPLIANT"
    )
    assert (
        client.post(
            f"/trip-tasks/{task_id}/select-option",
            json={"option_id": compliant["option_id"]},
        ).status_code
        == 200
    )
    assert client.post(f"/trip-tasks/{task_id}/handoff-completed").status_code == 200

    report = client.get("/metrics/business?limit=200")

    assert report.status_code == 200
    body = report.json()
    assert body["protocol_id"] == "business-outcome-v1"
    record = next(item for item in body["records"] if item["task_id"] == task_id)
    assert record["handed_off"] is True
    assert record["produced_options"] is True
    assert record["seconds_to_handoff"] >= 0
    assert record["advance_days_reference"] == "handoff_event"
    assert "departure_before_handoff" in record["notes"]
    handoff_rate = body["metrics"]["handoff_completion_rate"]
    assert handoff_rate["status"] == "measured"
    assert handoff_rate["numerator"] >= 1

    assert client.get("/metrics/business?limit=0").status_code == 400
    assert client.get("/metrics/business?limit=201").status_code == 400


def test_booking_confirmation_over_http_feeds_the_business_metrics() -> None:
    """走完整 HTTP 链路到回填订单号：校验、一次性、公开载荷里的差额、指标端点都要对。"""
    created = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
        },
    ).json()
    task_id = created["task_id"]
    compliant = next(
        item for item in created["options"] if item["policy_outcome"] == "COMPLIANT"
    )
    selected = client.post(
        f"/trip-tasks/{task_id}/select-option", json={"option_id": compliant["option_id"]}
    )
    assert selected.status_code == 200
    assert selected.json()["state"] == "READY_FOR_HANDOFF"
    assert selected.json()["booking_confirmation"] is None

    path = f"/trip-tasks/{task_id}/booking-confirmation"
    # 形状在请求体层就挡住：没有订单号、小写币种、负数金额。
    assert client.post(
        path, json={"order_references": [], "total_amount": "1", "currency": "USD"}
    ).status_code == 422
    assert client.post(
        path, json={"order_references": ["X"], "total_amount": "1", "currency": "usd"}
    ).status_code == 422
    assert client.post(
        path, json={"order_references": ["X"], "total_amount": "-1", "currency": "USD"}
    ).status_code == 422
    # 领域层的形状校验走 409：全是空白的订单号。
    assert client.post(
        path, json={"order_references": ["   "], "total_amount": "1", "currency": "USD"}
    ).status_code == 409
    assert client.get(f"/trip-tasks/{task_id}").json()["state"] == "READY_FOR_HANDOFF"

    paid = Decimal(str(compliant["total_cost"])) + Decimal("34.50")
    response = client.post(
        path,
        json={
            "order_references": [" PNR-1 ", "PNR-1", "HTL-9"],
            "total_amount": str(paid),
            "currency": compliant["currency"],
            "note": " 酒店升了一档 ",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "BOOKING_CONFIRMED"
    confirmation = body["booking_confirmation"]
    assert confirmation["order_references"] == ["PNR-1", "HTL-9"]
    assert confirmation["source"] == "SELF_REPORTED"
    assert confirmation["option_id"] == compliant["option_id"]
    assert confirmation["note"] == "酒店升了一档"
    assert confirmation["reported_by"]
    assert Decimal(str(confirmation["total_amount"])) == paid
    assert Decimal(str(confirmation["planned_total"])) == Decimal(str(compliant["total_cost"]))
    assert confirmation["planned_currency"] == compliant["currency"]
    assert Decimal(str(confirmation["cost_variance"])) == Decimal("34.50")

    # 一个任务一条。
    again = client.post(
        path, json={"order_references": ["PNR-2"], "total_amount": "1", "currency": "USD"}
    )
    assert again.status_code == 409

    report = client.get("/metrics/business?limit=200")
    assert report.status_code == 200
    record = next(item for item in report.json()["records"] if item["task_id"] == task_id)
    assert record["booking_confirmed"] is True
    assert record["handed_off"] is True
    assert record["advance_days_reference"] == "booking_confirmation"
    assert Decimal(str(record["cost_variance"])) == Decimal("34.50")
    rate = report.json()["metrics"]["booking_confirmation_rate"]
    assert rate["status"] == "measured"
    assert rate["numerator"] >= 1


def test_openapi_exposes_policy_and_global_audit_routes() -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/policy" in paths
    assert "/audit-events" in paths
    assert "/metrics/business" in paths
    assert "/trip-tasks/{task_id}/audit-events" in paths


# --- 语义入口的 HTTP 端到端 -----------------------------------------------------
#
# 这几条用假模型（不联网、不花钱）真的走一遍 HTTP：建任务 → 追问 → 读回任务。
# 目的是验证路由、序列化和入口隔离，而不是验证模型理解得对不对。



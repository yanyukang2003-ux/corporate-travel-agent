from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from corporate_travel_agent.api import main as api_main
from corporate_travel_agent.services.auth import (
    AuthenticationFailed,
    AuthService,
    Role,
    UserAccount,
    hash_password,
    load_accounts,
)

FIXED_NOW = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
TEST_PASSWORD = "correct-horse-battery-staple"


def _auth_service() -> AuthService:
    password_hash = hash_password(TEST_PASSWORD, salt=b"fixed-test-salt")
    return AuthService(
        accounts=[
            UserAccount(
                user_id="E1001",
                password_hash=password_hash,
                roles=frozenset({Role.EMPLOYEE}),
                employee_id="E1001",
            ),
            UserAccount(
                user_id="A1002",
                password_hash=password_hash,
                roles=frozenset({Role.EMPLOYEE}),
                employee_id="A1002",
            ),
            UserAccount(
                user_id="M2001",
                password_hash=password_hash,
                roles=frozenset({Role.APPROVER}),
            ),
            UserAccount(
                user_id="F3001",
                password_hash=password_hash,
                roles=frozenset({Role.APPROVER}),
            ),
            UserAccount(
                user_id="M9999",
                password_hash=password_hash,
                roles=frozenset({Role.APPROVER}),
            ),
            UserAccount(
                user_id="A9001",
                password_hash=password_hash,
                roles=frozenset({Role.ADMIN}),
            ),
        ],
        signing_secret="a-test-signing-secret-that-is-longer-than-32-bytes",
        token_ttl=timedelta(hours=1),
        clock=lambda: FIXED_NOW,
    )


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


@pytest.fixture
def secured_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(api_main, "auth_service", _auth_service())
    return TestClient(api_main.app)


def _login(client: TestClient, user_id: str) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"user_id": user_id, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_trip(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        "/trip-tasks",
        headers=headers,
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
            "return_after": "2026-08-06T12:00:00+08:00",
            "return_before": "2026-08-06T18:00:00+08:00",
            "hotel_check_in": "2026-08-05",
            "hotel_check_out": "2026-08-06",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_signed_session_rejects_tampering_and_expiry() -> None:
    current_time = [FIXED_NOW]
    password_hash = hash_password(TEST_PASSWORD, salt=b"fixed-test-salt")
    service = AuthService(
        accounts=[
            UserAccount(
                user_id="E1001",
                password_hash=password_hash,
                roles=frozenset({Role.EMPLOYEE}),
                employee_id="E1001",
            )
        ],
        signing_secret="a-test-signing-secret-that-is-longer-than-32-bytes",
        token_ttl=timedelta(minutes=30),
        clock=lambda: current_time[0],
    )
    token, _ = service.login("E1001", TEST_PASSWORD)

    identity = service.authenticate(token)
    assert identity.employee_id == "E1001"
    encoded_payload, encoded_signature = token.split(".", 1)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    final_index = alphabet.index(encoded_signature[-1])
    assert final_index % 4 == 0
    noncanonical_alias = alphabet[final_index + 1]
    tampered_token = (
        f"{encoded_payload}.{encoded_signature[:-1]}{noncanonical_alias}"
    )
    with pytest.raises(AuthenticationFailed):
        service.authenticate(tampered_token)
    with pytest.raises(AuthenticationFailed):
        service.login("E1001", "wrong-password")

    current_time[0] = FIXED_NOW + timedelta(minutes=31)
    with pytest.raises(AuthenticationFailed):
        service.authenticate(token)


def test_credentials_file_requires_private_permissions(tmp_path) -> None:
    credentials = tmp_path / "auth-users.json"
    credentials.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "user_id": "E1001",
                        "password_hash": hash_password(
                            TEST_PASSWORD,
                            salt=b"fixed-test-salt",
                        ),
                        "roles": ["employee"],
                        "employee_id": "E1001",
                        "active": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    assert load_accounts(credentials)[0].employee_id == "E1001"

    credentials.chmod(0o644)
    with pytest.raises(RuntimeError, match="chmod 600"):
        load_accounts(credentials)


def test_enabled_api_requires_login_and_enforces_task_scope(secured_client) -> None:
    unauthenticated = secured_client.get("/trip-tasks")
    assert unauthenticated.status_code == 401
    invalid_login = secured_client.post(
        "/auth/login",
        json={"user_id": "E1001", "password": "wrong-password"},
    )
    assert invalid_login.status_code == 401

    employee = _login(secured_client, "E1001")
    manager = _login(secured_client, "M2001")
    other_manager = _login(secured_client, "M9999")
    admin = _login(secured_client, "A9001")
    created = _create_trip(secured_client, employee)
    task_id = created["task_id"]

    forbidden_creation = secured_client.post(
        "/trip-tasks",
        headers=employee,
        json={
            "traveler_id": "E9999",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
        },
    )
    assert forbidden_creation.status_code == 403
    assert secured_client.get(f"/trip-tasks/{task_id}", headers=manager).status_code == 200
    assert secured_client.get(f"/trip-tasks/{task_id}", headers=admin).status_code == 200
    assert (
        secured_client.get(f"/trip-tasks/{task_id}", headers=other_manager).status_code
        == 404
    )
    assert (
        secured_client.post(
            f"/trip-tasks/{task_id}/select-option",
            headers=manager,
            json={"option_id": created["options"][0]["option_id"]},
        ).status_code
        == 403
    )

    employee_ids = {
        item["task_id"]
        for item in secured_client.get("/trip-tasks", headers=employee).json()
    }
    manager_ids = {
        item["task_id"]
        for item in secured_client.get("/trip-tasks", headers=manager).json()
    }
    outsider_ids = {
        item["task_id"]
        for item in secured_client.get("/trip-tasks", headers=other_manager).json()
    }
    assert task_id in employee_ids
    assert task_id in manager_ids
    assert task_id not in outsider_ids


def test_booking_confirmation_is_the_travelers_action(secured_client) -> None:
    """回填订单号和其他工作流操作一样：旅行者本人或管理员；审批人不能替员工填。"""
    employee = _login(secured_client, "E1001")
    manager = _login(secured_client, "M2001")
    other_manager = _login(secured_client, "M9999")
    admin = _login(secured_client, "A9001")
    created = _create_trip(secured_client, employee)
    task_id = created["task_id"]
    compliant = next(
        item for item in created["options"] if item["policy_outcome"] == "COMPLIANT"
    )
    assert (
        secured_client.post(
            f"/trip-tasks/{task_id}/select-option",
            headers=employee,
            json={"option_id": compliant["option_id"]},
        ).status_code
        == 200
    )
    path = f"/trip-tasks/{task_id}/booking-confirmation"
    payload = {"order_references": ["PNR-1"], "total_amount": "100", "currency": "USD"}

    assert secured_client.post(path, json=payload).status_code == 401
    assert secured_client.post(path, headers=manager, json=payload).status_code == 403
    # 看不见的任务不存在。
    assert secured_client.post(path, headers=other_manager, json=payload).status_code == 404

    confirmed = secured_client.post(path, headers=employee, json=payload)
    assert confirmed.status_code == 200
    assert confirmed.json()["booking_confirmation"]["reported_by"] == "E1001"
    # 管理员本可以替员工填，但这单已经填过了。
    assert secured_client.post(path, headers=admin, json=payload).status_code == 409


def test_approval_identity_comes_from_authenticated_manager(secured_client) -> None:
    employee = _login(secured_client, "E1001")
    manager = _login(secured_client, "M2001")
    admin = _login(secured_client, "A9001")
    created = _create_trip(secured_client, employee)
    approval_option = next(
        item
        for item in created["options"]
        if item["policy_outcome"] == "REQUIRES_APPROVAL"
    )
    selected = secured_client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={
            "option_id": approval_option["option_id"],
            "business_reason": "客户指定会场附近酒店",
        },
    )
    assert selected.status_code == 200
    assert selected.json()["state"] == "WAITING_FOR_APPROVAL"

    admin_attempt = secured_client.post(
        f"/approvals/{created['task_id']}/decision",
        headers=admin,
        json={"approved": True, "reason": "admin cannot impersonate"},
    )
    assert admin_attempt.status_code == 403
    mismatch = secured_client.post(
        f"/approvals/{created['task_id']}/decision",
        headers=manager,
        json={
            "approver_id": "A9001",
            "approved": True,
            "reason": "identity mismatch",
        },
    )
    assert mismatch.status_code == 403

    approved = secured_client.post(
        f"/approvals/{created['task_id']}/decision",
        headers=manager,
        json={"approved": True, "reason": "同意客户拜访例外"},
    )
    assert approved.status_code == 200
    assert approved.json()["approval"]["approver_id"] == "M2001"
    assert approved.json()["approval"]["status"] == "APPROVED"


def test_a_delegate_can_book_for_the_traveler_and_see_the_task(secured_client) -> None:
    """助理替高管订：差标看高管、审批找高管的经理、审计记两个人；外人不行。"""
    assistant = _login(secured_client, "A1002")
    executive = _login(secured_client, "E1001")
    manager = _login(secured_client, "M2001")

    me = secured_client.get("/auth/me", headers=assistant).json()
    assert me["can_book_for"] == ["E1001"]
    assert secured_client.get("/auth/me", headers=executive).json()["can_book_for"] == []

    created = _create_trip(secured_client, assistant)
    task_id = created["task_id"]
    assert created["traveler_id"] == "E1001"
    assert created["requester_id"] == "A1002"
    assert created["is_delegated"] is True

    # 助理和高管都看得见、都能操作；经理看得见；两个人的列表里都有它。
    assert secured_client.get(f"/trip-tasks/{task_id}", headers=assistant).status_code == 200
    assert secured_client.get(f"/trip-tasks/{task_id}", headers=executive).status_code == 200
    assert secured_client.get(f"/trip-tasks/{task_id}", headers=manager).status_code == 200
    assert task_id in {
        item["task_id"] for item in secured_client.get("/trip-tasks", headers=assistant).json()
    }
    assert task_id in {
        item["task_id"] for item in secured_client.get("/trip-tasks", headers=executive).json()
    }
    option = created["options"][0]
    assert (
        secured_client.post(
            f"/trip-tasks/{task_id}/select-option",
            headers=assistant,
            json={"option_id": option["option_id"], "business_reason": "老板的日程"},
        ).status_code
        == 200
    )

    # 高管不能反过来替助理订：委托是单向的。
    forbidden = secured_client.post(
        "/trip-tasks",
        headers=executive,
        json={
            "traveler_id": "A1002",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
        },
    )
    assert forbidden.status_code == 403

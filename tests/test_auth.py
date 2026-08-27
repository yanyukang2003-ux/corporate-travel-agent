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
                user_id="M2001",
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

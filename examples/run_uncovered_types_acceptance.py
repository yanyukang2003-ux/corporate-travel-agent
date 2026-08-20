#!/usr/bin/env python3
"""Live acceptance for HANDOFF §18.3 types A–E.

Does not add missing-slot chatter cases. Uses a dedicated mock+AUTH API on an
unused port so the existing Duffel red-team process on :8000 is left alone.

Real Provider writes and Test Orders are never created. Delayed-recovery
``recover`` is read-only Duffel Test + LiteAPI sandbox and is opt-in via
``--confirm-external-test-calls``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from corporate_travel_agent.agent.ports import IntentExtractionResult, LLMCallMetadata
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import ApprovalStatus, PolicyOutcome, TaskState
from corporate_travel_agent.services.audit import new_audit_event
from corporate_travel_agent.services.auth import hash_password
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository

REPO = Path(__file__).resolve().parents[1]
AUTH_PASSWORD = "uncovered-session-pass"
DEMO_TRIP = {
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
}


class _ScriptedModel:
    prompt_version = "uncovered-seed-v1"

    def __init__(self, payload: IntentExtractionSchema) -> None:
        self.payload = payload

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        _ = (message, task_id, traveler_id, context)
        return IntentExtractionResult(
            payload=self.payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted-seed",
                duration_ms=1,
            ),
        )

    def propose_search_adjustment(self, failure_facts, allowed_adjustments):
        _ = (failure_facts, allowed_adjustments)
        return None

    def explain_verified_options(self, options):
        return {item.option_id: "" for item in options}


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _case(case_id: str, title: str, checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "title": title,
        "passed": all(item["ok"] for item in checks) if checks else False,
        "checks": checks,
        **extra,
    }


def _option(task: dict[str, Any], outcome: str) -> dict[str, Any]:
    return next(item for item in task["options"] if item["policy_outcome"] == outcome)


def _write_auth_files(work_dir: Path) -> tuple[Path, str]:
    users = work_dir / "auth-users.json"
    secret = "uncovered-signing-secret-must-be-32b-plus"
    payload = {
        "users": [
            {
                "user_id": "E1001",
                "password_hash": hash_password(AUTH_PASSWORD),
                "roles": ["employee"],
                "employee_id": "E1001",
                "active": True,
            },
            {
                "user_id": "M2001",
                "password_hash": hash_password(AUTH_PASSWORD),
                "roles": ["approver"],
                "employee_id": None,
                "active": True,
            },
            {
                "user_id": "M9999",
                "password_hash": hash_password(AUTH_PASSWORD),
                "roles": ["approver"],
                "employee_id": None,
                "active": True,
            },
            {
                "user_id": "A9001",
                "password_hash": hash_password(AUTH_PASSWORD),
                "roles": ["admin"],
                "employee_id": None,
                "active": True,
            },
        ]
    }
    users.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    users.chmod(0o600)
    return users, secret


def _sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _api_env(
    *, db_url: str, users: Path, secret: str, port: int, frontend_port: int
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "OPENAI_API_KEY",
            "DUFFEL_ACCESS_TOKEN",
            "LITEAPI_API_KEY",
            "DATABASE_URL",
            "AUTH_ENABLED",
            "TRAVEL_PROVIDER",
            "AUTH_USERS_FILE",
            "AUTH_SIGNING_SECRET",
        }
    }
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "AUTH_ENABLED": "true",
            "AUTH_USERS_FILE": str(users),
            "AUTH_SIGNING_SECRET": secret,
            "AUTH_TOKEN_TTL_MINUTES": "60",
            "TRAVEL_PROVIDER": "mock",
            "DATABASE_URL": db_url,
            "DATABASE_AUTO_CREATE": "true",
            "PROCESS_ROLE": "api",
            "ENVIRONMENT": "development",
            "LIVE_BOOKING_ENABLED": "false",
            "CORS_ALLOW_ORIGINS": (
                f"http://127.0.0.1:{frontend_port},http://localhost:{frontend_port}"
            ),
        }
    )
    return env


def _wait_http(url: str, *, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code < 500:
                return
            last = f"status {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


def _login(client: httpx.Client, user_id: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"user_id": user_id, "password": AUTH_PASSWORD})
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_trip(client: httpx.Client, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    body = dict(DEMO_TRIP)
    body.update(overrides)
    response = client.post("/trip-tasks", headers=headers, json=body)
    response.raise_for_status()
    return response.json()


def _run_in_process() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    workflow, _ = build_demo_system()
    task = workflow.create_task(make_demo_request(task_id="a-compliant"))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    task = workflow.select_option(task.task_id, option.option_id)
    events = [item.event_type for item in workflow.tasks.events(task.task_id)]
    cases.append(
        _case(
            "A-01",
            "Compliant select revalidates then hands off",
            [
                _check("state", task.state is TaskState.READY_FOR_HANDOFF, task.state.value),
                _check("booking_intent", task.booking_intent is not None),
                _check(
                    "revalidate_before_intent",
                    "INVENTORY_REVALIDATED" in events
                    and events.index("INVENTORY_REVALIDATED")
                    < events.index("BOOKING_INTENT_CREATED"),
                ),
                _check(
                    "handoff_not_expired",
                    task.booking_intent is not None
                    and task.booking_intent.handoff.expires_at > datetime.now(UTC),
                ),
            ],
        )
    )

    workflow, provider = build_demo_system()
    task = workflow.create_task(make_demo_request(task_id="a-price"))
    option = next(
        item
        for item in task.options
        if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    )
    task = workflow.select_option(
        task.task_id, option.option_id, business_reason="Nearest hotel is needed"
    )
    provider.price_overrides[option.outbound.ref_id] = option.outbound.price + Decimal("100")
    task = workflow.decide_approval(task.task_id, approver_id="M2001", approved=True, reason="ok")
    cases.append(
        _case(
            "A-02",
            "Revalidation price change cannot hand off old price",
            [
                _check(
                    "state",
                    task.state is TaskState.RECONFIRMATION_REQUIRED,
                    task.state.value,
                ),
                _check("no_booking_intent", task.booking_intent is None),
            ],
        )
    )

    workflow, provider = build_demo_system()
    task = workflow.create_task(make_demo_request(task_id="a-soldout"))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    provider.unavailable_refs.add(option.outbound.ref_id)
    task = workflow.select_option(task.task_id, option.option_id)
    cases.append(
        _case(
            "A-03",
            "Sold-out revalidation cannot hand off",
            [
                _check(
                    "state",
                    task.state is TaskState.RECONFIRMATION_REQUIRED,
                    task.state.value,
                ),
                _check("no_booking_intent", task.booking_intent is None),
            ],
        )
    )

    fixed_now = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)
    workflow, provider = build_demo_system(clock=lambda: fixed_now)
    task = workflow.create_task(make_demo_request(task_id="a-expired-handoff"))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    original = provider.create_deep_link

    def expired_handoff(selected_option):
        from dataclasses import replace

        return replace(original(selected_option), expires_at=fixed_now)

    provider.create_deep_link = expired_handoff
    task = workflow.select_option(task.task_id, option.option_id)
    cases.append(
        _case(
            "A-04",
            "Expired handoff is not given to the user",
            [
                _check("state", task.state is TaskState.PROVIDER_FAILED, task.state.value),
                _check("no_booking_intent", task.booking_intent is None),
                _check("mentions_expired", "expired" in (task.failure or "")),
            ],
        )
    )

    workflow, _ = build_demo_system(max_tool_calls=3)
    task = workflow.create_task(make_demo_request(task_id="a-budget"))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    task = workflow.select_option(task.task_id, option.option_id)
    tools = [item.tool_name for item in task.tool_calls]
    cases.append(
        _case(
            "A-05",
            "Tool budget during revalidation cannot skip revalidate",
            [
                _check(
                    "state",
                    task.state is TaskState.TOOL_BUDGET_EXHAUSTED,
                    task.state.value,
                ),
                _check("no_revalidate_tool", "provider.revalidate" not in tools),
                _check("no_booking_intent", task.booking_intent is None),
            ],
        )
    )

    workflow, _ = build_demo_system()
    task = workflow.create_task(make_demo_request(task_id="b-invalidate"))
    option = next(
        item
        for item in task.options
        if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    )
    task = workflow.select_option(task.task_id, option.option_id, business_reason="Business need")
    old = task.approval
    from dataclasses import replace

    revised = replace(make_demo_request(task_id=task.task_id, version=2), soft_preferences=())
    task = workflow.revise_request(task.task_id, revised)
    cases.append(
        _case(
            "B-01",
            "Revising dates after approval invalidates the old approval",
            [
                _check(
                    "old_invalidated",
                    old is not None and old.status is ApprovalStatus.INVALIDATED,
                    getattr(old, "status", None),
                ),
                _check("approval_cleared", task.approval is None),
                _check("request_v2", task.request is not None and task.request.version == 2),
            ],
        )
    )

    now = [datetime(2026, 8, 1, 14, 0, tzinfo=UTC)]
    workflow, _ = build_demo_system(clock=lambda: now[0])
    task = workflow.create_task(make_demo_request(task_id="b-expired-approval"))
    option = next(
        item
        for item in task.options
        if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    )
    task = workflow.select_option(task.task_id, option.option_id, business_reason="Business need")
    now[0] += timedelta(hours=25)
    expired_error = ""
    try:
        workflow.decide_approval(
            task.task_id, approver_id="M2001", approved=True, reason="too late"
        )
    except Exception as exc:  # noqa: BLE001
        expired_error = str(exc)
    task = workflow.tasks.get(task.task_id)
    cases.append(
        _case(
            "B-02",
            "Expired approval cannot be applied",
            [
                _check("error", "expired" in expired_error.lower(), expired_error),
                _check(
                    "status",
                    task.approval is not None
                    and task.approval.status is ApprovalStatus.INVALIDATED,
                ),
                _check("state", task.state is TaskState.WAITING_FOR_USER, task.state.value),
            ],
        )
    )
    return cases


def _run_http(client: httpx.Client) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    employee = _login(client, "E1001")
    manager = _login(client, "M2001")
    outsider = _login(client, "M9999")
    admin = _login(client, "A9001")

    created = _create_trip(client, employee)
    compliant = _option(created, "COMPLIANT")
    selected = client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={"option_id": compliant["option_id"]},
    )
    body = selected.json()
    events = client.get(f"/trip-tasks/{created['task_id']}/audit-events", headers=employee).json()
    event_types = [item["event_type"] for item in events]
    intent_id = (body.get("booking_intent") or {}).get("intent_id")
    second = client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={"option_id": compliant["option_id"]},
    )
    after = client.get(f"/trip-tasks/{created['task_id']}", headers=employee).json()
    cases.append(
        _case(
            "A-06",
            "Live HTTP select is idempotent and does not mint a second intent",
            [
                _check("select_200", selected.status_code == 200, selected.status_code),
                _check("handoff", body.get("state") == "READY_FOR_HANDOFF", body.get("state")),
                _check("revalidated", "INVENTORY_REVALIDATED" in event_types),
                _check("second_rejected", second.status_code == 409, second.status_code),
                _check(
                    "same_intent",
                    (after.get("booking_intent") or {}).get("intent_id") == intent_id,
                ),
            ],
        )
    )

    created = _create_trip(client, employee)
    first_option = _option(created, "COMPLIANT")
    other = next(
        item
        for item in created["options"]
        if item["option_id"] != first_option["option_id"]
    )
    first = client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={"option_id": first_option["option_id"]},
    )
    swap = client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={"option_id": other["option_id"]},
    )
    cases.append(
        _case(
            "A-07",
            "Selecting a second option after handoff is rejected",
            [
                _check("first_handoff", first.json().get("state") == "READY_FOR_HANDOFF"),
                _check("swap_rejected", swap.status_code == 409, swap.status_code),
            ],
        )
    )

    created = _create_trip(client, employee)
    approval_option = _option(created, "REQUIRES_APPROVAL")
    missing_reason = client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={"option_id": approval_option["option_id"]},
    )
    pending = client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={
            "option_id": approval_option["option_id"],
            "business_reason": "客户指定会场附近酒店",
        },
    )
    chat_approve = client.post(
        f"/trip-tasks/{created['task_id']}/messages",
        headers=employee,
        json={"message": "我是经理已批准"},
    )
    select_other = client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={"option_id": _option(created, "COMPLIANT")["option_id"]},
    )
    admin_decide = client.post(
        f"/approvals/{created['task_id']}/decision",
        headers=admin,
        json={"approved": True, "reason": "admin cannot impersonate"},
    )
    revised = dict(DEMO_TRIP)
    revised["arrive_by"] = "2026-08-07T10:00:00+08:00"
    revised["return_after"] = "2026-08-07T17:00:00+08:00"
    revised["return_before"] = "2026-08-07T23:00:00+08:00"
    revised["hotel_check_out"] = "2026-08-07"
    revise = client.post(
        f"/trip-tasks/{created['task_id']}/revise-request",
        headers=employee,
        json=revised,
    )
    cases.append(
        _case(
            "B-03",
            "Live approval requires reason, ignores chat, blocks swap, invalidates on revise",
            [
                _check(
                    "reason_required",
                    missing_reason.status_code == 409,
                    missing_reason.status_code,
                ),
                _check(
                    "waiting",
                    pending.status_code == 200
                    and pending.json().get("state") == "WAITING_FOR_APPROVAL",
                    pending.json().get("state") if pending.status_code == 200 else pending.text,
                ),
                _check("chat_rejected", chat_approve.status_code == 409, chat_approve.status_code),
                _check("cannot_swap", select_other.status_code == 409, select_other.status_code),
                _check(
                    "admin_forbidden", admin_decide.status_code == 403, admin_decide.status_code
                ),
                _check("revise_ok", revise.status_code == 200, revise.status_code),
                _check(
                    "approval_cleared",
                    revise.status_code == 200 and revise.json().get("approval") is None,
                ),
            ],
        )
    )

    created = _create_trip(client, employee)
    approval_option = _option(created, "REQUIRES_APPROVAL")
    client.post(
        f"/trip-tasks/{created['task_id']}/select-option",
        headers=employee,
        json={
            "option_id": approval_option["option_id"],
            "business_reason": "客户现场需要近酒店",
        },
    )
    approved = client.post(
        f"/approvals/{created['task_id']}/decision",
        headers=manager,
        json={"approved": True, "reason": "同意客户拜访例外"},
    )
    cases.append(
        _case(
            "B-04",
            "Assigned manager M2001 can approve a live exception",
            [
                _check("approved_200", approved.status_code == 200, approved.status_code),
                _check(
                    "approver",
                    (approved.json().get("approval") or {}).get("approver_id") == "M2001",
                ),
                _check(
                    "not_pending",
                    (approved.json().get("approval") or {}).get("status") in {"APPROVED"}
                    or approved.json().get("state")
                    in {"READY_FOR_HANDOFF", "RECONFIRMATION_REQUIRED"},
                    approved.json().get("state"),
                ),
            ],
        )
    )

    unauth = client.get("/trip-tasks")
    bad_login = client.post(
        "/auth/login", json={"user_id": "E1001", "password": "wrong-password-12"}
    )
    created = _create_trip(client, employee)
    outsider_get = client.get(f"/trip-tasks/{created['task_id']}", headers=outsider)
    manager_create = client.post("/trip-tasks", headers=manager, json=DEMO_TRIP)
    admin_create_ok = client.post("/trip-tasks", headers=admin, json=DEMO_TRIP)
    tampered = dict(employee)
    token = tampered["Authorization"].replace("Bearer ", "")
    tampered["Authorization"] = f"Bearer {token[:-1]}x"
    tampered_get = client.get("/trip-tasks", headers=tampered)
    health = client.get("/health").json()
    cases.append(
        _case(
            "C-01",
            "AUTH live: login, 404 not 403, approver cannot create, bad token",
            [
                _check("auth_enabled", health.get("authentication") == "enabled"),
                _check("unauth_401", unauth.status_code == 401, unauth.status_code),
                _check("bad_login_401", bad_login.status_code == 401, bad_login.status_code),
                _check("outsider_404", outsider_get.status_code == 404, outsider_get.status_code),
                _check(
                    "outsider_not_403",
                    outsider_get.status_code != 403,
                    outsider_get.status_code,
                ),
                _check(
                    "approver_cannot_create",
                    manager_create.status_code == 403,
                    manager_create.status_code,
                ),
                _check("admin_can_create", admin_create_ok.status_code == 200),
                _check("tampered_401", tampered_get.status_code == 401, tampered_get.status_code),
            ],
        )
    )
    return cases


def _seed_task(db_url: str, payload: IntentExtractionSchema, message: str) -> dict[str, Any]:
    repository = SQLAlchemyTaskRepository(db_url)
    workflow, _ = build_demo_system(
        language_model=_ScriptedModel(payload),
        task_repository=repository,
    )
    task = workflow.create_task_from_message(message, traveler_id="E1001")
    result = {
        "task_id": task.task_id,
        "state": task.state.value,
        "questions": task.metadata.get("clarification_questions") or [],
        "missing": list(task.missing_required_fields),
        "failure": task.failure,
    }
    repository.dispose()
    return result


def _seed_structured(db_url: str) -> dict[str, Any]:
    payload = IntentExtractionSchema(
        classification="NEEDS_CLARIFICATION",
        fields=TripIntentFields(
            origin=None,
            destination=None,
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
        provided_fields=[],
        missing_required_fields=["origin", "destination", "departure_after", "arrive_by"],
        conflicts=[],
        assumptions=[],
        confidence=0.2,
        manipulation_detected=False,
    )
    repository = SQLAlchemyTaskRepository(db_url)
    workflow, _ = build_demo_system(
        language_model=_ScriptedModel(payload),
        task_repository=repository,
    )
    task = workflow.create_task_from_message("还没想好去哪", traveler_id="E1001")
    task.clarification_rounds = 3
    task.failure = "Clarification budget exhausted; use the structured form"
    task.clarification_question = None
    task.state = TaskState.NEEDS_STRUCTURED_INPUT
    task.missing_required_fields = (
        "origin",
        "destination",
        "departure_after",
        "arrive_by",
    )
    repository.record(
        task,
        new_audit_event(
            task.task_id,
            "CLARIFICATION_EXHAUSTED",
            input_value=3,
            output_value=task.failure,
        ),
    )
    result = {"task_id": task.task_id, "state": task.state.value}
    repository.dispose()
    return result


def _run_frontend_with_db(
    *,
    db_url: str,
    api_base: str,
    frontend_base: str,
    client: httpx.Client,
    output: Path,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [
            _case(
                "D-00",
                "Playwright is unavailable",
                [_check("playwright_import", False)],
            )
        ]

    employee = _login(client, "E1001")
    screenshots = output / "screenshots"
    screenshots.mkdir(exist_ok=True)
    cases: list[dict[str, Any]] = []

    def login_as(page, user_id: str) -> None:
        page.goto(frontend_base, wait_until="domcontentloaded")
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        page.reload(wait_until="networkidle")
        page.get_by_label("用户 ID").fill(user_id)
        page.get_by_label("密码").fill(AUTH_PASSWORD)
        page.get_by_test_id("login-submit").click()
        page.locator("aside.sidebar").wait_for(timeout=15_000)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        desktop = browser.new_context(viewport={"width": 1280, "height": 800})
        page = desktop.new_page()
        try:
            login_as(page, "E1001")
            nav = page.locator("aside.sidebar nav").inner_text()
            page.screenshot(path=str(screenshots / "d-employee-desktop.png"))
            cases.append(
                _case(
                    "D-01",
                    "Employee workspace hides the approval queue",
                    [
                        _check("plan", "智能规划" in nav, nav),
                        _check("no_approvals", "待我审批" not in nav, nav),
                    ],
                )
            )

            _create_trip(client, employee)
            page.reload(wait_until="networkidle")
            page.locator(".option-card").first.wait_for(timeout=10_000)
            confirm = page.get_by_test_id("confirm-selection")
            if confirm.is_disabled():
                page.locator(".option-card", has_text="全部合规").first.get_by_role(
                    "button", name="选择方案"
                ).click()
            confirm.click()
            page.wait_for_timeout(1500)
            heading = page.locator(".heading-status").inner_text()
            page.screenshot(path=str(screenshots / "d-select-compliant.png"))
            live = client.get("/trip-tasks?summary=false&limit=1", headers=employee).json()[0]
            cases.append(
                _case(
                    "D-02",
                    "Clicking confirm revalidates via the live API",
                    [
                        _check("ui_handoff", "交接" in heading or "就绪" in heading, heading),
                        _check(
                            "api_handoff",
                            live.get("state") == "READY_FOR_HANDOFF",
                            live.get("state"),
                        ),
                    ],
                )
            )

            _create_trip(client, employee)
            page.reload(wait_until="networkidle")
            page.locator(".option-card").first.wait_for(timeout=10_000)
            card = page.locator(".option-card", has_text="需要审批")
            select_over_cap = card.first.get_by_role("button", name="选择方案")
            if select_over_cap.count():
                select_over_cap.click()
            confirm = page.get_by_test_id("confirm-selection")
            disabled_before = confirm.is_disabled()
            page.get_by_test_id("business-reason").fill("客户同楼必须住这家")
            confirm.click()
            page.wait_for_timeout(1500)
            heading = page.locator(".heading-status").inner_text()
            page.screenshot(path=str(screenshots / "d-exception-reason.png"))
            live = client.get("/trip-tasks?summary=false&limit=1", headers=employee).json()[0]
            cases.append(
                _case(
                    "D-03",
                    "Exception option blocks confirm until a business reason is filled",
                    [
                        _check("disabled_without_reason", disabled_before),
                        _check(
                            "waiting",
                            live.get("state") == "WAITING_FOR_APPROVAL",
                            live.get("state"),
                        ),
                        _check("ui_approval", "审批" in heading, heading),
                    ],
                )
            )

            clarify_payload = IntentExtractionSchema(
                classification="TRIP",
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
            seeded = _seed_task(db_url, clarify_payload, "从北京去上海出差，时间还没定")
            page.reload(wait_until="networkidle")
            option = page.get_by_test_id("clarification-option-template:overnight")
            if option.count() == 0:
                option = page.get_by_test_id("clarification-option-template:day_trip")
            clicked = option.count() > 0
            if clicked:
                option.first.click()
                page.wait_for_timeout(2000)
            page.screenshot(path=str(screenshots / "d-clarification-click.png"))
            live = client.get(f"/trip-tasks/{seeded['task_id']}", headers=employee).json()
            cases.append(
                _case(
                    "D-04",
                    "Clarification panel click submits a structured option token",
                    [
                        _check("seed_clarify", seeded["state"] == "NEEDS_CLARIFICATION", seeded),
                        _check("button_present", clicked),
                        _check(
                            "moved_on",
                            live.get("state")
                            in {
                                "WAITING_FOR_USER",
                                "NEEDS_CLARIFICATION",
                                "READY_FOR_HANDOFF",
                                "NO_FEASIBLE_OPTION",
                            },
                            live.get("state"),
                        ),
                        _check(
                            "token_or_search",
                            bool(live.get("options"))
                            or any(
                                "template" in str(item).casefold()
                                for item in (live.get("assumptions") or [])
                            )
                            or live.get("state") != "NEEDS_CLARIFICATION",
                            live.get("assumptions"),
                        ),
                    ],
                )
            )

            structured = _seed_structured(db_url)
            page.reload(wait_until="networkidle")
            form = page.get_by_test_id("structured-request-form")
            form.wait_for(timeout=10_000)
            form_shown = form.count() > 0
            page.screenshot(path=str(screenshots / "d-structured-form.png"))
            page.get_by_test_id("structured-origin").fill("Beijing")
            page.get_by_test_id("structured-destination").fill("Shanghai")
            page.get_by_test_id("structured-departure").fill("2026-08-05T08:00")
            page.get_by_test_id("structured-arrive").fill("2026-08-06T10:00")
            page.get_by_test_id("structured-submit").click()
            page.wait_for_timeout(2000)
            page.screenshot(path=str(screenshots / "d-structured-form-after.png"))
            live = client.get(f"/trip-tasks/{structured['task_id']}", headers=employee).json()
            cases.append(
                _case(
                    "D-05",
                    "Exhausted clarification can submit the structured form and search",
                    [
                        _check("form_shown", form_shown),
                        _check(
                            "searched",
                            live.get("state")
                            in {"WAITING_FOR_USER", "NO_FEASIBLE_OPTION", "READY_FOR_HANDOFF"},
                            live.get("state"),
                        ),
                        _check("has_options", len(live.get("options") or []) >= 1),
                    ],
                )
            )

            page.evaluate("() => localStorage.clear()")
            login_as(page, "M2001")
            nav = page.locator("aside.sidebar nav").inner_text()
            page.screenshot(path=str(screenshots / "d-approver.png"))
            inbox_visible = "待我审批" in nav
            if inbox_visible:
                page.get_by_role("button", name="待我审批").click()
                page.wait_for_timeout(800)
                page.get_by_test_id("approvals-view").wait_for()
                reason = page.get_by_test_id("approval-reason")
                if reason.count():
                    reason.fill("现场需要，批准该例外")
                    page.get_by_test_id("approval-approve").click()
                    page.wait_for_timeout(1500)
                page.screenshot(path=str(screenshots / "d-approver-decide.png"))
            cases.append(
                _case(
                    "D-06",
                    "Approver M2001 sees the live inbox and cannot plan a new trip",
                    [
                        _check("has_inbox", inbox_visible, nav),
                        _check("no_plan", "智能规划" not in nav, nav),
                    ],
                )
            )

            page.evaluate("() => localStorage.clear()")
            login_as(page, "A9001")
            nav = page.locator("aside.sidebar nav").inner_text()
            page.screenshot(path=str(screenshots / "d-admin.png"))
            cases.append(
                _case(
                    "D-07",
                    "Admin workspace has no approval decision entry",
                    [
                        _check("no_plan", "智能规划" not in nav, nav),
                        _check("no_approvals", "待我审批" not in nav, nav),
                        _check("has_audit", "审计" in nav, nav),
                    ],
                )
            )
        except Exception as exc:  # noqa: BLE001
            page.screenshot(path=str(screenshots / "d-error.png"))
            cases.append(
                _case(
                    "D-EX",
                    "Frontend driver error",
                    [_check("no_exception", False, f"{type(exc).__name__}: {exc}")],
                )
            )
        finally:
            desktop.close()

        mobile = browser.new_context(viewport={"width": 390, "height": 844})
        page = mobile.new_page()
        try:
            login_as(page, "E1001")
            page.screenshot(path=str(screenshots / "d-employee-mobile.png"))
            sidebar = page.locator("aside.sidebar")
            cases.append(
                _case(
                    "D-08",
                    "Narrow viewport still renders the employee workspace",
                    [
                        _check("sidebar", sidebar.count() == 1),
                        _check("login_passed", page.get_by_test_id("login-submit").count() == 0),
                    ],
                )
            )
        except Exception as exc:  # noqa: BLE001
            cases.append(
                _case(
                    "D-08",
                    "Narrow viewport",
                    [_check("no_exception", False, f"{type(exc).__name__}: {exc}")],
                )
            )
        finally:
            mobile.close()
            browser.close()
    return cases


def _run_provider_delayed(output: Path, *, confirm_external: bool) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    script = REPO / "examples" / "run_provider_delayed_recovery_acceptance.py"
    for scenario, confirm in (("exhaust", False), ("circuit", False), ("recover", True)):
        if confirm and not confirm_external:
            cases.append(
                _case(
                    "E-recover-skipped",
                    "Real delayed recovery skipped (pass --confirm-external-test-calls)",
                    [_check("skipped_intentionally", True)],
                )
            )
            continue
        scenario_dir = output / f"provider-{scenario}"
        command = [
            sys.executable,
            str(script),
            "--scenario",
            scenario,
            "--output",
            str(scenario_dir),
        ]
        if scenario == "recover":
            command.append("--confirm-external-test-calls")
        env = os.environ.copy()
        completed = subprocess.run(
            command,
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        summary = {}
        summary_path = scenario_dir / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        passed = bool(summary.get("passed")) if summary else completed.returncode == 0
        cases.append(
            _case(
                f"E-{scenario}",
                f"Provider delayed-recovery scenario {scenario}",
                [
                    _check("exit_0", completed.returncode == 0, completed.stderr[-500:]),
                    _check("summary_passed", passed, summary.get("checks_summary")),
                ],
                stdout_tail=completed.stdout[-800:],
            )
        )
    return cases


def _render_markdown(results: dict[str, Any]) -> str:
    lines = [
        "# Uncovered-types acceptance (§18.3 A–E)",
        "",
        f"- run_id: `{results['run_id']}`",
        f"- started_at: `{results['started_at']}`",
        f"- passed: **{results['passed_count']}/{results['total']}**",
        "",
    ]
    for case in results["cases"]:
        mark = "PASS" if case["passed"] else "FAIL"
        lines.append(f"## {case['case_id']} {case['title']} — {mark}")
        for check in case["checks"]:
            status = "PASS" if check["ok"] else "FAIL"
            detail = f" — `{check['detail']}`" if check.get("detail") else ""
            lines.append(f"- [{status}] `{check['name']}`{detail}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="reports/evaluation-runs/uncovered-types-20260819",
    )
    parser.add_argument("--api-port", type=int, default=8010)
    parser.add_argument("--frontend-port", type=int, default=5174)
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    args = parser.parse_args()

    output = (
        (REPO / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output)
    )
    output.mkdir(parents=True, exist_ok=False)
    work_dir = output / "runtime"
    work_dir.mkdir()
    db_path = work_dir / "tasks.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    db_url = _sqlite_url(db_path)
    users, secret = _write_auth_files(work_dir)
    api_env = _api_env(
        db_url=db_url,
        users=users,
        secret=secret,
        port=args.api_port,
        frontend_port=args.frontend_port,
    )
    api_log = (work_dir / "api.log").open("w", encoding="utf-8")
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "corporate_travel_agent.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.api_port),
        ],
        cwd=REPO,
        env=api_env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    frontend: subprocess.Popen[str] | None = None
    frontend_log = None
    cases: list[dict[str, Any]] = []
    started = datetime.now(UTC).isoformat()
    try:
        _wait_http(f"http://127.0.0.1:{args.api_port}/health")
        cases.extend(_run_in_process())
        with httpx.Client(base_url=f"http://127.0.0.1:{args.api_port}", timeout=30.0) as client:
            cases.extend(_run_http(client))
            if not args.skip_frontend:
                frontend_log = (work_dir / "frontend.log").open("w", encoding="utf-8")
                fe_env = os.environ.copy()
                fe_env["VITE_API_BASE"] = f"http://127.0.0.1:{args.api_port}"
                frontend = subprocess.Popen(
                    [
                        "npm",
                        "run",
                        "dev",
                        "--",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(args.frontend_port),
                        "--strictPort",
                    ],
                    cwd=REPO / "frontend",
                    env=fe_env,
                    stdout=frontend_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    _wait_http(
                        f"http://127.0.0.1:{args.frontend_port}/", timeout=45.0
                    )
                    cases.extend(
                        _run_frontend_with_db(
                            db_url=db_url,
                            api_base=f"http://127.0.0.1:{args.api_port}",
                            frontend_base=f"http://127.0.0.1:{args.frontend_port}",
                            client=client,
                            output=output,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    cases.append(
                        _case(
                            "D-BOOT",
                            "Frontend dev server / Playwright driver",
                            [_check("frontend_ready", False, f"{type(exc).__name__}: {exc}")],
                        )
                    )
        cases.extend(
            _run_provider_delayed(output, confirm_external=args.confirm_external_test_calls)
        )
    finally:
        for proc in (frontend, api):
            if proc is None or proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        api_log.close()
        if frontend_log is not None:
            frontend_log.close()

    results = {
        "schema_version": 1,
        "run_id": output.name,
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat(),
        "cases": cases,
        "passed_count": sum(1 for item in cases if item["passed"]),
        "total": len(cases),
        "api_port": args.api_port,
        "frontend_port": args.frontend_port,
        "external_recover": args.confirm_external_test_calls,
    }
    results["passed"] = results["passed_count"] == results["total"]
    (output / "summary.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "results.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in cases),
        encoding="utf-8",
    )
    (output / "summary.md").write_text(_render_markdown(results), encoding="utf-8")
    print(
        json.dumps(
            {"passed": results["passed"], "score": f"{results['passed_count']}/{results['total']}"},
            ensure_ascii=False,
        )
    )
    if not results["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

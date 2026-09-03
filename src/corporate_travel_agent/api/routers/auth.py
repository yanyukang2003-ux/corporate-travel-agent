"""登录与当前身份。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from corporate_travel_agent.api.deps import CurrentIdentity, Runtime
from corporate_travel_agent.api.schemas import LoginRequest, LoginResponse, MeResponse
from corporate_travel_agent.services.auth import AuthenticationFailed

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, runtime: Runtime) -> dict[str, Any]:
    """登录并返回访问令牌。"""
    if not runtime.auth_service.enabled:
        raise HTTPException(status_code=409, detail="Authentication is disabled")
    try:
        token, expires_at = runtime.auth_service.login(payload.user_id, payload.password)
    except AuthenticationFailed as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid user ID or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return {"access_token": token, "token_type": "bearer", "expires_at": expires_at}


@router.get("/me", response_model=MeResponse)
def current_user(identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    """当前登录用户，以及"我可以替谁订"（出现在别人委托名单上的那些人）。"""
    can_book_for: tuple[str, ...] = ()
    if identity.employee_id:
        can_book_for = runtime.workflow.employees.delegators_of(identity.employee_id)
    return {
        "user_id": identity.user_id,
        "roles": sorted(role.value for role in identity.roles),
        "employee_id": identity.employee_id,
        "can_book_for": list(can_book_for),
    }

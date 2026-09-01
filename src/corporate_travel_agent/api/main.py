"""api.main：企业差旅 Agent 的 FastAPI 入口。

装配 Orchestrator、鉴权、Provider 与仓库，暴露任务创建/消息/选方案/审批等 HTTP 路由。
V1 只规划与合规校验，不代付、不预订、不退改。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from corporate_travel_agent.agent.orchestrator import (
    PARTIAL_COVERAGE_METADATA_KEY,
    LanguageModelUnavailable,
    WorkflowError,
)
from corporate_travel_agent.agent.ports import LanguageModelError
from corporate_travel_agent.agent.tool_loop_adapter import OpenAIToolCallingLanguageModel
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.constraints import HardConstraint, SoftPreference
from corporate_travel_agent.domain.enums import BookingScope, TripEventType
from corporate_travel_agent.domain.models import (
    AuditEvent,
    InventorySnapshot,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.domain.validation import validate_trip_request_values
from corporate_travel_agent.planning.cost_guidance import (
    CostGuidance,
    build_cost_guidance,
)
from corporate_travel_agent.planning.preferences import duration_minutes_per_unit
from corporate_travel_agent.providers.factory import travel_provider_from_environment
from corporate_travel_agent.services.auth import (
    AuthenticationFailed,
    AuthService,
    Role,
    UserIdentity,
)
from corporate_travel_agent.services.evaluation_business import (
    METRIC_LABELS,
    build_business_metrics_report,
)
from corporate_travel_agent.services.object_storage import (
    InMemoryRawResponseObjectStore,
    LocalRawResponseObjectStore,
    RawResponseObjectStore,
)
from corporate_travel_agent.services.policy_config import (
    load_policy_configuration_from_environment,
)
from corporate_travel_agent.services.provider_quote_context import (
    InMemoryProviderQuoteContextStore,
    ProviderQuoteContextStore,
)
from corporate_travel_agent.services.provider_resilience import ProviderRetryScheduler
from corporate_travel_agent.services.repositories import (
    ConcurrentUpdateError,
    InMemoryTaskRepository,
    NotFoundError,
    TaskRepository,
)
from corporate_travel_agent.services.runtime_config import validate_deployment_environment
from corporate_travel_agent.services.task_projections import pending_approver_id

provider_retry_scheduler: ProviderRetryScheduler | None = None

validate_deployment_environment(os.environ)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    """启动/停止 Provider 延迟重试调度器，并在退出时释放资源。"""
    if provider_retry_scheduler is not None:
        provider_retry_scheduler.start()
    try:
        yield
    finally:
        retry_worker_drained = True
        if provider_retry_scheduler is not None:
            retry_worker_drained = provider_retry_scheduler.stop()
        if retry_worker_drained:
            provider_close = getattr(workflow.provider, "close", None)
            if callable(provider_close):
                provider_close()
            repository_dispose = getattr(workflow.tasks, "dispose", None)
            if callable(repository_dispose):
                repository_dispose()


app = FastAPI(
    title="Corporate Travel Planning & Compliance Agent",
    version="0.1.0",
    description="V1 只规划与合规校验；不代付、不预订、不退改签。",
    lifespan=_lifespan,
)

_cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _configured_tool_calling_language_model() -> OpenAIToolCallingLanguageModel | None:
    """装配工具循环入口的模型端口。

    和上面两个用同一份凭证与模型名——差别不在模型，在于**要它做什么**：
    这里每轮只让它挑一个工具，不让它一次交出整个结构体。
    """
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        return OpenAIToolCallingLanguageModel(
            model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
            request_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60")),
        )
    except LanguageModelError:
        return None


def _configured_task_repository() -> TaskRepository:
    """按 DATABASE_URL 选择内存或 SQLAlchemy 任务仓库。"""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return InMemoryTaskRepository()
    try:
        from corporate_travel_agent.services.sqlalchemy_repository import (
            SQLAlchemyTaskRepository,
        )
    except ImportError as exc:
        raise RuntimeError(
            "DATABASE_URL requires installation with the persistence extra"
        ) from exc

    if (
        os.getenv("DATABASE_AUTO_CREATE", "false").casefold() == "true"
        and os.getenv("ENVIRONMENT", "development").casefold()
        in {"production", "prod", "staging"}
    ):
        raise RuntimeError(
            "DATABASE_AUTO_CREATE is forbidden when ENVIRONMENT is production/staging; "
            "run 'alembic upgrade head' instead"
        )

    repository = SQLAlchemyTaskRepository(database_url)
    if os.getenv("DATABASE_AUTO_CREATE", "false").casefold() == "true":
        repository.create_schema()
    repository.check_connection()
    repository.check_schema()
    return repository


def _configured_outbox_store():
    """装配 outbox 存储：和任务仓储同一个地方——内存仓储自带的那份，或同一个 SQL 引擎。"""
    from corporate_travel_agent.services.outbox import SQLAlchemyOutboxStore

    engine = getattr(workflow.tasks, "engine", None)
    if engine is None:
        return workflow.tasks.outbox
    return SQLAlchemyOutboxStore(engine)


def _configured_trip_repository(task_repository: TaskRepository):
    """差旅聚合仓储：和任务仓储同一个引擎；内存仓储时用内存。"""
    from corporate_travel_agent.services.trips import (
        InMemoryTripRepository,
        SQLAlchemyTripRepository,
    )

    engine = getattr(task_repository, "engine", None)
    if engine is None:
        return InMemoryTripRepository()
    return SQLAlchemyTripRepository(engine)


def _configured_provider_circuit_store(task_repository: TaskRepository):
    """熔断状态存储：和任务仓储同一个引擎，多实例共用；内存仓储时每个进程一份。"""
    engine = getattr(task_repository, "engine", None)
    if engine is None:
        return None
    from corporate_travel_agent.services.sqlalchemy_repository import (
        SQLAlchemyProviderCircuitStore,
    )

    return SQLAlchemyProviderCircuitStore(engine)


def _configured_expense_store():
    """费控记录存储：和任务仓储同一个引擎；内存仓储时用内存。"""
    from corporate_travel_agent.services.expense_reconciliation import InMemoryExpenseRecordStore

    engine = getattr(workflow.tasks, "engine", None)
    if engine is None:
        return InMemoryExpenseRecordStore()
    from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyExpenseRecordStore

    return SQLAlchemyExpenseRecordStore(engine)


def _configured_outbox_dispatcher(store):
    """装配投递器：配了 OUTBOX_WEBHOOK_URL 就 POST 给企业侧，否则记日志。

    至少一次投递；对面按 `event_id` 幂等。`POST /outbox/dispatch` 手动跑一轮，
    `examples/run_outbox_worker.py` 循环跑。
    """
    from corporate_travel_agent.services.outbox_dispatch import (
        LoggingChannel,
        OutboxDispatcher,
        WebhookChannel,
    )

    url = os.getenv("OUTBOX_WEBHOOK_URL")
    channel = (
        WebhookChannel(url, secret=os.getenv("OUTBOX_WEBHOOK_SECRET") or None)
        if url
        else LoggingChannel()
    )
    return OutboxDispatcher(
        store,
        default_channel=channel,
        max_attempts=int(os.getenv("OUTBOX_MAX_ATTEMPTS", "5")),
    )


def _configured_quote_context_store(
    task_repository: TaskRepository,
) -> ProviderQuoteContextStore:
    """装配供应商报价上下文存储。"""
    engine = getattr(task_repository, "engine", None)
    if engine is None:
        return InMemoryProviderQuoteContextStore()
    from corporate_travel_agent.services.sqlalchemy_repository import (
        SQLAlchemyProviderQuoteContextStore,
    )

    return SQLAlchemyProviderQuoteContextStore(engine)


def _configured_raw_response_store() -> RawResponseObjectStore:
    """装配原始 Provider 响应对象存储。"""
    store_dir = os.getenv("RAW_RESPONSE_STORE_DIR")
    if not store_dir:
        return InMemoryRawResponseObjectStore()
    return LocalRawResponseObjectStore(store_dir)


def _configured_retention_days() -> int:
    """原始响应保留天数。"""
    value = int(os.getenv("RAW_RESPONSE_RETENTION_DAYS", "90"))
    if not 1 <= value <= 3650:
        raise RuntimeError("RAW_RESPONSE_RETENTION_DAYS must be between 1 and 3650")
    return value


def _configured_provider_resilience() -> tuple[float, int, tuple[float, ...], float]:
    """读取 Provider 熔断与延迟重试相关配置。"""
    circuit_seconds = float(os.getenv("PROVIDER_CIRCUIT_OPEN_SECONDS", "60"))
    max_delayed_attempts = int(os.getenv("MAX_DELAYED_PROVIDER_RETRIES", "3"))
    schedule = tuple(
        float(value.strip())
        for value in os.getenv("PROVIDER_DELAYED_RETRY_SECONDS", "60,180,600").split(",")
        if value.strip()
    )
    poll_seconds = float(os.getenv("PROVIDER_RETRY_POLL_SECONDS", "5"))
    if not 0 <= circuit_seconds <= 3600:
        raise RuntimeError("PROVIDER_CIRCUIT_OPEN_SECONDS must be between 0 and 3600")
    if not 0 <= max_delayed_attempts <= 3:
        raise RuntimeError("MAX_DELAYED_PROVIDER_RETRIES must be between 0 and 3")
    if len(schedule) < max_delayed_attempts or any(value < 0 for value in schedule):
        raise RuntimeError("PROVIDER_DELAYED_RETRY_SECONDS must cover every delayed retry")
    if poll_seconds <= 0:
        raise RuntimeError("PROVIDER_RETRY_POLL_SECONDS must be greater than zero")
    return circuit_seconds, max_delayed_attempts, schedule, poll_seconds


raw_response_store = _configured_raw_response_store()
auth_service = AuthService.from_environment()
policy_configuration = load_policy_configuration_from_environment()
task_repository = _configured_task_repository()
quote_context_store = _configured_quote_context_store(task_repository)
process_role = os.getenv("PROCESS_ROLE", "api").strip().casefold()
if process_role not in {"api", "worker", "all"}:
    raise RuntimeError("PROCESS_ROLE must be api, worker, or all")
_active_policy = next(
    (
        item
        for item in policy_configuration.policy_snapshots
        if item.snapshot_id == policy_configuration.config.active_policy_snapshot_id
    ),
    policy_configuration.policy_snapshots[0]
    if policy_configuration.policy_snapshots
    else None,
)
configured_travel_provider = travel_provider_from_environment(
    raw_response_store=raw_response_store,
    policy_currency=_active_policy.currency if _active_policy is not None else None,
    quote_context_store=quote_context_store,
)
(
    provider_circuit_open_seconds,
    max_delayed_provider_attempts,
    delayed_provider_retry_seconds,
    provider_retry_poll_seconds,
) = _configured_provider_resilience()
workflow, _provider = build_demo_system(
    tool_calling_language_model=_configured_tool_calling_language_model(),
    task_repository=task_repository,
    trip_repository=_configured_trip_repository(task_repository),
    raw_response_store=raw_response_store,
    raw_response_retention_days=_configured_retention_days(),
    policy_configuration=policy_configuration,
    provider=configured_travel_provider,
    provider_circuit_open_seconds=provider_circuit_open_seconds,
    provider_circuit_store=_configured_provider_circuit_store(task_repository),
    max_delayed_provider_attempts=max_delayed_provider_attempts,
    delayed_provider_retry_seconds=delayed_provider_retry_seconds,
    max_concurrent_llm_calls=int(os.getenv("MAX_CONCURRENT_LLM_CALLS", "8")),
    max_concurrent_provider_calls=int(os.getenv("MAX_CONCURRENT_PROVIDER_CALLS", "16")),
    tool_acquire_timeout_seconds=float(os.getenv("TOOL_ACQUIRE_TIMEOUT_SECONDS", "5")),
    interrupted_task_stale_seconds=float(
        os.getenv("INTERRUPTED_TASK_STALE_SECONDS", "30")
    ),
    provider_retry_worker_id=os.getenv("PROVIDER_RETRY_WORKER_ID") or None,
    provider_retry_lease_seconds=float(
        os.getenv("PROVIDER_RETRY_LEASE_SECONDS", "900")
    ),
)
provider_retry_scheduler = (
    ProviderRetryScheduler(
        workflow,
        poll_seconds=provider_retry_poll_seconds,
    )
    if process_role in {"worker", "all"}
    else None
)
outbox_store = _configured_outbox_store()
expense_store = _configured_expense_store()
outbox_dispatcher = _configured_outbox_dispatcher(outbox_store)
bearer_scheme = HTTPBearer(auto_error=False)


class TripCreate(BaseModel):
    """结构化建任务请求体。"""
    model_config = ConfigDict(extra="forbid")

    traveler_id: str = "E1001"
    booking_scope: BookingScope | None = None
    origin: str
    destination: str
    departure_after: datetime
    arrive_by: datetime
    return_after: datetime | None = None
    return_before: datetime | None = None
    hotel_check_in: date | None = None
    hotel_check_out: date | None = None
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    soft_preferences: list[SoftPreference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_windows(self) -> TripCreate:
        validate_trip_request_values(self.model_dump()).require_valid()
        return self


class NaturalLanguageTripCreate(BaseModel):
    """自然语言建任务请求体。"""
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    traveler_id: str = "E1001"


class MessageCreate(BaseModel):
    """提交跟进消息请求体。"""
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


class OptionSelection(BaseModel):
    """选中方案请求体。"""
    option_id: str
    business_reason: str | None = None


class ApprovalDecision(BaseModel):
    """审批决策请求体。"""
    approver_id: str | None = None
    approved: bool
    reason: str = Field(min_length=1, max_length=2000)


class BookingConfirmationCreate(BaseModel):
    """员工回填下单确认的请求体：订单号、实付金额、币种，可选下单时刻和备注。

    这里只限形状；订单号是不是真的、金额对不对，系统核不了——见
    `domain/validation.validate_booking_confirmation_values`。
    """
    model_config = ConfigDict(extra="forbid")

    order_references: list[str] = Field(min_length=1, max_length=10)
    total_amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    booked_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)


class LoginRequest(BaseModel):
    """登录请求体。"""
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


def _current_identity(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> UserIdentity:
    """从 Bearer Token 解析当前用户身份。"""
    token = credentials.credentials if credentials else None
    try:
        return auth_service.authenticate(token)
    except AuthenticationFailed as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentIdentity = Annotated[UserIdentity, Depends(_current_identity)]


@app.get("/health")
def health() -> dict[str, Any]:
    """健康检查：服务、LLM、仓库与 Provider 重试状态。"""
    persistence_details: dict[str, Any] = {"backend": workflow.tasks.backend_name}
    operational = getattr(workflow.tasks, "operational_status", None)
    status = "ok"
    if callable(operational):
        try:
            persistence_details.update(operational())
        except Exception as exc:  # noqa: BLE001 - health must stay available
            persistence_details["error"] = str(exc)
            status = "degraded"
    outbox_status: dict[str, Any] = {"backend": outbox_store.backend_name}
    try:
        outbox_status["unpublished_count"] = outbox_store.unpublished_count()
    except Exception as exc:  # noqa: BLE001
        outbox_status["error"] = str(exc)
        status = "degraded"
    return {
        "status": status,
        "booking_capability": "disabled",
        # 只剩一条自然语言入口（工具循环）；`language_model` 这个键保留给前端和旧脚本读。
        "language_model": (
            "configured" if workflow.tool_calling_language_model else "not_configured"
        ),
        "tool_calling_language_model": (
            "configured" if workflow.tool_calling_language_model else "not_configured"
        ),
        "intent_entrypoints": {
            "structured": "/trip-tasks",
            "agentic": "/agentic/trip-tasks",
        },
        "language_model_status": (
            getattr(workflow, "llm_runtime_status", "unknown")
            if workflow.tool_calling_language_model
            else "not_configured"
        ),
        "language_model_ready": bool(
            workflow.tool_calling_language_model
            and getattr(workflow, "llm_runtime_status", "unknown")
            not in {"billing_blocked", "auth_failed"}
        ),
        "language_model_fallback": getattr(workflow, "fallback_model", None),
        "travel_provider": workflow.provider.name,
        "travel_provider_mode": getattr(workflow.provider, "provider_mode", "deterministic"),
        "persistence": workflow.tasks.backend_name,
        "persistence_details": persistence_details,
        "outbox": outbox_status,
        "raw_response_store": raw_response_store.backend_name,
        "authentication": "enabled" if auth_service.enabled else "disabled",
        "policy_config": policy_configuration.source_type,
        "policy_config_version": policy_configuration.config.config_version,
        "active_policy_snapshot": policy_configuration.active_policy.snapshot_id,
        "policy_config_sha256": policy_configuration.sha256,
        "provider_resilience": {
            "immediate_attempts": workflow.max_provider_attempts,
            "circuit": workflow.provider_circuit_breaker.snapshot(),
            "max_delayed_retries": (
                workflow.delayed_provider_retry_policy.max_attempts
            ),
            "delayed_retry_seconds": list(
                workflow.delayed_provider_retry_policy.delays_seconds[
                    : workflow.delayed_provider_retry_policy.max_attempts
                ]
            ),
            "process_role": process_role,
            "retry_worker_enabled": provider_retry_scheduler is not None,
            "retry_lease_seconds": workflow.provider_retry_lease_duration.total_seconds(),
            "retry_metrics": workflow.provider_retry_metrics(),
        },
    }


@app.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    """登录并返回访问令牌。"""
    if not auth_service.enabled:
        raise HTTPException(status_code=409, detail="Authentication is disabled")
    try:
        token, expires_at = auth_service.login(payload.user_id, payload.password)
    except AuthenticationFailed as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid user ID or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at,
    }


@app.get("/auth/me")
def current_user(identity: CurrentIdentity) -> dict[str, Any]:
    """返回当前登录用户信息。"""
    can_book_for: tuple[str, ...] = ()
    if identity.employee_id:
        delegators_of = getattr(workflow.employees, "delegators_of", None)
        if callable(delegators_of):
            can_book_for = delegators_of(identity.employee_id)
    return {
        "user_id": identity.user_id,
        "roles": sorted(role.value for role in identity.roles),
        "employee_id": identity.employee_id,
        # 我可以替谁订：出现在别人委托名单上的那些人。前端据此显示"为谁出差"。
        "can_book_for": list(can_book_for),
    }


@app.post("/trip-tasks")
def create_trip(
    payload: TripCreate,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """仅用结构化请求创建差旅任务。"""
    _require_can_create(identity, payload.traveler_id)
    task_id = str(uuid4())
    request = _to_request(payload, task_id=task_id, version=1)
    return _run(lambda: workflow.create_task(request, requester_id=_requester_id(identity)))


@app.post("/agentic/trip-tasks")
def create_agentic_trip(
    payload: NaturalLanguageTripCreate,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """用工具循环入口创建自然语言任务。

    和 `/semantic` 并行存在，按 ADR-0002 的并行迁移方式：新入口另起一条，
    旧的一个字不动，两条链路可以拿同一句话直接对照。
    """
    _require_can_create(identity, payload.traveler_id)
    return _run(
        lambda: workflow.create_task_from_agentic_message(
            payload.message,
            traveler_id=payload.traveler_id,
            requester_id=_requester_id(identity),
        )
    )


@app.get("/trip-tasks")
def list_trips(
    identity: CurrentIdentity,
    summary: bool = True,
    limit: int = 100,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """列出调用方可视的任务。

    默认返回紧凑摘要（不含方案/工具轨迹）；传 ``summary=false`` 返回完整公开任务文档。
    """
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    summaries = _visible_task_summaries(identity, state=state, limit=limit)
    if summary:
        return [_public_task_summary(item) for item in summaries]
    tasks = []
    for item in summaries:
        try:
            task = workflow.tasks.get(item.task_id)
        except NotFoundError:
            continue
        if _can_read_task(identity, task):
            tasks.append(_public_task(task))
    return tasks


@app.get("/approvals/inbox")
def approval_inbox(
    identity: CurrentIdentity,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """当前审批人的待办例外审批（基于投影）。"""
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    if not (
        identity.has_role(Role.APPROVER) or identity.has_role(Role.ADMIN)
    ):
        raise HTTPException(status_code=403, detail="Approver role required")
    # 收件箱按"当前待谁批"查，不按直属经理：分级审批走到第二级时，
    # 该看见它的是财务，不再是经理。
    if identity.has_role(Role.ADMIN):
        summaries = workflow.tasks.list_task_summaries(
            state="WAITING_FOR_APPROVAL", limit=limit
        )
    else:
        summaries = workflow.tasks.list_task_summaries(
            pending_approver_id=identity.user_id,
            state="WAITING_FOR_APPROVAL",
            limit=limit,
        )
    return [_public_task_summary(item) for item in summaries]


@app.post("/agentic/trip-tasks/{task_id}/messages")
def submit_agentic_message(
    task_id: str,
    payload: MessageCreate,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """仅向工具循环任务提交跟进消息。"""
    _require_can_operate(identity, _visible_task(task_id, identity))
    return _run(lambda: workflow.submit_agentic_message(task_id, payload.message))


@app.post("/trip-tasks/{task_id}/structured-request")
def submit_structured_request(
    task_id: str,
    payload: TripCreate,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """在澄清失败后用完整结构化请求继续。"""
    def operation() -> TripTask:
        task = _visible_task(task_id, identity)
        _require_can_operate(identity, task)
        if payload.traveler_id != task.employee.employee_id:
            raise ValueError("traveler_id cannot change within an existing task")
        request = _to_request(
            payload,
            task_id=task_id,
            version=1 if task.request is None else task.request.version + 1,
        )
        return workflow.complete_with_structured_request(task_id, request)

    return _run(operation)


@app.get("/trip-tasks/{task_id}")
def get_trip(task_id: str, identity: CurrentIdentity) -> dict[str, Any]:
    """获取单个任务的公开视图。"""
    return _public_task(_visible_task(task_id, identity))


@app.post("/trip-tasks/{task_id}/select-option")
def select_option(
    task_id: str,
    payload: OptionSelection,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """选中某个 TravelOption。"""
    _require_can_operate(identity, _visible_task(task_id, identity))
    return _run(
        lambda: workflow.select_option(
            task_id, payload.option_id, business_reason=payload.business_reason
        )
    )


@app.post("/trip-tasks/{task_id}/replan")
def replan(task_id: str, identity: CurrentIdentity) -> dict[str, Any]:
    """触发重试或重规划。"""
    _require_can_operate(identity, _visible_task(task_id, identity))
    return _run(lambda: workflow.retry_or_replan(task_id))


@app.post("/trip-tasks/{task_id}/revise-request")
def revise_request(
    task_id: str,
    payload: TripCreate,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """用新版结构化请求修订并重搜。"""
    task = _visible_task(task_id, identity)
    _require_can_operate(identity, task)
    if payload.traveler_id != task.employee.employee_id:
        raise HTTPException(status_code=409, detail="traveler_id cannot change")
    if task.request is None:
        raise HTTPException(status_code=409, detail="Draft task has no request to revise")
    request = _to_request(payload, task_id=task_id, version=task.request.version + 1)
    return _run(lambda: workflow.revise_request(task_id, request))


@app.post("/trip-tasks/{task_id}/handoff-completed")
def handoff_completed(task_id: str, identity: CurrentIdentity) -> dict[str, Any]:
    """标记已完成向 Provider 的交接。"""
    _require_can_operate(identity, _visible_task(task_id, identity))
    return _run(lambda: workflow.mark_handed_off(task_id))


@app.post("/trip-tasks/{task_id}/booking-confirmation")
def confirm_booking(
    task_id: str,
    payload: BookingConfirmationCreate,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """员工回填"我订好了"：订单号、实付金额。

    这是交接之后系统唯一能拿到的"真的订了"的证据，业务指标层靠它把交接完成率变成
    有分子可对照的数。**只是自述**——系统核不了订单号和金额；`source` 固定为
    `SELF_REPORTED`。一个任务只能回填一次，填错了开新任务。

    从 `READY_FOR_HANDOFF` 直接回填也行：后端会先记一笔交接完成，再记确认。
    权限和其他工作流操作一样：旅行者本人或管理员；审批人不能替员工填。
    """
    task = _visible_task(task_id, identity)
    _require_can_operate(identity, task)
    reported_by = identity.employee_id or identity.user_id
    return _run(
        lambda: workflow.confirm_booking(
            task_id,
            order_references=payload.order_references,
            total_amount=payload.total_amount,
            currency=payload.currency,
            reported_by=reported_by,
            booked_at=payload.booked_at,
            note=payload.note,
        )
    )


@app.post("/approvals/{task_id}/decision")
def decide_approval(
    task_id: str,
    payload: ApprovalDecision,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """提交例外审批决定。"""
    task = _visible_task(task_id, identity)
    approver_id = payload.approver_id or task.employee.manager_id
    if auth_service.enabled:
        _require_can_approve(identity, task)
        if payload.approver_id and payload.approver_id != identity.user_id:
            raise HTTPException(status_code=403, detail="Approver identity mismatch")
        approver_id = identity.user_id
    return _run(
        lambda: workflow.decide_approval(
            task_id,
            approver_id=approver_id,
            approved=payload.approved,
            reason=payload.reason,
        )
    )


@app.get("/policy")
def active_policy(identity: CurrentIdentity) -> dict[str, Any]:
    """返回当前生效政策快照的只读视图。

    政策是公司规则而不是个人数据，因此对所有已认证身份可读；返回内容直接来自
    ``PolicySnapshot``，不复制一份可能过时的展示副本。
    """
    snapshot = workflow.policies.current()
    employee_level: str | None = None
    if identity.employee_id:
        try:
            employee_level = workflow.employees.snapshot(identity.employee_id).level
        except NotFoundError:
            employee_level = None
    return {
        "snapshot_id": snapshot.snapshot_id,
        "policy_version": snapshot.policy_version,
        "content_hash": snapshot.content_hash,
        "currency": snapshot.currency,
        "effective_from": snapshot.effective_from.isoformat(),
        "effective_to": (
            snapshot.effective_to.isoformat() if snapshot.effective_to else None
        ),
        "arrival_buffer_minutes": snapshot.arrival_buffer_minutes,
        "viewer_level": employee_level,
        "level_rules": [
            {
                "level": level,
                "allowed_flight_classes": sorted(rule.allowed_flight_classes),
                "allowed_train_classes": sorted(rule.allowed_train_classes),
            }
            for level, rule in sorted(snapshot.level_rules.items())
        ],
        "hotel_city_caps": [
            {"city": city, "nightly_cap": str(cap)}
            for city, cap in sorted(snapshot.hotel_city_caps.items())
        ],
        "exception_allowed_rule_ids": sorted(snapshot.exception_allowed_rule_ids),
        # 后加的三个维度。None / 空列表就是"这版政策没有这条规则"。
        "min_advance_booking_days": snapshot.min_advance_booking_days,
        "hotel_seasonal_caps": [
            {
                "city": item.city,
                "label": item.label,
                "season_from": item.season_from.isoformat(),
                "season_to": item.season_to.isoformat(),
                "nightly_cap": str(item.nightly_cap),
            }
            for item in snapshot.hotel_seasonal_caps
        ],
        "cost_center_budgets": [
            {
                "cost_center": item.cost_center,
                "amount": str(item.amount),
                "currency": item.currency,
                "period_from": item.period_from.isoformat(),
                "period_to": item.period_to.isoformat(),
            }
            for item in sorted(snapshot.cost_center_budgets.values(), key=lambda b: b.cost_center)
        ],
    }


class TripEventRequest(BaseModel):
    """外部变更事件：航变（航司/供应商推送）或会议改期（日历/旅行者报）。"""
    model_config = ConfigDict(extra="forbid")

    event_type: TripEventType
    ref_id: str | None = Field(default=None, max_length=128)
    new_depart_at: datetime | None = None
    new_arrive_by: datetime | None = None
    note: str | None = Field(default=None, max_length=500)


def _visible_trip(trip_id: str, identity: UserIdentity):
    try:
        trip = workflow.trips.get(trip_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Trip not found") from exc
    if identity.has_role(Role.ADMIN):
        return trip
    if identity.has_role(Role.EMPLOYEE) and identity.employee_id in {
        trip.traveler_id,
        trip.requester_id,
    }:
        return trip
    raise HTTPException(status_code=404, detail="Trip not found")


def _public_trip(trip) -> dict[str, Any]:
    return {
        "trip_id": trip.trip_id,
        "traveler_id": trip.traveler_id,
        "requester_id": trip.requester_id,
        "status": trip.status.value,
        "task_ids": list(trip.task_ids),
        "created_at": trip.created_at,
        "watch": asdict(trip.watch) if trip.watch is not None else None,
        "events": [asdict(item) for item in trip.events],
    }


@app.get("/trips")
def list_trip_aggregates(identity: CurrentIdentity, limit: int = 100) -> list[dict[str, Any]]:
    """我的差旅（我是旅行者或发起人）；管理员看全部。"""
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    if identity.has_role(Role.ADMIN):
        trips = workflow.trips.list_all(limit=limit)
    elif identity.has_role(Role.EMPLOYEE) and identity.employee_id:
        trips = workflow.trips.list_involving(identity.employee_id, limit=limit)
    else:
        trips = ()
    return [_public_trip(item) for item in trips]


@app.get("/trips/{trip_id}")
def get_trip_aggregate(trip_id: str, identity: CurrentIdentity) -> dict[str, Any]:
    return _public_trip(_visible_trip(trip_id, identity))


@app.post("/trips/{trip_id}/events")
def report_trip_event(
    trip_id: str, payload: TripEventRequest, identity: CurrentIdentity
) -> dict[str, Any]:
    """报一条变更事件；系统开一个改期任务挂在这趟差旅下，原任务一个字不动。

    航变由管理员（代表航司/供应商推送）报；会议改期旅行者或发起人自己也能报。
    """
    trip = _visible_trip(trip_id, identity)
    if payload.event_type is TripEventType.FLIGHT_CHANGED and not identity.has_role(Role.ADMIN):
        raise HTTPException(
            status_code=403, detail="Flight changes are reported by the carrier feed"
        )
    reported_by = _requester_id(identity) or identity.user_id
    return _run(
        lambda: workflow.report_trip_event(
            trip.trip_id,
            event_type=payload.event_type,
            ref_id=payload.ref_id,
            new_depart_at=payload.new_depart_at,
            new_arrive_by=payload.new_arrive_by,
            note=payload.note,
            reported_by=reported_by,
        )
    )


class ExpenseImportRequest(BaseModel):
    """费控系统推来的一批报销记录。每条至少要有订单号，那是对账的钥匙。"""
    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(default="expense-system", min_length=1, max_length=64)
    records: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


@app.post("/expenses/import")
def import_expenses(payload: ExpenseImportRequest, identity: CurrentIdentity) -> dict[str, Any]:
    """导入费控记录并逐条对账（管理员）。同一个 expense_id 重复导入会被跳过。

    对上了的自述从此算"核实过"；对不上的两边都留着；本系统里没有对应确认的，
    就是渠道外预订——`GET /metrics/business` 的 `off_channel_expense_rate` 从这里来。
    """
    from corporate_travel_agent.services.expense_reconciliation import (
        reconcile_expenses,
        record_from_payload,
    )

    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    try:
        records = [
            record_from_payload({"source_system": payload.source_system, **item})
            for item in payload.records
        ]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report = reconcile_expenses(
        records,
        repository=workflow.tasks,
        store=expense_store,
        reconcile=workflow.reconcile_expense,
        now=workflow.clock,
    )
    return report.as_dict()


@app.get("/expenses/records")
def expense_records(identity: CurrentIdentity, limit: int = 100) -> list[dict[str, Any]]:
    """导入过的费控记录及对账结果（管理员）。"""
    if not 1 <= limit <= 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    return [
        {
            "expense_id": item.record.expense_id,
            "employee_id": item.record.employee_id,
            "amount": str(item.record.amount),
            "currency": item.record.currency,
            "expensed_at": item.record.expensed_at,
            "order_references": list(item.record.order_references),
            "source_system": item.record.source_system,
            "cost_center": item.record.cost_center,
            "description": item.record.description,
            "status": item.status.value,
            "matched_task_id": item.matched_task_id,
            "note": item.note,
            "imported_at": item.imported_at,
        }
        for item in expense_store.list_records(limit=limit)
    ]


class OutboxDispatchRequest(BaseModel):
    """手动跑一轮投递的请求体。"""
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=500)


@app.post("/outbox/dispatch")
def dispatch_outbox(payload: OutboxDispatchRequest, identity: CurrentIdentity) -> dict[str, Any]:
    """管理员手动投递一轮发件箱。生产里由 `examples/run_outbox_worker.py` 循环做同一件事。"""
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    report = outbox_dispatcher.dispatch_once(limit=payload.limit)
    return {
        **report.as_dict(),
        "channel": outbox_dispatcher.default_channel.name,
        "unpublished_count": outbox_store.unpublished_count(),
    }


@app.get("/outbox/events")
def outbox_events(identity: CurrentIdentity, limit: int = 50) -> list[dict[str, Any]]:
    """最近的发件箱事件（已发布和未发布都有），管理员看投递有没有卡住。"""
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    return [
        {
            "event_id": item.event_id,
            "event_type": item.event_type,
            "aggregate_type": item.aggregate_type,
            "aggregate_id": item.aggregate_id,
            "created_at": item.created_at,
            "published_at": item.published_at,
            "attempt_count": item.attempt_count,
            "last_error": item.last_error,
            "dead_lettered": (
                item.published_at is None and item.attempt_count >= outbox_dispatcher.max_attempts
            ),
            "payload": item.payload,
        }
        for item in outbox_store.list_recent(limit=limit)
    ]


@app.get("/audit-events")
def recent_audit_events(
    identity: CurrentIdentity,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """管理员可见的近期审计事件（跨任务）。

    审计事件包含跨员工的任务标识，因此只对管理员开放；其他身份仍只能读自己任务的
    ``/trip-tasks/{task_id}/audit-events``。

    这里走的是任务投影而不是全表 ``list_tasks()``，因此扫描量由 ``limit`` 约束。它是
    近期事件视图，不是完整审计导出；正式取证仍应从审计存储直接导出。
    """
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    events: list[dict[str, Any]] = []
    for summary in workflow.tasks.list_task_summaries(limit=limit):
        try:
            events.extend(asdict(item) for item in workflow.tasks.events(summary.task_id))
        except NotFoundError:
            continue
    events.sort(key=lambda item: item["created_at"], reverse=True)
    return events[:limit]


@app.get("/metrics/business")
def business_metrics(
    identity: CurrentIdentity,
    limit: int = 200,
) -> dict[str, Any]:
    """业务结果指标：用了多久、提前多少天订、多少单超标、员工有没有真去下单。

    和 ``/audit-events`` 一样跨员工聚合，所以只对管理员开放。

    这里读的是最近 ``limit`` 个任务的完整聚合和它们的审计事件，因此 ``limit`` 上限
    比审计视图低。它是运营看板用的近期视图，不是评测产物；要留证据请用
    ``examples/run_business_metrics_report.py`` 写进新的报告目录。

    时钟：线上编排器用真实时间，审计时间戳和出发时间在同一条时间线上，所以这里
    不传 ``handoff_reference_time``。冻了时钟的离线跑法必须自己传，理由见
    ``services/evaluation_business`` 模块开头。
    """
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    tasks: list[TripTask] = []
    events: dict[str, tuple[AuditEvent, ...]] = {}
    for summary in workflow.tasks.list_task_summaries(limit=limit):
        try:
            tasks.append(workflow.tasks.get(summary.task_id))
            events[summary.task_id] = workflow.tasks.events(summary.task_id)
        except NotFoundError:
            continue
    report = build_business_metrics_report(
        tasks, events, expense_records=expense_store.list_records(limit=limit)
    )
    payload = report.model_dump(mode="json")
    payload["labels"] = dict(METRIC_LABELS)
    return payload


@app.get("/duty-of-care")
def duty_of_care(
    identity: CurrentIdentity,
    at: datetime | None = None,
    include_completed: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """谁在哪：从已确认行程的航段推出每位旅行者此刻的位置。只对管理员开放。

    只用系统里有的事实——员工回填了下单确认的那份方案。没确认的行程不在这里：
    系统不知道人到底订没订，就不假装知道人在哪。
    """
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    if not 1 <= limit <= 2000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 2000")
    from corporate_travel_agent.services.duty_of_care import whereabouts

    moment = at or workflow.clock()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    rows = whereabouts(
        workflow.trips.list_all(limit=limit), at=moment, include_completed=include_completed
    )
    return {
        "at": moment,
        "travelers": [
            {
                **asdict(item),
                "status": item.status.value,
                "trip_status": item.trip_status.value,
            }
            for item in rows
        ],
    }


@app.get("/budgets")
def budgets(identity: CurrentIdentity, limit: int = 200) -> dict[str, Any]:
    """成本中心预算消耗：政策里的额度、账本里确认过的支出、已交接还没确认的在途金额。

    "在途"是选定方案的价格——人拿着链接去订了、还没回来填单号；它没进账本，
    但看板上要能看见，不然预算会在回填的那一刻突然跳一截。
    """
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    policy = workflow.policies.current()
    ledger = workflow.budget_ledger
    committed: dict[tuple[str, str], Decimal] = {}
    for summary in workflow.tasks.list_task_summaries(limit=limit):
        if summary.state not in {"READY_FOR_HANDOFF", "HANDED_OFF"}:
            continue
        try:
            task = workflow.tasks.get(summary.task_id)
        except NotFoundError:
            continue
        option = task.selected_option()
        if option is None or task.employee.cost_center is None:
            continue
        key = (task.employee.cost_center, option.currency)
        committed[key] = committed.get(key, Decimal("0")) + option.total_cost
    lines = []
    for budget in policy.cost_center_budgets.values():
        spent = (
            ledger.spent(
                budget.cost_center,
                currency=budget.currency,
                period_from=budget.period_from,
                period_to=budget.period_to,
            )
            if ledger is not None
            else None
        )
        in_flight = committed.get((budget.cost_center, budget.currency), Decimal("0"))
        lines.append(
            {
                "cost_center": budget.cost_center,
                "currency": budget.currency,
                "limit": str(budget.amount),
                "period_from": budget.period_from,
                "period_to": budget.period_to,
                "spent": str(spent) if spent is not None else None,
                "committed": str(in_flight),
                "remaining": str(budget.amount - spent) if spent is not None else None,
                "ledger_available": ledger is not None,
            }
        )
    return {"policy_snapshot_id": policy.snapshot_id, "budgets": lines}


@app.get("/trip-tasks/{task_id}/audit-events")
def audit_events(task_id: str, identity: CurrentIdentity) -> list[dict[str, Any]]:
    """列出任务审计事件。"""
    _visible_task(task_id, identity)
    try:
        return [asdict(item) for item in workflow.tasks.events(task_id)]
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/trip-tasks/{task_id}/options/{option_id}/provenance")
def option_provenance(
    task_id: str,
    option_id: str,
    identity: CurrentIdentity,
) -> dict[str, Any]:
    """一条方案的依据链：每一步凭什么，以及哪些地方说不出来（`gaps`）。"""
    _visible_task(task_id, identity)
    try:
        return workflow.option_provenance_record(task_id, option_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/trip-tasks/{task_id}/provenance-check")
def provenance_check(task_id: str, identity: CurrentIdentity) -> dict[str, Any]:
    """重算依据链，和交接时钉住的指纹比对。对不上就是有东西被改过。"""
    _visible_task(task_id, identity)
    try:
        return workflow.verify_provenance(task_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/trip-tasks/{task_id}/inventory-snapshots")
def inventory_snapshots(
    task_id: str,
    identity: CurrentIdentity,
) -> list[dict[str, Any]]:
    """列出任务相关库存快照（脱敏公开视图）。"""
    _visible_task(task_id, identity)
    try:
        return [_public_snapshot(item) for item in workflow.tasks.snapshots(task_id)]
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _visible_task(task_id: str, identity: UserIdentity) -> TripTask:
    """按权限取任务；不可见则 404。"""
    try:
        task = workflow.tasks.get(task_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    if not _can_read_task(identity, task):
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _requester_id(identity: UserIdentity) -> str | None:
    """记到任务上的发起人：员工记员工 ID，管理员记用户 ID；鉴权关闭的开发身份不记。"""
    if identity.employee_id:
        return identity.employee_id
    if identity.has_role(Role.ADMIN) and identity.user_id != "development-system":
        return identity.user_id
    return None


def _can_read_task(identity: UserIdentity, task: TripTask) -> bool:
    """当前身份是否可读该任务：旅行者本人、发起人（代订的助理）、直属经理或当前审批人、管理员。"""
    return (
        identity.has_role(Role.ADMIN)
        or (
            identity.has_role(Role.EMPLOYEE)
            and identity.employee_id in {task.employee.employee_id, task.requested_by}
        )
        or (
            identity.has_role(Role.APPROVER)
            and identity.user_id in {task.employee.manager_id, pending_approver_id(task)}
        )
    )


def _require_can_create(identity: UserIdentity, traveler_id: str) -> None:
    """校验是否可为该出行人创建任务：本人、旅行者委托名单上的人、管理员。"""
    if identity.has_role(Role.ADMIN):
        return
    if identity.has_role(Role.EMPLOYEE) and identity.employee_id:
        if identity.employee_id == traveler_id:
            return
        may_book_for = getattr(workflow.employees, "may_book_for", None)
        if callable(may_book_for) and may_book_for(identity.employee_id, traveler_id):
            return
    raise HTTPException(status_code=403, detail="Cannot create a task for this traveler")


def _require_can_operate(identity: UserIdentity, task: TripTask) -> None:
    """校验是否可操作该任务（消息/选方案等）：旅行者本人、发起人、管理员。"""
    if identity.has_role(Role.ADMIN):
        return
    if (
        identity.has_role(Role.EMPLOYEE)
        and identity.employee_id in {task.employee.employee_id, task.requested_by}
    ):
        return
    raise HTTPException(
        status_code=403, detail="Only the traveler or the requester can modify this task"
    )


def _require_can_approve(identity: UserIdentity, task: TripTask) -> None:
    """校验是否可审批该任务：必须是**当前这一级**该批的人。

    分级审批走到财务那一级时，直属经理已经批过了——他不能再替财务批。
    """
    pending = pending_approver_id(task)
    if identity.has_role(Role.APPROVER) and pending is not None and identity.user_id == pending:
        return
    if pending is None and identity.has_role(Role.APPROVER) and (
        identity.user_id == task.employee.manager_id
    ):
        # 不在等审批：让编排器给出准确的 409（"没有待处理的审批"），而不是 403。
        return
    raise HTTPException(status_code=403, detail="Task is outside this approver's scope")


def _public_snapshot(snapshot: InventorySnapshot) -> dict[str, Any]:
    """库存快照的对外脱敏序列化。"""
    result = asdict(snapshot)
    raw_response = snapshot.raw_response
    result["raw_response"] = (
        {
            "archived": True,
            "sha256": raw_response.sha256,
            "size_bytes": raw_response.size_bytes,
            "content_type": raw_response.content_type,
            "stored_at": raw_response.stored_at,
            "retention_until": raw_response.retention_until,
            "access_policy": raw_response.access_policy,
        }
        if raw_response
        else {"archived": False}
    )
    return result


def _run(operation: Any) -> dict[str, Any]:
    """执行 Orchestrator 操作并映射领域异常为 HTTP。"""
    try:
        return _public_task(operation())
    except LanguageModelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ConcurrentUpdateError, WorkflowError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _visible_task_summaries(
    identity: UserIdentity,
    *,
    state: str | None,
    limit: int,
):
    from corporate_travel_agent.services.task_projections import TaskSummary

    if identity.has_role(Role.ADMIN):
        return workflow.tasks.list_task_summaries(state=state, limit=limit)
    if identity.has_role(Role.EMPLOYEE) and identity.employee_id:
        # 我是旅行者的，加上我替别人发起的。
        return workflow.tasks.list_task_summaries(
            involving_employee_id=identity.employee_id,
            state=state,
            limit=limit,
        )
    if identity.has_role(Role.APPROVER):
        return workflow.tasks.list_task_summaries(
            manager_id=identity.user_id,
            state=state,
            limit=limit,
        )
    empty: tuple[TaskSummary, ...] = ()
    return empty


def _public_task_summary(item: Any) -> dict[str, Any]:
    """任务列表项的公开摘要。"""
    return {
        "task_id": item.task_id,
        "state": item.state,
        "employee_id": item.employee_id,
        "manager_id": item.manager_id,
        "policy_snapshot_id": item.policy_snapshot_id,
        "request_version": item.request_version,
        "selected_option_id": item.selected_option_id,
        "clarification_rounds": item.clarification_rounds,
        "option_count": item.option_count,
        "failure": item.failure,
        "provider_retry": {
            "next_retry_at": item.next_retry_at,
            "delayed_retry_count": item.delayed_retry_count,
        },
        "updated_at": item.updated_at,
        "pending_approver_id": item.pending_approver_id,
        "requester_id": item.requester_id,
        "summary": True,
    }


def _public_task(task: TripTask) -> dict[str, Any]:
    """任务详情的公开视图（含方案、澄清题等）。"""
    return {
        "task_id": task.task_id,
        "intent_entrypoint": task.metadata.get("intent_entrypoint", "legacy"),
        "state": task.state,
        "traveler_id": task.employee.employee_id,
        # 谁发起的；代订时和旅行者不是同一个人。差标、审批、预算全看旅行者。
        "requester_id": task.requested_by,
        "is_delegated": task.is_delegated,
        # 属于哪趟差旅；改期任务还记它改的是哪个任务、因为哪条事件。
        "trip_id": task.trip_id,
        "parent_task_id": task.parent_task_id,
        "change_event_id": task.change_event_id,
        "is_change_task": task.is_change_task,
        "change_event": task.metadata.get("change_event"),
        "request_version": task.request.version if task.request else None,
        "booking_scope": (
            task.request.resolved_booking_scope
            if task.request is not None
            else task.intent_fields.get("booking_scope")
        ),
        "client_location": task.request.client_location if task.request else None,
        "commitments": (
            [asdict(item) for item in task.request.commitments]
            if task.request is not None
            else []
        ),
        "transport_legs": (
            [asdict(leg) for leg in task.request.transport_legs()]
            if task.request is not None
            else []
        ),
        "failure": task.failure,
        "failure_details": task.metadata.get("no_feasible_reasons", ()),
        "provider_retry": workflow.provider_retry_status(task),
        "coverage_notices": task.metadata.get(PARTIAL_COVERAGE_METADATA_KEY, ()),
        "intent_fields": task.intent_fields,
        "missing_required_fields": task.missing_required_fields,
        "conflicts": task.intent_conflicts,
        "assumptions": task.assumptions,
        "clarification_question": task.clarification_question,
        "clarification_questions": task.metadata.get("clarification_questions") or [],
        "uncertain_slots": task.metadata.get("uncertain_slots") or [],
        "clarification_rounds": task.clarification_rounds,
        "manipulation_detected": bool(task.metadata.get("manipulation_detected")),
        "tool_budget": {
            "limit": task.tool_call_limit,
            "used": task.tool_calls_used,
            "remaining": task.tool_calls_remaining,
            "blocked": task.state.value == "TOOL_BUDGET_EXHAUSTED",
            "calls": [asdict(item) for item in task.tool_calls],
        },
        "messages": [
            {
                "role": item.role,
                "content": item.content,
                "created_at": item.created_at,
            }
            for item in task.messages
        ],
        "original_instruction": next(
            (item.content for item in task.messages if item.role == "user"),
            None,
        ),
        # 排序是怎么算出来的：把价格和时长折算到同一个分数上的那个比例，
        # 以及此刻真正生效的整程偏好。
        #
        # **分数本身每条方案上已经有了（`score`），缺的是"分数怎么来的"。**
        # 少了这个比例，客户端只能拿 score - cost - penalty 反推，除零就崩——
        # 与其让每个客户端各猜一遍，不如把这个数说出口：它是一个写明的选择，
        # 不是自然常数（见 planning/preferences.py 的注释）。
        "scoring": (
            {
                "minutes_per_unit": duration_minutes_per_unit(task.request),
                "journey_preferences": sorted(task.request.journey_wide_preferences()),
            }
            if task.request is not None
            else None
        ),
        # 工具循环写给旅行者的那段话：推荐理由 + 还没定的事。
        #
        # **这里只是把已经存在的东西读出来。** 它不进 `task.messages`，所以不会进
        # 下一轮喂给模型的对话，模型行为逐字不变——但聊天视图要靠它，助手才有话说：
        # 全都定下来时（没有未决问题）对话里一条助手消息都不会有，用户只看得到
        # 自己说过的话和一堆卡片。
        # 这趟任务用的习惯画像，以及每条习惯凭什么成立。
        #
        # 摆出来是必须的：画像会改排序，改了排序就得说得出理由。员工问"你凭什么
        # 觉得我要坐高铁"，答案就在每条的 `evidence` 里。没接历史来源时是 null。
        "travel_profile": task.metadata.get("travel_profile"),
        # 规划那一刻钉住的预算余额；没接账本、没成本中心或政策没配预算时是 null。
        "budget_snapshot": task.metadata.get("budget_snapshot"),
        "agentic_proposal": task.metadata.get("agentic_proposal"),
        "extract_failure": task.metadata.get("extract_failure"),
        "model_fallback": task.metadata.get("model_fallback"),
        "options": [
            {
                "option_id": item.option_id,
                "version": item.version,
                "trip_request_version": item.trip_request_version,
                "inventory_snapshot_ids": item.inventory_snapshot_ids,
                "inventory_refs": item.inventory_refs,
                # 完整的有序航段列表。三段以上只有这里读得到——
                # outbound / inbound 仍然照旧给，前端不改也能跑。
                "legs": [_public_transport_offer(leg) for leg in item.legs],
                # 这条方案要买几张票、各多少钱。**展示价格读这里，不要逐段读 price**
                # ——整票只有一个价，记在它第一段上，其余段为 0。
                "fares": [
                    {"fare_ref": ref, "total": total} for ref, total in item.fares
                ],
                "outbound": _public_transport_offer(item.outbound),
                "inbound": (
                    _public_transport_offer(item.inbound) if item.inbound else None
                ),
                # 完整的有序住宿列表，一站一条。两处以上只有这里读得到——
                # hotel 仍然照旧给第一处，前端不改也能跑。
                "stays": [_public_hotel_offer(stay) for stay in item.stays],
                "hotel": _public_hotel_offer(item.hotel) if item.hotel else None,
                "total_cost": item.total_cost,
                "total_duration_minutes": item.total_duration_minutes,
                "currency": item.currency,
                "feasibility": asdict(item.feasibility),
                "preference_penalty": item.preference_penalty,
                "score": item.score,
                "policy_outcome": item.policy_decision.outcome,
                "rule_evidence": [
                    {
                        **asdict(rule),
                        # `overage_amount` 是属性不是字段，`asdict` 带不出来。
                        # 前端要的正是这个数——超出差标多少——所以显式补上。
                        "overage_amount": rule.overage_amount,
                    }
                    for rule in item.policy_decision.evidence
                ],
                # 选它要付出什么：贵多少、超标多少、该谁批、换哪条能省。
                # 从已经存在的方案里算，不查库存、不花工具预算，所以不进持久化——
                # 每次读的时候按当时的方案列表现算。
                "cost_guidance": asdict(guidance),
                "facts": item.explanation_facts,
            }
            for item, guidance in zip(task.options, _option_cost_guidance(task), strict=True)
        ],
        "selected_option_id": task.selected_option_id,
        "approval": asdict(task.approval) if task.approval else None,
        "booking_intent": asdict(task.booking_intent) if task.booking_intent else None,
        "booking_confirmation": _public_booking_confirmation(task),
        # 费控对账结果；没对过账是 null。对上了，自述才算"核实过"。
        "expense_reconciliation": (
            {
                **asdict(task.expense_reconciliation),
                "amount_variance": (
                    task.expense_reconciliation.amount_variance(task.booking_confirmation)
                    if task.booking_confirmation is not None
                    else None
                ),
            }
            if task.expense_reconciliation is not None
            else None
        ),
        "summary": False,
    }


def _public_booking_confirmation(task: TripTask) -> dict[str, Any] | None:
    """回填记录，外加"方案价多少 / 实付多少 / 差多少"。差额读时现算，不进持久化。

    币种对不上时 `cost_variance` 是 null，方案价照给——读的人自己看得出两个币种不同，
    系统不替他换算。
    """
    confirmation = task.booking_confirmation
    if confirmation is None:
        return None
    option = task.confirmed_option()
    return {
        **asdict(confirmation),
        "planned_total": option.total_cost if option is not None else None,
        "planned_currency": option.currency if option is not None else None,
        "cost_variance": task.booking_cost_variance(),
    }


def _option_cost_guidance(task: TripTask) -> tuple[CostGuidance, ...]:
    """算这批方案各自的代价说明。

    审批人取员工快照上的直属经理——和真正创建 `ApprovalRequest` 时用的是同一个来源
    （`task.employee.manager_id`），所以卡片上写的"该谁批"和后面真正落到谁头上
    不会是两个人。
    """
    return build_cost_guidance(task.options, approver_id=task.employee.manager_id)


def _public_transport_offer(offer: Any) -> dict[str, Any]:
    """Serialize the itinerary fields needed by clients without provider internals."""
    return {
        "ref_id": offer.ref_id,
        "snapshot_id": offer.snapshot_id,
        "provider": offer.provider,
        "mode": offer.mode,
        "origin": offer.origin,
        "destination": offer.destination,
        "depart_at": offer.depart_at,
        "arrive_at": offer.arrive_at,
        "price": offer.price,
        "seat_class": offer.seat_class,
        "available": offer.available,
        "is_direct": offer.is_direct,
        "currency": offer.currency,
        # 这一段属于哪张票。null = 它自己就是一张票（分段购买）。
        # 同一个 fare_ref 的几段是一张整票，不能拆开——**price 只在第一段上**。
        "fare_ref": offer.fare_ref,
    }


def _public_hotel_offer(offer: Any) -> dict[str, Any]:
    """Serialize hotel evidence used by the plan and approval screens."""
    return {
        "ref_id": offer.ref_id,
        "snapshot_id": offer.snapshot_id,
        "provider": offer.provider,
        "name": offer.name,
        "city": offer.city,
        "check_in": offer.check_in,
        "check_out": offer.check_out,
        "nightly_price": offer.nightly_price,
        "nights": offer.nights,
        "total_price": offer.total_price,
        "commute_minutes": offer.commute_minutes,
        "commute_known": offer.commute_known,
        "available": offer.available,
        "currency": offer.currency,
    }


def _to_request(payload: TripCreate, *, task_id: str, version: int) -> TripRequestVersion:
    """把 TripCreate 载荷转为 TripRequestVersion。"""
    return TripRequestVersion(
        task_id=task_id,
        version=version,
        traveler_id=payload.traveler_id,
        origin=payload.origin,
        destination=payload.destination,
        departure_after=payload.departure_after,
        arrive_by=payload.arrive_by,
        return_after=payload.return_after,
        return_before=payload.return_before,
        hotel_check_in=payload.hotel_check_in,
        hotel_check_out=payload.hotel_check_out,
        hard_constraints=tuple(payload.hard_constraints),
        soft_preferences=tuple(payload.soft_preferences),
        booking_scope=payload.booking_scope,
    )

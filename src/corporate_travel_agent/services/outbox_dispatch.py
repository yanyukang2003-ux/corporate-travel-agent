"""发件箱投递：把 `outbox_events` 表里还没发出去的事件交给通道，成功标记、失败计次。

## 为什么是"通道"而不是"OA 客户端"

企业侧的审批系统、费控系统各不相同，本系统能承诺的只有一件事：**每一条该发的通知
至少发出去一次，而且可追踪**。所以这里的抽象是"通道"（`OutboxChannel`）——拿到一条
事件、要么投递成功、要么抛异常。三种实现：

- `LoggingChannel`：记在内存里。没接任何外部系统时的默认通道，收件箱本身就是投递。
- `WebhookChannel`：HTTP POST 到企业配置的地址，带事件 ID 和 HMAC 签名——接真实 OA
  或费控系统时用这个。它不知道对方是谁，也不该知道。
- `SimulatedApprovalSystemChannel`：仓库内的"外部审批系统"模拟：收到审批请求就记成待办，
  由它决定后**回调**本系统。demo 和测试用它证明这条链路是通的——端口是真的，
  只是对面那个系统是我们自己扮的。

## 口径

- 至少一次，不是恰好一次：通道必须按 `event_id` 幂等。
- 失败计次，超过 `max_attempts` 就不再重试（死信），但**不标已发布**：死信是运维要看的事实。
- 一次 `dispatch_once` 处理一批；worker 循环调它，API 也可以由管理员手动触发一轮。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from corporate_travel_agent.services.outbox_events import OutboxEvent, OutboxStore

APPROVAL_EVENT_TYPES = frozenset(
    {"APPROVAL_REQUESTED", "APPROVAL_STEP_REQUESTED", "APPROVAL_DECIDED", "APPROVAL_INVALIDATED"}
)


class OutboxChannel(Protocol):
    """一个投递目标。投递失败就抛异常；成功就静默返回。"""

    name: str

    def deliver(self, event: OutboxEvent) -> None: ...


class DeliveryError(RuntimeError):
    """通道明确报告的投递失败。"""


class LoggingChannel:
    """把事件记在内存里。没接外部系统时的默认通道。"""

    name = "log"

    def __init__(self) -> None:
        self.delivered: list[OutboxEvent] = []

    def deliver(self, event: OutboxEvent) -> None:
        self.delivered.append(event)


class WebhookChannel:
    """HTTP POST 到企业侧地址。请求体是事件本身；头里带事件 ID、类型和 HMAC-SHA256 签名。"""

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.url = url
        self._secret = secret.encode() if secret else None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def deliver(self, event: OutboxEvent) -> None:
        body = json.dumps(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "created_at": event.created_at.isoformat(),
                "payload": event.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Outbox-Event-Id": event.event_id,
            "X-Outbox-Event-Type": event.event_type,
        }
        if self._secret is not None:
            headers["X-Outbox-Signature"] = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        try:
            response = self._client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"webhook transport error: {type(exc).__name__}") from exc
        if not 200 <= response.status_code < 300:
            raise DeliveryError(f"webhook returned HTTP {response.status_code}")


@dataclass(slots=True)
class SimulatedApprovalItem:
    """模拟审批系统里的一条待办。"""

    event_id: str
    task_id: str
    approval_id: str
    step_index: int
    step_label: str
    approver_id: str
    approved_price: str
    currency: str | None
    violations: tuple[str, ...]
    business_reason: str
    received_at: datetime


class SimulatedApprovalSystemChannel:
    """仓库内扮演的"外部审批系统"。

    收到 `APPROVAL_REQUESTED` / `APPROVAL_STEP_REQUESTED` 就记一条待办；收到
    `APPROVAL_DECIDED` / `APPROVAL_INVALIDATED` 就把对应待办清掉。它自己决定之后，
    通过 `decide()` **回调**本系统——和真实 OA 调 `POST /approvals/{task_id}/decision`
    是同一个动作。按 `event_id` 幂等：同一条事件送两次只记一次。
    """

    name = "simulated-oa"

    def __init__(self, decide: Callable[..., Any]) -> None:
        self._decide = decide
        self._pending: dict[tuple[str, int], SimulatedApprovalItem] = {}
        self._seen: set[str] = set()
        self.log: list[tuple[str, str]] = []

    def deliver(self, event: OutboxEvent) -> None:
        if event.event_id in self._seen:
            return
        self._seen.add(event.event_id)
        payload = event.payload
        key = (str(payload.get("approval_id")), int(payload.get("step_index", 0)))
        if event.event_type in {"APPROVAL_REQUESTED", "APPROVAL_STEP_REQUESTED"}:
            # 走到下一级，说明上一级已经定了：它的待办撤掉，只留当前这一级。
            for earlier in [k for k in self._pending if k[0] == key[0] and k[1] < key[1]]:
                self._pending.pop(earlier, None)
            self._pending[key] = SimulatedApprovalItem(
                event_id=event.event_id,
                task_id=str(payload["task_id"]),
                approval_id=str(payload["approval_id"]),
                step_index=int(payload.get("step_index", 0)),
                step_label=str(payload.get("step_label", "")),
                approver_id=str(payload["approver_id"]),
                approved_price=str(payload.get("approved_price", "")),
                currency=payload.get("currency"),
                violations=tuple(payload.get("violations") or ()),
                business_reason=str(payload.get("business_reason", "")),
                received_at=event.created_at,
            )
        elif event.event_type in {"APPROVAL_DECIDED", "APPROVAL_INVALIDATED"}:
            approval_id = str(payload.get("approval_id"))
            for pending_key in [k for k in self._pending if k[0] == approval_id]:
                self._pending.pop(pending_key, None)
        self.log.append((event.event_type, event.event_id))

    def pending(self) -> tuple[SimulatedApprovalItem, ...]:
        return tuple(sorted(self._pending.values(), key=lambda item: item.received_at))

    def decide(self, task_id: str, *, approved: bool, reason: str) -> Any:
        """模拟系统里的人做了决定：以待办上记着的审批人身份回调本系统。"""
        item = next((it for it in self._pending.values() if it.task_id == task_id), None)
        if item is None:
            raise KeyError(f"no pending approval for task {task_id} in the simulated system")
        return self._decide(
            task_id, approver_id=item.approver_id, approved=approved, reason=reason
        )


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """一轮投递的结果。"""

    delivered: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    dead_lettered: tuple[str, ...] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "delivered": list(self.delivered),
            "failed": [{"event_id": event_id, "error": error} for event_id, error in self.failed],
            "dead_lettered": list(self.dead_lettered),
        }


class OutboxDispatcher:
    """一轮拉一批未发布事件，按事件类型选通道投递。"""

    def __init__(
        self,
        store: OutboxStore,
        *,
        default_channel: OutboxChannel,
        routes: Mapping[str, OutboxChannel] | None = None,
        max_attempts: int = 5,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.store = store
        self.default_channel = default_channel
        self.routes = dict(routes or {})
        self.max_attempts = max_attempts
        self.clock = clock or (lambda: datetime.now(UTC))

    def channel_for(self, event_type: str) -> OutboxChannel:
        return self.routes.get(event_type, self.default_channel)

    def dispatch_once(self, *, limit: int = 50) -> DispatchReport:
        delivered: list[str] = []
        failed: list[tuple[str, str]] = []
        dead: list[str] = []
        started = self.clock()
        for event in self.store.list_unpublished(limit=limit):
            if event.attempt_count >= self.max_attempts:
                dead.append(event.event_id)
                continue
            channel = self.channel_for(event.event_type)
            try:
                channel.deliver(event)
            except Exception as exc:  # noqa: BLE001 — 任何投递异常都只是这一条失败计次
                error = f"{channel.name}: {type(exc).__name__}: {exc}"
                self.store.mark_failed(event.event_id, error)
                failed.append((event.event_id, error))
                continue
            self.store.mark_published(event.event_id, published_at=self.clock())
            delivered.append(event.event_id)
        return DispatchReport(
            delivered=tuple(delivered),
            failed=tuple(failed),
            dead_lettered=tuple(dead),
            started_at=started,
        )

    def dead_letters(self, *, limit: int = 50) -> tuple[OutboxEvent, ...]:
        """试过 `max_attempts` 次还没发出去的事件。它们不再自动重试，但也不会被标成已发布。"""
        return tuple(
            event
            for event in self.store.list_unpublished(limit=limit)
            if event.attempt_count >= self.max_attempts
        )

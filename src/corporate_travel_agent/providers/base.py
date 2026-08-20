"""Provider 基座：错误类型、查询模型与库存适配器 Protocol。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Protocol

from corporate_travel_agent.domain.models import (
    InventorySnapshot,
    ProviderHandoff,
    RevalidationResult,
    TravelOptionVersion,
)


class ProviderError(RuntimeError):
    """Provider 失败；绝不可被解释为空库存。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "PROVIDER_ERROR",
        layer: str = "provider_adapter",
        cause_type: str | None = None,
        cause_chain: tuple[str, ...] = (),
        retryable: bool = False,
        response_received: bool | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.layer = layer
        self.cause_type = cause_type
        self.cause_chain = cause_chain
        self.retryable = retryable
        self.response_received = response_received
        self.http_status = http_status
        self.request_id = request_id

    def trace_details(self) -> dict[str, Any]:
        """导出可写入追踪日志的结构化错误字段。"""
        return {
            "error_code": self.error_code,
            "error_layer": self.layer,
            "cause_type": self.cause_type,
            "cause_chain": self.cause_chain,
            "retryable": self.retryable,
            "response_received": self.response_received,
            "http_status": self.http_status,
            "request_id": self.request_id,
        }


class RetryableProviderError(ProviderError):
    """瞬时 Provider 失败，有界重试可能恢复。

    仅在重复同一请求安全且前次无副作用时抛出；其余情况保持普通
    ProviderError，首次即 fail-closed。
    """

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs["retryable"] = True
        super().__init__(message, **kwargs)


@dataclass(frozen=True, slots=True)
class TransportSearchQuery:
    """交通库存搜索查询（起讫点与出发/到达窗口）。"""

    origin: str
    destination: str
    depart_after: datetime
    arrive_before: datetime | None


@dataclass(frozen=True, slots=True)
class HotelSearchQuery:
    """酒店库存搜索查询（城市与入住/离店日期）。"""

    city: str
    check_in: date
    check_out: date


class TravelInventoryProvider(Protocol):
    """差旅库存适配器协议：搜索、重校验与 deep-link 交接。"""

    name: str

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot: ...

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot: ...

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult: ...

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff: ...


def inventory_query_hash(query: TransportSearchQuery | HotelSearchQuery) -> str:
    """返回 live / mock / replay 共用的规范化查询哈希键。"""

    query_json = json.dumps(asdict(query), default=str, sort_keys=True)
    return hashlib.sha256(query_json.encode()).hexdigest()

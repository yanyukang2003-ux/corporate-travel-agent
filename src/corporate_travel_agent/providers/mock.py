"""Mock Provider：可控库存，用于确定性工作流与测试。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from corporate_travel_agent.domain.enums import (
    RawResponseAccessPolicy,
    RevalidationStatus,
    SourceType,
)
from corporate_travel_agent.domain.models import (
    HotelOffer,
    InventorySnapshot,
    ProviderHandoff,
    RevalidationResult,
    TransportOffer,
    TravelOptionVersion,
)
from corporate_travel_agent.services.object_storage import (
    InMemoryRawResponseObjectStore,
    RawResponseObjectStore,
)

from .base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
    inventory_query_hash,
)


class MockProvider:
    """可控库存 Provider，用于确定性工作流与测试注入故障/变价。"""

    name = "mock"

    def __init__(
        self,
        transports: list[TransportOffer],
        hotels: list[HotelOffer],
        *,
        now: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
        raw_response_store: RawResponseObjectStore | None = None,
        raw_response_retention_days: int = 90,
    ) -> None:
        if now is not None and clock is not None:
            raise ValueError("Supply either now or clock, not both")
        self._transports = {item.ref_id: item for item in transports}
        self._hotels = {item.ref_id: item for item in hotels}
        self._clock = clock or (lambda: now or datetime.now(UTC))
        if raw_response_retention_days < 1:
            raise ValueError("raw_response_retention_days must be positive")
        self.raw_response_store = raw_response_store or InMemoryRawResponseObjectStore()
        self.raw_response_retention_days = raw_response_retention_days
        self.price_overrides: dict[str, Decimal] = {}
        self.unavailable_refs: set[str] = set()
        self.fail_search = False
        self.fail_search_remaining = 0  # 失败 N 次后成功
        self.fail_revalidation = False
        self.inventory_valid_until: datetime | None = None
        self.provider_warnings: tuple[str, ...] = ()
        self.handoff_expires_at: datetime | None = None

    def _maybe_fail_search(self) -> None:
        if self.fail_search:
            raise ProviderError("mock transport search timed out")
        if self.fail_search_remaining > 0:
            self.fail_search_remaining -= 1
            # 瞬时超时，供编排层做有界重试
            raise RetryableProviderError("mock transport search timed out")

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        """按起讫点与时间窗口过滤内存交通库存。"""
        self._maybe_fail_search()
        items = tuple(
            item
            for item in self._transports.values()
            if item.origin == query.origin
            and item.destination == query.destination
            and item.depart_at >= query.depart_after
            and (query.arrive_before is None or item.arrive_at <= query.arrive_before)
        )
        return self._snapshot("transport", query, items)

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        """按城市与入住覆盖日期过滤内存酒店库存。"""
        self._maybe_fail_search()
        items = tuple(
            item
            for item in self._hotels.values()
            if item.city == query.city
            and item.check_in <= query.check_in
            and item.check_out >= query.check_out
        )
        return self._snapshot("hotel", query, items)

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult:
        """对照 price_overrides / unavailable_refs 模拟价格变更与不可用。"""
        checked_at = self._clock()
        if self.fail_revalidation:
            return RevalidationResult(
                status=RevalidationStatus.PROVIDER_FAILED,
                checked_at=checked_at,
                current_prices={},
                warnings=("revalidation unavailable",),
            )

        unavailable = tuple(sorted(set(refs) & self.unavailable_refs))
        prices: dict[str, Decimal] = {}
        changed = False
        for ref in refs:
            item = self._transports.get(ref) or self._hotels.get(ref)
            if item is None:
                unavailable = tuple(sorted((*unavailable, ref)))
                continue
            original = item.price if isinstance(item, TransportOffer) else item.nightly_price
            current = self.price_overrides.get(ref, original)
            prices[ref] = current
            changed = changed or current != original

        status = RevalidationStatus.UNCHANGED
        if unavailable:
            status = RevalidationStatus.UNAVAILABLE
        elif changed:
            status = RevalidationStatus.PRICE_CHANGED
        return RevalidationResult(
            status=status,
            checked_at=checked_at,
            current_prices=prices,
            unavailable_refs=unavailable,
        )

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff:
        """生成示例 deep-link handoff（不可用于真实预订）。"""
        query = urlencode({"refs": ",".join(option.inventory_refs), "mode": "user-confirmed"})
        created_at = self._clock()
        expires_at = self.handoff_expires_at or (created_at + timedelta(minutes=10))
        return ProviderHandoff(
            provider=self.name,
            url_or_instructions=f"https://example.invalid/travel-handoff?{query}",
            expires_at=expires_at,
        )

    def _snapshot(
        self,
        category: str,
        query: object,
        items: tuple[TransportOffer | HotelOffer, ...],
    ) -> InventorySnapshot:
        """归档原始响应并构造带 snapshot_id 的 InventorySnapshot。"""
        captured_at = self._clock()
        query_hash = inventory_query_hash(query)
        raw_payload = {
            "category": category,
            "provider": self.name,
            "query": asdict(query),
            "results": [asdict(item) for item in items],
        }
        raw_bytes = json.dumps(
            raw_payload,
            default=str,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        snapshot_identity = "|".join(
            (category, query_hash, captured_at.isoformat(), raw_hash)
        )
        snapshot_suffix = hashlib.sha256(snapshot_identity.encode()).hexdigest()[:12]
        snapshot_id = f"mock-{category}-{snapshot_suffix}"
        object_key = (
            f"{self.name}/{captured_at:%Y/%m/%d}/{category}/{snapshot_id}.json"
        )
        raw_response = self.raw_response_store.put_bytes(
            object_key=object_key,
            content=raw_bytes,
            content_type="application/json",
            stored_at=captured_at,
            retention_until=captured_at
            + timedelta(days=self.raw_response_retention_days),
            access_policy=RawResponseAccessPolicy.SYSTEM_REPLAY_OR_AUDIT_ADMIN,
        )
        normalized = tuple(replace(item, snapshot_id=snapshot_id) for item in items)
        valid_until = self.inventory_valid_until or (captured_at + timedelta(minutes=15))
        return InventorySnapshot(
            snapshot_id=snapshot_id,
            provider=self.name,
            source_type=SourceType.MOCK,
            captured_at=captured_at,
            valid_until=valid_until,
            query_hash=query_hash,
            raw_payload_hash=raw_hash,
            items=normalized,
            raw_response=raw_response,
            provider_warnings=self.provider_warnings,
        )

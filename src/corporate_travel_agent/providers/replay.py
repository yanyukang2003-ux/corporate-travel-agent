"""回放 Provider：按确定性查询哈希返回不可变录制快照。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from corporate_travel_agent.domain.enums import RevalidationStatus
from corporate_travel_agent.domain.models import (
    HotelOffer,
    InventorySnapshot,
    ProviderHandoff,
    RevalidationResult,
    TransportOffer,
    TravelOptionVersion,
)

from .base import (
    HotelSearchQuery,
    ProviderError,
    TransportSearchQuery,
    inventory_query_hash,
)


class ReplayProvider:
    """按确定性 query hash 返回不可变录制快照，用于可复现回放。"""

    name = "replay"

    def __init__(self, snapshots: list[InventorySnapshot]) -> None:
        query_hashes = [item.query_hash for item in snapshots]
        if len(query_hashes) != len(set(query_hashes)):
            raise ValueError("Replay snapshots must have unique query hashes")
        self._snapshots = {item.query_hash: item for item in snapshots}
        self._items = {
            item.ref_id: item
            for snapshot in snapshots
            for item in snapshot.items
        }

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        return self._lookup(query)

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        return self._lookup(query)

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult:
        """基于录制库存检查可用性并返回当时价格（不发起外部请求）。"""
        unavailable = tuple(
            ref
            for ref in refs
            if ref not in self._items or not self._items[ref].available
        )
        prices: dict[str, Decimal] = {}
        for ref in refs:
            item = self._items.get(ref)
            if isinstance(item, TransportOffer):
                prices[ref] = item.price
            elif isinstance(item, HotelOffer):
                prices[ref] = item.nightly_price
        return RevalidationResult(
            status=(
                RevalidationStatus.UNAVAILABLE
                if unavailable
                else RevalidationStatus.UNCHANGED
            ),
            checked_at=datetime.now(UTC),
            current_prices=prices,
            unavailable_refs=unavailable,
        )

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff:
        """回放数据不可下单，仅返回需在官方平台重跑的说明。"""
        return ProviderHandoff(
            provider=self.name,
            url_or_instructions=(
                "Replay data cannot book. Re-run these references on the official platform: "
                + ", ".join(option.inventory_refs)
            ),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    def _lookup(self, query: TransportSearchQuery | HotelSearchQuery) -> InventorySnapshot:
        query_hash = inventory_query_hash(query)
        try:
            return self._snapshots[query_hash]
        except KeyError as exc:
            raise ProviderError(f"No replay snapshot for query {query_hash[:12]}") from exc

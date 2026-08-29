"""组合 Provider：将交通与酒店库存分别委托给独立适配器。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from corporate_travel_agent.domain.enums import RevalidationStatus
from corporate_travel_agent.domain.models import (
    InventorySnapshot,
    ProviderHandoff,
    RevalidationResult,
    TravelOptionVersion,
)

from .base import HotelSearchQuery, ProviderError, TransportSearchQuery, TravelInventoryProvider


class _ReferenceOwningProvider(TravelInventoryProvider, Protocol):
    provider_mode: str

    def owns_ref(self, ref: str) -> bool: ...


class CompositeTravelInventoryProvider:
    """把交通与酒店库存分别委托给独立 Provider 适配器。"""

    def __init__(
        self,
        *,
        transport_provider: _ReferenceOwningProvider,
        hotel_provider: _ReferenceOwningProvider,
    ) -> None:
        self.transport_provider = transport_provider
        self.hotel_provider = hotel_provider
        self.name = f"{transport_provider.name}+{hotel_provider.name}"
        self.provider_mode = (
            f"{transport_provider.provider_mode}+{hotel_provider.provider_mode}"
        )

    def close(self) -> None:
        """关闭子 Provider（若其实现了 close）。"""
        for provider in (self.transport_provider, self.hotel_provider):
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        return self.transport_provider.search_transport(query)

    def search_multi_city(
        self, queries: Sequence[TransportSearchQuery]
    ) -> InventorySnapshot:
        """整票搜索透传给交通 Provider；它不支持就当作"这条路没有"。"""
        search = getattr(self.transport_provider, "search_multi_city", None)
        if search is None:
            raise ProviderError("transport provider does not support multi-city search")
        return search(queries)

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        return self.hotel_provider.search_hotels(query)

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult:
        """按归属拆分 refs，合并各子 Provider 的重校验结果。"""
        partitions = self._partition(refs)
        results: list[RevalidationResult] = []
        for provider, owned_refs in partitions:
            if owned_refs:
                results.append(provider.revalidate(owned_refs))

        unknown = tuple(
            ref
            for ref in dict.fromkeys(refs)
            if not self.transport_provider.owns_ref(ref)
            and not self.hotel_provider.owns_ref(ref)
        )
        if not results:
            return RevalidationResult(
                status=RevalidationStatus.UNAVAILABLE,
                checked_at=datetime.now().astimezone(),
                current_prices={},
                unavailable_refs=unknown,
                warnings=("No selected references belong to a configured provider",),
            )

        current_prices = {}
        unavailable: list[str] = list(unknown)
        warnings: list[str] = []
        for result in results:
            duplicates = set(current_prices) & set(result.current_prices)
            if duplicates:
                raise ProviderError(
                    "Multiple providers returned the same inventory reference: "
                    + ", ".join(sorted(duplicates))
                )
            current_prices.update(result.current_prices)
            unavailable.extend(result.unavailable_refs)
            warnings.extend(result.warnings)
        if unknown:
            warnings.append("Unknown inventory references: " + ", ".join(unknown))

        # 失败态优先：PROVIDER_FAILED > UNAVAILABLE > PRICE_CHANGED > UNCHANGED
        statuses = {result.status for result in results}
        status = RevalidationStatus.UNCHANGED
        if RevalidationStatus.PROVIDER_FAILED in statuses:
            status = RevalidationStatus.PROVIDER_FAILED
        elif unknown or RevalidationStatus.UNAVAILABLE in statuses:
            status = RevalidationStatus.UNAVAILABLE
        elif RevalidationStatus.PRICE_CHANGED in statuses:
            status = RevalidationStatus.PRICE_CHANGED
        return RevalidationResult(
            status=status,
            checked_at=max(result.checked_at for result in results),
            current_prices=current_prices,
            unavailable_refs=tuple(dict.fromkeys(unavailable)),
            warnings=tuple(warnings),
        )

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff:
        """合并交通与酒店侧 handoff 说明，过期时间取最早。"""
        handoffs: list[ProviderHandoff] = []
        if any(self.transport_provider.owns_ref(ref) for ref in option.inventory_refs):
            handoffs.append(self.transport_provider.create_deep_link(option))
        if any(self.hotel_provider.owns_ref(ref) for ref in option.inventory_refs):
            handoffs.append(self.hotel_provider.create_deep_link(option))
        if not handoffs:
            raise ProviderError("The selected option contains no recognized provider references")
        return ProviderHandoff(
            provider=self.name,
            url_or_instructions=" | ".join(
                f"{handoff.provider}: {handoff.url_or_instructions}" for handoff in handoffs
            ),
            expires_at=min(handoff.expires_at for handoff in handoffs),
        )

    def _partition(
        self,
        refs: tuple[str, ...],
    ) -> tuple[
        tuple[_ReferenceOwningProvider, tuple[str, ...]],
        tuple[_ReferenceOwningProvider, tuple[str, ...]],
    ]:
        transport_refs: list[str] = []
        hotel_refs: list[str] = []
        for ref in dict.fromkeys(refs):
            transport_owns = self.transport_provider.owns_ref(ref)
            hotel_owns = self.hotel_provider.owns_ref(ref)
            if transport_owns and hotel_owns:
                raise ProviderError(f"Ambiguous provider ownership for inventory reference {ref}")
            if transport_owns:
                transport_refs.append(ref)
            elif hotel_owns:
                hotel_refs.append(ref)
        return (
            (self.transport_provider, tuple(transport_refs)),
            (self.hotel_provider, tuple(hotel_refs)),
        )

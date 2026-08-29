from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from corporate_travel_agent.domain.enums import RevalidationStatus, SourceType
from corporate_travel_agent.domain.models import (
    HotelOffer,
    InventorySnapshot,
    ProviderHandoff,
    RevalidationResult,
    TransportOffer,
    TravelOptionVersion,
)
from corporate_travel_agent.providers.base import HotelSearchQuery, TransportSearchQuery
from corporate_travel_agent.providers.composite import CompositeTravelInventoryProvider

NOW = datetime(2026, 8, 9, tzinfo=UTC)


@dataclass
class _StubProvider:
    name: str
    provider_mode: str
    prefix: str
    price: Decimal

    def owns_ref(self, ref: str) -> bool:
        return ref.startswith(self.prefix)

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        return _snapshot(self.name, "transport")

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        return _snapshot(self.name, "hotel")

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult:
        return RevalidationResult(
            status=RevalidationStatus.UNCHANGED,
            checked_at=NOW,
            current_prices={ref: self.price for ref in refs},
        )

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff:
        return ProviderHandoff(
            provider=self.name,
            url_or_instructions=f"{self.name} read-only handoff",
            expires_at=NOW + timedelta(minutes=10),
        )


def _snapshot(provider: str, category: str) -> InventorySnapshot:
    del category
    return InventorySnapshot(
        snapshot_id=f"{provider}-snapshot",
        provider=provider,
        source_type=SourceType.AUTHORIZED_API,
        captured_at=NOW,
        valid_until=NOW + timedelta(minutes=10),
        query_hash="query",
        raw_payload_hash="hash",
        items=(),
    )


def test_composite_delegates_search_and_merges_revalidation() -> None:
    transport = _StubProvider("duffel", "test", "off_", Decimal("100"))
    hotel = _StubProvider("liteapi", "sandbox-read-only", "litehotel_", Decimal("200"))
    provider = CompositeTravelInventoryProvider(
        transport_provider=transport,
        hotel_provider=hotel,
    )

    transport_snapshot = provider.search_transport(
        TransportSearchQuery("LHR", "JFK", NOW, NOW + timedelta(days=1))
    )
    hotel_snapshot = provider.search_hotels(
        HotelSearchQuery("JFK", date(2026, 9, 1), date(2026, 9, 2))
    )
    result = provider.revalidate(("off_1", "litehotel_1"))

    assert provider.name == "duffel+liteapi"
    assert provider.provider_mode == "test+sandbox-read-only"
    assert transport_snapshot.provider == "duffel"
    assert hotel_snapshot.provider == "liteapi"
    assert result.status is RevalidationStatus.UNCHANGED
    assert result.current_prices == {
        "off_1": Decimal("100"),
        "litehotel_1": Decimal("200"),
    }


def test_composite_unknown_ref_fails_revalidation_closed() -> None:
    provider = CompositeTravelInventoryProvider(
        transport_provider=_StubProvider("duffel", "test", "off_", Decimal("100")),
        hotel_provider=_StubProvider(
            "liteapi", "sandbox-read-only", "litehotel_", Decimal("200")
        ),
    )

    result = provider.revalidate(("off_1", "unknown_1"))

    assert result.status is RevalidationStatus.UNAVAILABLE
    assert result.unavailable_refs == ("unknown_1",)
    assert any("Unknown inventory references" in warning for warning in result.warnings)


def test_combined_handoff_contains_both_read_only_instructions() -> None:
    provider = CompositeTravelInventoryProvider(
        transport_provider=_StubProvider("duffel", "test", "off_", Decimal("100")),
        hotel_provider=_StubProvider(
            "liteapi", "sandbox-read-only", "litehotel_", Decimal("200")
        ),
    )
    option = _option()

    handoff = provider.create_deep_link(option)

    assert handoff.provider == "duffel+liteapi"
    assert "duffel read-only handoff" in handoff.url_or_instructions
    assert "liteapi read-only handoff" in handoff.url_or_instructions


def _option() -> TravelOptionVersion:
    transport = TransportOffer(
        ref_id="off_1",
        snapshot_id="flight",
        provider="duffel",
        mode="FLIGHT",  # type: ignore[arg-type]
        origin="LHR",
        destination="JFK",
        depart_at=NOW,
        arrive_at=NOW + timedelta(hours=8),
        price=Decimal("100"),
        seat_class="ECONOMY",
    )
    hotel = HotelOffer(
        ref_id="litehotel_1",
        snapshot_id="hotel",
        provider="liteapi",
        name="Hotel",
        city="JFK",
        check_in=date(2026, 9, 1),
        check_out=date(2026, 9, 2),
        nightly_price=Decimal("200"),
        commute_minutes=1_440,
    )
    return TravelOptionVersion(
        option_id="option",
        version=1,
        trip_request_version=1,
        inventory_snapshot_ids=("flight", "hotel"),
        legs=(transport,),
        hotel=hotel,
        total_cost=Decimal("300"),
        total_duration_minutes=480,
        feasibility=None,  # type: ignore[arg-type]
        policy_decision=None,  # type: ignore[arg-type]
        preference_penalty=Decimal("0"),
        score=Decimal("300"),
        explanation_facts=(),
    )

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from corporate_travel_agent.services.provider_quote_context import (
    InMemoryProviderQuoteContextStore,
    ProviderQuoteContext,
    ProviderQuoteContextExpiredError,
    ProviderQuoteContextMissingError,
)

NOW = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)


def _context(ref_id: str = "off_1", *, minutes: int = 10) -> ProviderQuoteContext:
    return ProviderQuoteContext(
        provider="duffel",
        snapshot_id=f"snapshot-{ref_id}",
        ref_id=ref_id,
        price=Decimal("100.00"),
        currency="USD",
        captured_at=NOW,
        expires_at=NOW + timedelta(minutes=minutes),
        payload={"offer_id": ref_id},
    )


def test_memory_store_distinguishes_missing_and_expired_context() -> None:
    store = InMemoryProviderQuoteContextStore()
    with pytest.raises(ProviderQuoteContextMissingError):
        store.get("duffel", "off_missing", observed_at=NOW)

    store.put_many((_context(),))
    with pytest.raises(ProviderQuoteContextExpiredError):
        store.get("duffel", "off_1", observed_at=NOW + timedelta(minutes=10))


def test_memory_store_is_bounded_and_supports_ttl_cleanup() -> None:
    store = InMemoryProviderQuoteContextStore(max_entries=2)
    store.put_many((_context("off_1", minutes=1), _context("off_2", minutes=2)))
    store.put_many((_context("off_3", minutes=3),))

    assert len(store) == 2
    with pytest.raises(ProviderQuoteContextMissingError):
        store.get("duffel", "off_1", observed_at=NOW)
    assert store.purge_expired(before=NOW + timedelta(minutes=2)) == 1
    assert len(store) == 1


def test_context_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        ProviderQuoteContext(
            provider="duffel",
            snapshot_id="snapshot",
            ref_id="off_1",
            price=Decimal("100"),
            currency="USD",
            captured_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            payload={"headers": {"Authorization": "Bearer secret"}},
        )

from __future__ import annotations

import pytest

from corporate_travel_agent.providers.composite import CompositeTravelInventoryProvider
from corporate_travel_agent.providers.factory import travel_provider_from_environment


def test_factory_builds_duffel_liteapi_composite(monkeypatch) -> None:
    monkeypatch.setenv("TRAVEL_PROVIDER", "duffel_liteapi")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_factory")
    monkeypatch.setenv("DUFFEL_LIVE_MODE", "false")
    monkeypatch.setenv("LITEAPI_API_KEY", "sand_factory")
    monkeypatch.setenv("LITEAPI_REQUIRE_SANDBOX", "true")
    monkeypatch.setenv("LIVE_BOOKING_ENABLED", "false")
    monkeypatch.setenv("LITEAPI_BOOKING_ENABLED", "false")

    provider = travel_provider_from_environment()

    assert isinstance(provider, CompositeTravelInventoryProvider)
    assert provider.name == "duffel+liteapi"
    assert provider.provider_mode == "test+sandbox-read-only"
    provider.close()


def test_factory_requires_liteapi_key_for_composite(monkeypatch) -> None:
    monkeypatch.setenv("TRAVEL_PROVIDER", "duffel_liteapi")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_factory")
    monkeypatch.delenv("LITEAPI_API_KEY", raising=False)
    monkeypatch.setenv("LIVE_BOOKING_ENABLED", "false")

    with pytest.raises(RuntimeError, match="LITEAPI_API_KEY"):
        travel_provider_from_environment()


def test_factory_rejects_any_liteapi_booking_enablement(monkeypatch) -> None:
    monkeypatch.setenv("TRAVEL_PROVIDER", "duffel_liteapi")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_factory")
    monkeypatch.setenv("LITEAPI_API_KEY", "sand_factory")
    monkeypatch.setenv("LITEAPI_BOOKING_ENABLED", "true")
    monkeypatch.setenv("LIVE_BOOKING_ENABLED", "false")

    with pytest.raises(RuntimeError, match="outside the current read-only scope"):
        travel_provider_from_environment()


def test_factory_rejects_liteapi_currency_mismatch_with_policy(monkeypatch) -> None:
    monkeypatch.setenv("TRAVEL_PROVIDER", "duffel_liteapi")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_factory")
    monkeypatch.setenv("LITEAPI_API_KEY", "sand_factory")
    monkeypatch.setenv("LITEAPI_CURRENCY", "CNY")
    monkeypatch.setenv("LIVE_BOOKING_ENABLED", "false")
    monkeypatch.setenv("LITEAPI_BOOKING_ENABLED", "false")

    with pytest.raises(
        RuntimeError,
        match="LITEAPI_CURRENCY=CNY must match active policy currency USD",
    ):
        travel_provider_from_environment(policy_currency="USD")


def test_factory_accepts_matching_policy_currency(monkeypatch) -> None:
    monkeypatch.setenv("TRAVEL_PROVIDER", "duffel_liteapi")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_factory")
    monkeypatch.setenv("LITEAPI_API_KEY", "sand_factory")
    monkeypatch.setenv("LITEAPI_CURRENCY", "USD")
    monkeypatch.setenv("DUFFEL_EXPECTED_CURRENCY", "USD")
    monkeypatch.setenv("LIVE_BOOKING_ENABLED", "false")
    monkeypatch.setenv("LITEAPI_BOOKING_ENABLED", "false")

    provider = travel_provider_from_environment(policy_currency="USD")
    assert isinstance(provider, CompositeTravelInventoryProvider)
    provider.close()

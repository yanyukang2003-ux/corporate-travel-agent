from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from corporate_travel_agent.domain.enums import RevalidationStatus, SourceType
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
)
from corporate_travel_agent.providers.liteapi import (
    COMMUTE_UNKNOWN_MINUTES,
    LiteAPIHotelProvider,
)
from corporate_travel_agent.services.object_storage import (
    InMemoryRawResponseObjectStore,
    RawResponseReadContext,
    RawResponseReadPurpose,
)
from corporate_travel_agent.services.provider_quote_context import (
    InMemoryProviderQuoteContextStore,
)
from corporate_travel_agent.services.sqlalchemy_repository import (
    SQLAlchemyProviderQuoteContextStore,
    SQLAlchemyTaskRepository,
)

FIXED_NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
QUERY = HotelSearchQuery(
    city="Shanghai",
    check_in=date(2026, 9, 15),
    check_out=date(2026, 9, 18),
)


def _payload(*, amount: str = "600.00", offer_id: str = "opaque-offer-1") -> dict:
    return {
        "sandbox": True,
        "guestLevel": 0,
        "hotels": [
            {
                "id": "lp-shanghai-1",
                "name": "Shanghai Contract Hotel",
                "address": "1 Test Road",
            }
        ],
        "data": [
            {
                "hotelId": "lp-shanghai-1",
                "roomTypes": [
                    {
                        "offerId": offer_id,
                        "offerRetailRate": {"amount": amount, "currency": "CNY"},
                        "rates": [
                            {
                                "rateId": "rate-1",
                                "name": "Business King Room",
                                "boardName": "Breakfast Included",
                                "mappedRoomId": 101,
                                "retailRate": {
                                    "total": [{"amount": amount, "currency": "CNY"}]
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _provider(handler, *, store=None, require_sandbox: bool = True) -> LiteAPIHotelProvider:
    return LiteAPIHotelProvider(
        "sand_contract_secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        currency="CNY",
        guest_nationality="CN",
        require_sandbox=require_sandbox,
        clock=lambda: FIXED_NOW,
        raw_response_store=store,
    )


def test_hotel_rates_map_to_authorized_snapshot_without_archiving_api_key() -> None:
    store = InMemoryRawResponseObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v3.0/hotels/rates"
        assert request.headers["X-API-Key"] == "sand_contract_secret"
        body = json.loads(request.content)
        assert body == {
            "occupancies": [{"adults": 1}],
            "currency": "CNY",
            "guestNationality": "CN",
            "checkin": "2026-09-15",
            "checkout": "2026-09-18",
            "timeout": 8,
            "maxRatesPerHotel": 3,
            "includeHotelData": True,
            "limit": 10,
            "cityName": "Shanghai",
            "countryCode": "CN",
        }
        return httpx.Response(200, json=_payload())

    provider = _provider(handler, store=store)
    snapshot = provider.search_hotels(QUERY)

    assert snapshot.provider == "liteapi"
    assert snapshot.source_type is SourceType.AUTHORIZED_API
    assert snapshot.valid_until > snapshot.captured_at
    assert len(snapshot.items) == 1
    offer = snapshot.items[0]
    assert offer.name == "Shanghai Contract Hotel"
    assert offer.ref_id.startswith("litehotel_")
    assert offer.snapshot_id == snapshot.snapshot_id
    assert offer.nightly_price == Decimal("200.00")
    assert offer.total_price == Decimal("600.00")
    assert offer.currency == "CNY"
    assert offer.commute_minutes == COMMUTE_UNKNOWN_MINUTES
    assert any("Sandbox" in warning for warning in snapshot.provider_warnings)
    assert any("no prebook" in warning for warning in snapshot.provider_warnings)
    assert snapshot.raw_response is not None
    raw = store.get_bytes(
        snapshot.raw_response,
        context=RawResponseReadContext(
            actor_id="audit",
            roles=frozenset({"audit_admin"}),
            purpose=RawResponseReadPurpose.AUDIT,
        ),
    )
    assert b"sand_contract_secret" not in raw
    assert snapshot.raw_payload_hash == snapshot.raw_response.sha256
    assert provider.external_request_log == [("POST", "/hotels/rates")]


def test_quote_refresh_detects_price_change_without_prebook_or_booking_calls() -> None:
    responses = iter((_payload(amount="600.00"), _payload(amount="630.00")))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "hotelIds" in body:
            assert body["hotelIds"] == ["lp-shanghai-1"]
            assert "cityName" not in body
            assert "limit" not in body
        return httpx.Response(200, json=next(responses))

    provider = _provider(handler)
    snapshot = provider.search_hotels(QUERY)
    ref = snapshot.items[0].ref_id
    result = provider.revalidate((ref,))

    assert result.status is RevalidationStatus.PRICE_CHANGED
    assert result.current_prices == {ref: Decimal("210.00")}
    assert result.unavailable_refs == ()
    assert provider.last_revalidation_result is result
    assert provider.external_request_log == [
        ("POST", "/hotels/rates"),
        ("POST", "/hotels/rates"),
    ]
    assert len(provider.revalidation_raw_responses) == 1
    assert all(
        path not in {"/rates/prebook", "/rates/book"}
        for _, path in provider.external_request_log
    )


def test_quote_refresh_survives_provider_instance_replacement() -> None:
    """A new worker rebuilds the refresh request from shared quote context."""

    context_store = InMemoryProviderQuoteContextStore()
    first = LiteAPIHotelProvider(
        "sand_contract_secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_payload(amount="600.00"))
            )
        ),
        currency="CNY",
        guest_nationality="CN",
        require_sandbox=True,
        clock=lambda: FIXED_NOW,
        quote_context_store=context_store,
    )
    snapshot = first.search_hotels(QUERY)
    second_calls: list[str] = []

    def second_handler(request: httpx.Request) -> httpx.Response:
        second_calls.append(request.url.path)
        body = json.loads(request.content)
        assert body["hotelIds"] == ["lp-shanghai-1"]
        return httpx.Response(200, json=_payload(amount="630.00"))

    second = LiteAPIHotelProvider(
        "sand_contract_secret",
        client=httpx.Client(transport=httpx.MockTransport(second_handler)),
        currency="CNY",
        guest_nationality="CN",
        require_sandbox=True,
        clock=lambda: FIXED_NOW,
        quote_context_store=context_store,
    )
    result = second.revalidate((snapshot.items[0].ref_id,))

    assert second_calls == ["/v3.0/hotels/rates"]
    assert result.status is RevalidationStatus.PRICE_CHANGED


def test_quote_refresh_context_survives_sql_store_restart(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'liteapi-context.db'}"
    repository = SQLAlchemyTaskRepository(database_url)
    repository.create_schema()
    first = LiteAPIHotelProvider(
        "sand_contract_secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_payload(amount="600.00"))
            )
        ),
        currency="CNY",
        guest_nationality="CN",
        require_sandbox=True,
        clock=lambda: FIXED_NOW,
        quote_context_store=SQLAlchemyProviderQuoteContextStore(repository.engine),
    )
    snapshot = first.search_hotels(QUERY)
    repository.dispose()

    restarted = SQLAlchemyTaskRepository(database_url)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_payload(amount="630.00"))

    second = LiteAPIHotelProvider(
        "sand_contract_secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        currency="CNY",
        guest_nationality="CN",
        require_sandbox=True,
        clock=lambda: FIXED_NOW,
        quote_context_store=SQLAlchemyProviderQuoteContextStore(restarted.engine),
    )
    result = second.revalidate((snapshot.items[0].ref_id,))

    assert calls == ["/v3.0/hotels/rates"]
    assert result.status is RevalidationStatus.PRICE_CHANGED
    restarted.dispose()


def test_rotated_offer_token_can_match_the_same_verified_room_signature() -> None:
    responses = iter((_payload(), _payload(offer_id="rotated-offer-2")))
    provider = _provider(
        lambda request: httpx.Response(200, json=next(responses))
    )
    snapshot = provider.search_hotels(QUERY)
    result = provider.revalidate((snapshot.items[0].ref_id,))

    assert result.status is RevalidationStatus.UNCHANGED
    assert any("rotated the offer token" in warning for warning in result.warnings)


def test_missing_sandbox_marker_fails_closed() -> None:
    payload = _payload()
    payload.pop("sandbox")
    provider = _provider(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(ProviderError, match="not explicitly marked as Sandbox"):
        provider.search_hotels(QUERY)


def test_empty_inventory_and_204_are_successful_empty_snapshots() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"sandbox": True, "data": [], "hotels": []}),
            httpx.Response(204),
        )
    )
    provider = _provider(lambda request: next(responses))

    assert provider.search_hotels(QUERY).items == ()


def test_all_rooms_with_wrong_currency_fail_closed_with_currency_message() -> None:
    """When provider expects USD but sandbox returns only CNY, fail with FX guidance."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["currency"] == "USD"
        return httpx.Response(200, json=_payload())  # fixture rates are CNY

    provider = LiteAPIHotelProvider(
        "sand_contract_secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        currency="USD",
        guest_nationality="US",
        require_sandbox=True,
        clock=lambda: FIXED_NOW,
    )
    with pytest.raises(ProviderError, match="expected currency USD") as exc_info:
        provider.search_hotels(QUERY)
    assert "FX snapshot" in str(exc_info.value)
    assert "currency_mismatch" in str(exc_info.value)


def test_unknown_location_and_transport_coverage_fail_explicitly() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="No verified LiteAPI location mapping"):
        provider.search_hotels(
            HotelSearchQuery(
                city="Invented City",
                check_in=QUERY.check_in,
                check_out=QUERY.check_out,
            )
        )
    assert calls == 0


@pytest.mark.parametrize(
    ("city", "iata_code"),
    [("London LHR", "LHR"), ("New York JFK", "JFK"), ("london lhr", "LHR")],
)
def test_exact_trusted_compound_aliases_map_to_iata_codes(
    city: str, iata_code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["iataCode"] == iata_code
        return httpx.Response(200, json={"sandbox": True, "hotels": [], "data": []})

    provider = _provider(handler)
    snapshot = provider.search_hotels(
        HotelSearchQuery(
            city=city,
            check_in=date(2026, 9, 15),
            check_out=date(2026, 9, 16),
        )
    )

    assert snapshot.items == ()


def test_untrusted_compound_airport_label_fails_before_network_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="No verified LiteAPI location mapping"):
        provider.search_hotels(
            HotelSearchQuery(
                city="London Heathrow Airport",
                check_in=date(2026, 9, 15),
                check_out=date(2026, 9, 16),
            )
        )
    assert calls == 0

    with pytest.raises(ProviderError, match="hotel-only"):
        provider.search_transport(
            TransportSearchQuery(
                origin="SHA",
                destination="PEK",
                depart_after=datetime(2026, 9, 15, tzinfo=UTC),
                arrive_before=None,
            )
        )


def test_retryable_http_and_timeout_are_classified_without_fabricated_inventory() -> None:
    provider = _provider(lambda request: httpx.Response(429, json={"error": "limited"}))
    with pytest.raises(RetryableProviderError) as rate_limited:
        provider.search_hotels(QUERY)
    assert rate_limited.value.http_status == 429
    assert rate_limited.value.response_received is True

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("contract timeout", request=request)

    with pytest.raises(RetryableProviderError, match="ReadTimeout"):
        _provider(timeout).search_hotels(QUERY)


def test_currency_mismatch_is_not_silently_converted() -> None:
    payload = deepcopy(_payload())
    payload["data"][0]["roomTypes"][0]["offerRetailRate"]["currency"] = "USD"
    provider = _provider(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(ProviderError, match="approved FX snapshot"):
        provider.search_hotels(QUERY)

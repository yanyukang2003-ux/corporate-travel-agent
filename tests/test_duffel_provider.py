from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from corporate_travel_agent.domain.enums import RevalidationStatus, SourceType
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
)
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.services.provider_quote_context import (
    InMemoryProviderQuoteContextStore,
)
from corporate_travel_agent.services.sqlalchemy_repository import (
    SQLAlchemyProviderQuoteContextStore,
    SQLAlchemyTaskRepository,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "evals/providers/duffel-provider-contract-v1.json"
REVALIDATION_SMOKE_PATH = PROJECT_ROOT / "evals/subsets/duffel-real-revalidation-smoke-v1.json"
FIXED_NOW = datetime.fromisoformat("2026-08-03T07:00:00+00:00")


def _dataset() -> dict[str, Any]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def _case(case_id: str) -> dict[str, Any]:
    return next(item for item in _dataset()["cases"] if item["case_id"] == case_id)


def _query(case: dict[str, Any]) -> TransportSearchQuery:
    values = case["query"]
    return TransportSearchQuery(
        origin=values["origin"],
        destination=values["destination"],
        depart_after=datetime.fromisoformat(values["depart_after"]),
        arrive_before=datetime.fromisoformat(values["arrive_before"]),
    )


def _provider(handler) -> DuffelProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DuffelProvider(
        "duffel_test_contract",
        client=client,
        clock=lambda: FIXED_NOW,
        expected_currency="USD",
    )


def test_contract_dataset_is_frozen_and_has_the_four_declared_cases() -> None:
    dataset = _dataset()

    assert dataset["status"] == "frozen"
    assert dataset["records"] == 4
    assert [item["case_id"] for item in dataset["cases"]] == [
        "normal-offers",
        "empty-inventory",
        "price-change",
        "timeout",
    ]


def test_real_revalidation_smoke_dataset_is_frozen_and_read_only() -> None:
    dataset = json.loads(REVALIDATION_SMOKE_PATH.read_text(encoding="utf-8"))

    assert dataset["status"] == "frozen"
    assert dataset["provider_mode"] == "test"
    assert dataset["records"] == 1
    assert dataset["maximum_external_calls"] == 2
    assert dataset["booking_enabled"] is False
    assert dataset["cases"][0]["workflow_action"] == "search_select_revalidate"
    assert dataset["cases"][0]["expected"]["allowed_revalidation_statuses"] == [
        "UNCHANGED",
        "PRICE_CHANGED",
    ]


def test_normal_offer_maps_to_authorized_snapshot_and_archives_raw_response() -> None:
    case = _case("normal-offers")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/air/offer_requests"
        assert request.headers["Duffel-Version"] == "v2"
        assert request.headers["Authorization"] == "Bearer duffel_test_contract"
        payload = json.loads(request.content)
        assert payload["data"]["slices"] == [
            {
                "origin": "LHR",
                "destination": "JFK",
                "departure_date": "2026-09-15",
            }
        ]
        assert payload["data"]["passengers"] == [{"type": "adult"}]
        return httpx.Response(
            case["http"]["status_code"],
            json=case["http"]["json"],
        )

    snapshot = _provider(handler).search_transport(_query(case))
    expected = case["expected"]

    assert snapshot.source_type is SourceType.AUTHORIZED_API
    assert snapshot.provider == "duffel"
    assert len(snapshot.items) == expected["offer_count"]
    offer = snapshot.items[0]
    assert offer.ref_id == expected["offer_id"]
    assert offer.snapshot_id == snapshot.snapshot_id
    assert offer.price == Decimal(expected["price"])
    assert offer.currency == expected["currency"]
    assert offer.is_direct is expected["is_direct"]
    assert str(offer.depart_at.tzinfo) == "Europe/London"
    assert str(offer.arrive_at.tzinfo) == "America/New_York"
    assert any(
        expected["provider_warning_contains"] in warning for warning in snapshot.provider_warnings
    )
    assert snapshot.raw_response is not None
    assert snapshot.raw_response.sha256 == snapshot.raw_payload_hash


def test_empty_inventory_is_a_successful_empty_snapshot_not_a_provider_error() -> None:
    case = _case("empty-inventory")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(case["http"]["status_code"], json=case["http"]["json"])

    snapshot = _provider(handler).search_transport(_query(case))

    assert snapshot.source_type is SourceType.AUTHORIZED_API
    assert snapshot.items == ()
    assert snapshot.valid_until > snapshot.captured_at


def test_revalidation_detects_a_price_change_from_the_original_offer() -> None:
    case = _case("price-change")
    responses = iter(case["http"])
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        configured = next(responses)
        return httpx.Response(configured["status_code"], json=configured["json"])

    provider = _provider(handler)
    snapshot = provider.search_transport(_query(case))
    result = provider.revalidate((snapshot.items[0].ref_id,))

    assert methods == ["POST", "GET"]
    assert provider.external_request_count == 2
    assert provider.external_request_log == [
        ("POST", "/air/offer_requests"),
        ("GET", f"/air/offers/{snapshot.items[0].ref_id}"),
    ]
    assert result.status is RevalidationStatus.PRICE_CHANGED
    assert result.current_prices == {
        case["expected"]["offer_id"]: Decimal(case["expected"]["current_price"])
    }
    assert result.unavailable_refs == ()
    assert provider.last_revalidation_result is result
    assert len(provider.revalidation_raw_responses) == 1
    archived = provider.revalidation_raw_responses[0]
    assert archived.size_bytes > 0
    assert archived.content_type == "application/json"
    assert "/revalidation/" in archived.object_key


def test_revalidation_survives_provider_instance_replacement() -> None:
    """A second worker must revalidate from shared context, not process memory."""

    case = _case("price-change")
    search_response, revalidation_response = case["http"]
    context_store = InMemoryProviderQuoteContextStore()
    first_methods: list[str] = []
    second_methods: list[str] = []

    def first_handler(request: httpx.Request) -> httpx.Response:
        first_methods.append(request.method)
        return httpx.Response(
            search_response["status_code"],
            json=search_response["json"],
        )

    first = DuffelProvider(
        "duffel_test_contract",
        client=httpx.Client(transport=httpx.MockTransport(first_handler)),
        clock=lambda: FIXED_NOW,
        expected_currency="USD",
        quote_context_store=context_store,
    )
    snapshot = first.search_transport(_query(case))

    def second_handler(request: httpx.Request) -> httpx.Response:
        second_methods.append(request.method)
        return httpx.Response(
            revalidation_response["status_code"],
            json=revalidation_response["json"],
        )

    second = DuffelProvider(
        "duffel_test_contract",
        client=httpx.Client(transport=httpx.MockTransport(second_handler)),
        clock=lambda: FIXED_NOW,
        expected_currency="USD",
        quote_context_store=context_store,
    )
    result = second.revalidate((snapshot.items[0].ref_id,))

    assert first_methods == ["POST"]
    assert second_methods == ["GET"]
    assert result.status is RevalidationStatus.PRICE_CHANGED


def test_revalidation_context_survives_sql_store_restart(tmp_path) -> None:
    case = _case("price-change")
    search_response, revalidation_response = case["http"]
    database_url = f"sqlite+pysqlite:///{tmp_path / 'duffel-context.db'}"
    repository = SQLAlchemyTaskRepository(database_url)
    repository.create_schema()
    first = DuffelProvider(
        "duffel_test_contract",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    search_response["status_code"], json=search_response["json"]
                )
            )
        ),
        clock=lambda: FIXED_NOW,
        expected_currency="USD",
        quote_context_store=SQLAlchemyProviderQuoteContextStore(repository.engine),
    )
    snapshot = first.search_transport(_query(case))
    repository.dispose()

    restarted = SQLAlchemyTaskRepository(database_url)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            revalidation_response["status_code"], json=revalidation_response["json"]
        )

    second = DuffelProvider(
        "duffel_test_contract",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FIXED_NOW,
        expected_currency="USD",
        quote_context_store=SQLAlchemyProviderQuoteContextStore(restarted.engine),
    )
    result = second.revalidate((snapshot.items[0].ref_id,))

    assert calls == [f"/air/offers/{snapshot.items[0].ref_id}"]
    assert result.status is RevalidationStatus.PRICE_CHANGED
    restarted.dispose()


def test_missing_revalidation_context_has_stable_error_code_and_makes_no_http_call() -> None:
    calls: list[str] = []
    provider = _provider(lambda request: calls.append(request.url.path))

    with pytest.raises(ProviderError) as exc_info:
        provider.revalidate(("off_missing_context",))

    assert exc_info.value.error_code == "PROVIDER_QUOTE_CONTEXT_MISSING"
    assert exc_info.value.layer == "provider_quote_context"
    assert calls == []


def test_required_owner_filter_exposes_revalidated_test_order_context() -> None:
    case = _case("normal-offers")
    search_payload = deepcopy(case["http"]["json"])
    raw_offer = search_payload["data"]["offers"][0]
    raw_offer["owner"] = {"name": "Duffel Airways"}
    raw_offer["passengers"] = [{"id": "pas_contract", "type": "adult"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=search_payload)
        assert request.url.path == f"/air/offers/{raw_offer['id']}"
        return httpx.Response(200, json={"data": raw_offer})

    provider = DuffelProvider(
        "duffel_test_contract",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FIXED_NOW,
        expected_currency="USD",
        required_owner_name="Duffel Airways",
    )
    snapshot = provider.search_transport(_query(case))
    revalidation = provider.revalidate((snapshot.items[0].ref_id,))
    context = provider.test_order_offer(snapshot.items[0].ref_id)

    assert revalidation.status is RevalidationStatus.UNCHANGED
    assert context.offer_id == snapshot.items[0].ref_id
    assert context.amount == snapshot.items[0].price
    assert context.currency == "USD"
    assert context.passenger_ids == ("pas_contract",)
    assert context.owner_name == "Duffel Airways"


def test_timeout_is_classified_as_retryable_without_fabricating_inventory() -> None:
    case = _case("timeout")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("contract timeout", request=request)

    with pytest.raises(RetryableProviderError, match="ReadTimeout"):
        _provider(handler).search_transport(_query(case))


def test_successful_http_response_is_archived_before_normalization_failure() -> None:
    case = _case("normal-offers")
    malformed = deepcopy(case["http"]["json"])
    segment = malformed["data"]["offers"][0]["slices"][0]["segments"][0]
    segment["origin"].pop("time_zone")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed)

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="raw_response_sha256="):
        provider.search_transport(_query(case))
    assert provider.last_raw_response is not None
    assert provider.last_raw_response.size_bytes > 0


def test_time_window_filter_is_reported_when_all_offers_are_skipped() -> None:
    case = _case("normal-offers")
    payload = deepcopy(case["http"]["json"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    # Flight is 10:00–13:00 local on 2026-09-15; force arrive_before before that.
    query = TransportSearchQuery(
        origin=case["query"]["origin"],
        destination=case["query"]["destination"],
        depart_after=datetime.fromisoformat("2026-09-15T00:00:00+00:00"),
        arrive_before=datetime.fromisoformat("2026-09-15T08:00:00+00:00"),
    )
    snapshot = _provider(handler).search_transport(query)
    warnings = " ".join(snapshot.provider_warnings)
    assert snapshot.items == ()
    assert "filtered_by_arrive_before" in warnings
    assert "filter_summary=filtered_by_arrive_before=1" in warnings
    assert "raw_response_sha256=" in warnings


def test_depart_after_filter_is_reported_when_all_offers_are_skipped() -> None:
    case = _case("normal-offers")
    payload = deepcopy(case["http"]["json"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    query = TransportSearchQuery(
        origin=case["query"]["origin"],
        destination=case["query"]["destination"],
        depart_after=datetime.fromisoformat("2026-09-15T20:00:00+00:00"),
        arrive_before=datetime.fromisoformat("2026-09-16T23:59:00+00:00"),
    )
    snapshot = _provider(handler).search_transport(query)
    warnings = " ".join(snapshot.provider_warnings)
    assert snapshot.items == ()
    assert "filter_summary=filtered_by_depart_after=1" in warnings


def test_unknown_location_fails_before_any_network_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    query = TransportSearchQuery(
        origin="Invented Airport",
        destination="JFK",
        depart_after=datetime.fromisoformat("2026-09-15T00:00:00+00:00"),
        arrive_before=datetime.fromisoformat("2026-09-16T00:00:00+00:00"),
    )

    with pytest.raises(ProviderError, match="No verified Duffel IATA mapping"):
        _provider(handler).search_transport(query)
    assert calls == 0


@pytest.mark.parametrize(
    ("origin", "destination", "expected_origin", "expected_destination"),
    [
        ("London LHR", "New York JFK", "LHR", "JFK"),
        ("london lhr", "new york jfk", "LHR", "JFK"),
        ("纽约", "费城", "NYC", "PHL"),
        ("New York", "Philadelphia", "NYC", "PHL"),
        ("洛杉矶", "旧金山", "LAX", "SFO"),
        ("伦敦", "巴黎", "LON", "PAR"),
        ("东京", "新加坡", "TYO", "SIN"),
        ("Boston", "Chicago", "BOS", "CHI"),
    ],
)
def test_exact_trusted_compound_aliases_map_to_iata_codes(
    origin: str,
    destination: str,
    expected_origin: str,
    expected_destination: str,
) -> None:
    case = _case("empty-inventory")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["data"]["slices"][0]["origin"] == expected_origin
        assert payload["data"]["slices"][0]["destination"] == expected_destination
        return httpx.Response(case["http"]["status_code"], json=case["http"]["json"])

    query = TransportSearchQuery(
        origin=origin,
        destination=destination,
        depart_after=datetime.fromisoformat("2026-09-15T00:00:00+00:00"),
        arrive_before=datetime.fromisoformat("2026-09-16T00:00:00+00:00"),
    )

    snapshot = _provider(handler).search_transport(query)

    assert snapshot.items == ()


def test_untrusted_compound_airport_label_fails_before_network_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    query = TransportSearchQuery(
        origin="London Heathrow Airport",
        destination="New York JFK",
        depart_after=datetime.fromisoformat("2026-09-15T00:00:00+00:00"),
        arrive_before=datetime.fromisoformat("2026-09-16T00:00:00+00:00"),
    )

    with pytest.raises(ProviderError, match="No verified Duffel IATA mapping"):
        _provider(handler).search_transport(query)
    assert calls == 0


def test_alias_collision_fails_before_network_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    query = TransportSearchQuery(
        origin="London LHR",
        destination="LHR",
        depart_after=datetime.fromisoformat("2026-09-15T00:00:00+00:00"),
        arrive_before=datetime.fromisoformat("2026-09-16T00:00:00+00:00"),
    )

    with pytest.raises(ProviderError, match="must differ"):
        _provider(handler).search_transport(query)
    assert calls == 0


def test_hotel_search_and_non_test_tokens_fail_explicitly() -> None:
    provider = _provider(lambda request: httpx.Response(500))

    with pytest.raises(ProviderError, match="flight-only"):
        provider.search_hotels(
            HotelSearchQuery(
                city="London",
                check_in=datetime.fromisoformat("2026-09-15T00:00:00").date(),
                check_out=datetime.fromisoformat("2026-09-16T00:00:00").date(),
            )
        )
    with pytest.raises(ValueError, match="only accepts duffel_test_"):
        DuffelProvider("duffel_live_not_allowed")

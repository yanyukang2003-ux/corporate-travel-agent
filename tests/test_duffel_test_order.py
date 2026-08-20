from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from corporate_travel_agent.providers.base import ProviderError
from corporate_travel_agent.providers.duffel_test_order import (
    CANCEL_TEST_ORDER_CONFIRMATION,
    CREATE_TEST_ORDER_CONFIRMATION,
    DuffelTestOrderClient,
    RevalidatedTestOffer,
    SyntheticTestPassenger,
)

NOW = datetime.fromisoformat("2026-08-09T10:00:00+00:00")
OFFER = RevalidatedTestOffer(
    offer_id="off_test_order",
    amount=Decimal("228.34"),
    currency="USD",
    expires_at=NOW + timedelta(minutes=30),
    passenger_ids=("pas_test_order",),
    owner_name="Duffel Airways",
)
PASSENGER = SyntheticTestPassenger(
    given_name="Test",
    family_name="Traveller",
    born_on=date(1990, 1, 1),
    gender="m",
    title="mr",
    email="test-traveller@example.com",
    phone_number="+442080160508",
)


def _order(*, cancelled: bool = False) -> dict:
    cancellation = (
        {
            "id": "ore_test_order",
            "confirmed_at": "2026-08-09T10:04:00Z",
        }
        if cancelled
        else None
    )
    return {
        "data": {
            "id": "ord_test_order",
            "offer_id": OFFER.offer_id,
            "live_mode": False,
            "available_actions": [] if cancelled else ["cancel"],
            "booking_reference": "TEST01",
            "payment_status": {"status": "paid"},
            "cancellation": cancellation,
        }
    }


def _cancellation(*, confirmed: bool) -> dict:
    return {
        "data": {
            "id": "ore_test_order",
            "order_id": "ord_test_order",
            "live_mode": False,
            "refund_amount": "228.34",
            "refund_currency": "USD",
            "refund_to": "balance",
            "confirmed_at": "2026-08-09T10:04:00Z" if confirmed else None,
        }
    }


def test_test_order_create_read_cancel_confirm_and_final_read() -> None:
    responses = iter(
        [
            httpx.Response(201, json=_order()),
            httpx.Response(200, json=_order()),
            httpx.Response(201, json=_cancellation(confirmed=False)),
            httpx.Response(200, json=_cancellation(confirmed=True)),
            httpx.Response(200, json=_order(cancelled=True)),
        ]
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    orders = DuffelTestOrderClient(
        "duffel_test_contract",
        test_order_writes_enabled=True,
        client=client,
        clock=lambda: NOW,
    )

    created = orders.create_order(
        OFFER,
        PASSENGER,
        confirmation=CREATE_TEST_ORDER_CONFIRMATION,
    )
    read_back = orders.get_order(created.order_id)
    quote = orders.create_cancellation(
        read_back,
        confirmation=CANCEL_TEST_ORDER_CONFIRMATION,
    )
    confirmed = orders.confirm_cancellation(
        quote,
        confirmation=CANCEL_TEST_ORDER_CONFIRMATION,
    )
    final = orders.get_order(created.order_id, category="order-final")

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/air/orders"),
        ("GET", "/air/orders/ord_test_order"),
        ("POST", "/air/order_cancellations"),
        ("POST", "/air/order_cancellations/ore_test_order/actions/confirm"),
        ("GET", "/air/orders/ord_test_order"),
    ]
    order_payload = json.loads(requests[0].content)
    assert order_payload["data"]["type"] == "instant"
    assert order_payload["data"]["selected_offers"] == [OFFER.offer_id]
    assert order_payload["data"]["payments"] == [
        {"type": "balance", "currency": "USD", "amount": "228.34"}
    ]
    assert order_payload["data"]["passengers"][0]["email"].endswith("@example.com")
    assert created.live_mode is False
    assert read_back.payment_status == "paid"
    assert quote.confirmed_at is None
    assert confirmed.confirmed_at is not None
    assert final.cancelled is True
    assert orders.external_request_count == 5
    assert len(orders.raw_responses) == 5
    assert len(orders.request_payload_hashes) == 2


def test_order_mutations_require_literal_confirmation_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    orders = DuffelTestOrderClient(
        "duffel_test_contract",
        test_order_writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )

    with pytest.raises(ProviderError, match="not explicitly confirmed"):
        orders.create_order(OFFER, PASSENGER, confirmation="")
    assert calls == 0


def test_order_client_rejects_live_tokens_disabled_writes_and_real_email() -> None:
    with pytest.raises(ValueError, match="only accepts duffel_test_"):
        DuffelTestOrderClient("duffel_live_forbidden", test_order_writes_enabled=True)
    with pytest.raises(ValueError, match="explicit enable switch"):
        DuffelTestOrderClient("duffel_test_contract", test_order_writes_enabled=False)

    orders = DuffelTestOrderClient(
        "duffel_test_contract",
        test_order_writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        clock=lambda: NOW,
    )
    real_email = SyntheticTestPassenger(
        given_name=PASSENGER.given_name,
        family_name=PASSENGER.family_name,
        born_on=PASSENGER.born_on,
        gender=PASSENGER.gender,
        title=PASSENGER.title,
        email="person@company.invalid",
        phone_number=PASSENGER.phone_number,
    )
    with pytest.raises(ProviderError, match="example.com"):
        orders.create_order(
            OFFER,
            real_email,
            confirmation=CREATE_TEST_ORDER_CONFIRMATION,
        )


def test_order_transport_failure_is_not_retryable_or_repeated() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("contract timeout", request=request)

    orders = DuffelTestOrderClient(
        "duffel_test_contract",
        test_order_writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )

    with pytest.raises(ProviderError) as captured:
        orders.create_order(
            OFFER,
            PASSENGER,
            confirmation=CREATE_TEST_ORDER_CONFIRMATION,
        )
    assert captured.value.retryable is False
    assert calls == 1
    assert orders.external_request_count == 1

"""Duffel Test Mode 订单客户端：与只读 DuffelProvider 隔离的显式写操作门控。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from corporate_travel_agent.domain.enums import RawResponseAccessPolicy
from corporate_travel_agent.domain.models import RawResponseReference
from corporate_travel_agent.services.object_storage import (
    InMemoryRawResponseObjectStore,
    RawResponseObjectStore,
)

from .base import ProviderError

CREATE_TEST_ORDER_CONFIRMATION = "CREATE_DUFFEL_TEST_ORDER"
CANCEL_TEST_ORDER_CONFIRMATION = "CANCEL_DUFFEL_TEST_ORDER"


@dataclass(frozen=True, slots=True)
class RevalidatedTestOffer:
    """重校验后的 Test Mode offer 结账事实。"""

    offer_id: str
    amount: Decimal
    currency: str
    expires_at: datetime
    passenger_ids: tuple[str, ...]
    owner_name: str


@dataclass(frozen=True, slots=True)
class SyntheticTestPassenger:
    """合成测试乘客资料（仅用于 Test Mode 下单）。"""

    given_name: str
    family_name: str
    born_on: date
    gender: str
    title: str
    email: str
    phone_number: str
    synthetic: bool = True


@dataclass(frozen=True, slots=True)
class TestOrderRecord:
    """Duffel Test Mode 订单快照。"""

    order_id: str
    offer_id: str
    live_mode: bool
    available_actions: tuple[str, ...]
    booking_reference: str | None
    payment_status: str | None
    cancelled: bool


@dataclass(frozen=True, slots=True)
class TestOrderCancellation:
    """Test Mode 取消报价/确认记录。"""

    cancellation_id: str
    order_id: str
    live_mode: bool
    refund_amount: Decimal | None
    refund_currency: str | None
    refund_to: str | None
    confirmed_at: datetime | None


class DuffelTestOrderClient:
    """显式门控的 Duffel Test Mode 下单与清理客户端。

    有意与 ``DuffelProvider`` 分离，使普通应用构造无法获得写能力。
    拒绝 live token，要求进程级启用开关与每次变更操作的字面确认，且从不重试写请求。
    """

    def __init__(
        self,
        access_token: str,
        *,
        test_order_writes_enabled: bool,
        client: httpx.Client | None = None,
        api_base_url: str = "https://api.duffel.com",
        api_version: str = "v2",
        clock: Any | None = None,
        raw_response_store: RawResponseObjectStore | None = None,
        raw_response_retention_days: int = 90,
    ) -> None:
        token = access_token.strip()
        if not token.startswith("duffel_test_"):
            raise ValueError("DuffelTestOrderClient only accepts duffel_test_ tokens")
        if not test_order_writes_enabled:
            raise ValueError("Duffel Test Order writes require an explicit enable switch")
        if api_version != "v2":
            raise ValueError("DuffelTestOrderClient supports Duffel-Version v2 only")
        if not api_base_url.startswith("https://"):
            raise ValueError("Duffel API base URL must use HTTPS")
        if not 1 <= raw_response_retention_days <= 3650:
            raise ValueError("raw_response_retention_days must be between 1 and 3650")

        self._access_token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._api_version = api_version
        self._client = client or httpx.Client(timeout=65.0)
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self.raw_response_store = raw_response_store or InMemoryRawResponseObjectStore()
        self.raw_response_retention_days = raw_response_retention_days
        self.external_request_count = 0
        self.external_request_log: list[tuple[str, str]] = []
        self.raw_responses: list[RawResponseReference] = []
        self.request_payload_hashes: list[str] = []

    def close(self) -> None:
        """关闭自建 HTTP 客户端。"""
        if self._owns_client:
            self._client.close()

    def create_order(
        self,
        offer: RevalidatedTestOffer,
        passenger: SyntheticTestPassenger,
        *,
        confirmation: str,
    ) -> TestOrderRecord:
        """在字面确认后创建 Test Mode 即时订单（不重试）。"""
        if confirmation != CREATE_TEST_ORDER_CONFIRMATION:
            raise ProviderError("Duffel Test Order creation was not explicitly confirmed")
        self._validate_offer(offer)
        self._validate_passenger(passenger)
        if len(offer.passenger_ids) != 1:
            raise ProviderError("The Test Order runner supports exactly one passenger")
        payload = {
            "data": {
                "type": "instant",
                "selected_offers": [offer.offer_id],
                "payments": [
                    {
                        "type": "balance",
                        "currency": offer.currency,
                        "amount": format(offer.amount, "f"),
                    }
                ],
                "passengers": [
                    {
                        "id": offer.passenger_ids[0],
                        "given_name": passenger.given_name,
                        "family_name": passenger.family_name,
                        "born_on": passenger.born_on.isoformat(),
                        "gender": passenger.gender,
                        "title": passenger.title,
                        "email": passenger.email,
                        "phone_number": passenger.phone_number,
                    }
                ],
            }
        }
        response_payload = self._request_json(
            "POST",
            "/air/orders",
            category="order-create",
            identity=offer.offer_id,
            json_body=payload,
        )
        order = self._order_record(response_payload)
        if order.offer_id != offer.offer_id:
            raise ProviderError("Duffel Test Order response referenced a different offer")
        return order

    def get_order(self, order_id: str, *, category: str = "order-read") -> TestOrderRecord:
        """读取 Test Mode 订单当前状态。"""
        self._require_ref(order_id, "ord_", "order")
        payload = self._request_json(
            "GET",
            f"/air/orders/{order_id}",
            category=category,
            identity=order_id,
        )
        order = self._order_record(payload)
        if order.order_id != order_id:
            raise ProviderError("Duffel returned a different Test Order ID")
        return order

    def create_cancellation(
        self,
        order: TestOrderRecord,
        *,
        confirmation: str,
    ) -> TestOrderCancellation:
        """在确认后创建取消报价；拒绝 live 订单。"""
        if confirmation != CANCEL_TEST_ORDER_CONFIRMATION:
            raise ProviderError("Duffel Test Order cancellation was not explicitly confirmed")
        if order.live_mode:
            raise ProviderError("Refusing to cancel a live Duffel Order")
        if "cancel" not in order.available_actions:
            raise ProviderError("Duffel Test Order does not advertise the cancel action")
        payload = self._request_json(
            "POST",
            "/air/order_cancellations",
            category="cancellation-quote",
            identity=order.order_id,
            json_body={"data": {"order_id": order.order_id}},
        )
        cancellation = self._cancellation_record(payload)
        if cancellation.order_id != order.order_id:
            raise ProviderError("Cancellation quote referenced a different Test Order")
        return cancellation

    def confirm_cancellation(
        self,
        cancellation: TestOrderCancellation,
        *,
        confirmation: str,
    ) -> TestOrderCancellation:
        """在确认后提交取消；拒绝 live 订单与重复确认。"""
        if confirmation != CANCEL_TEST_ORDER_CONFIRMATION:
            raise ProviderError("Duffel Test Order cancellation was not explicitly confirmed")
        if cancellation.live_mode:
            raise ProviderError("Refusing to confirm cancellation of a live Duffel Order")
        if cancellation.confirmed_at is not None:
            raise ProviderError("Duffel Test Order cancellation is already confirmed")
        payload = self._request_json(
            "POST",
            f"/air/order_cancellations/{cancellation.cancellation_id}/actions/confirm",
            category="cancellation-confirm",
            identity=cancellation.cancellation_id,
        )
        confirmed = self._cancellation_record(payload)
        if confirmed.cancellation_id != cancellation.cancellation_id:
            raise ProviderError("Duffel confirmed a different Test Order cancellation")
        if confirmed.confirmed_at is None:
            raise ProviderError("Duffel cancellation response has no confirmed_at timestamp")
        return confirmed

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        category: str,
        identity: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_method = method.upper()
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {self._access_token}",
            "Duffel-Version": self._api_version,
        }
        kwargs: dict[str, Any] = {"headers": headers}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["json"] = json_body
            self.request_payload_hashes.append(_payload_hash(json_body))
        self.external_request_count += 1
        self.external_request_log.append((normalized_method, path))
        try:
            response = self._client.request(
                normalized_method,
                f"{self._api_base_url}{path}",
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Duffel Test Order {normalized_method} failed before a response: "
                f"{type(exc).__name__}",
                error_code="DUFFEL_TEST_ORDER_TRANSPORT_ERROR",
                layer="duffel_test_order_transport",
                cause_type=type(exc).__name__,
                retryable=False,
                response_received=False,
            ) from exc
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError("Duffel Test Order API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Duffel Test Order response must be a JSON object")
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"Duffel Test Order API returned HTTP {response.status_code}",
                error_code=f"DUFFEL_TEST_ORDER_HTTP_{response.status_code}",
                layer="duffel_test_order_http",
                cause_type="HTTPStatusError",
                retryable=False,
                response_received=True,
                http_status=response.status_code,
                request_id=response.headers.get("x-request-id"),
            )
        captured_at = self._aware_now()
        self.raw_responses.append(
            self._archive_payload(
                payload,
                captured_at=captured_at,
                category=category,
                identity=f"{identity}|{captured_at.isoformat()}",
            )
        )
        return payload

    def _order_record(self, payload: Mapping[str, Any]) -> TestOrderRecord:
        data = _response_data(payload)
        _require_test_mode(data, "order")
        order_id = _required_ref(data, "id", "ord_", "order")
        offer_id = _required_ref(data, "offer_id", "off_", "offer")
        available_actions = data.get("available_actions")
        if not isinstance(available_actions, list) or not all(
            isinstance(item, str) for item in available_actions
        ):
            raise ProviderError("Duffel Test Order available_actions must be a string list")
        booking_reference = data.get("booking_reference")
        if booking_reference is not None and not isinstance(booking_reference, str):
            raise ProviderError("Duffel Test Order booking_reference must be a string")
        payment_status_value = data.get("payment_status")
        payment_status = None
        if isinstance(payment_status_value, Mapping):
            raw_status = payment_status_value.get("status")
            payment_status = raw_status if isinstance(raw_status, str) else None
        cancellation = data.get("cancellation")
        cancelled = (
            isinstance(cancellation, Mapping) and cancellation.get("confirmed_at") is not None
        )
        return TestOrderRecord(
            order_id=order_id,
            offer_id=offer_id,
            live_mode=False,
            available_actions=tuple(available_actions),
            booking_reference=booking_reference,
            payment_status=payment_status,
            cancelled=cancelled,
        )

    def _cancellation_record(self, payload: Mapping[str, Any]) -> TestOrderCancellation:
        data = _response_data(payload)
        _require_test_mode(data, "order cancellation")
        cancellation_id = _required_ref(data, "id", "ore_", "order cancellation")
        order_id = _required_ref(data, "order_id", "ord_", "order")
        raw_amount = data.get("refund_amount")
        refund_amount = Decimal(raw_amount) if isinstance(raw_amount, str) else None
        raw_currency = data.get("refund_currency")
        refund_currency = raw_currency if isinstance(raw_currency, str) else None
        raw_refund_to = data.get("refund_to")
        refund_to = raw_refund_to if isinstance(raw_refund_to, str) else None
        raw_confirmed_at = data.get("confirmed_at")
        confirmed_at = (
            _parse_datetime(raw_confirmed_at, "confirmed_at")
            if raw_confirmed_at is not None
            else None
        )
        return TestOrderCancellation(
            cancellation_id=cancellation_id,
            order_id=order_id,
            live_mode=False,
            refund_amount=refund_amount,
            refund_currency=refund_currency,
            refund_to=refund_to,
            confirmed_at=confirmed_at,
        )

    def _validate_offer(self, offer: RevalidatedTestOffer) -> None:
        self._require_ref(offer.offer_id, "off_", "offer")
        if offer.expires_at <= self._aware_now():
            raise ProviderError("Refusing to order an expired Duffel Test Mode offer")
        if not offer.amount.is_finite() or offer.amount <= 0:
            raise ProviderError("Duffel Test Mode offer amount must be positive")
        if not re.fullmatch(r"[A-Z]{3}", offer.currency):
            raise ProviderError("Duffel Test Mode offer currency is invalid")
        if offer.owner_name != "Duffel Airways":
            raise ProviderError("Test Order evaluation requires a Duffel Airways offer")
        for passenger_id in offer.passenger_ids:
            self._require_ref(passenger_id, "pas_", "passenger")

    @staticmethod
    def _validate_passenger(passenger: SyntheticTestPassenger) -> None:
        if not passenger.synthetic:
            raise ProviderError("Test Order passenger must be explicitly synthetic")
        if not passenger.email.casefold().endswith("@example.com"):
            raise ProviderError("Synthetic Test Order email must use example.com")
        if passenger.gender not in {"m", "f"}:
            raise ProviderError("Synthetic Test Order gender must be m or f")
        if passenger.title not in {"mr", "mrs", "ms", "miss"}:
            raise ProviderError("Synthetic Test Order title is invalid")
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", passenger.phone_number):
            raise ProviderError("Synthetic Test Order phone must use E.164 format")
        for field_name, value in (
            ("given_name", passenger.given_name),
            ("family_name", passenger.family_name),
        ):
            if not re.fullmatch(r"[A-Za-z][A-Za-z '-]{0,19}", value):
                raise ProviderError(f"Synthetic Test Order {field_name} is invalid")

    @staticmethod
    def _require_ref(value: str, prefix: str, label: str) -> None:
        if not isinstance(value, str) or not value.startswith(prefix):
            raise ProviderError(f"Duffel Test Mode {label} ID is invalid")

    def _archive_payload(
        self,
        payload: Mapping[str, Any],
        *,
        captured_at: datetime,
        category: str,
        identity: str,
    ) -> RawResponseReference:
        raw_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return self.raw_response_store.put_bytes(
            object_key=(
                f"duffel-test/{captured_at:%Y/%m/%d}/{category}/"
                f"duffel-{category}-{identity_hash}-{raw_hash[:12]}.json"
            ),
            content=raw_bytes,
            content_type="application/json",
            stored_at=captured_at,
            retention_until=captured_at + timedelta(days=self.raw_response_retention_days),
            access_policy=RawResponseAccessPolicy.SYSTEM_REPLAY_OR_AUDIT_ADMIN,
        )

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise RuntimeError("DuffelTestOrderClient clock must be timezone-aware")
        return value


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _response_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ProviderError("Duffel Test Order response is missing the data object")
    return data


def _require_test_mode(data: Mapping[str, Any], label: str) -> None:
    if data.get("live_mode") is not False:
        raise ProviderError(f"Duffel {label} was not explicitly marked as Test Mode")


def _required_ref(
    data: Mapping[str, Any],
    field: str,
    prefix: str,
    label: str,
) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ProviderError(f"Duffel Test Mode {label} ID is invalid")
    return value


def _parse_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderError(f"Duffel Test Mode {field} must be an ISO datetime")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ProviderError(f"Duffel Test Mode {field} must include a timezone")
    return parsed

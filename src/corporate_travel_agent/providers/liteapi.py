"""LiteAPI/Nuitee 酒店适配器：只读搜索与报价刷新，不创建预订。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from corporate_travel_agent.domain.enums import (
    RawResponseAccessPolicy,
    RevalidationStatus,
    SourceType,
)
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    HotelOffer,
    InventorySnapshot,
    ProviderHandoff,
    RawResponseReference,
    RevalidationResult,
    TravelOptionVersion,
)

# 供既有测试与调用方继续从本模块导入
__all__ = ("COMMUTE_UNKNOWN_MINUTES", "LiteAPIHotelProvider")
from corporate_travel_agent.services.city_registry import city_registry
from corporate_travel_agent.services.object_storage import (
    InMemoryRawResponseObjectStore,
    RawResponseObjectStore,
)
from corporate_travel_agent.services.provider_quote_context import (
    InMemoryProviderQuoteContextStore,
    ProviderQuoteContext,
    ProviderQuoteContextExpiredError,
    ProviderQuoteContextMissingError,
    ProviderQuoteContextStore,
)

from .base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
    inventory_query_hash,
)

LITEAPI_API_BASE_URL = "https://api.liteapi.travel/v3.0"
SANDBOX_DISCLOSURE = (
    "LiteAPI/Nuitee Connect Sandbox hotel rates; availability and prices are test data"
)
READ_ONLY_DISCLOSURE = (
    "LiteAPI hotel rates are search quotes only; no prebook, booking, or payment was created"
)
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_IATA_CODE = re.compile(r"^[A-Z]{3}$")
_ISO2_CODE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")

#: 用户侧地名 → LiteAPI 定位。**由城市登记表派生**（`config/cities.json`）。
#: 登记表里 `liteapi` 为 null 的城市**不会出现在这里**——那表示这家供应商那边
#: 还没验证过这座城市。查不到时 `_location_fields` 会抛 ProviderError：
#: 拿一条没验证过的定位去搜，搜不到还说不清为什么。
DEFAULT_LOCATIONS: dict[str, dict[str, str]] = city_registry().liteapi_locations()


@dataclass(frozen=True, slots=True)
class _RateCandidate:
    raw_offer_id: str
    hotel_id: str
    hotel_name: str
    total_price: Decimal
    currency: str
    room_signature: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class _CachedHotelOffer:
    query: HotelSearchQuery
    raw_offer_id: str
    hotel_id: str
    room_signature: tuple[str, str, str]
    nightly_price: Decimal
    currency: str
    expires_at: datetime


class LiteAPIHotelProvider:
    """只读 LiteAPI/Nuitee Connect 酒店报价适配器。

    仅暴露酒店搜索与报价刷新；从不调用 prebook、book、支付、取消或旅客数据接口。
    """

    name = "liteapi"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        api_base_url: str = LITEAPI_API_BASE_URL,
        currency: str = "USD",
        guest_nationality: str = "CN",
        adults: int = 1,
        result_limit: int = 10,
        max_rates_per_hotel: int = 3,
        supplier_timeout_seconds: int = 8,
        require_sandbox: bool = True,
        locations: Mapping[str, Mapping[str, str]] | None = None,
        clock: Callable[[], datetime] | None = None,
        raw_response_store: RawResponseObjectStore | None = None,
        raw_response_retention_days: int = 90,
        quote_context_store: ProviderQuoteContextStore | None = None,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise ValueError("LiteAPI API key cannot be blank")
        if not api_base_url.startswith("https://"):
            raise ValueError("LiteAPI API base URL must use HTTPS")
        normalized_currency = currency.strip().upper()
        if not _CURRENCY_CODE.fullmatch(normalized_currency):
            raise ValueError("LiteAPI currency must be a three-letter code")
        normalized_nationality = guest_nationality.strip().upper()
        if not _ISO2_CODE.fullmatch(normalized_nationality):
            raise ValueError("LiteAPI guest nationality must be an ISO-2 code")
        if not 1 <= adults <= 8:
            raise ValueError("LiteAPI adults must be between 1 and 8")
        if not 1 <= result_limit <= 200:
            raise ValueError("LiteAPI result limit must be between 1 and 200")
        if not 1 <= max_rates_per_hotel <= 20:
            raise ValueError("LiteAPI max rates per hotel must be between 1 and 20")
        if not 4 <= supplier_timeout_seconds <= 12:
            raise ValueError("LiteAPI supplier timeout must be between 4 and 12 seconds")
        if not 1 <= raw_response_retention_days <= 3650:
            raise ValueError("raw_response_retention_days must be between 1 and 3650")

        normalized_locations = {
            key.casefold(): dict(value) for key, value in DEFAULT_LOCATIONS.items()
        }
        for location, value in (locations or {}).items():
            normalized_locations[location.strip().casefold()] = self._validate_location(value)

        self._api_key = key
        self._api_base_url = api_base_url.rstrip("/")
        self._currency = normalized_currency
        self._guest_nationality = normalized_nationality
        self._adults = adults
        self._result_limit = result_limit
        self._max_rates_per_hotel = max_rates_per_hotel
        self._supplier_timeout_seconds = supplier_timeout_seconds
        self._require_sandbox = require_sandbox
        self.provider_mode = "sandbox-read-only" if require_sandbox else "read-only"
        self._locations = normalized_locations
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = client or httpx.Client(timeout=float(supplier_timeout_seconds + 10))
        self._owns_client = client is None
        self.raw_response_store = raw_response_store or InMemoryRawResponseObjectStore()
        self.raw_response_retention_days = raw_response_retention_days
        self.quote_context_store = (
            quote_context_store
            if quote_context_store is not None
            else InMemoryProviderQuoteContextStore()
        )
        self.last_raw_response: RawResponseReference | None = None
        self.revalidation_raw_responses: deque[RawResponseReference] = deque(maxlen=100)
        self.last_revalidation_result: RevalidationResult | None = None
        self.external_request_count = 0
        self.external_request_log: list[tuple[str, str]] = []

    @classmethod
    def from_environment(
        cls,
        *,
        raw_response_store: RawResponseObjectStore | None = None,
        clock: Callable[[], datetime] | None = None,
        quote_context_store: ProviderQuoteContextStore | None = None,
    ) -> LiteAPIHotelProvider:
        """从环境变量构造只读酒店 Provider；拒绝真实预订开关。"""
        api_key = os.getenv("LITEAPI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "LITEAPI_API_KEY is required when LiteAPI hotel coverage is enabled"
            )
        if _environment_bool("LIVE_BOOKING_ENABLED", default=False):
            raise RuntimeError("Live booking must remain disabled for LiteAPI hotel coverage")
        if _environment_bool("LITEAPI_BOOKING_ENABLED", default=False):
            raise RuntimeError("LiteAPI booking is outside the current read-only scope")

        locations: Mapping[str, Mapping[str, str]] | None = None
        raw_locations = os.getenv("LITEAPI_LOCATIONS_JSON", "").strip()
        if raw_locations:
            try:
                parsed = json.loads(raw_locations)
            except json.JSONDecodeError as exc:
                raise RuntimeError("LITEAPI_LOCATIONS_JSON must be valid JSON") from exc
            if not isinstance(parsed, dict) or not all(
                isinstance(key, str) and isinstance(value, dict)
                for key, value in parsed.items()
            ):
                raise RuntimeError("LITEAPI_LOCATIONS_JSON must map names to location objects")
            locations = parsed

        try:
            return cls(
                api_key,
                api_base_url=os.getenv("LITEAPI_API_BASE_URL", LITEAPI_API_BASE_URL),
                currency=os.getenv("LITEAPI_CURRENCY", "USD"),
                guest_nationality=os.getenv("LITEAPI_GUEST_NATIONALITY", "CN"),
                adults=int(os.getenv("LITEAPI_ADULTS", "1")),
                result_limit=int(os.getenv("LITEAPI_RESULT_LIMIT", "10")),
                max_rates_per_hotel=int(
                    os.getenv("LITEAPI_MAX_RATES_PER_HOTEL", "3")
                ),
                supplier_timeout_seconds=int(
                    os.getenv("LITEAPI_SUPPLIER_TIMEOUT_SECONDS", "8")
                ),
                require_sandbox=_environment_bool("LITEAPI_REQUIRE_SANDBOX", default=True),
                locations=locations,
                clock=clock,
                raw_response_store=raw_response_store,
                raw_response_retention_days=int(
                    os.getenv("RAW_RESPONSE_RETENTION_DAYS", "90")
                ),
                quote_context_store=quote_context_store,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid LiteAPI configuration: {exc}") from exc

    def close(self) -> None:
        """关闭自建 HTTP 客户端。"""
        if self._owns_client:
            self._client.close()

    def owns_ref(self, ref: str) -> bool:
        """判断库存引用是否属于 LiteAPI 酒店报价（``litehotel_`` 前缀）。"""
        return ref.startswith("litehotel_")

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        """仅支持酒店；交通必须由其他 Provider 覆盖。"""
        del query
        raise ProviderError(
            "LiteAPIHotelProvider is hotel-only; transport coverage requires another provider"
        )

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        """调用 hotels/rates 搜索并归一化为 InventorySnapshot。"""
        self._validate_query(query)
        response = self._send("POST", "/hotels/rates", json_body=self._search_payload(query))
        payload = self._successful_payload(response)
        self._require_expected_mode(payload, status_code=response.status_code)
        return self._snapshot_from_rates(query, payload)

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult:
        """按缓存报价上下文重新搜索同酒店同房型，刷新价格与可用性。"""
        checked_at = self._aware_now()
        current_prices: dict[str, Decimal] = {}
        unavailable: list[str] = []
        warnings: list[str] = []
        changed = False

        for ref in dict.fromkeys(refs):
            if not self.owns_ref(ref):
                unavailable.append(ref)
                warnings.append(f"{ref}: reference does not belong to LiteAPI")
                continue
            context = self._quote_context(ref, observed_at=checked_at)
            cached = self._cached_offer(context)

            body = self._search_payload(cached.query, hotel_ids=(cached.hotel_id,))
            response = self._send("POST", "/hotels/rates", json_body=body)
            payload = self._successful_payload(response)
            self._require_expected_mode(payload, status_code=response.status_code)
            archived_at = self._aware_now()
            self.revalidation_raw_responses.append(
                self._archive_raw_payload(
                    payload,
                    captured_at=archived_at,
                    category="revalidation",
                    identity=f"{ref}|{archived_at.isoformat()}",
                )
            )
            candidates, parse_warnings, raw_hotel_count = self._rate_candidates(payload)
            warnings.extend(parse_warnings)
            if raw_hotel_count and not candidates:
                raise ProviderError(
                    "LiteAPI returned hotel rates but none could be safely normalized"
                )
            match = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.raw_offer_id == cached.raw_offer_id
                ),
                None,
            )
            if match is None:
                match = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.hotel_id == cached.hotel_id
                        and candidate.room_signature == cached.room_signature
                    ),
                    None,
                )
                if match is not None:
                    warnings.append(
                        f"{ref}: LiteAPI rotated the offer token during quote refresh"
                    )
            if match is None:
                unavailable.append(ref)
                continue

            nightly_price = self._nightly_price(match.total_price, cached.query)
            current_prices[ref] = nightly_price
            changed = changed or nightly_price != cached.nightly_price
            self.quote_context_store.put_many(
                (
                    self._context_from_cached(
                        context.snapshot_id,
                        ref,
                        _CachedHotelOffer(
                            query=cached.query,
                            raw_offer_id=match.raw_offer_id,
                            hotel_id=cached.hotel_id,
                            room_signature=cached.room_signature,
                            nightly_price=nightly_price,
                            currency=cached.currency,
                            expires_at=checked_at + timedelta(minutes=10),
                        ),
                        captured_at=checked_at,
                    ),
                )
            )

        status = RevalidationStatus.UNCHANGED
        if unavailable:
            status = RevalidationStatus.UNAVAILABLE
        elif changed:
            status = RevalidationStatus.PRICE_CHANGED
        result = RevalidationResult(
            status=status,
            checked_at=checked_at,
            current_prices=current_prices,
            unavailable_refs=tuple(unavailable),
            warnings=tuple(warnings),
        )
        self.last_revalidation_result = result
        return result

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff:
        """生成只读搜索报价说明；不创建 prebook/booking/支付。"""
        now = self._aware_now()
        liteapi_refs = tuple(ref for ref in option.inventory_refs if self.owns_ref(ref))
        if not liteapi_refs:
            raise ProviderError("The selected option contains no LiteAPI hotel quote")
        contexts = [self._quote_context(ref, observed_at=now) for ref in liteapi_refs]
        expires_at = min(item.expires_at for item in contexts)
        return ProviderHandoff(
            provider=self.name,
            url_or_instructions=(
                "LiteAPI search-only hotel quote "
                + ", ".join(liteapi_refs)
                + "; no prebook, booking, or payment was created. A separately approved "
                "checkout integration is required to continue."
            ),
            expires_at=expires_at,
        )

    def _snapshot_from_rates(
        self,
        query: HotelSearchQuery,
        payload: dict[str, Any],
    ) -> InventorySnapshot:
        captured_at = self._aware_now()
        raw_bytes = self._canonical_payload_bytes(payload)
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        query_hash = inventory_query_hash(query)
        identity = "|".join((query_hash, captured_at.isoformat(), raw_hash, self.provider_mode))
        snapshot_id = f"liteapi-hotel-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        raw_response = self._archive_raw_payload(
            payload,
            captured_at=captured_at,
            category="hotel",
            identity=snapshot_id,
        )
        self.last_raw_response = raw_response
        candidates, parse_warnings, raw_hotel_count = self._rate_candidates(payload)
        warnings = [READ_ONLY_DISCLOSURE]
        if payload.get("sandbox") is True:
            warnings.insert(0, SANDBOX_DISCLOSURE)
        else:
            warnings.insert(0, "LiteAPI production hotel rates queried in read-only mode")
        warnings.append(
            "LiteAPI does not provide client-office commute time; commute_minutes=1440 "
            "marks this evidence as unknown"
        )
        warnings.extend(parse_warnings)
        if raw_hotel_count and not candidates:
            reasons = "; ".join(parse_warnings[:3])
            raise ProviderError(
                "LiteAPI returned hotel rates but none could be safely normalized; "
                f"raw_response_sha256={raw_hash}; {reasons}"
            )

        items: list[HotelOffer] = []
        quote_contexts: list[ProviderQuoteContext] = []
        expires_at = captured_at + timedelta(minutes=10)
        seen_refs: set[str] = set()
        planning_candidates = self._cheapest_rate_per_hotel(candidates)
        for candidate in planning_candidates:
            ref = self._internal_ref(candidate.raw_offer_id)
            if ref in seen_refs:
                warnings.append(f"Duplicate LiteAPI hotel offer skipped: {ref}")
                continue
            seen_refs.add(ref)
            nightly_price = self._nightly_price(candidate.total_price, query)
            offer = HotelOffer(
                ref_id=ref,
                snapshot_id=snapshot_id,
                provider=self.name,
                name=candidate.hotel_name,
                city=query.city,
                check_in=query.check_in,
                check_out=query.check_out,
                nightly_price=nightly_price,
                commute_minutes=COMMUTE_UNKNOWN_MINUTES,
                available=True,
                currency=candidate.currency,
            )
            cached = _CachedHotelOffer(
                query=query,
                raw_offer_id=candidate.raw_offer_id,
                hotel_id=candidate.hotel_id,
                room_signature=candidate.room_signature,
                nightly_price=nightly_price,
                currency=candidate.currency,
                expires_at=expires_at,
            )
            quote_contexts.append(
                self._context_from_cached(
                    snapshot_id,
                    ref,
                    cached,
                    captured_at=captured_at,
                )
            )
            items.append(offer)

        self.quote_context_store.put_many(tuple(quote_contexts))

        return InventorySnapshot(
            snapshot_id=snapshot_id,
            provider=self.name,
            source_type=SourceType.AUTHORIZED_API,
            captured_at=captured_at,
            valid_until=expires_at,
            query_hash=query_hash,
            raw_payload_hash=raw_hash,
            items=tuple(items),
            provider_warnings=tuple(warnings),
            raw_response=raw_response,
        )

    def _quote_context(
        self,
        ref_id: str,
        *,
        observed_at: datetime,
    ) -> ProviderQuoteContext:
        try:
            return self.quote_context_store.get(
                self.name,
                ref_id,
                observed_at=observed_at,
            )
        except ProviderQuoteContextMissingError as exc:
            raise ProviderError(
                f"LiteAPI quote context is missing for {ref_id}; re-run the search",
                error_code="PROVIDER_QUOTE_CONTEXT_MISSING",
                layer="provider_quote_context",
                retryable=False,
            ) from exc
        except ProviderQuoteContextExpiredError as exc:
            raise ProviderError(
                f"LiteAPI quote context expired for {ref_id}; re-run the search",
                error_code="PROVIDER_QUOTE_CONTEXT_EXPIRED",
                layer="provider_quote_context",
                retryable=False,
            ) from exc

    def _cached_offer(self, context: ProviderQuoteContext) -> _CachedHotelOffer:
        try:
            query_payload = context.payload["query"]
            room_signature = context.payload["room_signature"]
            if not isinstance(query_payload, dict) or not isinstance(room_signature, list):
                raise TypeError("invalid query or room signature")
            query = HotelSearchQuery(
                city=str(query_payload["city"]),
                check_in=datetime.fromisoformat(str(query_payload["check_in"])).date(),
                check_out=datetime.fromisoformat(str(query_payload["check_out"])).date(),
            )
            if len(room_signature) != 3:
                raise ValueError("room signature must contain three values")
            return _CachedHotelOffer(
                query=query,
                raw_offer_id=str(context.payload["raw_offer_id"]),
                hotel_id=str(context.payload["hotel_id"]),
                room_signature=tuple(str(value) for value in room_signature),  # type: ignore[arg-type]
                nightly_price=context.price,
                currency=context.currency,
                expires_at=context.expires_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                f"LiteAPI quote context is invalid for {context.ref_id}; re-run the search",
                error_code="PROVIDER_QUOTE_CONTEXT_INVALID",
                layer="provider_quote_context",
                retryable=False,
            ) from exc

    def _context_from_cached(
        self,
        snapshot_id: str,
        ref_id: str,
        cached: _CachedHotelOffer,
        *,
        captured_at: datetime,
    ) -> ProviderQuoteContext:
        return ProviderQuoteContext(
            provider=self.name,
            snapshot_id=snapshot_id,
            ref_id=ref_id,
            price=cached.nightly_price,
            currency=cached.currency,
            captured_at=captured_at,
            expires_at=cached.expires_at,
            payload={
                "query": {
                    "city": cached.query.city,
                    "check_in": cached.query.check_in.isoformat(),
                    "check_out": cached.query.check_out.isoformat(),
                },
                "raw_offer_id": cached.raw_offer_id,
                "hotel_id": cached.hotel_id,
                "room_signature": list(cached.room_signature),
            },
        )

    def _rate_candidates(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[list[_RateCandidate], list[str], int]:
        raw_data = payload.get("data", [])
        if not isinstance(raw_data, list):
            raise ProviderError("LiteAPI response data must be an array")
        hotel_names = self._hotel_names(payload)
        candidates: list[_RateCandidate] = []
        warnings: list[str] = []
        for hotel_index, raw_hotel in enumerate(raw_data):
            if not isinstance(raw_hotel, Mapping):
                warnings.append(f"LiteAPI hotel index {hotel_index} is not an object")
                continue
            hotel_id = raw_hotel.get("hotelId")
            room_types = raw_hotel.get("roomTypes")
            if not isinstance(hotel_id, str) or not hotel_id.strip():
                warnings.append(f"LiteAPI hotel index {hotel_index} has no hotelId")
                continue
            if not isinstance(room_types, list):
                warnings.append(f"LiteAPI hotel {hotel_id} has no roomTypes array")
                continue
            embedded_name = raw_hotel.get("name")
            hotel_name = hotel_names.get(hotel_id)
            if hotel_name is None and isinstance(embedded_name, str) and embedded_name.strip():
                hotel_name = embedded_name.strip()
            hotel_name = hotel_name or f"LiteAPI hotel {hotel_id}"
            for room_index, room in enumerate(room_types):
                try:
                    candidates.append(
                        self._candidate(room, hotel_id=hotel_id, hotel_name=hotel_name)
                    )
                except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                    warnings.append(
                        f"LiteAPI hotel {hotel_id} room index {room_index} was skipped: "
                        f"{type(exc).__name__}: {str(exc)[:160]}"
                    )
        if raw_data and not candidates:
            currency_skips = [
                item
                for item in warnings
                if "currency_mismatch" in item or "expected" in item.casefold()
            ]
            if currency_skips and len(currency_skips) == len(warnings):
                raise ProviderError(
                    f"LiteAPI returned hotel rates but none used expected currency "
                    f"{self._currency}; an approved FX snapshot is required before "
                    "policy evaluation can combine them with other inventory. "
                    + "; ".join(currency_skips[:3])
                )
        return candidates, warnings, len(raw_data)

    @staticmethod
    def _cheapest_rate_per_hotel(candidates: list[_RateCandidate]) -> list[_RateCandidate]:
        """每家酒店只保留一条规划用报价，同店其他房型价视为克隆。"""
        cheapest: dict[str, _RateCandidate] = {}
        for candidate in candidates:
            current = cheapest.get(candidate.hotel_id)
            if current is None or candidate.total_price < current.total_price:
                cheapest[candidate.hotel_id] = candidate
        return list(cheapest.values())

    def _candidate(
        self,
        room: object,
        *,
        hotel_id: str,
        hotel_name: str,
    ) -> _RateCandidate:
        if not isinstance(room, Mapping):
            raise TypeError("room type must be an object")
        raw_offer_id = room["offerId"]
        if not isinstance(raw_offer_id, str) or not raw_offer_id.strip():
            raise ValueError("offerId is missing")
        price_value = room.get("offerRetailRate")
        if isinstance(price_value, Mapping):
            raw_amount = price_value.get("amount")
            raw_currency = price_value.get("currency")
        else:
            rates = room.get("rates")
            if not isinstance(rates, list) or not rates or not isinstance(rates[0], Mapping):
                raise ValueError("offer price and rates are missing")
            retail_rate = rates[0].get("retailRate")
            if not isinstance(retail_rate, Mapping):
                raise ValueError("retailRate is missing")
            totals = retail_rate.get("total")
            if not isinstance(totals, list) or not totals or not isinstance(totals[0], Mapping):
                raise ValueError("retailRate.total is missing")
            raw_amount = totals[0].get("amount")
            raw_currency = totals[0].get("currency")
        total_price = Decimal(str(raw_amount))
        if not total_price.is_finite() or total_price <= 0:
            raise ValueError("offer total price is invalid")
        if not isinstance(raw_currency, str):
            raise ValueError("offer currency is missing")
        currency = raw_currency.strip().upper()
        if not _CURRENCY_CODE.fullmatch(currency):
            raise ValueError("offer currency is invalid")
        if currency != self._currency:
            # 跳过该房型（由 _rate_candidates 汇总），勿在循环中中断整次 HTTP 解析；
            # 币种过滤后为空则 fail-closed。
            raise ValueError(
                f"currency_mismatch: returned {currency}, expected {self._currency}"
            )
        rates = room.get("rates")
        first_rate = rates[0] if isinstance(rates, list) and rates else {}
        if not isinstance(first_rate, Mapping):
            first_rate = {}
        room_name = first_rate.get("name")
        board_name = first_rate.get("boardName")
        mapped_room_id = first_rate.get("mappedRoomId")
        signature = (
            room_name.strip().casefold() if isinstance(room_name, str) else "",
            board_name.strip().casefold() if isinstance(board_name, str) else "",
            str(mapped_room_id) if mapped_room_id is not None else "",
        )
        return _RateCandidate(
            raw_offer_id=raw_offer_id,
            hotel_id=hotel_id,
            hotel_name=hotel_name,
            total_price=total_price,
            currency=currency,
            room_signature=signature,
        )

    @staticmethod
    def _hotel_names(payload: Mapping[str, Any]) -> dict[str, str]:
        hotels = payload.get("hotels", [])
        if not isinstance(hotels, list):
            return {}
        names: dict[str, str] = {}
        for hotel in hotels:
            if not isinstance(hotel, Mapping):
                continue
            hotel_id = hotel.get("id")
            name = hotel.get("name")
            if isinstance(hotel_id, str) and isinstance(name, str) and name.strip():
                names[hotel_id] = name.strip()
        return names

    def _search_payload(
        self,
        query: HotelSearchQuery,
        *,
        hotel_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "occupancies": [{"adults": self._adults}],
            "currency": self._currency,
            "guestNationality": self._guest_nationality,
            "checkin": query.check_in.isoformat(),
            "checkout": query.check_out.isoformat(),
            "timeout": self._supplier_timeout_seconds,
            "maxRatesPerHotel": self._max_rates_per_hotel,
            "includeHotelData": True,
        }
        if hotel_ids is not None:
            body["hotelIds"] = list(hotel_ids)
        else:
            body["limit"] = self._result_limit
            body.update(self._location_fields(query.city))
        return body

    def _location_fields(self, city: str) -> dict[str, str]:
        normalized = city.strip()
        direct_iata = normalized.upper()
        if _IATA_CODE.fullmatch(direct_iata):
            return {"iataCode": direct_iata}
        try:
            return dict(self._locations[normalized.casefold()])
        except KeyError as exc:
            raise ProviderError(
                f"No verified LiteAPI location mapping is configured for {city!r}"
            ) from exc

    @staticmethod
    def _validate_location(value: Mapping[str, str]) -> dict[str, str]:
        normalized = dict(value)
        if set(normalized) == {"iataCode"}:
            iata = normalized["iataCode"].strip().upper()
            if not _IATA_CODE.fullmatch(iata):
                raise ValueError("LiteAPI iataCode must be three letters")
            return {"iataCode": iata}
        if set(normalized) == {"cityName", "countryCode"}:
            city_name = normalized["cityName"].strip()
            country_code = normalized["countryCode"].strip().upper()
            if not city_name or not _ISO2_CODE.fullmatch(country_code):
                raise ValueError("LiteAPI city location requires cityName and ISO-2 countryCode")
            return {"cityName": city_name, "countryCode": country_code}
        if set(normalized) == {"placeId"}:
            place_id = normalized["placeId"].strip()
            if not place_id:
                raise ValueError("LiteAPI placeId cannot be blank")
            return {"placeId": place_id}
        raise ValueError(
            "LiteAPI locations must contain iataCode, placeId, or cityName+countryCode"
        )

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> httpx.Response:
        normalized_method = method.upper()
        if path != "/hotels/rates":
            raise ProviderError("LiteAPI read-only boundary rejected a non-rates endpoint")
        self.external_request_count += 1
        self.external_request_log.append((normalized_method, path))
        try:
            return self._client.request(
                normalized_method,
                f"{self._api_base_url}{path}",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-Key": self._api_key,
                },
                json=json_body,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError(
                f"LiteAPI request failed transiently: {type(exc).__name__}",
                error_code=(
                    "LITEAPI_TRANSPORT_TIMEOUT"
                    if isinstance(exc, httpx.TimeoutException)
                    else "LITEAPI_TRANSPORT_CONNECTION_ERROR"
                ),
                layer="liteapi_transport",
                cause_type=type(exc).__name__,
                cause_chain=_exception_type_chain(exc),
                response_received=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"LiteAPI request failed: {type(exc).__name__}",
                error_code="LITEAPI_HTTP_CLIENT_ERROR",
                layer="liteapi_http_client",
                cause_type=type(exc).__name__,
                cause_chain=_exception_type_chain(exc),
            ) from exc

    def _successful_payload(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 204:
            return {"data": []}
        payload = self._json_payload(response)
        if response.status_code in _RETRYABLE_STATUS_CODES:
            raise RetryableProviderError(
                f"LiteAPI returned retryable HTTP {response.status_code}",
                error_code=f"LITEAPI_HTTP_{response.status_code}",
                layer="liteapi_http",
                cause_type="HTTPStatusError",
                cause_chain=("HTTPStatusError",),
                response_received=True,
                http_status=response.status_code,
                request_id=_response_request_id(response),
            )
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"LiteAPI returned HTTP {response.status_code}",
                error_code=f"LITEAPI_HTTP_{response.status_code}",
                layer="liteapi_http",
                cause_type="HTTPStatusError",
                cause_chain=("HTTPStatusError",),
                response_received=True,
                http_status=response.status_code,
                request_id=_response_request_id(response),
            )
        return payload

    @staticmethod
    def _json_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError("LiteAPI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("LiteAPI response must be a JSON object")
        return payload

    def _require_expected_mode(self, payload: Mapping[str, Any], *, status_code: int) -> None:
        if status_code == 204:
            return
        sandbox = payload.get("sandbox")
        if self._require_sandbox and sandbox is not True:
            raise ProviderError(
                "LiteAPI response was not explicitly marked as Sandbox; "
                "set LITEAPI_REQUIRE_SANDBOX=false only for approved read-only production rates"
            )

    def _archive_raw_payload(
        self,
        payload: Mapping[str, Any],
        *,
        captured_at: datetime,
        category: str,
        identity: str,
    ) -> RawResponseReference:
        raw_bytes = self._canonical_payload_bytes(payload)
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return self.raw_response_store.put_bytes(
            object_key=(
                f"liteapi-{self.provider_mode}/{captured_at:%Y/%m/%d}/{category}/"
                f"liteapi-{category}-{identity_hash}-{raw_hash[:12]}.json"
            ),
            content=raw_bytes,
            content_type="application/json",
            stored_at=captured_at,
            retention_until=captured_at + timedelta(days=self.raw_response_retention_days),
            access_policy=RawResponseAccessPolicy.SYSTEM_REPLAY_OR_AUDIT_ADMIN,
        )

    @staticmethod
    def _canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _internal_ref(raw_offer_id: str) -> str:
        return f"litehotel_{hashlib.sha256(raw_offer_id.encode()).hexdigest()[:24]}"

    @staticmethod
    def _nightly_price(total_price: Decimal, query: HotelSearchQuery) -> Decimal:
        nights = (query.check_out - query.check_in).days
        return (total_price / Decimal(nights)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

    def _validate_query(self, query: HotelSearchQuery) -> None:
        if not query.city.strip():
            raise ProviderError("LiteAPI hotel city is required")
        if query.check_out <= query.check_in:
            raise ProviderError("LiteAPI checkout must be after checkin")
        if query.check_in < self._aware_now().date():
            raise ProviderError("LiteAPI checkin cannot be in the past")

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise RuntimeError("LiteAPI provider clock must return a timezone-aware datetime")
        if not math.isfinite(value.timestamp()):
            raise RuntimeError("LiteAPI provider clock returned an invalid datetime")
        return value


def _environment_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _exception_type_chain(exc: BaseException) -> tuple[str, ...]:
    chain: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 8:
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _response_request_id(response: httpx.Response) -> str | None:
    for header in ("x-request-id", "request-id", "x-correlation-id"):
        value = response.headers.get(header)
        if value:
            return value[:200]
    return None

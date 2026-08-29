"""Duffel 航班适配器：仅安全的 Test Mode 搜索/重校验与说明型交接。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from corporate_travel_agent.domain.enums import (
    RawResponseAccessPolicy,
    RevalidationStatus,
    SourceType,
    TransportMode,
)
from corporate_travel_agent.domain.models import (
    InventorySnapshot,
    ProviderHandoff,
    RawResponseReference,
    RevalidationResult,
    TransportOffer,
    TravelOptionVersion,
)
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
from .duffel_test_order import RevalidatedTestOffer

DUFFEL_API_BASE_URL = "https://api.duffel.com"
DUFFEL_API_VERSION = "v2"
TEST_MODE_DISCLOSURE = (
    "Duffel Test Mode sandbox inventory; schedules and prices are not production data"
)
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_IATA_CODE = re.compile(r"^[A-Z]{3}$")

#: 一次请求最多放几个 slice。抄的是 Amadeus 的上限，和 `validation.MAX_JOURNEY_LEGS`
#: 是同一个数、同一个来源——两处对不上的话，宿主会拼出一个供应商必然拒绝的请求。
MAX_JOURNEY_SLICES = 6

# 已核对的城市/机场标签 → Duffel place code（多机场城市优先 metro code）。
# fail-closed：仅精确匹配键；Test Mode 对国际线比国内线更稳。
#: 用户侧地名 → Duffel 用的 IATA 码。
#: **由城市登记表派生**（`config/cities.json`）——此前这里手写一份、LiteAPI 手写一份、
#: 时区表手写一份、政策配置手写一份，加一座城市要改四个地方，漏一个就是一种新的坏法。
#: 查不到时 `_location_code` 会抛 ProviderError：**宁可搜不了，也不拿猜的码去搜。**
DEFAULT_LOCATION_CODES: dict[str, str] = city_registry().duffel_codes()


class DuffelProvider:
    """Duffel 航班适配器，仅限安全的 Test Mode 操作。

    V1 实现航班搜索、offer 拉取/重校验与说明型 handoff，从不创建订单或支付。
    酒店搜索显式失败，避免把不支持覆盖误判为空供应商结果。
    """

    name = "duffel"
    provider_mode = "test"

    def __init__(
        self,
        access_token: str,
        *,
        client: httpx.Client | None = None,
        api_base_url: str = DUFFEL_API_BASE_URL,
        api_version: str = DUFFEL_API_VERSION,
        cabin_class: str = "economy",
        max_offers: int = 50,
        supplier_timeout_ms: int = 20_000,
        expected_currency: str | None = None,
        location_codes: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        raw_response_store: RawResponseObjectStore | None = None,
        raw_response_retention_days: int = 90,
        required_owner_name: str | None = None,
        quote_context_store: ProviderQuoteContextStore | None = None,
    ) -> None:
        token = access_token.strip()
        if not token.startswith("duffel_test_"):
            raise ValueError("DuffelProvider only accepts duffel_test_ access tokens")
        if api_version != DUFFEL_API_VERSION:
            raise ValueError("DuffelProvider currently supports Duffel-Version v2 only")
        if not api_base_url.startswith("https://"):
            raise ValueError("Duffel API base URL must use HTTPS")
        if cabin_class not in {"economy", "premium_economy", "business", "first"}:
            raise ValueError("Unsupported Duffel cabin class")
        if not 1 <= max_offers <= 200:
            raise ValueError("max_offers must be between 1 and 200")
        if not 2_000 <= supplier_timeout_ms <= 60_000:
            raise ValueError("supplier_timeout_ms must be between 2000 and 60000")
        if not 1 <= raw_response_retention_days <= 3650:
            raise ValueError("raw_response_retention_days must be between 1 and 3650")

        normalized_currency = expected_currency.strip().upper() if expected_currency else None
        if normalized_currency and not re.fullmatch(r"[A-Z]{3}", normalized_currency):
            raise ValueError("expected_currency must be a three-letter currency code")

        codes = dict(DEFAULT_LOCATION_CODES)
        for location, code in (location_codes or {}).items():
            normalized_code = code.strip().upper()
            if not _IATA_CODE.fullmatch(normalized_code):
                raise ValueError(f"Invalid IATA location code for {location!r}")
            codes[location.strip().casefold()] = normalized_code
        normalized_owner_name = required_owner_name.strip() if required_owner_name else None
        if required_owner_name is not None and not normalized_owner_name:
            raise ValueError("required_owner_name cannot be blank")

        self._access_token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._api_version = api_version
        self._cabin_class = cabin_class
        self._max_offers = max_offers
        self._supplier_timeout_ms = supplier_timeout_ms
        self._expected_currency = normalized_currency
        self._location_codes = codes
        self._required_owner_name = normalized_owner_name
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = client or httpx.Client(timeout=65.0)
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
        self._revalidated_test_offers: dict[str, RevalidatedTestOffer] = {}
        self.external_request_count = 0
        self.external_request_log: list[tuple[str, str]] = []

    @classmethod
    def from_environment(
        cls,
        *,
        raw_response_store: RawResponseObjectStore | None = None,
        clock: Callable[[], datetime] | None = None,
        quote_context_store: ProviderQuoteContextStore | None = None,
    ) -> DuffelProvider:
        """从环境变量构造 Provider；拒绝 live mode / 真实预订开关。"""
        token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DUFFEL_ACCESS_TOKEN is required when TRAVEL_PROVIDER=duffel")
        if os.getenv("DUFFEL_LIVE_MODE", "false").casefold() == "true":
            raise RuntimeError("Duffel live mode is outside the current project scope")
        if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
            raise RuntimeError("Live booking must remain disabled for DuffelProvider V1")

        location_codes: Mapping[str, str] | None = None
        raw_location_codes = os.getenv("DUFFEL_LOCATION_CODES_JSON", "").strip()
        if raw_location_codes:
            try:
                parsed = json.loads(raw_location_codes)
            except json.JSONDecodeError as exc:
                raise RuntimeError("DUFFEL_LOCATION_CODES_JSON must be valid JSON") from exc
            if not isinstance(parsed, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
            ):
                raise RuntimeError("DUFFEL_LOCATION_CODES_JSON must be a string map")
            location_codes = parsed

        return cls(
            token,
            api_base_url=os.getenv("DUFFEL_API_BASE_URL", DUFFEL_API_BASE_URL),
            api_version=os.getenv("DUFFEL_API_VERSION", DUFFEL_API_VERSION),
            cabin_class=os.getenv("DUFFEL_CABIN_CLASS", "economy"),
            expected_currency=os.getenv("DUFFEL_EXPECTED_CURRENCY") or None,
            location_codes=location_codes,
            clock=clock,
            raw_response_store=raw_response_store,
            raw_response_retention_days=int(os.getenv("RAW_RESPONSE_RETENTION_DAYS", "90")),
            quote_context_store=quote_context_store,
        )

    def close(self) -> None:
        """关闭自建 HTTP 客户端。"""
        if self._owns_client:
            self._client.close()

    def owns_ref(self, ref: str) -> bool:
        """判断库存引用是否属于 Duffel offer（``off_`` 前缀）。"""
        return ref.startswith("off_")

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        """调用 Duffel offer_requests 搜索航班并归一化为 InventorySnapshot。"""
        self._validate_query(query)
        origin_code = self._location_code(query.origin)
        destination_code = self._location_code(query.destination)
        if origin_code == destination_code:
            raise ProviderError("Duffel origin and destination must differ after normalization")
        payload = {
            "data": {
                "cabin_class": self._cabin_class,
                "slices": [
                    {
                        "origin": origin_code,
                        "destination": destination_code,
                        "departure_date": query.depart_after.date().isoformat(),
                    }
                ],
                "passengers": [{"type": "adult"}],
            }
        }
        response = self._send(
            "POST",
            "/air/offer_requests",
            params={
                "return_offers": "true",
                "supplier_timeout": str(self._supplier_timeout_ms),
            },
            json_body=payload,
        )
        response_payload = self._successful_payload(response)
        return self._snapshot_from_search(query, response_payload)

    def search_multi_city(
        self, queries: Sequence[TransportSearchQuery]
    ) -> InventorySnapshot:
        """一次请求问完整条多段行程，返回**整票**报价。

        今天每段各发一次搜索，等于买 N 张单程票。Duffel 把多城当一次请求：
        多个 slice、一个覆盖全部航段的 offer，按 IATA 票价构造规则定价——
        **不是几张单程相加**。实测两次、四条行程八次比价全部同向，整票便宜
        15%–76%（`reports/evaluation-runs/multicity-pricing-*/`），
        连普通往返都便宜 18%–23%。

        返回的 snapshot 里，**一张整票被摊成它的每一段**，同一张票的几段共用一个
        ``fare_ref``；整票的价记在第一段上，其余段为 0。这样 ``sum(leg.price)``
        仍然是真实总价，而"这几段不能拆开"这件事在数据里说得出口。
        """
        if len(queries) < 2:
            raise ProviderError("multi-city search needs at least two legs")
        if len(queries) > MAX_JOURNEY_SLICES:
            raise ProviderError(
                f"multi-city search supports at most {MAX_JOURNEY_SLICES} legs"
            )
        for query in queries:
            self._validate_query(query)
        codes = [
            (self._location_code(item.origin), self._location_code(item.destination))
            for item in queries
        ]
        for origin_code, destination_code in codes:
            if origin_code == destination_code:
                raise ProviderError(
                    "Duffel origin and destination must differ after normalization"
                )
        payload = {
            "data": {
                "cabin_class": self._cabin_class,
                "slices": [
                    {
                        "origin": origin_code,
                        "destination": destination_code,
                        "departure_date": query.depart_after.date().isoformat(),
                    }
                    for query, (origin_code, destination_code) in zip(
                        queries, codes, strict=True
                    )
                ],
                "passengers": [{"type": "adult"}],
            }
        }
        response = self._send(
            "POST",
            "/air/offer_requests",
            params={
                "return_offers": "true",
                "supplier_timeout": str(self._supplier_timeout_ms),
            },
            json_body=payload,
        )
        response_payload = self._successful_payload(response)
        return self._snapshot_from_multi_city(tuple(queries), response_payload)

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        """V1 仅支持航班；酒店必须由其他 Provider 覆盖。"""
        del query
        raise ProviderError(
            "DuffelProvider V1 is flight-only; hotel coverage requires another provider"
        )

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult:
        """逐条 GET offer，核对币种/过期并更新报价上下文。"""
        checked_at = self._aware_now()
        current_prices: dict[str, Decimal] = {}
        unavailable: list[str] = []
        warnings: list[str] = []
        changed = False

        for ref in dict.fromkeys(refs):
            if not self.owns_ref(ref):
                unavailable.append(ref)
                warnings.append(f"{ref}: reference does not belong to Duffel")
                continue
            context = self._quote_context(ref, observed_at=checked_at)

            response = self._send("GET", f"/air/offers/{ref}")
            if self._offer_is_unavailable(response):
                unavailable.append(ref)
                continue
            payload = self._successful_payload(response)
            archived_at = self._aware_now()
            self.revalidation_raw_responses.append(
                self._archive_raw_payload(
                    payload,
                    captured_at=archived_at,
                    category="revalidation",
                    identity=f"{ref}|{archived_at.isoformat()}",
                )
            )
            offer = self._response_data(payload)
            self._require_test_mode(offer)
            current_price, currency = self._offer_price(offer)
            if currency != context.currency:
                raise ProviderError(
                    f"Duffel revalidation changed currency for {ref}: "
                    f"{context.currency} to {currency}"
                )
            expires_at = self._offer_expiry(offer)
            if expires_at <= checked_at:
                unavailable.append(ref)
                continue
            current_prices[ref] = current_price
            changed = changed or current_price != context.price
            self.quote_context_store.put_many(
                (
                    ProviderQuoteContext(
                        provider=self.name,
                        snapshot_id=context.snapshot_id,
                        ref_id=ref,
                        price=current_price,
                        currency=currency,
                        captured_at=checked_at,
                        expires_at=expires_at,
                        payload=context.payload,
                    ),
                )
            )
            if self._required_owner_name is not None:
                self._revalidated_test_offers[ref] = RevalidatedTestOffer(
                    offer_id=ref,
                    amount=current_price,
                    currency=currency,
                    expires_at=expires_at,
                    passenger_ids=self._offer_passenger_ids(offer),
                    owner_name=self._offer_owner_name(offer),
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

    def test_order_offer(self, ref: str) -> RevalidatedTestOffer:
        """返回最近一次成功重校验留下的不可变 Test Mode 结账事实。"""

        try:
            offer = self._revalidated_test_offers[ref]
        except KeyError as exc:
            raise ProviderError(
                "Duffel offer has not completed a successful Test Mode revalidation"
            ) from exc
        if offer.expires_at <= self._aware_now():
            raise ProviderError("Duffel Test Mode offer expired before checkout")
        return offer

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff:
        """生成说明型 handoff（Test Mode，不创建订单）。"""
        now = self._aware_now()
        duffel_refs = tuple(ref for ref in option.inventory_refs if self.owns_ref(ref))
        if not duffel_refs:
            raise ProviderError("The selected option contains no Duffel offer reference")
        contexts = [self._quote_context(ref, observed_at=now) for ref in duffel_refs]
        expires_at = min(item.expires_at for item in contexts)
        return ProviderHandoff(
            provider=self.name,
            url_or_instructions=(
                "Test Mode only; no order was created. Re-open the Duffel test flow for "
                + ", ".join(duffel_refs)
            ),
            expires_at=expires_at,
        )

    def _snapshot_from_search(
        self,
        query: TransportSearchQuery,
        payload: dict[str, Any],
    ) -> InventorySnapshot:
        captured_at = self._aware_now()
        raw_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        query_hash = inventory_query_hash(query)
        identity = "|".join((query_hash, captured_at.isoformat(), raw_hash, self.provider_mode))
        snapshot_id = f"duffel-flight-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        raw_response = self._archive_raw_payload(
            payload,
            captured_at=captured_at,
            category="flight",
            identity=snapshot_id,
        )
        self.last_raw_response = raw_response
        data = self._response_data(payload)
        self._require_test_mode(data)
        raw_offers = data.get("offers")
        if not isinstance(raw_offers, list):
            raise ProviderError("Duffel response data.offers must be a list")

        normalized: list[TransportOffer] = []
        expirations: list[datetime] = []
        warnings = [TEST_MODE_DISCLOSURE]
        skip_reasons: list[str] = []
        filter_counts: dict[str, int] = {}
        seen_refs: set[str] = set()
        for index, raw_offer in enumerate(raw_offers[: self._max_offers]):
            try:
                offer, expires_at, filter_reason = self._normalize_offer(query, raw_offer)
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                reason = f"parse_error:{type(exc).__name__}"
                detail = (
                    f"Duffel offer index {index} was skipped: "
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                )
                warnings.append(detail)
                skip_reasons.append(detail)
                filter_counts[reason] = filter_counts.get(reason, 0) + 1
                continue
            if offer is None:
                code = filter_reason or "filtered_unknown"
                detail = f"Duffel offer index {index} was skipped: {code}"
                warnings.append(detail)
                skip_reasons.append(detail)
                filter_counts[code] = filter_counts.get(code, 0) + 1
                continue
            if offer.ref_id in seen_refs:
                warnings.append(f"Duplicate Duffel offer skipped: {offer.ref_id}")
                continue
            seen_refs.add(offer.ref_id)
            normalized.append(offer)
            expirations.append(expires_at)

        if raw_offers and not normalized:
            if self._required_owner_name is not None:
                raise ProviderError(
                    "Duffel returned no safely normalized offers owned by "
                    f"{self._required_owner_name}"
                )
            summary = _format_filter_summary(filter_counts)
            samples = "; ".join(skip_reasons[:3])
            reasons = "; ".join(part for part in (summary, samples) if part)
            # 解析/畸形载荷保持 ProviderError（不可伪装为空库存）。
            # 时间窗口过滤后可用库存=0 应走向 NO_FEASIBLE_OPTION，而非连接失败。
            parse_failures = any(code.startswith("parse_error:") for code in filter_counts)
            constraint_only = bool(filter_counts) and all(
                code in _CONSTRAINT_FILTER_CODES for code in filter_counts
            )
            if parse_failures or not constraint_only:
                raise ProviderError(
                    "Duffel returned offers but none could be safely normalized; "
                    f"raw_response_sha256={raw_hash}; {reasons}"
                )
            warnings.append(
                "Duffel returned offers outside the requested time window; "
                f"raw_response_sha256={raw_hash}; {reasons}"
            )
        if len(raw_offers) > self._max_offers:
            warnings.append(f"Duffel response was capped at {self._max_offers} normalized offers")

        items = tuple(replace(item, snapshot_id=snapshot_id) for item in normalized)
        self.quote_context_store.put_many(
            tuple(
                ProviderQuoteContext(
                    provider=self.name,
                    snapshot_id=snapshot_id,
                    ref_id=item.ref_id,
                    price=item.price,
                    currency=item.currency,
                    captured_at=captured_at,
                    expires_at=expires_at,
                    payload={"offer_id": item.ref_id},
                )
                for item, expires_at in zip(items, expirations, strict=True)
            )
        )
        valid_until = (
            min(captured_at + timedelta(minutes=15), *expirations)
            if expirations
            else captured_at + timedelta(minutes=5)
        )
        return InventorySnapshot(
            snapshot_id=snapshot_id,
            provider=self.name,
            source_type=SourceType.AUTHORIZED_API,
            captured_at=captured_at,
            valid_until=valid_until,
            query_hash=query_hash,
            raw_payload_hash=raw_hash,
            items=items,
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
                f"Duffel quote context is missing for {ref_id}; re-run the search",
                error_code="PROVIDER_QUOTE_CONTEXT_MISSING",
                layer="provider_quote_context",
                retryable=False,
            ) from exc
        except ProviderQuoteContextExpiredError as exc:
            raise ProviderError(
                f"Duffel quote context expired for {ref_id}; re-run the search",
                error_code="PROVIDER_QUOTE_CONTEXT_EXPIRED",
                layer="provider_quote_context",
                retryable=False,
            ) from exc

    def _normalize_offer(
        self,
        query: TransportSearchQuery,
        raw_offer: object,
    ) -> tuple[TransportOffer | None, datetime, str | None]:
        if not isinstance(raw_offer, Mapping):
            raise TypeError("offer must be an object")
        self._require_test_mode(raw_offer)
        ref = raw_offer["id"]
        if not isinstance(ref, str) or not ref.startswith("off_"):
            raise ValueError("invalid Duffel offer ID")
        price, currency = self._offer_price(raw_offer)
        expires_at = self._offer_expiry(raw_offer)
        if expires_at <= self._aware_now():
            return None, expires_at, "filtered_expired"
        if (
            self._required_owner_name is not None
            and self._offer_owner_name(raw_offer) != self._required_owner_name
        ):
            return None, expires_at, "filtered_owner"

        slices = raw_offer["slices"]
        if not isinstance(slices, list) or len(slices) != 1:
            raise ValueError("one-way search offer must contain exactly one slice")
        segments = slices[0]["segments"]
        if not isinstance(segments, list) or not segments:
            raise ValueError("offer slice must contain segments")
        first = segments[0]
        last = segments[-1]
        if not isinstance(first, Mapping) or not isinstance(last, Mapping):
            raise TypeError("offer segment must be an object")
        depart_at = self._parse_flight_datetime(
            first["departing_at"],
            "departing_at",
            first.get("origin"),
        )
        arrive_at = self._parse_flight_datetime(
            last["arriving_at"],
            "arriving_at",
            last.get("destination"),
        )
        if arrive_at <= depart_at:
            raise ValueError("offer arrival must be after departure")
        if depart_at < query.depart_after:
            return None, expires_at, "filtered_by_depart_after"
        if query.arrive_before is not None and arrive_at > query.arrive_before:
            return None, expires_at, "filtered_by_arrive_before"

        return (
            TransportOffer(
                ref_id=ref,
                snapshot_id="pending",
                provider=self.name,
                mode=TransportMode.FLIGHT,
                origin=query.origin,
                destination=query.destination,
                depart_at=depart_at,
                arrive_at=arrive_at,
                price=price,
                seat_class=self._cabin_class.upper(),
                available=True,
                is_direct=len(segments) == 1,
                currency=currency,
            ),
            expires_at,
            None,
        )

    def _snapshot_from_multi_city(
        self,
        queries: tuple[TransportSearchQuery, ...],
        payload: dict[str, Any],
    ) -> InventorySnapshot:
        """把多段响应归一化成"一张票摊成几段"的快照。

        **和单段那条路各走各的，一行不共用。** 单段那条路被冻结数据集、评测轨迹与
        实链路 runner 盯着，它的行为一个字都不该因为多城而变。
        """
        captured_at = self._aware_now()
        raw_bytes = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        query_hash = hashlib.sha256(
            "|".join(inventory_query_hash(item) for item in queries).encode()
        ).hexdigest()
        identity = "|".join(
            (query_hash, captured_at.isoformat(), raw_hash, self.provider_mode)
        )
        snapshot_id = f"duffel-journey-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        raw_response = self._archive_raw_payload(
            payload, captured_at=captured_at, category="flight", identity=snapshot_id
        )
        self.last_raw_response = raw_response
        data = self._response_data(payload)
        self._require_test_mode(data)
        raw_offers = data.get("offers")
        if not isinstance(raw_offers, list):
            raise ProviderError("Duffel response data.offers must be a list")

        legs: list[TransportOffer] = []
        expirations: list[datetime] = []
        warnings = [TEST_MODE_DISCLOSURE]
        filter_counts: dict[str, int] = {}
        seen_fares: set[str] = set()
        for index, raw_offer in enumerate(raw_offers[: self._max_offers]):
            try:
                fare_legs, expires_at, reason = self._normalize_journey_offer(
                    queries, raw_offer
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                code = f"parse_error:{type(exc).__name__}"
                filter_counts[code] = filter_counts.get(code, 0) + 1
                warnings.append(
                    f"Duffel journey offer index {index} was skipped: "
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                )
                continue
            if fare_legs is None:
                code = reason or "filtered_unknown"
                filter_counts[code] = filter_counts.get(code, 0) + 1
                continue
            fare_ref = fare_legs[0].fare_ref or ""
            if fare_ref in seen_fares:
                continue
            seen_fares.add(fare_ref)
            legs.extend(fare_legs)
            expirations.extend([expires_at] * len(fare_legs))

        if filter_counts:
            warnings.append(
                "Duffel journey offers filtered: " + _format_filter_summary(filter_counts)
            )
        if raw_offers and not legs:
            parse_failures = any(code.startswith("parse_error:") for code in filter_counts)
            if parse_failures:
                raise ProviderError(
                    "Duffel returned journey offers but none could be safely "
                    f"normalized; raw_response_sha256={raw_hash}"
                )
            warnings.append(
                "Duffel returned journey offers outside the requested time windows; "
                f"raw_response_sha256={raw_hash}"
            )

        items = tuple(replace(item, snapshot_id=snapshot_id) for item in legs)
        self.quote_context_store.put_many(
            tuple(
                ProviderQuoteContext(
                    provider=self.name,
                    snapshot_id=snapshot_id,
                    ref_id=item.ref_id,
                    price=item.price,
                    currency=item.currency,
                    captured_at=captured_at,
                    expires_at=expires_at,
                    # 复核时要拿**整票**的 offer id 去查，不是这一段的合成 ref。
                    payload={"offer_id": item.fare_ref or item.ref_id},
                )
                for item, expires_at in zip(items, expirations, strict=True)
            )
        )
        valid_until = (
            min(captured_at + timedelta(minutes=15), *expirations)
            if expirations
            else captured_at + timedelta(minutes=5)
        )
        return InventorySnapshot(
            snapshot_id=snapshot_id,
            provider=self.name,
            source_type=SourceType.AUTHORIZED_API,
            captured_at=captured_at,
            valid_until=valid_until,
            query_hash=query_hash,
            raw_payload_hash=raw_hash,
            items=items,
            provider_warnings=tuple(warnings),
            raw_response=raw_response,
        )

    def _normalize_journey_offer(
        self,
        queries: tuple[TransportSearchQuery, ...],
        raw_offer: Any,
    ) -> tuple[list[TransportOffer] | None, datetime, str | None]:
        """一张整票 → 它的每一段。整票的价只记在第一段上。

        少一段的报价一律丢掉：那是另一趟行程，便宜是应该的，拿来比价没有意义。
        """
        if not isinstance(raw_offer, Mapping):
            raise TypeError("Duffel offer must be an object")
        fare_ref = str(raw_offer.get("id") or "")
        if not fare_ref.startswith("off_"):
            raise ValueError("invalid Duffel offer ID")
        price, currency = self._offer_price(raw_offer)
        expires_at = self._offer_expiry(raw_offer)
        if expires_at <= self._aware_now():
            return None, expires_at, "filtered_expired"
        if (
            self._required_owner_name is not None
            and self._offer_owner_name(raw_offer) != self._required_owner_name
        ):
            return None, expires_at, "filtered_owner"

        slices = raw_offer["slices"]
        if not isinstance(slices, list):
            raise ValueError("journey offer must contain a slices list")
        if len(slices) != len(queries):
            return None, expires_at, "filtered_partial_journey"

        legs: list[TransportOffer] = []
        for index, (raw_slice, query) in enumerate(zip(slices, queries, strict=True)):
            segments = raw_slice["segments"]
            if not isinstance(segments, list) or not segments:
                raise ValueError("offer slice must contain segments")
            first, last = segments[0], segments[-1]
            if not isinstance(first, Mapping) or not isinstance(last, Mapping):
                raise TypeError("offer segment must be an object")
            depart_at = self._parse_flight_datetime(
                first["departing_at"], "departing_at", first.get("origin")
            )
            arrive_at = self._parse_flight_datetime(
                last["arriving_at"], "arriving_at", last.get("destination")
            )
            if arrive_at <= depart_at:
                raise ValueError("offer arrival must be after departure")
            if depart_at < query.depart_after:
                return None, expires_at, "filtered_by_depart_after"
            if query.arrive_before is not None and arrive_at > query.arrive_before:
                return None, expires_at, "filtered_by_arrive_before"
            legs.append(
                TransportOffer(
                    # 一张票的每一段各需要一个自己的 ref，否则去重会把它们当成一条。
                    # 前缀保留 off_ 是因为 `owns_ref` 认这个前缀。
                    ref_id=f"{fare_ref}#{index}",
                    snapshot_id="pending",
                    provider=self.name,
                    mode=TransportMode.FLIGHT,
                    origin=query.origin,
                    destination=query.destination,
                    depart_at=depart_at,
                    arrive_at=arrive_at,
                    # 整票只有一个价，记在第一段上；其余段为 0。
                    price=price if index == 0 else Decimal("0"),
                    seat_class=self._cabin_class.upper(),
                    available=True,
                    is_direct=len(segments) == 1,
                    currency=currency,
                    fare_ref=fare_ref,
                )
            )
        return legs, expires_at, None

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {
            "headers": {
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Authorization": f"Bearer {self._access_token}",
                "Duffel-Version": self._api_version,
            },
        }
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
            kwargs["headers"]["Content-Type"] = "application/json"
        normalized_method = method.upper()
        self.external_request_count += 1
        self.external_request_log.append((normalized_method, path))
        try:
            return self._client.request(
                normalized_method,
                f"{self._api_base_url}{path}",
                **kwargs,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError(
                f"Duffel {method} request failed transiently: {type(exc).__name__}",
                error_code=(
                    "DUFFEL_TRANSPORT_TIMEOUT"
                    if isinstance(exc, httpx.TimeoutException)
                    else "DUFFEL_TRANSPORT_CONNECTION_ERROR"
                ),
                layer="duffel_transport",
                cause_type=type(exc).__name__,
                cause_chain=_exception_type_chain(exc),
                response_received=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Duffel {method} request failed: {type(exc).__name__}",
                error_code="DUFFEL_HTTP_CLIENT_ERROR",
                layer="duffel_http_client",
                cause_type=type(exc).__name__,
                cause_chain=_exception_type_chain(exc),
            ) from exc

    def _archive_raw_payload(
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

    def _successful_payload(self, response: httpx.Response) -> dict[str, Any]:
        payload = self._json_payload(response)
        if response.status_code in _RETRYABLE_STATUS_CODES:
            raise RetryableProviderError(
                f"Duffel returned retryable HTTP {response.status_code}"
                + self._safe_error_suffix(payload),
                error_code=f"DUFFEL_HTTP_{response.status_code}",
                layer="duffel_http",
                cause_type="HTTPStatusError",
                cause_chain=("HTTPStatusError",),
                response_received=True,
                http_status=response.status_code,
                request_id=_response_request_id(response),
            )
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"Duffel returned HTTP {response.status_code}" + self._safe_error_suffix(payload),
                error_code=f"DUFFEL_HTTP_{response.status_code}",
                layer="duffel_http",
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
            raise ProviderError("Duffel returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Duffel response must be a JSON object")
        return payload

    @staticmethod
    def _safe_error_suffix(payload: Mapping[str, Any]) -> str:
        errors = payload.get("errors")
        if not isinstance(errors, list) or not errors or not isinstance(errors[0], Mapping):
            return ""
        code = errors[0].get("code")
        error_type = errors[0].get("type")
        safe = ":".join(
            value
            for value in (error_type, code)
            if isinstance(value, str) and re.fullmatch(r"[a-z0-9_\-]{1,80}", value)
        )
        return f" ({safe})" if safe else ""

    def _offer_is_unavailable(self, response: httpx.Response) -> bool:
        if response.status_code in {404, 410}:
            return True
        if 200 <= response.status_code < 300:
            return False
        payload = self._json_payload(response)
        errors = payload.get("errors")
        if isinstance(errors, list):
            codes = {item.get("code") for item in errors if isinstance(item, Mapping)}
            if codes & {
                "offer_no_longer_available",
                "offer_expired",
                "offer_not_found",
            }:
                return True
        return False

    def _offer_price(self, offer: Mapping[str, Any]) -> tuple[Decimal, str]:
        raw_amount = offer.get("total_amount")
        raw_currency = offer.get("total_currency")
        if not isinstance(raw_amount, str) or not isinstance(raw_currency, str):
            raise ValueError("offer price fields are missing")
        price = Decimal(raw_amount)
        if not price.is_finite() or price < 0:
            raise ValueError("offer total_amount is invalid")
        currency = raw_currency.upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("offer total_currency is invalid")
        if self._expected_currency and currency != self._expected_currency:
            raise ProviderError(
                f"Duffel returned {currency}, expected {self._expected_currency}; "
                "an approved FX snapshot is required before policy evaluation"
            )
        return price, currency

    def _offer_expiry(self, offer: Mapping[str, Any]) -> datetime:
        return self._parse_datetime(offer.get("expires_at"), "expires_at")

    @staticmethod
    def _offer_owner_name(offer: Mapping[str, Any]) -> str:
        owner = offer.get("owner")
        if not isinstance(owner, Mapping):
            raise ValueError("offer owner is missing")
        name = owner.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("offer owner name is missing")
        return name.strip()

    @staticmethod
    def _offer_passenger_ids(offer: Mapping[str, Any]) -> tuple[str, ...]:
        passengers = offer.get("passengers")
        if not isinstance(passengers, list) or not passengers:
            raise ValueError("offer passengers are missing")
        passenger_ids: list[str] = []
        for passenger in passengers:
            if not isinstance(passenger, Mapping):
                raise ValueError("offer passenger must be an object")
            passenger_id = passenger.get("id")
            if not isinstance(passenger_id, str) or not passenger_id.startswith("pas_"):
                raise ValueError("offer passenger ID is invalid")
            passenger_ids.append(passenger_id)
        return tuple(passenger_ids)

    @staticmethod
    def _response_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ProviderError("Duffel response is missing the data object")
        return data

    @staticmethod
    def _require_test_mode(value: Mapping[str, Any]) -> None:
        if value.get("live_mode") is not False:
            raise ProviderError("Duffel response was not explicitly marked as Test Mode")

    @staticmethod
    def _parse_datetime(value: object, field_name: str) -> datetime:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be an ISO datetime")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError(f"{field_name} must include a timezone")
        return parsed

    @classmethod
    def _parse_flight_datetime(
        cls,
        value: object,
        field_name: str,
        location: object,
    ) -> datetime:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be an ISO datetime")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is not None:
            return parsed
        if not isinstance(location, Mapping):
            raise ValueError(f"{field_name} requires an airport time_zone")
        time_zone = location.get("time_zone")
        if not isinstance(time_zone, str):
            raise ValueError(f"{field_name} requires an airport time_zone")
        try:
            return parsed.replace(tzinfo=ZoneInfo(time_zone))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{field_name} contains an unknown airport time_zone") from exc

    def _location_code(self, value: str) -> str:
        normalized = value.strip()
        direct = normalized.upper()
        if _IATA_CODE.fullmatch(direct):
            return direct
        try:
            return self._location_codes[normalized.casefold()]
        except KeyError as exc:
            raise ProviderError(
                f"No verified Duffel IATA mapping is configured for location {value!r}"
            ) from exc

    @staticmethod
    def _validate_query(query: TransportSearchQuery) -> None:
        if not query.origin.strip() or not query.destination.strip():
            raise ProviderError("Duffel origin and destination are required")
        if query.origin.strip().casefold() == query.destination.strip().casefold():
            raise ProviderError("Duffel origin and destination must differ")
        if query.depart_after.utcoffset() is None:
            raise ProviderError("Duffel depart_after must include a timezone")
        if query.arrive_before is not None:
            if query.arrive_before.utcoffset() is None:
                raise ProviderError("Duffel arrive_before must include a timezone")
            if query.arrive_before <= query.depart_after:
                raise ProviderError("Duffel arrive_before must be after depart_after")

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise RuntimeError("DuffelProvider clock must return timezone-aware datetimes")
        if not math.isfinite(value.timestamp()):
            raise RuntimeError("DuffelProvider clock returned an invalid datetime")
        return value


def _exception_type_chain(exc: BaseException) -> tuple[str, ...]:
    result: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(result) < 8:
        seen.add(id(current))
        result.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return tuple(result)


_CONSTRAINT_FILTER_CODES = frozenset(
    {
        "filtered_by_arrive_before",
        "filtered_by_depart_after",
        "filtered_expired",
    }
)


def _format_filter_summary(filter_counts: Mapping[str, int]) -> str:
    if not filter_counts:
        return ""
    parts = [
        f"{code}={count}"
        for code, count in sorted(filter_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return "filter_summary=" + ",".join(parts)


def _response_request_id(response: httpx.Response) -> str | None:
    return response.headers.get("x-request-id") or response.headers.get("request-id")

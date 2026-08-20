"""按环境变量组装外部库存 Provider（或回退到 mock）。"""

from __future__ import annotations

import os
import re

from corporate_travel_agent.services.object_storage import RawResponseObjectStore
from corporate_travel_agent.services.provider_quote_context import (
    ProviderQuoteContextStore,
)

from .base import TravelInventoryProvider
from .composite import CompositeTravelInventoryProvider
from .duffel import DuffelProvider
from .liteapi import LiteAPIHotelProvider

_CURRENCY = re.compile(r"^[A-Z]{3}$")


def travel_provider_from_environment(
    *,
    raw_response_store: RawResponseObjectStore | None = None,
    policy_currency: str | None = None,
    quote_context_store: ProviderQuoteContextStore | None = None,
) -> TravelInventoryProvider | None:
    """返回显式配置的外部 Provider；未配置时返回 None（由调用方使用 Mock）。

    若提供 ``policy_currency``，LiteAPI/Duffel 报价币种必须与之匹配。
    无 FX 快照时混合币种无法通过政策证据，故在构造阶段拒绝错误配置。
    """

    expected = _normalize_policy_currency(policy_currency)
    provider_name = os.getenv("TRAVEL_PROVIDER", "mock").strip().casefold()
    if provider_name in {"", "mock"}:
        return None
    if provider_name == "duffel":
        _require_env_currency_match(
            env_name="DUFFEL_EXPECTED_CURRENCY",
            policy_currency=expected,
            optional=True,
        )
        return DuffelProvider.from_environment(
            raw_response_store=raw_response_store,
            quote_context_store=quote_context_store,
        )
    if provider_name in {"duffel_liteapi", "duffel+liteapi", "duffel-liteapi"}:
        _require_env_currency_match(
            env_name="LITEAPI_CURRENCY",
            policy_currency=expected,
            optional=False,
            default_when_unset="USD",
        )
        _require_env_currency_match(
            env_name="DUFFEL_EXPECTED_CURRENCY",
            policy_currency=expected,
            optional=True,
        )
        hotel_provider = LiteAPIHotelProvider.from_environment(
            raw_response_store=raw_response_store,
            quote_context_store=quote_context_store,
        )
        try:
            transport_provider = DuffelProvider.from_environment(
                raw_response_store=raw_response_store,
                quote_context_store=quote_context_store,
            )
        except Exception:
            hotel_provider.close()
            raise
        return CompositeTravelInventoryProvider(
            transport_provider=transport_provider,
            hotel_provider=hotel_provider,
        )
    raise RuntimeError(f"Unsupported TRAVEL_PROVIDER: {provider_name}")


def _normalize_policy_currency(policy_currency: str | None) -> str | None:
    if policy_currency is None:
        return None
    value = policy_currency.strip().upper()
    if not _CURRENCY.fullmatch(value):
        raise RuntimeError(
            f"policy currency must be a three-letter code, got {policy_currency!r}"
        )
    return value


def _require_env_currency_match(
    *,
    env_name: str,
    policy_currency: str | None,
    optional: bool,
    default_when_unset: str | None = None,
) -> None:
    """校验环境变量币种与政策币种一致，避免混合币种库存。"""
    if policy_currency is None:
        return
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        if optional:
            return
        configured = (default_when_unset or "").strip().upper()
    else:
        configured = raw.strip().upper()
    if not configured:
        return
    if not _CURRENCY.fullmatch(configured):
        raise RuntimeError(f"{env_name} must be a three-letter currency code")
    if configured != policy_currency:
        raise RuntimeError(
            f"{env_name}={configured} must match active policy currency "
            f"{policy_currency}; mixed inventory currencies are rejected without "
            "an approved FX snapshot"
        )

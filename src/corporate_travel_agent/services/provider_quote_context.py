"""供应商报价再校验上下文：无凭证、可过期的本地持久化刷新输入。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Any, Protocol


class ProviderQuoteContextError(RuntimeError):
    """本地供应商再校验上下文相关错误基类。"""


class ProviderQuoteContextMissingError(ProviderQuoteContextError):
    """指定供应商/引用 ID 没有持久化报价上下文。"""

    def __init__(self, provider: str, ref_id: str) -> None:
        super().__init__(f"No persisted quote context for {provider}:{ref_id}")
        self.provider = provider
        self.ref_id = ref_id


class ProviderQuoteContextExpiredError(ProviderQuoteContextError):
    """持久化报价上下文已过期。"""

    def __init__(
        self,
        provider: str,
        ref_id: str,
        *,
        expires_at: datetime,
    ) -> None:
        super().__init__(
            f"Persisted quote context expired for {provider}:{ref_id} "
            f"at {expires_at.isoformat()}"
        )
        self.provider = provider
        self.ref_id = ref_id
        self.expires_at = expires_at


_FORBIDDEN_CREDENTIAL_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "headers",
        "password",
        "secret",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderQuoteContext:
    """刷新单条供应商报价所需的最小、无凭证输入。"""

    provider: str
    snapshot_id: str
    ref_id: str
    price: Decimal
    currency: str
    captured_at: datetime
    expires_at: datetime
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.snapshot_id.strip() or not self.ref_id.strip():
            raise ValueError("provider, snapshot_id, and ref_id are required")
        if self.price < 0:
            raise ValueError("quote context price cannot be negative")
        if len(self.currency) != 3 or self.currency != self.currency.upper():
            raise ValueError("quote context currency must be a three-letter uppercase code")
        if not _timezone_aware(self.captured_at) or not _timezone_aware(self.expires_at):
            raise ValueError("quote context timestamps must be timezone-aware")
        if self.expires_at <= self.captured_at:
            raise ValueError("quote context expires_at must be after captured_at")
        payload = deepcopy(dict(self.payload))
        _reject_credentials(payload)
        try:
            json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("quote context payload must be JSON serializable") from exc
        object.__setattr__(self, "payload", payload)

    @property
    def key(self) -> tuple[str, str, str]:
        """唯一键：(provider, snapshot_id, ref_id)。"""
        return self.provider, self.snapshot_id, self.ref_id


class ProviderQuoteContextStore(Protocol):
    """供应商返回搜索结果前写入/读取报价上下文的持久化缝。"""

    def put_many(self, contexts: tuple[ProviderQuoteContext, ...]) -> None: ...

    def get(
        self,
        provider: str,
        ref_id: str,
        *,
        observed_at: datetime,
    ) -> ProviderQuoteContext: ...

    def purge_expired(self, *, before: datetime, limit: int = 1_000) -> int: ...


class InMemoryProviderQuoteContextStore:
    """有界开发/测试适配器；共享注入可模拟多 worker。"""

    backend_name = "memory"

    def __init__(self, *, max_entries: int = 10_000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._contexts: dict[tuple[str, str, str], ProviderQuoteContext] = {}
        self._lock = RLock()

    def put_many(self, contexts: tuple[ProviderQuoteContext, ...]) -> None:
        if len({item.key for item in contexts}) != len(contexts):
            raise ValueError("quote context batch contains duplicate keys")
        with self._lock:
            if contexts:
                cutoff = max(item.captured_at for item in contexts)
                expired_keys = [
                    key
                    for key, item in self._contexts.items()
                    if item.expires_at <= cutoff
                ]
                for key in expired_keys:
                    self._contexts.pop(key, None)
            for context in contexts:
                self._contexts[context.key] = context
            overflow = len(self._contexts) - self._max_entries
            if overflow > 0:
                oldest = sorted(
                    self._contexts.values(),
                    key=lambda item: (item.expires_at, item.captured_at, item.key),
                )[:overflow]
                for item in oldest:
                    self._contexts.pop(item.key, None)

    def get(
        self,
        provider: str,
        ref_id: str,
        *,
        observed_at: datetime,
    ) -> ProviderQuoteContext:
        _require_aware(observed_at, "observed_at")
        with self._lock:
            candidates = [
                item
                for item in self._contexts.values()
                if item.provider == provider and item.ref_id == ref_id
            ]
        if not candidates:
            raise ProviderQuoteContextMissingError(provider, ref_id)
        context = max(candidates, key=lambda item: (item.expires_at, item.captured_at))
        if context.expires_at <= observed_at:
            raise ProviderQuoteContextExpiredError(
                provider,
                ref_id,
                expires_at=context.expires_at,
            )
        return context

    def purge_expired(self, *, before: datetime, limit: int = 1_000) -> int:
        _require_aware(before, "before")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._lock:
            expired = sorted(
                (
                    item
                    for item in self._contexts.values()
                    if item.expires_at <= before
                ),
                key=lambda item: (item.expires_at, item.key),
            )[:limit]
            for item in expired:
                self._contexts.pop(item.key, None)
        return len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._contexts)


def _reject_credentials(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_CREDENTIAL_KEYS:
                raise ValueError(f"quote context cannot contain credentials at {path}.{key}")
            _reject_credentials(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_credentials(nested, path=f"{path}[{index}]")


def _timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _require_aware(value: datetime, name: str) -> None:
    if not _timezone_aware(value):
        raise ValueError(f"{name} must be timezone-aware")

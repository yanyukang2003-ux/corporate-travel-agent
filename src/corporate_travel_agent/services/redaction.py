"""JSON 脱敏：按敏感键与模式剥离 PII/凭证，供日志、审计与评测输出使用。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import isfinite
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

REDACTION_PROFILE_VERSION: Final = "travel-redaction-v1"
REDACTED_VALUE = "[REDACTED]"
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 1_000_000

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "password",
        "secret",
        "cookie",
        "sessionid",
        "passport",
        "idcard",
        "nationalid",
        "travelerid",
        "employeeid",
        "customerid",
        "memberid",
        "loyaltynumber",
        "phone",
        "mobile",
        "email",
        "personname",
        "passengername",
        "givenname",
        "firstname",
        "lastname",
        "fullname",
        "dateofbirth",
        "birthdate",
        "homeaddress",
        "billingaddress",
    }
)

_STRING_PATTERNS = (
    (
        "email",
        re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w.-])"),
    ),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
    (
        "national_id",
        re.compile(
            r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)"
        ),
    ),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
            r"(?:\.[A-Za-z0-9_-]{8,})?\b"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """脱敏结果：脱敏后载荷、命中次数与按原因分类的计数。"""

    payload: Any
    redaction_count: int
    reasons: dict[str, int]


class RedactionLimitError(ValueError):
    """JSON 深度或节点数超过脱敏安全上限。"""


def redact_json(payload: Any) -> RedactionResult:
    """返回 JSON 兼容值的脱敏副本，并汇总脱敏原因计数。"""

    reasons: dict[str, int] = {}
    node_count = 0

    def record(reason: str, count: int = 1) -> None:
        reasons[reason] = reasons.get(reason, 0) + count

    def visit(value: Any, *, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_JSON_NODES:
            raise RedactionLimitError("JSON payload exceeds the redaction node limit")
        if depth > MAX_JSON_DEPTH:
            raise RedactionLimitError("JSON payload exceeds the redaction depth limit")

        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                if _is_sensitive_key(key):
                    result[key] = REDACTED_VALUE
                    record("sensitive_key")
                else:
                    result[key] = visit(child, depth=depth + 1)
            return result
        if isinstance(value, list):
            return [visit(child, depth=depth + 1) for child in value]
        if isinstance(value, str):
            if re.fullmatch(r"[a-f0-9]{64}", value):
                return value
            redacted = value
            if redacted.startswith(("http://", "https://")):
                parts = urlsplit(redacted)
                if parts.query or parts.fragment or parts.username or parts.password:
                    host = parts.hostname or ""
                    if parts.port is not None:
                        host = f"{host}:{parts.port}"
                    redacted = urlunsplit((parts.scheme, host, parts.path, "", ""))
                    record("url_credentials_or_query")
            for reason, pattern in _STRING_PATTERNS:
                redacted, count = pattern.subn(REDACTED_VALUE, redacted)
                if count:
                    record(reason, count)
            return redacted
        if isinstance(value, float) and not isfinite(value):
            raise TypeError("JSON numbers must be finite")
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise TypeError(f"Unsupported non-JSON value: {type(value).__name__}")

    redacted_payload = visit(payload, depth=0)
    return RedactionResult(
        payload=redacted_payload,
        redaction_count=sum(reasons.values()),
        reasons=reasons,
    )


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)

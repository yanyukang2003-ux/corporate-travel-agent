"""OpenAI SDK/HTTP 异常 → 带可重试标记的 ``LanguageModelError``。

工具循环适配器和评测 Judge 共用这一份映射。它原本住在旧的意图抽取适配器里
（`openai_adapter.py`，已随 legacy / semantic 入口一起删除，见 ADR-0003）。
"""

from __future__ import annotations

from .ports import LanguageModelError


def _classified_openai_error(exc: Exception) -> LanguageModelError:
    """将 SDK/HTTP 异常映射为可重试标记的 LanguageModelError。"""
    cause_type = type(exc).__name__
    cause_chain = _exception_type_chain(exc)
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    if cause_type == "APITimeoutError":
        return LanguageModelError(
            "Intent extraction failed at OpenAI transport: APITimeoutError",
            error_code="OPENAI_API_TIMEOUT",
            layer="openai_transport",
            cause_type=cause_type,
            cause_chain=cause_chain,
            retryable=True,
            response_received=False,
        )
    if cause_type == "APIConnectionError":
        return LanguageModelError(
            "Intent extraction failed at OpenAI transport: APIConnectionError",
            error_code="OPENAI_API_CONNECTION_ERROR",
            layer="openai_transport",
            cause_type=cause_type,
            cause_chain=cause_chain,
            retryable=True,
            response_received=False,
        )
    if isinstance(status, int):
        # 短暂 HTTP：限流、超时与多数 5xx。永久 4xx（鉴权/校验/未找到）不可重试，改走澄清。
        retryable = status in {408, 409, 425, 429} or 500 <= status <= 599
        return LanguageModelError(
            f"Intent extraction failed at OpenAI HTTP layer: {cause_type}",
            error_code=f"OPENAI_HTTP_{status}",
            layer="openai_http",
            cause_type=cause_type,
            cause_chain=cause_chain,
            retryable=retryable,
            response_received=True,
            http_status=status,
            request_id=str(request_id) if request_id else None,
        )
    return LanguageModelError(
        f"Intent extraction failed in OpenAI adapter: {cause_type}",
        error_code="OPENAI_ADAPTER_OR_PARSE_ERROR",
        layer="openai_adapter",
        cause_type=cause_type,
        cause_chain=cause_chain,
        retryable=False,
        response_received=None,
    )


def _exception_type_chain(exc: BaseException) -> tuple[str, ...]:
    """收集异常类型链（cause/context），最多 8 层。"""
    result: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(result) < 8:
        seen.add(id(current))
        result.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return tuple(result)

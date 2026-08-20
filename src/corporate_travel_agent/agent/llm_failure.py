"""llm_failure：对 LLM 抽取失败分类，避免把账单/鉴权故障当成「缺字段」。"""

from __future__ import annotations

from enum import StrEnum

from corporate_travel_agent.agent.ports import LanguageModelError


class LLMFailureClass(StrEnum):
    """LLM 失败类别：账单、鉴权、限流、容量、传输、解析等。"""

    BILLING = "billing"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    CAPACITY = "capacity"
    TRANSPORT = "transport"
    PARSE = "parse"
    UNKNOWN = "unknown"


_BILLING_MARKERS = ("credit", "balance", "quota", "insufficient", "payment required")
_PARSE_CODES = {
    "OPENAI_ADAPTER_OR_PARSE_ERROR",
    "LANGUAGE_MODEL_ERROR",
}


def classify_llm_failure(exc: LanguageModelError) -> LLMFailureClass:
    """根据 HTTP 状态、错误码与文案将 LanguageModelError 归类。"""
    status = exc.http_status
    code = (exc.error_code or "").upper()
    message = str(exc).casefold()
    billed = status == 402 or code.endswith("_402")
    if billed or any(token in message for token in _BILLING_MARKERS):
        return LLMFailureClass.BILLING
    if status in {401, 403} or code.endswith("_401") or code.endswith("_403"):
        return LLMFailureClass.AUTH
    if status == 429 or code.endswith("_429"):
        return LLMFailureClass.RATE_LIMIT
    if status == 529 or (status is not None and 500 <= status <= 599):
        return LLMFailureClass.CAPACITY
    if exc.layer == "openai_transport" or code in {
        "OPENAI_API_TIMEOUT",
        "OPENAI_API_CONNECTION_ERROR",
    }:
        return LLMFailureClass.TRANSPORT
    if exc.layer == "openai_adapter" or code in _PARSE_CODES:
        return LLMFailureClass.PARSE
    return LLMFailureClass.UNKNOWN


def consumes_clarification_round(failure: LLMFailureClass) -> bool:
    """账单/鉴权属基础设施故障，不应消耗用户澄清轮次预算。"""
    return failure not in {LLMFailureClass.BILLING, LLMFailureClass.AUTH}


def should_try_fallback_model(failure: LLMFailureClass) -> bool:
    """仅短暂容量/限流/传输故障才可切换备用付费模型。"""
    return failure in {
        LLMFailureClass.RATE_LIMIT,
        LLMFailureClass.CAPACITY,
        LLMFailureClass.TRANSPORT,
    }


def health_status_for(failure: LLMFailureClass | None, *, configured: bool) -> str:
    """映射为对外健康状态字符串（如 billing_blocked / ok）。"""
    if not configured:
        return "not_configured"
    if failure is LLMFailureClass.BILLING:
        return "billing_blocked"
    if failure is LLMFailureClass.AUTH:
        return "auth_failed"
    if failure is LLMFailureClass.RATE_LIMIT:
        return "rate_limited"
    if failure is None:
        return "ok"
    return "unavailable"


def user_preamble(failure: LLMFailureClass) -> str:
    """面向用户的失败说明前缀（中文）。"""
    if failure is LLMFailureClass.BILLING:
        return (
            "模型服务额度或账户受限，这次没有调用成功。"
            "你的原话已保存；下面是本地解析仍为空的字段。"
            "请检查账户额度后点「用原指令重试」，或直接补全字段。"
        )
    if failure is LLMFailureClass.AUTH:
        return (
            "模型服务认证失败，没有写入行程字段。"
            "请检查 API 密钥后用原指令重试。"
        )
    if failure is LLMFailureClass.RATE_LIMIT:
        return "模型服务限流。若备用模型也失败，请稍后用原指令重试。"
    if failure is LLMFailureClass.CAPACITY:
        return "模型服务过载。已尝试备用模型（如已配置）。请稍后用原指令重试。"
    if failure is LLMFailureClass.TRANSPORT:
        return "模型服务网络波动，没能完成抽取。请用原指令重试。"
    return "刚才无法稳定抽取行程。请核对下面仍为空的字段后重试。"

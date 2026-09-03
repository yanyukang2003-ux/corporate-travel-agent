"""评测什么：供应商只读调用的全链路契约——重试边界、Duffel/LiteAPI 请求序列是否合法。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from corporate_travel_agent.agent.orchestrator import TRANSIENT_RETRY_REASON
from corporate_travel_agent.domain.enums import ToolCallStatus
from corporate_travel_agent.domain.models import ToolCallRecord


def provider_retry_contract_valid(
    records: Sequence[ToolCallRecord],
    *,
    max_attempts: int,
) -> bool:
    """校验工具调用重试是否有界，且仅挂接在显式可重试失败之后。"""

    by_sequence = {item.sequence: item for item in records}
    attempts_by_tool: dict[str, int] = {}
    for item in records:
        attempts_by_tool[item.tool_name] = attempts_by_tool.get(item.tool_name, 0) + 1
        if attempts_by_tool[item.tool_name] > max_attempts:
            return False
        if item.retry_of is None:
            if item.reason_code is not None:
                return False
            continue
        previous = by_sequence.get(item.retry_of)
        if (
            previous is None
            or previous.sequence >= item.sequence
            or previous.tool_name != item.tool_name
            or previous.status is not ToolCallStatus.FAILED
            or previous.retryable is not True
            or item.reason_code != TRANSIENT_RETRY_REASON
        ):
            return False
    return True


def duffel_read_sequence_valid(
    requests: Sequence[Mapping[str, str]],
    *,
    selected_flight_ref: str | None,
    max_attempts: int,
) -> bool:
    """校验 Duffel 航班搜索与报价刷新在逻辑顺序与次数上限内是否合法。"""

    if selected_flight_ref is None:
        return False
    search = {"provider": "duffel", "method": "POST", "path": "/air/offer_requests"}
    revalidate = {
        "provider": "duffel",
        "method": "GET",
        "path": f"/air/offers/{selected_flight_ref}",
    }
    search_attempts = requests.count(search)
    revalidation_attempts = requests.count(revalidate)
    return (
        1 <= search_attempts <= max_attempts
        and 1 <= revalidation_attempts <= max_attempts
        and list(requests)
        == [search] * search_attempts + [revalidate] * revalidation_attempts
    )


def liteapi_read_sequence_valid(
    requests: Sequence[Mapping[str, str]],
    *,
    max_attempts: int,
) -> bool:
    """校验 LiteAPI 两阶段只读请求及其有界重试是否符合约定。"""

    request = {"provider": "liteapi", "method": "POST", "path": "/hotels/rates"}
    return 2 <= len(requests) <= 2 * max_attempts and all(
        item == request for item in requests
    )

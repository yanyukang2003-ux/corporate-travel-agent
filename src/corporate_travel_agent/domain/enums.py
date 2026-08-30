"""domain.enums：任务状态、政策结果、交通模式等领域枚举。"""

from enum import StrEnum


class TaskState(StrEnum):
    """差旅任务状态机状态（Orchestrator 按此分支）。"""

    DRAFT = "DRAFT"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NEEDS_STRUCTURED_INPUT = "NEEDS_STRUCTURED_INPUT"
    SEARCHING = "SEARCHING"
    WAITING_FOR_PROVIDER = "WAITING_FOR_PROVIDER"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PLANNING = "PLANNING"
    OPTIONS_READY = "OPTIONS_READY"
    NO_FEASIBLE_OPTION = "NO_FEASIBLE_OPTION"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    REVALIDATING = "REVALIDATING"
    RECONFIRMATION_REQUIRED = "RECONFIRMATION_REQUIRED"
    READY_FOR_HANDOFF = "READY_FOR_HANDOFF"
    HANDED_OFF = "HANDED_OFF"
    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ToolCallStatus(StrEnum):
    """单次工具调用生命周期状态。"""

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PolicyOutcome(StrEnum):
    """政策引擎对某条规则/方案的判定结果。"""

    COMPLIANT = "COMPLIANT"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    FORBIDDEN = "FORBIDDEN"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class TransportMode(StrEnum):
    """交通方式：航班或火车。"""

    FLIGHT = "FLIGHT"
    TRAIN = "TRAIN"


class BookingScope(StrEnum):
    """本次请求要预订哪些交通航段。"""

    OUTBOUND_ONLY = "OUTBOUND_ONLY"
    RETURN_ONLY = "RETURN_ONLY"
    ROUND_TRIP = "ROUND_TRIP"


class TripLegRole(StrEnum):
    """航段在整趟差旅中的角色。"""

    OUTBOUND = "OUTBOUND"
    RETURN = "RETURN"


class LodgingRequirement(StrEnum):
    """住宿需求三态：必须 / 不需要 / 未说明。"""

    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNSPECIFIED = "UNSPECIFIED"


class IntentEntrypoint(StrEnum):
    """创建并继续自然语言任务时固定使用的意图流程。"""

    STRUCTURED = "structured"
    LEGACY = "legacy"
    SEMANTIC = "semantic"
    #: 工具循环：模型每轮挑一个带类型的工具，校验在工具签名上，没有全局必填表。
    AGENTIC = "agentic"


class SourceType(StrEnum):
    """库存快照来源类型。"""

    MOCK = "MOCK"
    REPLAY = "REPLAY"
    BROWSER_ASSISTED = "BROWSER_ASSISTED"
    AUTHORIZED_API = "AUTHORIZED_API"


class RawResponseAccessPolicy(StrEnum):
    """原始 Provider 响应对外访问策略。"""

    SYSTEM_REPLAY_OR_AUDIT_ADMIN = "SYSTEM_REPLAY_OR_AUDIT_ADMIN"


class ApprovalStatus(StrEnum):
    """例外审批单状态。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"


class RevalidationStatus(StrEnum):
    """选中方案再校验（revalidate）结果。"""

    UNCHANGED = "UNCHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    UNAVAILABLE = "UNAVAILABLE"
    PROVIDER_FAILED = "PROVIDER_FAILED"

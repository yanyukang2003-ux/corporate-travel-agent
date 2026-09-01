"""编排器的公共底座：异常、常量、几个模块都用得到的小函数。

这一层不依赖任何 mixin；各职责模块从这里取名字，包根再把公开名字原样导出。
"""

from __future__ import annotations

from typing import TypeVar


class WorkflowError(RuntimeError):
    """工作流层可预期错误基类。"""

    pass


class LanguageModelUnavailable(WorkflowError):
    """未配置或不可用语言模型。"""

    pass


class ToolBudgetExceeded(WorkflowError):
    """任务工具调用预算耗尽。"""

    pass


ToolResult = TypeVar("ToolResult")

MAX_PROVIDER_ATTEMPTS = 3
"""每次 Provider 调用的总尝试次数（含首次）；有意设上限。"""

# 默认两次，使单次 429/5xx 可在澄清前恢复（HANDOFF §13 Step2）。
MAX_LLM_ATTEMPTS = 2
"""默认 LLM 尝试次数；生产/评测可改为显式单次重试。"""

TRANSIENT_RETRY_REASON = "TRANSIENT_PROVIDER_FAULT"
TRANSIENT_LLM_RETRY_REASON = "TRANSIENT_LLM_TRANSPORT_FAULT"
PARTIAL_COVERAGE_METADATA_KEY = "provider_coverage_notices"

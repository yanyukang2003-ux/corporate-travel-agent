"""工作流状态机：约束任务 TaskState 的合法迁移。"""

from corporate_travel_agent.domain.enums import TaskState


class InvalidTransition(ValueError):
    """尝试执行不允许的状态迁移时抛出。"""

    pass


_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DRAFT: frozenset(
        {
            TaskState.NEEDS_CLARIFICATION,
            TaskState.NEEDS_STRUCTURED_INPUT,
            TaskState.SEARCHING,
            TaskState.TOOL_BUDGET_EXHAUSTED,
            TaskState.OUT_OF_SCOPE,
        }
    ),
    TaskState.NEEDS_CLARIFICATION: frozenset({TaskState.DRAFT}),
    TaskState.NEEDS_STRUCTURED_INPUT: frozenset({TaskState.DRAFT}),
    TaskState.SEARCHING: frozenset(
        {
            TaskState.PLANNING,
            TaskState.WAITING_FOR_PROVIDER,
            TaskState.PROVIDER_FAILED,
            TaskState.TOOL_BUDGET_EXHAUSTED,
        }
    ),
    TaskState.WAITING_FOR_PROVIDER: frozenset(
        {
            TaskState.SEARCHING,
            TaskState.REVALIDATING,
            TaskState.PROVIDER_FAILED,
            TaskState.DRAFT,
        }
    ),
    # DRAFT：允许搜索后意图修订（多轮改订 → 重新抽取）
    TaskState.PROVIDER_FAILED: frozenset({TaskState.SEARCHING, TaskState.DRAFT}),
    TaskState.PLANNING: frozenset(
        {
            TaskState.OPTIONS_READY,
            TaskState.NO_FEASIBLE_OPTION,
            TaskState.PROVIDER_FAILED,
        }
    ),
    TaskState.NO_FEASIBLE_OPTION: frozenset({TaskState.SEARCHING, TaskState.DRAFT}),
    TaskState.OPTIONS_READY: frozenset({TaskState.WAITING_FOR_USER}),
    TaskState.WAITING_FOR_USER: frozenset(
        {
            TaskState.SEARCHING,
            TaskState.WAITING_FOR_APPROVAL,
            TaskState.REVALIDATING,
            TaskState.DRAFT,
        }
    ),
    TaskState.WAITING_FOR_APPROVAL: frozenset(
        {TaskState.REVALIDATING, TaskState.OPTIONS_READY, TaskState.SEARCHING}
    ),
    TaskState.REVALIDATING: frozenset(
        {
            TaskState.READY_FOR_HANDOFF,
            TaskState.RECONFIRMATION_REQUIRED,
            TaskState.WAITING_FOR_PROVIDER,
            TaskState.PROVIDER_FAILED,
            TaskState.TOOL_BUDGET_EXHAUSTED,
        }
    ),
    TaskState.RECONFIRMATION_REQUIRED: frozenset({TaskState.SEARCHING}),
    TaskState.READY_FOR_HANDOFF: frozenset({TaskState.HANDED_OFF}),
    TaskState.HANDED_OFF: frozenset(),
    TaskState.TOOL_BUDGET_EXHAUSTED: frozenset(),
    # 用户显式消息可重开误判或已过时的 OOS 任务
    TaskState.OUT_OF_SCOPE: frozenset({TaskState.DRAFT}),
}


class StateMachine:
    """根据预定义邻接表校验并执行任务状态迁移。"""

    def transition(self, current: TaskState, target: TaskState) -> TaskState:
        """若 ``current → target`` 合法则返回 target，否则抛出 InvalidTransition。"""
        if target not in _ALLOWED[current]:
            raise InvalidTransition(f"Transition {current.value} -> {target.value} is not allowed")
        return target

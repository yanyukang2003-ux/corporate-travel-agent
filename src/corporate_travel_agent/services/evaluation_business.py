"""评测什么：**业务结果**——用了多久、提前多少天订、多少单超标、员工最后有没有真去下单。

## 这个模块为什么存在

在它之前，仓库里所有评测指标都在模型层：任务通过率、硬断言通过率、规则输出完整率、
裁判打分、工具调用效率。这些数字回答的是"模型答得像不像话"，**没有一个回答
"这东西对企业有没有用"**。

产品设计思路里那句批评说得很直白：模型回答得像模像样但员工还是绕开去用携程，
说明产品是失败的。要看见这件事，只能量四个业务侧的数：

| 指标 | 这里的口径 |
|---|---|
| 交接完成率 | 员工点了"我去官方平台订好了"的比例——**渠道内预订率的上限估计** |
| 确认预订率 | 员工回填了订单号和实付金额的比例——仍是自述，但比"点了一下"多一个订单号和一个数 |
| 平均耗时 | 从任务第一条审计事件到交接完成的**墙上时间**，含人等审批、人在想 |
| 提前预订天数 | 员工回填的下单时刻（没回填就用交接时刻）→ 首段实际出发时刻 |
| 实付偏差 | （实付 − 方案价）÷ 方案价，只算币种一致的确认记录 |
| 费控对账率 | 回填过的任务里费控对上账的比例——对上了的自述才算"核实过" |
| 渠道外预订率 | 费控记录里本系统找不到对应确认的比例——第一次有真值的"绕开系统" |
| 变更人工介入率 | 航变/会议改期开出的改期任务里，人不得不插手的比例 |
| 超标发生率 | 方案里带"需审批/禁止"证据的比例，分选中和展示两个口径 |

## 数据从哪来

只有两样输入：`TripTask` 聚合和它的 `AuditEvent` 序列。**不调模型、不调供应商、
不读任何评测数据集**，所以它既能算离线评测跑出来的任务，也能算线上真实任务，
两边口径完全一样。

`AuditEvent` 只存输入/输出的哈希，不存明文，所以这里只用它的两个明文字段：
`event_type` 和 `created_at`。任何需要值本身的东西（政策证据、航段时间、澄清轮数）
一律从 `TripTask` 聚合上读。

## 时钟：离线评测必须显式传进来

审计事件的时间戳走的是真实墙上时间（`new_audit_event` 里的 `datetime.now(UTC)`），
**不是编排器的时钟**。线上这两者是同一个，没有区别；但离线评测通常把编排器时钟
冻在某一天，于是"交接时刻"在今天、"出发时刻"在冻住的那条时间线上，两者相减出来的
提前预订天数是一个没有意义的数。

所以 `build_business_metrics_report` 收一个可选的 `handoff_reference_time`：冻了时钟的
跑法把冻住的那个时刻传进来，提前天数就用它算；线上不传，用审计里的交接时刻。
用了哪一个记在 `TaskBusinessRecord.advance_days_reference` 上，不靠读代码猜。

耗时（`seconds_to_handoff`）**没有**这个开关，因为它本来就该量墙上时间。但要注意：
离线评测里它量的是**跑批脚本有多快**，不是员工花了多久，跨跑次比较没有意义。

## 三处必须说在前面的口径限制

1. **交接完成率不是真实的渠道内预订率。** 分子是员工自己在本系统里点的"订好了"
   （`HANDOFF_COMPLETED`），不是供应商回执。他可能点了却没订，也可能订了不点。
   接到真实预订回执之前，这个数只能当上限估计看，不能当结论。
   **确认预订率**（`booking_confirmation_rate`）好一截但仍不是回执：分子是员工回填了
   订单号和实付金额的任务（`BookingConfirmation.source == SELF_REPORTED`）。订单号
   系统核不了；它比"点了一下"多的是一个具体的号和一个具体的数，少的是核实。
   两个数之间的差（`booking_confirmation_rate.of_handed_off`）就是交接完成率高估了多少。
2. **平均耗时是墙上时间，不是系统耗时。** 它包含员工去开会、经理三小时后才批的时间。
   这是故意的：设计思路里"员工订一次复杂行程平均要花半小时到一小时"量的就是墙上时间。
   要看系统自己快不快，看 `evaluation_performance`，不要看这里。
3. **超标发生率实际上量的是"需要审批"的比例。** 按已编码不变量，`FORBIDDEN` 方案
   在规划阶段就被硬过滤，根本不会出现在给员工看的列表里。所以"展示口径"这个数里
   几乎只有 `REQUIRES_APPROVAL`。

## 分母为零时不写零

一次跑里没有任何任务走到交接，交接完成率的正确答案是"**测不出来**"，不是 0。
所有指标沿用 `MetricResult` 的 `status` 字段：`measured` / `unavailable`，
`unavailable` 时 `value` 是 `None`。把测不出来记成 0 会让一条平的曲线看起来像
"一直很差"，而事实是"一直没数据"。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState
from corporate_travel_agent.domain.models import (
    AuditEvent,
    TravelOptionVersion,
    TripTask,
)
from corporate_travel_agent.services.evaluation_quality import MetricResult

BUSINESS_PROTOCOL_ID: Final = "business-outcome-v1"


class BusinessMetricsError(RuntimeError):
    """业务指标报告写盘失败。"""


#: 任务开始的那条审计事件。四个入口（结构化、旧自然语言、语义、工具循环）各有一个名字，
#: 都是任务建立后写的第一条事件。少写一个入口，那个入口的耗时就会静默变成"测不出来"。
TASK_START_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "TASK_CREATED",
        "TASK_CREATED_FROM_MESSAGE",
        "SEMANTIC_TASK_CREATED_FROM_MESSAGE",
        "AGENTIC_TASK_CREATED_FROM_MESSAGE",
    }
)

#: 员工确认"我去官方平台订好了"时写的事件。
HANDOFF_COMPLETED_EVENT_TYPE = "HANDOFF_COMPLETED"

#: 员工回填订单号和实付金额时写的事件。
BOOKING_CONFIRMED_EVENT_TYPE = "BOOKING_CONFIRMED"

#: 改期任务上出现这些事件之一，就算"人不得不插手"：改需求、补结构化表、没方案、供应商失败。
CHANGE_INTERVENTION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "REQUEST_REVISED",
        "STRUCTURED_FALLBACK_SUBMITTED",
        "NO_FEASIBLE_OPTION",
        "PROVIDER_FAILED",
        "AGENTIC_CLARIFICATION_REQUESTED",
    }
)

#: 算作"交接过"的状态：点过"我去订了"，或者回填过订单号（回填会先记交接）。
HANDED_OFF_STATES: frozenset[TaskState] = frozenset(
    {TaskState.HANDED_OFF, TaskState.BOOKING_CONFIRMED}
)

#: 算进"超标"的政策结论。`INSUFFICIENT_EVIDENCE`（判不了）不算超标——它是缺数据，
#: 不是违规，混进来会把两件完全不同的事搅成一个数。
VIOLATION_OUTCOMES: frozenset[PolicyOutcome] = frozenset(
    {PolicyOutcome.REQUIRES_APPROVAL, PolicyOutcome.FORBIDDEN}
)


class BusinessModel(BaseModel):
    """业务指标模型基类。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class TaskBusinessRecord(BusinessModel):
    """一个任务在业务口径上留下的全部可测量事实。

    每个可能测不出来的字段都是 `None` 而不是 0，配套的原因写进 `notes`。
    """

    task_id: str
    state: str
    #: 系统是否真的给出过方案。没给出过的任务不该进"交接完成率"的分母——
    #: 那是系统没干活，不是员工不用。
    produced_options: bool
    handed_off: bool
    started_at: datetime | None
    handed_off_at: datetime | None
    #: 墙上时间，秒。含人等审批、人在想的时间。
    seconds_to_handoff: float | None
    #: 员工是否回填了订单号和实付金额。**自述，不是回执**——比"点了一下"多的是
    #: 一个具体的订单号和一个具体的数，少的是核实。
    booking_confirmed: bool
    #: 员工说的下单时刻；没回填时为 `None`。
    booked_at: datetime | None
    #: 方案价（确认对应方案的 `total_cost`）与实付（回填的 `total_amount`）。没回填时为 `None`。
    planned_total: Decimal | None
    actual_total: Decimal | None
    #: 实付减方案价。币种对不上时为 `None`，配 `confirmation_currency_mismatch` 说明。
    cost_variance: Decimal | None
    cost_variance_currency: str | None
    #: 费控对过账没有。对过账的自述才算"核实过"。
    expense_reconciled: bool
    reconciliation_status: str | None
    #: 费控金额减自述金额。没对账或币种不同时为 `None`。
    reconciliation_variance: Decimal | None
    #: 下单时刻 → 首段实际出发时刻，天。负数表示出发早于下单（时间线对不上时才会出现）。
    advance_days: float | None
    #: 提前天数用了哪个时刻做起点：员工回填的下单时刻、审计里的交接事件，
    #: 还是调用方传进来的冻住时钟。没算出提前天数时是 `None`。
    advance_days_reference: (
        Literal["booking_confirmation", "handoff_event", "supplied_clock"] | None
    )
    #: 员工是否已经选定了一条方案。超标发生率的「选中口径」用它做分母——
    #: 没选过的任务既不算合规也不算超标，进分母会把这个数稀释掉。
    has_selected_option: bool
    #: 选中方案里判为"需审批/禁止"的规则 ID。
    selected_violation_rule_ids: tuple[str, ...]
    offered_option_count: int = Field(ge=0)
    offered_options_with_violation: int = Field(ge=0)
    clarification_rounds: int = Field(ge=0)
    tool_calls_used: int = Field(ge=0)
    #: 这一单是否走了例外审批。
    approval_requested: bool
    #: 是不是改期任务（由外部变更事件开出来的）。
    is_change_task: bool = False
    #: 改期任务：系统有没有不经人插手就直接摆出了方案。
    change_auto_planned: bool | None = None
    #: 改期任务：人有没有不得不插手（改需求、补表、没方案、供应商失败）。
    change_needed_intervention: bool | None = None
    #: 为什么某个字段没算出来。空元组表示这个任务上的每个字段都有确定答案。
    notes: tuple[str, ...] = ()


class BusinessMetricsReport(BusinessModel):
    """一批任务的业务指标汇总。"""

    schema_version: Literal[1] = 1
    protocol_id: Literal["business-outcome-v1"] = BUSINESS_PROTOCOL_ID
    generated_at: datetime
    task_count: int = Field(ge=0)
    records: tuple[TaskBusinessRecord, ...]
    metrics: dict[str, MetricResult]
    #: 导入过的费控记录数。渠道外预订率的分母；0 表示费控没接。
    expense_records_imported: int = Field(default=0, ge=0)


def summarize_task(
    task: TripTask,
    events: Sequence[AuditEvent],
    *,
    handoff_reference_time: datetime | None = None,
) -> TaskBusinessRecord:
    """把一个任务压成业务口径的一行事实。纯函数，不读外部状态、不读当前时间。

    `handoff_reference_time` 只影响提前预订天数的起点，见模块开头「时钟」一节。
    """
    notes: list[str] = []

    started_at = _task_started_at(events)
    if started_at is None:
        notes.append("no_start_event")

    handed_off_at = _first_event_time(events, {HANDOFF_COMPLETED_EVENT_TYPE})
    handed_off = handed_off_at is not None
    # 状态到了交接之后但没有那条事件，说明审计轨迹缺了一段——记下来，
    # 不要拿状态当时间戳猜一个出来。
    if task.state in HANDED_OFF_STATES and not handed_off:
        notes.append("handed_off_state_without_event")

    confirmation = task.booking_confirmation
    booking_confirmed = confirmation is not None
    if (
        task.state is TaskState.BOOKING_CONFIRMED
        and _first_event_time(events, {BOOKING_CONFIRMED_EVENT_TYPE}) is None
    ):
        notes.append("booking_confirmed_state_without_event")

    seconds_to_handoff: float | None = None
    if started_at is not None and handed_off_at is not None:
        seconds_to_handoff = (handed_off_at - started_at).total_seconds()
        if seconds_to_handoff < 0:
            # 时钟倒流。宁可测不出来，也不要报一个负的耗时。
            seconds_to_handoff = None
            notes.append("handoff_before_start")

    selected = task.selected_option()
    advance_days: float | None = None
    advance_days_reference: (
        Literal["booking_confirmation", "handoff_event", "supplied_clock"] | None
    ) = None
    # 起点按证据强弱选：员工回填的下单时刻 > 交接事件。回填的时刻是他说的"我什么时候
    # 订的"，和出发时刻在同一条时间线上，所以不受冻住时钟的影响——传了
    # `handoff_reference_time` 也不替换它。没回填的任务退回交接时刻，只有真的交接过
    # 才谈得上"提前多少天订"；传了冻住的时钟替换的是这一刻的读数，不是"有没有这一刻"。
    booked_at: datetime | None
    if confirmation is not None:
        booked_at = confirmation.booked_at
        advance_reference: Literal["booking_confirmation", "handoff_event", "supplied_clock"] = (
            "booking_confirmation"
        )
    elif handed_off:
        booked_at = handoff_reference_time or handed_off_at
        advance_reference = (
            "supplied_clock" if handoff_reference_time is not None else "handoff_event"
        )
    else:
        booked_at = None
        advance_reference = "handoff_event"
    if booked_at is not None:
        departure = _first_departure(task)
        if departure is None:
            notes.append("no_departure_time")
        else:
            advance_days = (departure - booked_at).total_seconds() / 86400.0
            advance_days_reference = advance_reference
            if advance_days < 0:
                notes.append(
                    "departure_before_booking"
                    if advance_reference == "booking_confirmation"
                    else "departure_before_handoff"
                )

    planned_total: Decimal | None = None
    actual_total: Decimal | None = None
    cost_variance: Decimal | None = None
    cost_variance_currency: str | None = None
    reconciliation = task.expense_reconciliation
    reconciliation_variance: Decimal | None = None
    if reconciliation is not None and confirmation is not None:
        reconciliation_variance = reconciliation.amount_variance(confirmation)
        if reconciliation_variance is None:
            notes.append("reconciliation_currency_mismatch")
    if confirmation is not None:
        actual_total = confirmation.total_amount
        confirmed_option = task.confirmed_option()
        if confirmed_option is None:
            notes.append("confirmed_option_missing_from_task")
        else:
            planned_total = confirmed_option.total_cost
            # 差额只有一个算法，在领域对象上；这里只是读出来，并把"为什么没算出来"说清楚。
            cost_variance = task.booking_cost_variance()
            if cost_variance is None:
                notes.append("confirmation_currency_mismatch")
            else:
                cost_variance_currency = confirmation.currency

    selected_violations: tuple[str, ...] = ()
    if selected is not None:
        selected_violations = _violation_rule_ids(selected)
    elif task.selected_option_id is not None:
        notes.append("selected_option_missing_from_task")

    offered_with_violation = sum(
        1 for option in task.options if _violation_rule_ids(option)
    )

    is_change = task.is_change_task
    change_auto_planned: bool | None = None
    change_needed_intervention: bool | None = None
    if is_change:
        event_types = {item.event_type for item in events}
        change_auto_planned = "OPTIONS_VERIFIED" in event_types and not (
            event_types & CHANGE_INTERVENTION_EVENT_TYPES
        )
        change_needed_intervention = bool(event_types & CHANGE_INTERVENTION_EVENT_TYPES)

    return TaskBusinessRecord(
        task_id=task.task_id,
        state=task.state.value,
        is_change_task=is_change,
        change_auto_planned=change_auto_planned,
        change_needed_intervention=change_needed_intervention,
        produced_options=bool(task.options),
        handed_off=handed_off,
        booking_confirmed=booking_confirmed,
        booked_at=confirmation.booked_at if confirmation is not None else None,
        planned_total=planned_total,
        actual_total=actual_total,
        cost_variance=cost_variance,
        cost_variance_currency=cost_variance_currency,
        expense_reconciled=reconciliation is not None,
        reconciliation_status=reconciliation.status.value if reconciliation is not None else None,
        reconciliation_variance=reconciliation_variance,
        has_selected_option=selected is not None,
        started_at=started_at,
        handed_off_at=handed_off_at,
        seconds_to_handoff=seconds_to_handoff,
        advance_days=advance_days,
        advance_days_reference=advance_days_reference,
        selected_violation_rule_ids=selected_violations,
        offered_option_count=len(task.options),
        offered_options_with_violation=offered_with_violation,
        clarification_rounds=task.clarification_rounds,
        tool_calls_used=task.tool_calls_used,
        approval_requested=task.approval is not None,
        notes=tuple(notes),
    )


def build_business_metrics_report(
    tasks: Sequence[TripTask],
    events_by_task: Mapping[str, Sequence[AuditEvent]],
    *,
    generated_at: datetime | None = None,
    handoff_reference_time: datetime | None = None,
    expense_records: Sequence[Any] = (),
) -> BusinessMetricsReport:
    """把一批任务汇总成业务指标报告。

    `expense_records` 是导入过的费控记录（`StoredExpenseRecord`）：有了它才算得出
    **渠道外预订率**——费控里有、本系统里没有的报销，就是员工绕开系统订的。

    `events_by_task` 少了某个任务的条目按"没有审计事件"处理：耗时和提前天数测不出来，
    但方案数、超标、澄清轮数这些从聚合上读的字段照常可用。

    `handoff_reference_time` 给冻了时钟的离线评测用，见模块开头「时钟」一节。
    """
    records = tuple(
        summarize_task(
            task,
            tuple(events_by_task.get(task.task_id, ())),
            handoff_reference_time=handoff_reference_time,
        )
        for task in tasks
    )
    return BusinessMetricsReport(
        generated_at=generated_at or datetime.now(UTC),
        task_count=len(records),
        records=records,
        metrics=aggregate_metrics(records, expense_records=expense_records),
        expense_records_imported=len(expense_records),
    )


def aggregate_metrics(
    records: Sequence[TaskBusinessRecord],
    *,
    expense_records: Sequence[Any] = (),
) -> dict[str, MetricResult]:
    """把每任务一行的事实汇总成指标。分母为零一律 `unavailable`，不写 0。"""
    with_options = [item for item in records if item.produced_options]
    handed_off = [item for item in records if item.handed_off]
    confirmed = [item for item in records if item.booking_confirmed]
    reconciled = [item for item in confirmed if item.expense_reconciled]
    reconciliation_ratios = [
        float(item.reconciliation_variance / item.actual_total)
        for item in reconciled
        if item.reconciliation_variance is not None
        and item.actual_total is not None
        and item.actual_total != 0
    ]
    change_tasks = [item for item in records if item.is_change_task]
    unmatched_expenses = sum(
        1 for item in expense_records if getattr(item, "status", None) is not None
        and getattr(item.status, "value", item.status) == "UNMATCHED"
    )
    variance_ratios = [
        float(item.cost_variance / item.planned_total)
        for item in confirmed
        if item.cost_variance is not None
        and item.planned_total is not None
        and item.planned_total != 0
    ]
    durations = [
        item.seconds_to_handoff
        for item in records
        if item.seconds_to_handoff is not None
    ]
    advances = [item.advance_days for item in records if item.advance_days is not None]
    with_selection = [item for item in records if item.has_selected_option]
    offered_total = sum(item.offered_option_count for item in records)
    offered_violating = sum(item.offered_options_with_violation for item in records)

    return {
        "handoff_completion_rate": _rate(
            len(handed_off),
            len(with_options),
            unit="rate",
            confidence_note=(
                "分子是员工在本系统里点的「已在官方平台订好」，不是供应商回执；"
                "这是渠道内预订率的上限估计，不是渠道内预订率本身。"
                "分母只算系统真的给出过方案的任务。"
                "要看更硬一点的数，读 booking_confirmation_rate。"
            ),
        ),
        "booking_confirmation_rate": _rate(
            len(confirmed),
            len(with_options),
            unit="rate",
            confidence_note=(
                "分子是员工回填了订单号和实付金额的任务。仍是自述不是回执——"
                "订单号系统核不了；比「点了一下」多的是一个具体的号和一个具体的数。"
                "分母只算系统真的给出过方案的任务。"
            ),
        ),
        "booking_confirmation_rate.of_handed_off": _rate(
            len(confirmed),
            len(handed_off),
            unit="rate",
            confidence_note=(
                "说了「我去订了」的人里，回来填了订单号的比例。"
                "1 减它就是交接完成率高估了多少。"
            ),
        ),
        "booked_cost_variance_ratio_mean": _stat(
            variance_ratios,
            unit="ratio",
            statistic="mean",
            confidence_note=(
                "（实付 − 方案价）÷ 方案价，正数是多付。只算币种一致的确认记录；"
                "币种不一致的任务带 confirmation_currency_mismatch 说明，不换算。"
            ),
        ),
        "expense_reconciled_rate": _rate(
            len(reconciled),
            len(confirmed),
            unit="rate",
            confidence_note=(
                "回填了订单号的任务里，费控记录对上了的比例。对上了的自述才算「核实过」；"
                "费控没接时分母有、分子为 0，这个数就是 0——那是事实，不是测不出来。"
            ),
        ),
        "reconciliation_variance_ratio_mean": _stat(
            reconciliation_ratios,
            unit="ratio",
            statistic="mean",
            confidence_note=(
                "（费控金额 − 自述金额）÷ 自述金额，只算币种一致的对账记录。"
                "正数是员工少报、负数是多报。"
            ),
        ),
        "change_auto_planned_rate": _rate(
            sum(1 for item in change_tasks if item.change_auto_planned),
            len(change_tasks),
            unit="rate",
            confidence_note=(
                "航变/会议改期开出来的改期任务里，系统不经人插手就摆出了方案的比例。"
                "没有改期任务时测不出来。"
            ),
        ),
        "change_intervention_rate": _rate(
            sum(1 for item in change_tasks if item.change_needed_intervention),
            len(change_tasks),
            unit="rate",
            confidence_note=(
                "改期任务里人不得不插手（改需求、补表、没方案、供应商失败）的比例——"
                "设计文档里的「变更场景人工介入率」。"
            ),
        ),
        "off_channel_expense_rate": _rate(
            unmatched_expenses,
            len(expense_records),
            unit="rate",
            confidence_note=(
                "导入的费控记录里，本系统找不到对应下单确认的比例——员工绕开系统订的那部分。"
                "这是设计文档里「渠道内预订率」的补集，第一次有了真值；费控没接时测不出来。"
            ),
        ),
        "handoff_completion_rate.all_tasks": _rate(
            len(handed_off),
            len(records),
            unit="rate",
            confidence_note=(
                "分母是全部任务，含系统没能给出方案的那些。"
                "和上一条的差值是「系统没干成活」而不是「员工不愿用」。"
            ),
        ),
        "seconds_to_handoff_mean": _stat(
            durations,
            unit="seconds",
            statistic="mean",
            confidence_note=(
                "墙上时间，含员工离开去开会、经理隔天才批的等待。"
                "系统自身耗时见 evaluation_performance。"
            ),
        ),
        "seconds_to_handoff_p50": _stat(
            durations, unit="seconds", statistic="p50",
            confidence_note="线性 type-7 分位数，口径同 mean。",
        ),
        "seconds_to_handoff_p95": _stat(
            durations, unit="seconds", statistic="p95",
            confidence_note="线性 type-7 分位数，口径同 mean。",
        ),
        "advance_booking_days_mean": _stat(
            advances,
            unit="days",
            statistic="mean",
            confidence_note=(
                "起点优先用员工回填的下单时刻（booking_confirmation）；没回填的任务"
                "用交接完成时刻。终点是首段实际出发时刻。"
                "起点不用任务创建时刻，因为下单不发生在那一刻。"
            ),
        ),
        "advance_booking_days_p50": _stat(
            advances, unit="days", statistic="p50",
            confidence_note="线性 type-7 分位数，口径同 mean。",
        ),
        "policy_violation_rate.selected": _rate(
            sum(1 for item in with_selection if item.selected_violation_rule_ids),
            len(with_selection),
            unit="rate",
            confidence_note=(
                "员工最后选中的方案里带「需审批/禁止」证据的比例。"
                "「判不了」（证据不足）不计入——那是缺数据，不是超标。"
            ),
        ),
        "policy_violation_rate.offered": _rate(
            offered_violating,
            offered_total,
            unit="rate",
            confidence_note=(
                "展示给员工的方案里带「需审批/禁止」证据的比例。"
                "按已编码不变量，FORBIDDEN 方案在规划阶段已被硬过滤，"
                "所以这个数实际上量的是「需要审批」的比例。"
            ),
        ),
        "approval_request_rate": _rate(
            sum(1 for item in records if item.approval_requested),
            len(records),
            unit="rate",
            confidence_note="走了例外审批的任务比例。",
        ),
        "clarification_rounds_mean": _stat(
            [float(item.clarification_rounds) for item in records],
            unit="rounds",
            statistic="mean",
            confidence_note="每个任务追问了几轮；口径为全部任务。",
        ),
        "tool_calls_per_task_mean": _stat(
            [float(item.tool_calls_used) for item in records],
            unit="calls",
            statistic="mean",
            confidence_note="计入预算的工具调用次数；口径为全部任务。",
        ),
    }


def _task_started_at(events: Sequence[AuditEvent]) -> datetime | None:
    """任务开始时刻：优先用四个入口的创建事件，没有就退到最早的一条事件。"""
    explicit = _first_event_time(events, TASK_START_EVENT_TYPES)
    if explicit is not None:
        return explicit
    times = [item.created_at for item in events]
    return min(times) if times else None


def _first_event_time(
    events: Sequence[AuditEvent], event_types: frozenset[str] | set[str]
) -> datetime | None:
    times = [item.created_at for item in events if item.event_type in event_types]
    return min(times) if times else None


def _first_departure(task: TripTask) -> datetime | None:
    """首段实际出发时刻。

    优先读选中方案的第一段——那是员工真要坐的那班车。没有选中方案时退到请求里
    第一段的最早允许出发时间，并且**只在没有更好来源时才用**：请求里的是时间窗
    下沿，不是航班时刻。
    """
    selected = task.selected_option()
    if selected is not None and selected.legs:
        return selected.legs[0].depart_at
    if task.request is not None and task.request.journey:
        return task.request.journey[0].depart_after
    return None


def _violation_rule_ids(option: TravelOptionVersion) -> tuple[str, ...]:
    """一条方案里判为「需审批/禁止」的规则 ID。

    逐条读证据，不读聚合结论——聚合把「证据不足」排在「禁止」之上，
    一条既违规又缺数据的方案聚合出来是 `INSUFFICIENT_EVIDENCE`，
    只看聚合会把它的违规漏掉。
    """
    return tuple(
        item.rule_id
        for item in option.policy_decision.evidence
        if item.outcome in VIOLATION_OUTCOMES
    )


def _rate(
    numerator: int,
    denominator: int,
    *,
    unit: str,
    confidence_note: str,
) -> MetricResult:
    if denominator == 0:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=float(numerator),
            denominator=0.0,
            unit=unit,
            confidence_note=confidence_note,
            exposure_note="分母为零：这个指标测不出来，不是 0。",
        )
    return MetricResult(
        status="measured",
        value=numerator / denominator,
        numerator=float(numerator),
        denominator=float(denominator),
        unit=unit,
        confidence_note=confidence_note,
    )


def _stat(
    values: Sequence[float],
    *,
    unit: str,
    statistic: Literal["mean", "p50", "p95"],
    confidence_note: str,
) -> MetricResult:
    if not values:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=None,
            denominator=0.0,
            unit=unit,
            confidence_note=confidence_note,
            exposure_note="没有任何任务能提供这个统计量的输入。",
        )
    if statistic == "mean":
        value = sum(values) / len(values)
        numerator: float | None = float(sum(values))
    else:
        quantile = 0.50 if statistic == "p50" else 0.95
        value = _percentile(values, quantile)
        numerator = None
    return MetricResult(
        status="measured",
        value=value,
        numerator=numerator,
        denominator=float(len(values)),
        unit=unit,
        confidence_note=confidence_note,
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


#: 指标名 → 用大白话说这个数是什么。给 REPORT.md 用。
#: 没解释的指标不该出现在给人看的报告里——数字本身不会告诉人它的口径。
METRIC_LABELS: dict[str, str] = {
    "handoff_completion_rate": "系统给出了方案的任务里，员工说「已去官方平台订好」的比例",
    "handoff_completion_rate.all_tasks": "全部任务里说「已订好」的比例（含系统没能给出方案的）",
    "booking_confirmation_rate": "系统给出了方案的任务里，员工回填了订单号和实付金额的比例（自述）",
    "booking_confirmation_rate.of_handed_off": "说了「已订好」的任务里，回来填了订单号的比例",
    "booked_cost_variance_ratio_mean": "实付比方案价多付或少付了几成，平均（只算币种一致的）",
    "expense_reconciled_rate": "回填了订单号的任务里，费控对上账的比例（对上了才算核实过）",
    "reconciliation_variance_ratio_mean": "费控金额比员工自述多或少了几成，平均",
    "off_channel_expense_rate": "费控记录里在本系统找不到对应确认的比例（绕开系统订的）",
    "change_auto_planned_rate": "改期任务里系统直接摆出方案、没让人插手的比例",
    "change_intervention_rate": "改期任务里人不得不插手的比例（变更场景人工介入率）",
    "seconds_to_handoff_mean": "从任务开始到说「已订好」的平均墙上时间（秒）",
    "seconds_to_handoff_p50": "同上，中位数（秒）",
    "seconds_to_handoff_p95": "同上，95 分位（秒）",
    "advance_booking_days_mean": "订好那一刻距离出发还有几天，平均",
    "advance_booking_days_p50": "订好那一刻距离出发还有几天，中位数",
    "policy_violation_rate.selected": "员工选中的方案里，需要审批或被禁的比例",
    "policy_violation_rate.offered": "展示出去的方案里，需要审批或被禁的比例",
    "approval_request_rate": "走了例外审批的任务比例",
    "clarification_rounds_mean": "平均追问了几轮",
    "tool_calls_per_task_mean": "平均用掉几次工具调用（含模型和供应商）",
}


def write_business_metrics_report(
    report: BusinessMetricsReport,
    output_directory: str | Path,
) -> Path:
    """把报告写进一个**事先不存在**的目录，返回该目录。

    沿用仓库既有约定：输出目录必须是新的。这不是洁癖——评测目录是证据，
    覆盖一次就没有第二份可比。

    产出两个文件：机器读的 `report.json` 和人读的 `REPORT.md`。
    """
    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise BusinessMetricsError(f"Output directory already exists: {output_root}")
    output_root.mkdir(parents=True)
    (output_root / "report.json").write_bytes(
        report.model_dump_json(indent=2).encode("utf-8")
    )
    (output_root / "REPORT.md").write_bytes(render_business_report_markdown(report).encode("utf-8"))
    return output_root


def render_business_report_markdown(report: BusinessMetricsReport) -> str:
    """把报告渲染成给人读的 Markdown：每个数字都带着它的口径一起出现。"""
    lines = [
        "# 业务结果指标",
        "",
        f"协议：`{report.protocol_id}`　生成时间：{report.generated_at.isoformat()}　"
        f"任务数：{report.task_count}",
        "",
        "**这一层量的不是模型答得好不好，是这东西对企业有没有用。**",
        "分母为零的指标写「测不出来」，不写 0——一条平的曲线和一条没有数据的曲线",
        "是两回事。",
        "",
        "| 指标 | 这个数是什么 | 值 | 分子/分母 |",
        "|---|---|---|---|",
    ]
    for name, metric in report.metrics.items():
        label = METRIC_LABELS.get(name, "（这个指标还没写口径说明）")
        if metric.status != "measured" or metric.value is None:
            value = "测不出来"
        elif metric.unit == "rate":
            value = f"{metric.value:.1%}"
        elif metric.unit == "ratio":
            value = f"{metric.value:+.1%}"
        else:
            # 用有效数字而不是固定两位小数：秒级统计在演示里可能是 0.001，
            # 写成 "0.00 秒" 会让人以为没测到。
            value = f"{metric.value:.4g} {metric.unit or ''}".strip()
        numerator = "—" if metric.numerator is None else f"{metric.numerator:g}"
        denominator = "—" if metric.denominator is None else f"{metric.denominator:g}"
        lines.append(f"| `{name}` | {label} | {value} | {numerator} / {denominator} |")

    notes = sorted({note for item in report.records for note in item.notes})
    lines.extend(["", "## 口径说明", ""])
    for name, metric in report.metrics.items():
        if metric.confidence_note:
            lines.append(f"- **`{name}`**：{metric.confidence_note}")

    if notes:
        lines.extend(["", "## 这批任务上出现过的测不准原因", ""])
        for note in notes:
            count = sum(1 for item in report.records if note in item.notes)
            lines.append(f"- `{note}`：{count} 个任务")
    return "\n".join(lines) + "\n"

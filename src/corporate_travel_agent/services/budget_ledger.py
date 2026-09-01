"""预算账本：这个成本中心在这个预算期里已经花了多少——从员工回填的下单确认里算。

## 这是数据闭环反过来喂管控

政策快照里写的是**上限**（`CostCenterBudget`），那是公司的规则，钉在快照里不动。
**用掉多少**不在快照里：它来自 `BookingConfirmation`——员工回填的订单号和实付金额。
没有下单确认回流这一步（`docs/product-gap-review.md` 6.5），预算规则就只能判"上限"，
判不了"余额"；有了它，规划那一刻就能说"这趟 1,700，成本中心还剩 3,200"。

## 三条口径

1. **只算回填过的。** 说了"我去订了"但没回来填单号的（`HANDED_OFF`）不算——
   那没有金额。自述的可信度和确认记录一样：费控对账接上之前，它不是回执。
2. **只算同币种。** 预算是美元的，人民币的确认不换算，也不忽略——它进不了这个数，
   但会作为 `excluded_confirmations` 记在快照旁边，读的人知道有几笔没算进来。
3. **按下单时刻归期。** 落在 `[period_from, period_to]` 的算这一期；下单时刻是员工说的
   （`booked_at`），没说就是回填时刻。
4. **费控对过账的用费控的数。** 对账记录（`ExpenseReconciliation`）是外部佐证，比自述硬；
   有它就用它的金额，没有才用自述。

## 默认接上，但只有配了预算才会有规则

`build_demo_system` 和 API 默认用 `RepositoryBudgetLedger`（读任务仓储）。政策快照
没给员工的成本中心配预算，引擎一条证据都不产；配了才判。冻结评测集的政策没有预算，
所以它们的结果一个字不变。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.domain.models import (
    BudgetSnapshot,
    EmployeeProfileSnapshot,
    PolicySnapshot,
    TripTask,
)


class BudgetLedgerPort(Protocol):
    """预算账本端口：某成本中心在某预算期、某币种下已经确认了多少支出。"""

    def spent(
        self,
        cost_center: str,
        *,
        currency: str,
        period_from: date,
        period_to: date,
    ) -> Decimal: ...


class RepositoryTripBudgetLedger:
    """从任务仓储里回填过订单号的任务算支出。

    读的是 `BOOKING_CONFIRMED` 状态的任务：确认记录上的实付金额、币种和下单时刻，
    员工快照上的成本中心。
    """

    def __init__(self, repository, *, limit: int = 10_000) -> None:
        self._repository = repository
        self._limit = limit

    def spent(
        self,
        cost_center: str,
        *,
        currency: str,
        period_from: date,
        period_to: date,
    ) -> Decimal:
        tasks: Sequence[TripTask] = self._repository.list_by_state(
            TaskState.BOOKING_CONFIRMED.value, limit=self._limit
        )
        total = Decimal("0")
        for task in tasks:
            confirmation = task.booking_confirmation
            if confirmation is None or task.employee.cost_center != cost_center:
                continue
            booked_day = confirmation.booked_at.date()
            if not period_from <= booked_day <= period_to:
                continue
            # 费控对过账、而且币种对得上：用费控的数——那是外部记录，比自述硬。
            # 否则用自述；币种对不上的一律不进这个数。
            reconciliation = task.expense_reconciliation
            if reconciliation is not None and reconciliation.currency == currency:
                total += reconciliation.expense_amount
            elif confirmation.currency == currency:
                total += confirmation.total_amount
        return total


def derive_budget_snapshot(
    employee: EmployeeProfileSnapshot,
    policy: PolicySnapshot,
    ledger: BudgetLedgerPort,
    *,
    now: datetime,
) -> BudgetSnapshot | None:
    """这位员工此刻的预算余额快照；没有成本中心或政策没配预算就是 None。

    None 和"余额为零"是两回事：前者是没有这条规则，后者是花光了。
    """
    cost_center = employee.cost_center
    if cost_center is None:
        return None
    budget = policy.cost_center_budgets.get(cost_center)
    if budget is None:
        return None
    spent = ledger.spent(
        cost_center,
        currency=budget.currency,
        period_from=budget.period_from,
        period_to=budget.period_to,
    )
    content = {
        "cost_center": cost_center,
        "currency": budget.currency,
        "limit": str(budget.amount),
        "spent": str(spent),
        "period_from": budget.period_from.isoformat(),
        "period_to": budget.period_to.isoformat(),
        "policy_snapshot_id": policy.snapshot_id,
        "computed_at": now.isoformat(),
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return BudgetSnapshot(
        snapshot_id=f"budget-{cost_center}-{digest}",
        cost_center=cost_center,
        currency=budget.currency,
        limit=budget.amount,
        spent=spent,
        period_from=budget.period_from,
        period_to=budget.period_to,
        computed_at=now,
    )

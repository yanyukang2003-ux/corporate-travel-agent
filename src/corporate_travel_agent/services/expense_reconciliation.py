"""费控对账：把费控系统的报销记录和本系统里员工自述的下单确认对上。

## 这是数据闭环的最后一环

下单确认（`BookingConfirmation`）是员工说的：订单号、实付。**系统核不了。** 费控记录
是财务系统说的：报销了什么、多少钱。两边对上了，自述才算"核实过"；对不上、或者费控
里有本系统从没见过的订单，就是**渠道外预订**——设计文档里"员工绕开去用携程"的可观测形态。
在这之前，那个数只能靠"交接完成率"往上估。

## 匹配规则（故意简单）

按**员工 + 订单号**匹配：费控记录里的任一订单号出现在某个已确认任务的订单号里，且是同一位
旅行者，就算找到了。找到之后看金额：

| 情况 | 状态 |
|---|---|
| 同币种、金额相等 | `MATCHED` |
| 同币种、金额不等 | `AMOUNT_MISMATCH`——两边都留着，差额摆出来 |
| 币种不同 | `CURRENCY_MISMATCH`——不换算、不判对错 |
| 没找到确认 | `UNMATCHED`——渠道外预订 |
| 找到的任务已经对过账 | `DUPLICATE` |

不做模糊匹配（按金额、按日期猜）：猜错一次，"核实过"三个字就不值钱了。

## 导入是幂等的

同一个 `expense_id` 导第二次直接跳过，结果照旧。费控系统按批推送、失败重推，这里不能
因此多出一条。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from corporate_travel_agent.domain.enums import ReconciliationStatus, TaskState
from corporate_travel_agent.domain.models import TripTask


@dataclass(frozen=True, slots=True)
class ExpenseRecord:
    """费控系统推过来的一条报销记录。"""

    expense_id: str
    employee_id: str
    amount: Decimal
    currency: str
    expensed_at: datetime
    order_references: tuple[str, ...]
    source_system: str = "expense-system"
    cost_center: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class StoredExpenseRecord:
    """导入并对过账的记录：原记录 + 对账结果。"""

    record: ExpenseRecord
    status: ReconciliationStatus
    imported_at: datetime
    matched_task_id: str | None = None
    note: str | None = None


class ExpenseRecordStore(Protocol):
    """导入过的费控记录；渠道外预订率从这里算。"""

    backend_name: str

    def get(self, expense_id: str) -> StoredExpenseRecord | None: ...

    def add(self, stored: StoredExpenseRecord) -> None: ...

    def list_records(self, *, limit: int = 500) -> tuple[StoredExpenseRecord, ...]: ...


class InMemoryExpenseRecordStore:
    backend_name = "memory"

    def __init__(self) -> None:
        self._records: dict[str, StoredExpenseRecord] = {}

    def get(self, expense_id: str) -> StoredExpenseRecord | None:
        return self._records.get(expense_id)

    def add(self, stored: StoredExpenseRecord) -> None:
        if stored.record.expense_id in self._records:
            raise ValueError(f"expense record {stored.record.expense_id} already imported")
        self._records[stored.record.expense_id] = stored

    def list_records(self, *, limit: int = 500) -> tuple[StoredExpenseRecord, ...]:
        items = sorted(
            self._records.values(), key=lambda item: (item.imported_at, item.record.expense_id)
        )
        return tuple(items[: max(limit, 0)])


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    expense_id: str
    status: ReconciliationStatus
    matched_task_id: str | None = None
    variance: Decimal | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """一次导入的结果。"""

    imported: int
    skipped_duplicates: int
    outcomes: tuple[ReconciliationOutcome, ...]
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def count(self, status: ReconciliationStatus) -> int:
        return sum(1 for item in self.outcomes if item.status is status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "imported": self.imported,
            "skipped_duplicates": self.skipped_duplicates,
            "by_status": {status.value: self.count(status) for status in ReconciliationStatus},
            "outcomes": [
                {
                    "expense_id": item.expense_id,
                    "status": item.status.value,
                    "matched_task_id": item.matched_task_id,
                    "variance": str(item.variance) if item.variance is not None else None,
                    "note": item.note,
                }
                for item in self.outcomes
            ],
        }


def reconcile_expenses(
    records: Iterable[ExpenseRecord],
    *,
    repository,
    store: ExpenseRecordStore,
    reconcile: Callable[..., TripTask],
    now: Callable[[], datetime],
    limit: int = 10_000,
) -> ReconciliationReport:
    """导入一批费控记录并逐条对账。

    `reconcile` 是编排器的 `reconcile_expense`：找到并且不是重复时，由它把结果钉到任务上。
    """
    confirmed: Sequence[TripTask] = repository.list_by_state(
        TaskState.BOOKING_CONFIRMED.value, limit=limit
    )
    imported = 0
    skipped = 0
    outcomes: list[ReconciliationOutcome] = []
    started = now()
    for record in records:
        if store.get(record.expense_id) is not None:
            skipped += 1
            continue
        task = _find_confirmed_task(record, confirmed)
        if task is None:
            outcome = ReconciliationOutcome(
                expense_id=record.expense_id,
                status=ReconciliationStatus.UNMATCHED,
                note="no booking confirmation in this system shares an order reference",
            )
        elif task.expense_reconciliation is not None:
            outcome = ReconciliationOutcome(
                expense_id=record.expense_id,
                status=ReconciliationStatus.DUPLICATE,
                matched_task_id=task.task_id,
                note=f"task already reconciled against {task.expense_reconciliation.expense_id}",
            )
        else:
            confirmation = task.booking_confirmation
            assert confirmation is not None
            matched_refs = tuple(
                ref for ref in record.order_references if ref in confirmation.order_references
            )
            if record.currency != confirmation.currency:
                status = ReconciliationStatus.CURRENCY_MISMATCH
                variance = None
            elif record.amount == confirmation.total_amount:
                status = ReconciliationStatus.MATCHED
                variance = Decimal("0")
            else:
                status = ReconciliationStatus.AMOUNT_MISMATCH
                variance = record.amount - confirmation.total_amount
            updated = reconcile(
                task.task_id,
                expense_id=record.expense_id,
                source_system=record.source_system,
                expense_amount=record.amount,
                currency=record.currency,
                expensed_at=record.expensed_at,
                matched_order_references=matched_refs,
                status=status,
                note=record.description,
            )
            # 后面的记录看到的是对过账之后的任务，才判得出 DUPLICATE。
            confirmed = tuple(
                updated if item.task_id == updated.task_id else item for item in confirmed
            )
            outcome = ReconciliationOutcome(
                expense_id=record.expense_id,
                status=status,
                matched_task_id=task.task_id,
                variance=variance,
            )
        store.add(
            StoredExpenseRecord(
                record=record,
                status=outcome.status,
                imported_at=now(),
                matched_task_id=outcome.matched_task_id,
                note=outcome.note,
            )
        )
        imported += 1
        outcomes.append(outcome)
    return ReconciliationReport(
        imported=imported,
        skipped_duplicates=skipped,
        outcomes=tuple(outcomes),
        started_at=started,
    )


def _find_confirmed_task(record: ExpenseRecord, confirmed: Sequence[TripTask]) -> TripTask | None:
    wanted = set(record.order_references)
    for task in confirmed:
        confirmation = task.booking_confirmation
        if confirmation is None or task.employee.employee_id != record.employee_id:
            continue
        if wanted.intersection(confirmation.order_references):
            return task
    return None


def record_from_payload(payload: dict[str, Any]) -> ExpenseRecord:
    """把 API / 文件里的一条 JSON 记录读成值对象；形状不对就抛 ValueError。"""
    try:
        references = tuple(
            str(item).strip() for item in payload["order_references"] if str(item).strip()
        )
        if not references:
            raise ValueError("order_references must not be empty")
        expensed_at = payload["expensed_at"]
        if isinstance(expensed_at, str):
            expensed_at = datetime.fromisoformat(expensed_at.replace("Z", "+00:00"))
        if expensed_at.tzinfo is None:
            raise ValueError("expensed_at must be timezone-aware")
        return ExpenseRecord(
            expense_id=str(payload["expense_id"]).strip(),
            employee_id=str(payload["employee_id"]).strip(),
            amount=Decimal(str(payload["amount"])),
            currency=str(payload["currency"]).strip().upper(),
            expensed_at=expensed_at,
            order_references=references,
            source_system=str(payload.get("source_system") or "expense-system"),
            cost_center=payload.get("cost_center"),
            description=payload.get("description"),
        )
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise ValueError(f"invalid expense record: {exc}") from exc


def stored_record_from_row(row: Any) -> StoredExpenseRecord:
    """SQL 行 → 值对象；给 SQL 存储用。"""
    return StoredExpenseRecord(
        record=ExpenseRecord(
            expense_id=row.expense_id,
            employee_id=row.employee_id,
            amount=Decimal(str(row.amount)),
            currency=row.currency,
            expensed_at=row.expensed_at,
            order_references=tuple(row.order_references),
            source_system=row.source_system,
            cost_center=row.cost_center,
            description=row.description,
        ),
        status=ReconciliationStatus(row.status),
        imported_at=row.imported_at,
        matched_task_id=row.matched_task_id,
        note=row.note,
    )


__all__ = [
    "ExpenseRecord",
    "ExpenseRecordStore",
    "InMemoryExpenseRecordStore",
    "ReconciliationOutcome",
    "ReconciliationReport",
    "StoredExpenseRecord",
    "reconcile_expenses",
    "record_from_payload",
    "replace",
    "stored_record_from_row",
]

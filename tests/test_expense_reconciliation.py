"""费控对账：自述的下单确认有了外部佐证，找不到确认的报销就是渠道外预订。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, ReconciliationStatus, TaskState
from corporate_travel_agent.services.budget_ledger import RepositoryTripBudgetLedger
from corporate_travel_agent.services.business_metrics import build_business_metrics_report
from corporate_travel_agent.services.expense_reconciliation import (
    ExpenseRecord,
    InMemoryExpenseRecordStore,
    reconcile_expenses,
    record_from_payload,
)
from corporate_travel_agent.services.sqlalchemy_repository import (
    SQLAlchemyExpenseRecordStore,
    SQLAlchemyTaskRepository,
)

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def _record(expense_id: str, refs: tuple[str, ...], amount: str, **overrides) -> ExpenseRecord:
    values = dict(
        expense_id=expense_id,
        employee_id="E1001",
        amount=Decimal(amount),
        currency="USD",
        expensed_at=NOW,
        order_references=refs,
    )
    values.update(overrides)
    return ExpenseRecord(**values)


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        self.store = InMemoryExpenseRecordStore()

    def _confirmed(self, task_id: str, refs: tuple[str, ...], amount: str | None = None):
        task = self.workflow.create_task(make_demo_request(task_id=task_id))
        option = next(
            item for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(task.task_id, option.option_id)
        return self.workflow.confirm_booking(
            task.task_id,
            order_references=list(refs),
            total_amount=Decimal(amount) if amount else option.total_cost,
            currency=option.currency,
            reported_by="E1001",
        )

    def _reconcile(self, *records: ExpenseRecord):
        return reconcile_expenses(
            records,
            repository=self.workflow.tasks,
            store=self.store,
            reconcile=self.workflow.reconcile_expense,
            now=lambda: NOW,
        )

    def test_a_matching_record_pins_a_reconciliation_on_the_task(self) -> None:
        confirmed = self._confirmed("match", ("PNR-1", "HTL-1"), "2350")

        report = self._reconcile(_record("EXP-1", ("PNR-1",), "2350"))

        self.assertEqual(report.count(ReconciliationStatus.MATCHED), 1)
        stored = self.workflow.tasks.get(confirmed.task_id)
        assert stored.expense_reconciliation is not None
        self.assertIs(stored.expense_reconciliation.status, ReconciliationStatus.MATCHED)
        self.assertEqual(stored.expense_reconciliation.matched_order_references, ("PNR-1",))
        self.assertEqual(
            stored.expense_reconciliation.amount_variance(stored.booking_confirmation), Decimal("0")
        )
        # 确认记录本身一个字不改；对账是另一条记录。
        self.assertEqual(stored.booking_confirmation.order_references, ("PNR-1", "HTL-1"))
        self.assertEqual(stored.state, TaskState.BOOKING_CONFIRMED)
        events = [item.event_type for item in self.workflow.tasks.events(confirmed.task_id)]
        self.assertIn("EXPENSE_RECONCILED", events)

    def test_amount_and_currency_mismatches_are_kept_not_hidden(self) -> None:
        self._confirmed("amount", ("PNR-2",), "2350")
        self._confirmed("currency", ("PNR-3",), "2350")

        report = self._reconcile(
            _record("EXP-2", ("PNR-2",), "2385"),
            _record("EXP-3", ("PNR-3",), "16000", currency="CNY"),
        )

        by_id = {item.expense_id: item for item in report.outcomes}
        self.assertIs(by_id["EXP-2"].status, ReconciliationStatus.AMOUNT_MISMATCH)
        self.assertEqual(by_id["EXP-2"].variance, Decimal("35"))
        self.assertIs(by_id["EXP-3"].status, ReconciliationStatus.CURRENCY_MISMATCH)
        self.assertIsNone(by_id["EXP-3"].variance)
        amount_task = self.workflow.tasks.get("amount")
        self.assertEqual(
            amount_task.expense_reconciliation.amount_variance(amount_task.booking_confirmation),
            Decimal("35"),
        )

    def test_an_expense_nobody_confirmed_is_an_off_channel_booking(self) -> None:
        self._confirmed("confirmed", ("PNR-4",))

        report = self._reconcile(_record("EXP-4", ("CTRIP-777",), "900"))

        self.assertEqual(report.count(ReconciliationStatus.UNMATCHED), 1)
        stored = self.store.get("EXP-4")
        assert stored is not None
        self.assertIs(stored.status, ReconciliationStatus.UNMATCHED)
        self.assertIsNone(stored.matched_task_id)
        self.assertIsNone(self.workflow.tasks.get("confirmed").expense_reconciliation)

    def test_matching_requires_the_same_traveler(self) -> None:
        self._confirmed("someone-else", ("PNR-5",))
        report = self._reconcile(_record("EXP-5", ("PNR-5",), "2350", employee_id="A1002"))
        self.assertEqual(report.count(ReconciliationStatus.UNMATCHED), 1)

    def test_a_second_record_for_the_same_task_is_a_duplicate(self) -> None:
        self._confirmed("dup", ("PNR-6",), "2350")
        self._reconcile(_record("EXP-6", ("PNR-6",), "2350"))

        report = self._reconcile(_record("EXP-7", ("PNR-6",), "2350"))

        self.assertEqual(report.count(ReconciliationStatus.DUPLICATE), 1)
        self.assertEqual(report.outcomes[0].matched_task_id, "dup")
        self.assertEqual(self.workflow.tasks.get("dup").expense_reconciliation.expense_id, "EXP-6")

    def test_reimporting_the_same_expense_id_is_skipped(self) -> None:
        self._confirmed("idem", ("PNR-8",), "2350")
        first = self._reconcile(_record("EXP-8", ("PNR-8",), "2350"))
        again = self._reconcile(_record("EXP-8", ("PNR-8",), "2350"))
        self.assertEqual(first.imported, 1)
        self.assertEqual(again.imported, 0)
        self.assertEqual(again.skipped_duplicates, 1)

    def test_only_confirmed_tasks_can_be_reconciled(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="not-confirmed"))
        with self.assertRaises(WorkflowError):
            self.workflow.reconcile_expense(
                task.task_id,
                expense_id="X",
                source_system="s",
                expense_amount=Decimal("1"),
                currency="USD",
                expensed_at=NOW,
                matched_order_references=(),
                status=ReconciliationStatus.MATCHED,
            )

    def test_the_budget_ledger_prefers_the_reconciled_amount(self) -> None:
        self._confirmed("ledger", ("PNR-9",), "2350")
        ledger = RepositoryTripBudgetLedger(self.workflow.tasks)
        before = ledger.spent(
            "CC-SALES-CN", currency="USD", period_from=DEMO_CLOCK.date() - timedelta(days=30),
            period_to=DEMO_CLOCK.date() + timedelta(days=30),
        )
        self._reconcile(_record("EXP-9", ("PNR-9",), "2410"))
        after = ledger.spent(
            "CC-SALES-CN", currency="USD", period_from=DEMO_CLOCK.date() - timedelta(days=30),
            period_to=DEMO_CLOCK.date() + timedelta(days=30),
        )
        self.assertEqual(before, Decimal("2350"))
        self.assertEqual(after, Decimal("2410"))

    def test_metrics_split_verified_from_self_reported_and_count_off_channel(self) -> None:
        self._confirmed("m-verified", ("PNR-10",), "2350")
        self._confirmed("m-self", ("PNR-11",), "2350")
        self._reconcile(
            _record("EXP-10", ("PNR-10",), "2385"),
            _record("EXP-11", ("CTRIP-1",), "800"),
            _record("EXP-12", ("CTRIP-2",), "650"),
        )
        tasks = [self.workflow.tasks.get("m-verified"), self.workflow.tasks.get("m-self")]
        events = {t.task_id: self.workflow.tasks.events(t.task_id) for t in tasks}

        report = build_business_metrics_report(
            tasks, events, expense_records=self.store.list_records()
        )

        metrics = report.metrics
        self.assertEqual(metrics["expense_reconciled_rate"].value, 0.5)
        self.assertEqual(metrics["off_channel_expense_rate"].value, 2 / 3)
        self.assertEqual(report.expense_records_imported, 3)
        self.assertAlmostEqual(
            metrics["reconciliation_variance_ratio_mean"].value,
            float(Decimal("35") / Decimal("2350")),
        )
        verified = next(item for item in report.records if item.task_id == "m-verified")
        self.assertTrue(verified.expense_reconciled)
        self.assertEqual(verified.reconciliation_status, "AMOUNT_MISMATCH")
        self.assertEqual(verified.reconciliation_variance, Decimal("35"))
        # 费控没接：分母 0，测不出来，不是 0。
        empty = build_business_metrics_report(tasks, events)
        self.assertEqual(empty.metrics["off_channel_expense_rate"].status, "unavailable")


class PayloadTests(unittest.TestCase):
    def test_payload_parsing_normalises_and_rejects_bad_shapes(self) -> None:
        record = record_from_payload(
            {
                "expense_id": " EXP-1 ",
                "employee_id": "E1001",
                "amount": "12.50",
                "currency": "usd",
                "expensed_at": "2026-08-10T09:00:00Z",
                "order_references": [" PNR-1 ", ""],
            }
        )
        self.assertEqual(record.expense_id, "EXP-1")
        self.assertEqual(record.currency, "USD")
        self.assertEqual(record.order_references, ("PNR-1",))
        self.assertEqual(record.amount, Decimal("12.50"))
        for bad in (
            {"expense_id": "x"},
            {"expense_id": "x", "employee_id": "E", "amount": "1", "currency": "USD",
             "expensed_at": "2026-08-10T09:00:00Z", "order_references": []},
            {"expense_id": "x", "employee_id": "E", "amount": "1", "currency": "USD",
             "expensed_at": "2026-08-10T09:00:00", "order_references": ["P"]},
        ):
            with self.assertRaises(ValueError):
                record_from_payload(bad)


class SqlStoreTests(unittest.TestCase):
    def test_records_survive_a_restart_and_the_schema_check_knows_the_table(self) -> None:
        with TemporaryDirectory() as tmp:
            url = f"sqlite+pysqlite:///{Path(tmp) / 'expenses.db'}"
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", url)
            command.upgrade(config, "head")
            repository = SQLAlchemyTaskRepository(url)
            repository.check_schema()
            store = SQLAlchemyExpenseRecordStore(repository.engine)
            workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK, task_repository=repository)
            task = workflow.create_task(make_demo_request(task_id="sql-rec"))
            option = next(
                item for item in task.options
                if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
            )
            workflow.select_option(task.task_id, option.option_id)
            workflow.confirm_booking(
                task.task_id, order_references=["PNR-S"], total_amount=option.total_cost,
                currency=option.currency, reported_by="E1001",
            )
            reconcile_expenses(
                [
                    _record("EXP-S", ("PNR-S",), str(option.total_cost)),
                    _record("EXP-U", ("X",), "1"),
                ],
                repository=repository, store=store, reconcile=workflow.reconcile_expense,
                now=lambda: NOW,
            )
            repository.dispose()

            restarted = SQLAlchemyTaskRepository(url)
            records = SQLAlchemyExpenseRecordStore(restarted.engine).list_records()
            self.assertEqual([r.record.expense_id for r in records], ["EXP-S", "EXP-U"])
            self.assertEqual([r.status.value for r in records], ["MATCHED", "UNMATCHED"])
            self.assertEqual(records[0].record.amount, option.total_cost)
            stored = restarted.get("sql-rec")
            self.assertEqual(stored.expense_reconciliation.expense_id, "EXP-S")
            restarted.dispose()


class ApiTests(unittest.TestCase):
    def test_import_and_list_over_http(self) -> None:
        from corporate_travel_agent.api import main as api_main

        client = TestClient(api_main.app)
        previous = api_main.workflow.clock
        api_main.workflow.clock = lambda: DEMO_CLOCK
        try:
            created = client.post(
                "/trip-tasks",
                json={
                    "traveler_id": "E1001", "origin": "Beijing", "destination": "Shanghai",
                    "departure_after": "2026-08-05T05:00:00+08:00",
                    "arrive_by": "2026-08-06T10:00:00+08:00",
                },
            ).json()
            compliant = next(o for o in created["options"] if o["policy_outcome"] == "COMPLIANT")
            task_id = created["task_id"]
            client.post(
                f"/trip-tasks/{task_id}/select-option", json={"option_id": compliant["option_id"]}
            )
            client.post(
                f"/trip-tasks/{task_id}/booking-confirmation",
                json={"order_references": ["PNR-API"], "total_amount": str(compliant["total_cost"]),
                      "currency": compliant["currency"]},
            )
            response = client.post(
                "/expenses/import",
                json={
                    "source_system": "demo-erp",
                    "records": [
                        {"expense_id": "API-1", "employee_id": "E1001",
                         "amount": str(compliant["total_cost"]), "currency": compliant["currency"],
                         "expensed_at": "2026-08-10T09:00:00Z", "order_references": ["PNR-API"]},
                        {"expense_id": "API-2", "employee_id": "E1001", "amount": "50",
                         "currency": compliant["currency"], "expensed_at": "2026-08-10T09:00:00Z",
                         "order_references": ["OUTSIDE-1"]},
                    ],
                },
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["by_status"]["MATCHED"], 1)
            self.assertEqual(body["by_status"]["UNMATCHED"], 1)
            detail = client.get(f"/trip-tasks/{task_id}").json()
            self.assertEqual(detail["expense_reconciliation"]["status"], "MATCHED")
            self.assertEqual(detail["expense_reconciliation"]["source_system"], "demo-erp")
            listed = client.get("/expenses/records?limit=10").json()
            self.assertEqual({item["expense_id"] for item in listed} >= {"API-1", "API-2"}, True)
            metrics = client.get("/metrics/business").json()
            self.assertEqual(metrics["metrics"]["off_channel_expense_rate"]["status"], "measured")
            bad = client.post("/expenses/import", json={"records": [{"expense_id": "x"}]})
            self.assertEqual(bad.status_code, 422)
        finally:
            api_main.workflow.clock = previous


if __name__ == "__main__":
    unittest.main()

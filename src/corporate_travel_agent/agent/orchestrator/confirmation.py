"""确认与改期：下单确认回流、差旅聚合与观察对象、航变/会议改期开改期任务、费控对账。

从 `TripWorkflowOrchestrator` 按职责拆出来的一块；方法原文逐字来自拆分前的 orchestrator.py。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from corporate_travel_agent.agent.orchestrator.core import WorkflowError
from corporate_travel_agent.agent.orchestrator.state import OrchestratorState
from corporate_travel_agent.domain.enums import (
    BookingConfirmationSource,
    ReconciliationStatus,
    TaskState,
    TripEventType,
    TripStatus,
)
from corporate_travel_agent.domain.models import (
    BookingConfirmation,
    ChangeImpact,
    ExpenseReconciliation,
    TravelOptionVersion,
    Trip,
    TripEvent,
    TripRequestVersion,
    TripTask,
    TripWatch,
    TripWatchLeg,
)
from corporate_travel_agent.domain.validation import (
    BookingConfirmationValidationError,
    validate_booking_confirmation_values,
)
from corporate_travel_agent.services.change_impact import next_flight_check_at
from corporate_travel_agent.services.outbox_events import OutboxEventDraft


def _aware(value: datetime) -> datetime:
    """供应商给的时刻没带时区就按 UTC 记——观察期要和系统时钟比。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ConfirmationMixin(OrchestratorState):
    """确认与改期：下单确认回流、差旅聚合与观察对象、航变/会议改期开改期任务、费控对账。

    混入 `TripWorkflowOrchestrator`；状态都在宿主实例上，这里只放方法。
    """

    def confirm_booking(
        self,
        task_id: str,
        *,
        order_references: Sequence[str],
        total_amount: Decimal,
        currency: str,
        reported_by: str,
        booked_at: datetime | None = None,
        note: str | None = None,
    ) -> TripTask:
        """员工回填"我订好了，订单号是这个、花了这么多"。

        允许从两个状态进来：`READY_FOR_HANDOFF`（拿着链接去订了，回来直接填单号——
        这时先按交接完成记一笔，再记确认，`HANDOFF_COMPLETED` 事件不会少）和
        `HANDED_OFF`（先前点过"我去订了"，现在补单号）。交接链接过没过期在这里不管：
        链接的有效期管的是"能不能去订"，人已经订完回来了。

        一个任务只有一条确认，第二次进来直接拒绝：这条记录一写进去就进了业务指标，
        改它等于让历史曲线悄悄变形。填错了开新任务。

        **系统核不了订单号和金额。** 这里只校验形状（见 `validate_booking_confirmation_values`），
        `source` 固定为 `SELF_REPORTED`，读指标的人必须带着这一点看。
        """
        task = self.tasks.get(task_id)
        if task.booking_confirmation is not None:
            raise WorkflowError("The booking has already been confirmed for this task")
        if task.state not in {TaskState.READY_FOR_HANDOFF, TaskState.HANDED_OFF}:
            raise WorkflowError(f"Cannot confirm a booking in {task.state.value}")
        intent = task.booking_intent
        option = task.selected_option()
        if intent is None or option is None or intent.selected_option_id != option.option_id:
            raise WorkflowError("No validated handoff to confirm a booking against")
        if not reported_by or not reported_by.strip():
            raise WorkflowError("A booking confirmation must name who reported it")
        try:
            values = validate_booking_confirmation_values(
                order_references=order_references,
                total_amount=total_amount,
                currency=currency,
                booked_at=booked_at,
                now=self.clock(),
                note=note,
            )
        except BookingConfirmationValidationError as exc:
            raise WorkflowError(str(exc)) from exc

        if task.state is TaskState.READY_FOR_HANDOFF:
            self.recorder.transition(task, TaskState.HANDED_OFF)
            self.recorder.audit(task, "HANDOFF_COMPLETED", intent.intent_id, task.state.value)

        confirmation = BookingConfirmation(
            confirmation_id=str(uuid4()),
            intent_id=intent.intent_id,
            option_id=option.option_id,
            option_version=option.version,
            order_references=values.order_references,
            total_amount=values.total_amount,
            currency=values.currency,
            source=BookingConfirmationSource.SELF_REPORTED,
            reported_by=reported_by.strip(),
            reported_at=self.clock(),
            booked_at=values.booked_at,
            note=values.note,
        )
        # 状态迁移、确认记录、确认审计、发件箱通知、差旅观察对象——**一笔**落库。
        # 此前是三笔（迁移 / 审计 / 差旅），差旅那笔失败时留下"任务已确认、差旅没登记"的半截
        # （2026-09-02 真链路实测踩到过）。
        task.booking_confirmation = confirmation
        transition = self.recorder.transition_pending(task, TaskState.BOOKING_CONFIRMED)
        trip = self._trip_with_watch(task, option)
        self.recorder.audit(
            task,
            "BOOKING_CONFIRMED",
            {
                "order_reference_count": len(confirmation.order_references),
                "total_amount": str(confirmation.total_amount),
                "currency": confirmation.currency,
                "source": confirmation.source.value,
                "reported_by": confirmation.reported_by,
            },
            confirmation.confirmation_id,
            (intent.intent_id, option.option_id),
            # 费控、财务这类下游系统要的就是这一条：谁、哪个成本中心、花了多少、什么时候。
            outbox=(
                OutboxEventDraft(
                    event_type="BOOKING_CONFIRMED",
                    payload={
                        "task_id": task.task_id,
                        "confirmation_id": confirmation.confirmation_id,
                        "employee_id": task.employee.employee_id,
                        "cost_center": task.employee.cost_center,
                        "order_reference_count": len(confirmation.order_references),
                        "total_amount": str(confirmation.total_amount),
                        "currency": confirmation.currency,
                        "booked_at": confirmation.booked_at.isoformat(),
                        "source": confirmation.source.value,
                    },
                ),
            ),
            trip=trip,
            preceding=(transition,),
        )
        return task

    # ------------------------------------------------------------------
    # 一趟差旅：规划任务建它，下单确认让它进入观察，变更事件开改期任务
    # ------------------------------------------------------------------

    def _add_task_with_trip(self, task: TripTask, *, change_event: TripEvent | None = None) -> None:
        """新任务和它所属的差旅一起落库：规划任务建一趟新差旅，改期任务追加到已有差旅。

        以前是先 `tasks.add` 再 `trips.add/save`——改期任务建成了、差旅没记上，
        `find_by_task` 就找不到它（HANDOFF §8 那条窄缝）。现在走 `TaskRepository.add_with_trip`：
        两张表同一个库时一笔事务，否则先任务后差旅（内存里本来就没有"半截"可言）。
        改期事件也在这里一并写进差旅，不再事后补第二次保存。
        """
        assert task.trip_id is not None
        if task.parent_task_id is None:
            trip = Trip(
                trip_id=task.trip_id,
                traveler_id=task.employee.employee_id,
                requester_id=task.requested_by,
                status=TripStatus.PLANNED,
                task_ids=(task.task_id,),
                created_at=self.clock(),
            )
            trip_is_new = True
        else:
            trip = self.trips.get(task.trip_id)
            trip.task_ids = (*trip.task_ids, task.task_id)
            trip.status = TripStatus.CHANGE_REQUESTED
            if change_event is not None:
                trip.events = (*trip.events, replace(change_event, opened_task_id=task.task_id))
            trip_is_new = False
        # 一笔事务还是两笔，由仓储按"两张表是否在同一个库里"决定；编排器不看引擎。
        self.tasks.add_with_trip(task, trip=trip, trip_is_new=trip_is_new, trips=self.trips)

    def _register_trip_watch(self, task: TripTask, option: TravelOptionVersion) -> None:
        """单独登记观察对象（修复半截数据时用）；正常路径由 `confirm_booking` 随审计一笔写。"""
        trip = self._trip_with_watch(task, option)
        if trip is not None:
            self.trips.save(trip)

    def _trip_with_watch(self, task: TripTask, option: TravelOptionVersion) -> Trip | None:
        """把观察对象挂到差旅上（不保存）：盯着确认方案的每一段，盯到最后一段落地后一天。"""
        if task.trip_id is None:
            return None  # 接差旅聚合之前建的旧任务
        trip = self.trips.get(task.trip_id)
        now = self.clock()
        legs = tuple(
            TripWatchLeg(
                ref_id=leg.ref_id,
                provider=leg.provider,
                origin=leg.origin,
                destination=leg.destination,
                depart_at=_aware(leg.depart_at),
                arrive_at=_aware(leg.arrive_at),
            )
            for leg in option.legs
        )
        last_arrival = max((leg.arrive_at for leg in legs), default=now)
        trip.watch = TripWatch(
            task_id=task.task_id,
            legs=legs,
            registered_at=now,
            watch_until=max(last_arrival, now) + timedelta(days=1),
            # watch worker 第一次该去问动态源的时刻：离起飞越近越勤，48 小时外先不问。
            next_check_at=next_flight_check_at(
                now, legs, lookahead_hours=self.trip_watch_lookahead_hours
            ),
        )
        trip.status = TripStatus.REBOOKED if task.is_change_task else TripStatus.BOOKED
        return trip

    def report_trip_event(
        self,
        trip_id: str,
        *,
        event_type: TripEventType,
        reported_by: str,
        ref_id: str | None = None,
        new_depart_at: datetime | None = None,
        new_arrive_by: datetime | None = None,
        note: str | None = None,
        leg_index: int | None = None,
        impact: ChangeImpact | None = None,
    ) -> TripTask:
        """收一条外部变更事件，开一个改期任务挂在这趟差旅下。原任务一个字不动。

        接受的条件：差旅已订（`BOOKED`/`REBOOKED`）、观察期没过；航变必须对上观察对象里
        的某张票，会议改期必须带新的最晚到达时刻。改期任务复用原请求：会议改期改最晚到达
        时刻（`leg_index` 指明是哪一段的，默认第一段），航变把那张票排除在候选之外，然后
        照常走规划、政策、审批、交接、确认。

        `impact` 是 watch worker 算出的影响评估（取消 / 赶不上 / 接不上），随事件一起记下，
        改期任务的 `change_event` 元数据里能看到"为什么要改"。手工报的事件没有它。
        """
        trip = self.trips.get(trip_id)
        if trip.status is TripStatus.CANCELLED:
            raise WorkflowError(f"Trip {trip_id} has been cancelled; nothing to change")
        if trip.status is TripStatus.CHANGE_REQUESTED:
            raise WorkflowError(
                f"Trip {trip_id} already has a change in progress (task {trip.latest_task_id})"
            )
        if trip.status not in {TripStatus.BOOKED, TripStatus.REBOOKED} or trip.watch is None:
            raise WorkflowError(
                f"Trip {trip_id} has no confirmed booking to change ({trip.status.value})"
            )
        now = self.clock()
        if now > trip.watch.watch_until:
            raise WorkflowError("The trip has already been travelled; the watch window is over")
        if event_type is TripEventType.TRIP_CANCELLED:
            raise WorkflowError("Cancel a trip with cancel_trip, not as a change event")
        if event_type is TripEventType.FLIGHT_CHANGED:
            if not ref_id:
                raise WorkflowError("FLIGHT_CHANGED needs the ref_id of the affected ticket")
            if trip.watch.leg(ref_id) is None:
                raise WorkflowError(f"Ticket {ref_id} is not part of the booked trip")
        elif new_arrive_by is None:
            raise WorkflowError("MEETING_MOVED needs the new arrive-by time")
        booked_task = self.tasks.get(trip.watch.task_id)
        if booked_task.request is None:
            raise WorkflowError("The booked task has no structured request to rebook from")
        if leg_index is not None:
            leg_count = len(booked_task.request.transport_legs())
            if not 0 <= leg_index < leg_count:
                raise WorkflowError(
                    f"leg_index {leg_index} is out of range; this trip has {leg_count} legs"
                )
        event = TripEvent(
            event_id=str(uuid4()),
            event_type=event_type,
            received_at=now,
            reported_by=reported_by.strip(),
            ref_id=ref_id,
            new_depart_at=new_depart_at,
            new_arrive_by=new_arrive_by,
            note=note,
            opened_task_id=None,
            leg_index=leg_index,
            impact=impact,
        )
        # 改期任务、差旅上的任务列表和这条事件在 `_add_task_with_trip` 里一笔写入。
        return self.create_task(
            self._change_request(booked_task.request, event, now),
            requester_id=trip.requester_id,
            trip_id=trip.trip_id,
            parent_task_id=booked_task.task_id,
            change_event=event,
        )

    @staticmethod
    def _change_request(
        request: TripRequestVersion, event: TripEvent, now: datetime
    ) -> TripRequestVersion:
        """从被改的请求派生改期请求：新任务号、第 1 版；会议改期改**那一段**的最晚到达时刻。

        `leg_index` 没给按第一段——接这个字段之前的事件都是改第一段。多城行程里
        "杭州的客户改到 19 号见"改的是第二段，第一段一个字不动。
        """
        values: dict[str, Any] = {"task_id": str(uuid4()), "version": 1, "created_at": now}
        if event.event_type is TripEventType.MEETING_MOVED and event.new_arrive_by is not None:
            new_arrive_by = event.new_arrive_by
            index = event.leg_index or 0
            # 搜索窗口整体平移：会议从 20 号推到 22 号，出发窗口也该从 19 号傍晚挪到 21 号傍晚。
            # 只改到场时限的话，窗口会宽到三天，规划器会端出提前两天到的票——可行，但没人要。
            legs = request.transport_legs()
            shift = new_arrive_by - legs[index].arrive_before if index < len(legs) else None

            def _shift(depart_after: datetime) -> datetime:
                moved = depart_after + shift if shift is not None else depart_after
                return moved if moved < new_arrive_by else new_arrive_by - timedelta(hours=24)

            def _shifted(depart_after: datetime | None) -> datetime | None:
                return None if depart_after is None else _shift(depart_after)

            if request.journey:
                journey = list(request.journey)
                leg = journey[index]
                journey[index] = replace(
                    leg, arrive_before=new_arrive_by, depart_after=_shift(leg.depart_after)
                )
                values["journey"] = tuple(journey)
            if index == 0:
                values["arrive_by"] = new_arrive_by
                values["departure_after"] = _shifted(request.departure_after)
            elif index == 1 and not request.journey and request.return_before is not None:
                values["return_before"] = new_arrive_by
                values["return_after"] = _shifted(request.return_after)
        return replace(request, **values)

    def reconcile_expense(
        self,
        task_id: str,
        *,
        expense_id: str,
        source_system: str,
        expense_amount: Decimal,
        currency: str,
        expensed_at: datetime,
        matched_order_references: Sequence[str],
        status: ReconciliationStatus,
        note: str | None = None,
    ) -> TripTask:
        """把一条费控记录钉到这趟任务上：自述的下单确认从此有了外部佐证。

        只接受 `BOOKING_CONFIRMED`（没有确认就没什么可对）；一个任务只对一次，第二条
        记录该由对账服务标成 `DUPLICATE`，不该走到这里。状态不变——对账是确认的佐证，
        不是新的一步。
        """
        task = self.tasks.get(task_id)
        if task.state is not TaskState.BOOKING_CONFIRMED or task.booking_confirmation is None:
            raise WorkflowError(f"Cannot reconcile an expense against a task in {task.state.value}")
        if task.expense_reconciliation is not None:
            raise WorkflowError("This booking has already been reconciled")
        if status in {ReconciliationStatus.UNMATCHED, ReconciliationStatus.DUPLICATE}:
            raise WorkflowError(f"A {status.value} record does not belong on a task")
        if not expense_id.strip():
            raise WorkflowError("An expense record needs an id")
        reconciliation = ExpenseReconciliation(
            reconciliation_id=str(uuid4()),
            expense_id=expense_id.strip(),
            source_system=source_system.strip() or "expense-system",
            expense_amount=expense_amount,
            currency=currency,
            expensed_at=expensed_at,
            reconciled_at=self.clock(),
            status=status,
            matched_order_references=tuple(dict.fromkeys(matched_order_references)),
            note=note,
        )
        task.expense_reconciliation = reconciliation
        self.recorder.audit(
            task,
            "EXPENSE_RECONCILED",
            {
                "expense_id": reconciliation.expense_id,
                "source_system": reconciliation.source_system,
                "status": reconciliation.status.value,
                "expense_amount": str(reconciliation.expense_amount),
                "currency": reconciliation.currency,
                "variance": (
                    str(variance)
                    if (variance := reconciliation.amount_variance(task.booking_confirmation))
                    is not None
                    else None
                ),
            },
            reconciliation.reconciliation_id,
            (task.booking_confirmation.confirmation_id,),
        )
        return task

"""记录器：任务上每一次"记下来"——审计事件、状态迁移、评测轨迹、搜索出处——都经过它。

2026-09-03 从 `RecordsMixin` 抽出来的第一个协作对象（ADR-0006）。抽它而不是别的，是因为它是
写路径：任务更新、审计事件、发件箱事件、差旅聚合要不要放进同一笔事务，全在这里定；而它自己
不依赖任何其他 mixin，是耦合图上唯一的纯叶子。

它不做判断：状态能不能迁由 `StateMachine` 说了算，事务边界由仓储说了算（`record_with_trip`），
这里只负责把三件事按正确的顺序交给正确的对象。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from corporate_travel_agent.agent.ports import WorkflowTraceEvent, WorkflowTraceObserverPort
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.domain.models import (
    AuditEvent,
    InventorySnapshot,
    SearchProvenance,
    Trip,
    TripTask,
)
from corporate_travel_agent.providers.base import HotelSearchQuery, TransportSearchQuery
from corporate_travel_agent.services.audit import new_audit_event
from corporate_travel_agent.services.outbox_events import OutboxEventDraft
from corporate_travel_agent.services.repositories import TaskRepository
from corporate_travel_agent.services.trips import TripRepository
from corporate_travel_agent.workflow.state_machine import StateMachine


class TaskRecorder:
    """审计、状态迁移、轨迹、搜索出处的唯一写入口。"""

    def __init__(
        self,
        *,
        tasks: TaskRepository,
        trips: TripRepository,
        state_machine: StateMachine,
        trace_observer: WorkflowTraceObserverPort | None = None,
    ) -> None:
        self.tasks = tasks
        self.trips = trips
        self.state_machine = state_machine
        self.trace_observer = trace_observer

    # -- 状态迁移 --------------------------------------------------------------

    def transition(self, task: TripTask, target: TaskState) -> None:
        """经状态机校验后迁移任务状态，并立即落一条迁移审计。"""
        previous = task.state
        task.state = self.state_machine.transition(previous, target)
        self.audit(task, "STATE_TRANSITION", previous.value, target.value)

    def transition_pending(self, task: TripTask, target: TaskState) -> AuditEvent:
        """迁移状态但先不落库：把迁移审计交给下一次 `audit(preceding=...)` 同一笔写。

        下单确认用它：迁到 `BOOKING_CONFIRMED`、确认审计、发件箱通知、差旅观察对象四件事
        必须一起成或一起败，拆成两笔就会留下"状态改了、通知没发 / 差旅没登记"的半截。
        """
        previous = task.state
        task.state = self.state_machine.transition(previous, target)
        return new_audit_event(
            task.task_id, "STATE_TRANSITION", input_value=previous.value, output_value=target.value
        )

    # -- 审计 ------------------------------------------------------------------

    def audit(
        self,
        task: TripTask,
        event_type: str,
        input_value: object,
        output_value: object,
        evidence_refs: tuple[str, ...] = (),
        *,
        outbox: Sequence[OutboxEventDraft] = (),
        trip: Trip | None = None,
        preceding: Sequence[AuditEvent] = (),
    ) -> None:
        """写审计事件并可选推送轨迹观察者。

        `outbox` 是要**随这次状态变化一起**进发件箱的事件：仓储把任务更新、审计事件和
        发件箱事件放进同一笔事务。审批单建了却没通知出去、或通知出去了审批单其实没建成，
        都是不能接受的半截。

        `trip` 是要**随这次审计一起**保存的差旅聚合（下单确认登记观察对象、航班动态观察、
        取消差旅）。能不能做成一笔事务由任务仓储判断（两张表同一个引擎才行，见
        `TaskRepository.record_with_trip`）；做不成就先任务后差旅——内存仓储本来就没有半截可言。
        """
        event = new_audit_event(
            task.task_id,
            event_type,
            input_value=input_value,
            output_value=output_value,
            evidence_refs=evidence_refs,
        )
        preceding = tuple(preceding)
        if trip is not None:
            self.tasks.record_with_trip(
                task,
                event,
                trip=trip,
                trips=self.trips,
                outbox_events=tuple(outbox),
                preceding=preceding,
            )
        elif outbox or preceding:
            self.tasks.record(task, event, outbox_events=tuple(outbox), preceding=preceding)
        else:
            self.tasks.record(task, event)
        for earlier in preceding:
            if earlier.event_type == "STATE_TRANSITION":
                self.record_trace(
                    WorkflowTraceEvent(
                        kind="state_transition",
                        name="STATE_TRANSITION",
                        status="success",
                        started_at=datetime.now(UTC),
                        duration_ms=0.0,
                        state_before=str(earlier.input_hash),
                        state_after=task.state.value,
                        input_value=None,
                        output_value=task.state.value,
                        evidence_refs=(),
                    )
                )
        if event_type.startswith("TOOL_CALL_"):
            return
        state_before = task.state.value
        state_after = task.state.value
        if event_type == "STATE_TRANSITION":
            state_before = str(input_value)
            state_after = str(output_value)
        self.record_trace(
            WorkflowTraceEvent(
                kind=self.trace_kind(event_type),
                name=event_type,
                status="success",
                started_at=datetime.now(UTC),
                duration_ms=0.0,
                state_before=state_before,
                state_after=state_after,
                input_value=input_value,
                output_value=output_value,
                evidence_refs=evidence_refs,
            )
        )

    # -- 轨迹 ------------------------------------------------------------------

    def record_trace(self, event: WorkflowTraceEvent) -> None:
        """向轨迹观察者投递事件（若已配置）。"""
        if self.trace_observer is not None:
            self.trace_observer.record(event)

    @staticmethod
    def trace_kind(event_type: str) -> str:
        if event_type == "STATE_TRANSITION":
            return "state_transition"
        if "RECOVER" in event_type or event_type.startswith("RETRY"):
            return "recovery"
        if event_type.startswith(("APPROVAL_", "OPTIONS_", "POLICY_")):
            return "policy"
        if event_type in {
            "TASK_CREATED_FROM_MESSAGE",
            "CLARIFICATION_RECEIVED",
            "STRUCTURED_FALLBACK_SUBMITTED",
        }:
            return "user"
        return "audit"

    @staticmethod
    def trace_evidence_refs(result: object, input_value: object) -> tuple[str, ...]:
        refs: list[str] = []
        # `items` 这个名字在 InventorySnapshot 上是一串报价，在 dict 上却是个方法。
        # 工具循环的返回是 dict，直接迭代会炸——只认真正可迭代的那种。
        items = getattr(result, "items", ())
        if isinstance(items, (list, tuple)):
            for item in items:
                ref_id = getattr(item, "ref_id", None)
                if isinstance(ref_id, str):
                    refs.append(ref_id)
        if isinstance(result, dict):
            for option in result.get("options", ()) or ():
                ref_id = option.get("ref_id") if isinstance(option, dict) else None
                if isinstance(ref_id, str):
                    refs.append(ref_id)
        current_prices = getattr(result, "current_prices", None)
        if isinstance(current_prices, dict):
            refs.extend(str(key) for key in current_prices)
        refs.extend(str(item) for item in getattr(result, "unavailable_refs", ()))
        if isinstance(input_value, dict):
            for key in ("refs", "inventory_refs"):
                values = input_value.get(key, ())
                if isinstance(values, (list, tuple)):
                    refs.extend(str(item) for item in values)
        return tuple(dict.fromkeys(refs))

    # -- 搜索出处 ---------------------------------------------------------------

    @staticmethod
    def record_searches(task: TripTask, searches: Sequence[SearchProvenance]) -> None:
        """把这一轮的搜索出处并进任务，按 snapshot_id 去重。

        去重是必要的：同一条任务会多轮搜索，重验也会再走一次，而快照是不可变的
        ——同一个 snapshot_id 的出处不该记两遍。
        """
        known = {item.snapshot_id for item in task.searches}
        for item in searches:
            if item.snapshot_id in known:
                continue
            known.add(item.snapshot_id)
            task.searches.append(item)


def transport_provenance(
    query: TransportSearchQuery, snapshot: InventorySnapshot
) -> SearchProvenance:
    """结构化入口的出处。

    这条路上**没有用户原话可抄**——日期来自已经校验过的 `TripRequestVersion`，
    不是从一句话里读出来的。`date_evidence` 因此留空，如实反映"这一天的依据
    在请求版本里，不在对话里"。
    """
    return SearchProvenance(
        kind="transport",
        parameters=(
            ("origin", query.origin),
            ("destination", query.destination),
            ("depart_after", query.depart_after.isoformat()),
            # 没有到达时限的查询就不记这一项：出处只记真正用过的参数。
            *(
                (("arrive_by", query.arrive_before.isoformat()),)
                if query.arrive_before is not None
                else ()
            ),
        ),
        snapshot_id=snapshot.snapshot_id,
        query_hash=snapshot.query_hash,
        captured_at=snapshot.captured_at,
        valid_until=snapshot.valid_until,
    )


def hotel_provenance(query: HotelSearchQuery, snapshot: InventorySnapshot) -> SearchProvenance:
    return SearchProvenance(
        kind="hotel",
        parameters=(
            ("city", query.city),
            ("check_in", query.check_in.isoformat()),
            ("check_out", query.check_out.isoformat()),
        ),
        snapshot_id=snapshot.snapshot_id,
        query_hash=snapshot.query_hash,
        captured_at=snapshot.captured_at,
        valid_until=snapshot.valid_until,
    )

"""观察与变更：航班动态进来之后怎么判、怎么开改期任务；取消差旅；订好之后的聊天改期。

从 `ConfirmationMixin` 旁边长出来的一块：那边管"下单确认 → 登记观察对象 → 收事件开改期
任务"，这边管**事件从哪来**——

- **被动**：watch worker 按节奏问航班动态源（`process_due_flight_checks`），或者企业侧把
  推送 POST 进来；两条路都落到 `observe_flight_status`，先过确定性影响评估
  （`services/change_impact.py`），只有"取消 / 赶不上 / 接不上"才开改期任务，延误但来得及
  只通知。
- **主动**：旅行者在已订任务的聊天里说"会议改到周五了"或"这趟不去了"
  （`submit_change_message`），模型只负责把话读成一条变更请求（`agent/change_intent.py`），
  落到的还是同一个 `report_trip_event` / `cancel_trip`。

硬边界不变：改期任务照样走规划、政策、审批、交接；退票、改签由人去官方平台办。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

from corporate_travel_agent.agent.orchestrator.core import LanguageModelUnavailable, WorkflowError
from corporate_travel_agent.domain.enums import (
    ChangeImpactVerdict,
    TaskState,
    TripEventType,
    TripStatus,
)
from corporate_travel_agent.domain.models import (
    ConversationMessage,
    FlightObservation,
    FlightStatusReport,
    Trip,
    TripEvent,
    TripTask,
)
from corporate_travel_agent.providers.flight_status import FlightStatusError
from corporate_travel_agent.services.change_impact import (
    LANDED_GRACE,
    assess_flight_change,
    next_flight_check_at,
)
from corporate_travel_agent.services.outbox_events import OutboxEventDraft
from corporate_travel_agent.services.trips import WATCHED_STATUSES

LOGGER = logging.getLogger(__name__)


def _impact_dict(impact: Any) -> dict[str, Any]:
    return {
        "verdict": impact.verdict.value,
        "reasons": list(impact.reasons),
        "delay_minutes": impact.delay_minutes,
        "new_arrive_at": impact.new_arrive_at.isoformat() if impact.new_arrive_at else None,
        "latest_acceptable_arrival": (
            impact.latest_acceptable_arrival.isoformat()
            if impact.latest_acceptable_arrival
            else None
        ),
        "buffer_minutes": impact.buffer_minutes,
        "connection_ok": impact.connection_ok,
    }


class TripWatchMixin:
    """混入 `TripWorkflowOrchestrator`；状态都在宿主实例上，这里只放方法。"""

    # ------------------------------------------------------------------
    # 被动：航班动态 → 影响评估 → 通知或改期
    # ------------------------------------------------------------------

    def observe_flight_status(
        self, trip_id: str, report: FlightStatusReport, *, reported_by: str
    ) -> FlightObservation:
        """收一条航班动态，判影响，该通知的通知、该改期的开改期任务。

        worker 轮询和入站推送都走这里。同一段同样的状态第二次进来不再通知、不再记审计——
        动态源每 15 分钟说一遍"延误 40 分钟"，旅行者只该听到一次。
        """
        trip = self.trips.get(trip_id)
        if trip.status is TripStatus.CANCELLED:
            raise WorkflowError(f"Trip {trip_id} has been cancelled; it is no longer watched")
        if trip.status not in WATCHED_STATUSES or trip.watch is None:
            raise WorkflowError(f"Trip {trip_id} is not being watched ({trip.status.value})")
        watch = trip.watch
        index = watch.leg_index(report.ref_id)
        if index is None:
            raise WorkflowError(f"Ticket {report.ref_id} is not part of the booked trip")
        leg = watch.legs[index]
        now = self.clock()
        if now > watch.watch_until:
            raise WorkflowError("The trip has already been travelled; the watch window is over")
        booked_task = self.tasks.get(watch.task_id)
        if booked_task.request is None:
            raise WorkflowError("The booked task has no structured request to assess against")
        policy = self._policy_for(booked_task)
        following = watch.legs[index + 1] if index + 1 < len(watch.legs) else None
        impact = assess_flight_change(
            leg,
            report,
            request=booked_task.request,
            leg_index=index,
            arrival_buffer_minutes=policy.arrival_buffer_minutes,
            now=now,
            following=following,
            min_connection_minutes=self.min_connection_minutes,
            delay_notice_minutes=self.delay_notice_minutes,
        )
        previous = watch.observation(report.ref_id)
        duplicate = previous is not None and (
            previous.status,
            previous.estimated_depart_at,
            previous.estimated_arrive_at,
            previous.verdict,
        ) == (report.status, report.estimated_depart_at, report.estimated_arrive_at, impact.verdict)

        opened_task_id: str | None = None
        if impact.verdict is ChangeImpactVerdict.REBOOK_REQUIRED and not duplicate:
            change = self.report_trip_event(
                trip.trip_id,
                event_type=TripEventType.FLIGHT_CHANGED,
                ref_id=report.ref_id,
                new_depart_at=report.estimated_depart_at,
                note=(report.note or "；".join(impact.reasons)) or None,
                reported_by=reported_by,
                impact=impact,
            )
            opened_task_id = change.task_id
            # 改期任务和差旅在同一笔里落了库；本地这份差旅已经过期，重新读。
            trip = self.trips.get(trip.trip_id)
            watch = trip.watch
            assert watch is not None
        elif duplicate and previous is not None:
            opened_task_id = previous.opened_task_id

        observation = FlightObservation(
            ref_id=report.ref_id,
            status=report.status,
            observed_at=report.observed_at,
            source=report.source,
            verdict=impact.verdict,
            reasons=impact.reasons,
            estimated_depart_at=report.estimated_depart_at,
            estimated_arrive_at=report.estimated_arrive_at,
            opened_task_id=opened_task_id,
        )
        others = tuple(item for item in watch.observations if item.ref_id != report.ref_id)
        trip.watch = replace(watch, observations=(*others, observation), last_checked_at=now)
        if duplicate:
            self.trips.save(trip)  # 只更新"最近查过"，没有审计可记
        else:
            # 观察记录和审计、发件箱通知同一笔落库。
            self._audit_flight_observation(booked_task, trip, observation, impact)
        return observation

    def _audit_flight_observation(
        self, task: TripTask, trip: Trip, observation: FlightObservation, impact: Any
    ) -> None:
        """观察记进被观察任务的审计；有影响的（通知 / 改期）同一笔进发件箱。"""
        payload = {
            "trip_id": trip.trip_id,
            "task_id": task.task_id,
            "employee_id": task.employee.employee_id,
            "ref_id": observation.ref_id,
            "status": observation.status.value,
            "source": observation.source,
            "verdict": observation.verdict.value,
            "reasons": list(observation.reasons),
            "estimated_depart_at": (
                observation.estimated_depart_at.isoformat()
                if observation.estimated_depart_at
                else None
            ),
            "estimated_arrive_at": (
                observation.estimated_arrive_at.isoformat()
                if observation.estimated_arrive_at
                else None
            ),
            "opened_task_id": observation.opened_task_id,
        }
        outbox: tuple[OutboxEventDraft, ...] = ()
        if observation.verdict is not ChangeImpactVerdict.NO_CHANGE:
            outbox = (OutboxEventDraft(event_type="TRIP_FLIGHT_STATUS_NOTICE", payload=payload),)
        self._audit(
            task,
            "FLIGHT_STATUS_OBSERVED",
            {
                "ref_id": observation.ref_id,
                "status": observation.status.value,
                "source": observation.source,
                "estimated_depart_at": payload["estimated_depart_at"],
                "estimated_arrive_at": payload["estimated_arrive_at"],
            },
            _impact_dict(impact) | {"opened_task_id": observation.opened_task_id},
            outbox=outbox,
            trip=trip,
        )

    def process_due_flight_checks(self, *, limit: int = 20) -> tuple[str, ...]:
        """watch worker 的一轮：领取到点的差旅，逐段问动态源，评估，再排下一次。

        没接动态源（`NullFlightStatusSource`）就一趟都不领——不去问一个不存在的源。
        动态源这一次答不上来（`FlightStatusError`）记一笔，按节奏下次再问，不当成航班有变。
        """
        source = self.flight_status_source
        if not getattr(source, "configured", True):
            return ()
        now = self.clock()
        claimed = self.trips.claim_due_flight_checks(
            worker_id=self.trip_watch_worker_id,
            now=now,
            lease_duration=self.trip_watch_lease_duration,
            limit=limit,
        )
        processed: list[str] = []
        lookahead = timedelta(hours=self.trip_watch_lookahead_hours)
        for trip in claimed:
            self._trip_watch_metrics["claimed"] += 1
            watch = trip.watch
            assert watch is not None
            try:
                for leg in watch.legs:
                    if leg.arrive_at + LANDED_GRACE <= now or leg.depart_at - now > lookahead:
                        continue
                    try:
                        report = source.status_of(leg, now=now)
                    except FlightStatusError as exc:
                        LOGGER.warning("flight status source failed for %s: %s", leg.ref_id, exc)
                        self._trip_watch_metrics["source_errors"] += 1
                        continue
                    self._trip_watch_metrics["checked"] += 1
                    observation = self.observe_flight_status(
                        trip.trip_id, report, reported_by=f"flight-status:{source.name}"
                    )
                    if observation.opened_task_id is not None:
                        self._trip_watch_metrics["changes_opened"] += 1
                        break  # 差旅已进入改期，旧票不再盯
                    if observation.verdict is ChangeImpactVerdict.NOTIFY_ONLY:
                        self._trip_watch_metrics["notices"] += 1
            except WorkflowError as exc:
                LOGGER.info("trip %s left the watch during processing: %s", trip.trip_id, exc)
            finally:
                current = self.trips.get(trip.trip_id)
                if current.status in WATCHED_STATUSES and current.watch is not None:
                    current.watch = replace(
                        current.watch,
                        next_check_at=next_flight_check_at(
                            now,
                            current.watch.legs,
                            lookahead_hours=self.trip_watch_lookahead_hours,
                        ),
                        last_checked_at=now,
                        check_count=current.watch.check_count + 1,
                    )
                self.trips.save(current)  # 保存即释放租约
            processed.append(trip.trip_id)
        return tuple(processed)

    def trip_watch_metrics(self) -> dict[str, int]:
        return dict(self._trip_watch_metrics)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel_trip(self, trip_id: str, *, reported_by: str, reason: str | None = None) -> Trip:
        """旅行者或发起人取消整趟差旅：观察停止、看板不再显示、发件箱通知下游。

        不动任何任务的状态——任务记录的是"当时规划了什么"，取消是差旅这一层的事实。
        已订的票由人去官方平台退改；系统记不到退改费，报表里这趟的支出仍是回填的数。
        """
        trip = self.trips.get(trip_id)
        if trip.status is TripStatus.CANCELLED:
            raise WorkflowError(f"Trip {trip_id} is already cancelled")
        if not reported_by or not reported_by.strip():
            raise WorkflowError("A cancellation must name who reported it")
        now = self.clock()
        previous = trip.status
        watched_refs = [leg.ref_id for leg in trip.watch.legs] if trip.watch else []
        event = TripEvent(
            event_id=str(uuid4()),
            event_type=TripEventType.TRIP_CANCELLED,
            received_at=now,
            reported_by=reported_by.strip(),
            ref_id=None,
            new_depart_at=None,
            new_arrive_by=None,
            note=(reason or "").strip() or None,
            opened_task_id=None,
        )
        trip.events = (*trip.events, event)
        trip.status = TripStatus.CANCELLED
        if trip.watch is not None:
            trip.watch = replace(trip.watch, next_check_at=None)

        task = self.tasks.get(trip.latest_task_id)
        payload = {
            "trip_id": trip.trip_id,
            "task_id": task.task_id,
            "employee_id": trip.traveler_id,
            "requester_id": trip.requester_id,
            "previous_status": previous.value,
            "reported_by": event.reported_by,
            "reason": event.note,
            "watched_refs": watched_refs,
        }
        self._audit(
            task,
            "TRIP_CANCELLED",
            {"trip_id": trip.trip_id, "reported_by": event.reported_by, "reason": event.note},
            {"previous_status": previous.value, "watched_refs": watched_refs},
            outbox=(OutboxEventDraft(event_type="TRIP_CANCELLED", payload=payload),),
            trip=trip,  # 差旅状态、审计、发件箱通知同一笔
        )
        return trip

    # ------------------------------------------------------------------
    # 主动：订好之后的聊天改期
    # ------------------------------------------------------------------

    def submit_change_message(
        self, task_id: str, message: str, *, reported_by: str | None = None
    ) -> TripTask:
        """已订任务的聊天：把"会议改到周五" / "这趟不去了"读成一条变更请求。

        模型只做一件事——从话里读出 kind / 哪一段 / 新时限，还得逐字引用旅行者原话来证明
        日期是他说的（和搜索那道关卡同一条规则）。落地仍走 `report_trip_event` /
        `cancel_trip`：会议改期开一个改期任务并把这句话带过去，取消不开任务、回一句话。
        读不出来就问一句，任务状态不变。
        """
        from corporate_travel_agent.agent.change_intent import (
            ChangeKind,
            render_change_conversation,
            run_change_intent,
        )

        message = self._validate_message(message)
        task = self.tasks.get(task_id)
        if task.state is not TaskState.BOOKING_CONFIRMED:
            raise WorkflowError(f"Cannot submit a change message in {task.state.value}")
        if task.trip_id is None:
            raise WorkflowError("This task predates trip tracking; open a new task to change it")
        trip = self.trips.get(task.trip_id)
        if trip.status is TripStatus.CANCELLED:
            raise WorkflowError("This trip has been cancelled; open a new task to travel again")
        if trip.status is TripStatus.CHANGE_REQUESTED:
            raise WorkflowError(
                f"A change is already in progress for this trip (task {trip.latest_task_id})"
            )
        if trip.status not in WATCHED_STATUSES or trip.watch is None:
            raise WorkflowError(f"This trip is not in a changeable state ({trip.status.value})")
        if trip.watch.task_id != task.task_id:
            raise WorkflowError(
                f"This booking was superseded; continue on task {trip.watch.task_id}"
            )
        if task.request is None:
            raise WorkflowError("The booked task has no structured request to change")
        if self.tool_calling_language_model is None:
            raise LanguageModelUnavailable("No tool-calling language model adapter is configured")

        now = self.clock()
        reporter = (reported_by or "").strip() or task.requested_by
        task.messages.append(ConversationMessage(role="user", content=message, created_at=now))
        self._audit(task, "CHANGE_MESSAGE_RECEIVED", message, {"trip_id": trip.trip_id})

        conversation = render_change_conversation(
            task=task, watch=trip.watch, journey=task.request.transport_legs(), message=message
        )
        context = {
            "reference_time": now.isoformat(),
            "timezone": self.timezone_name,
            "clarification_round": 0,
            "max_clarification_rounds": 1,
            "mode": "trip_change",
        }
        outcome = run_change_intent(
            model=self.tool_calling_language_model,
            conversation=conversation,
            context=context,
            watched_refs=tuple(leg.ref_id for leg in trip.watch.legs),
            leg_count=len(task.request.transport_legs()),
            now=now,
            timezone_name=self.timezone_name,
            call=lambda operation: self._invoke_tool(
                task,
                tool_name="llm.change_intent",
                tool_kind="LLM",
                input_value={"task_id": task.task_id, "trip_id": trip.trip_id},
                operation=operation,
                counts_toward_budget=False,
            ),
        )
        self._note_llm_success()

        if outcome.change is None:
            question = outcome.question or "我没看懂你要改什么：是会议改了时间，还是这趟不去了？"
            task.messages.append(
                ConversationMessage(role="assistant", content=question, created_at=self.clock())
            )
            self._audit(task, "CHANGE_INTENT_QUESTION", message, question)
            return task

        change = outcome.change
        if change.kind is ChangeKind.CANCEL_TRIP:
            self.cancel_trip(trip.trip_id, reported_by=reporter, reason=change.reason)
            task = self.tasks.get(task_id)
            reply = (
                "已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。"
                "已经订好的票请到官方平台自行退改——本系统不代退、不代改。"
            )
            task.messages.append(
                ConversationMessage(role="assistant", content=reply, created_at=self.clock())
            )
            self._audit(
                task,
                "CHANGE_INTENT_RESOLVED",
                message,
                {"kind": change.kind.value, "reason": change.reason, "trip_id": trip.trip_id},
            )
            return task

        if change.kind is ChangeKind.MEETING_MOVED:
            change_task = self.report_trip_event(
                trip.trip_id,
                event_type=TripEventType.MEETING_MOVED,
                leg_index=change.leg_index,
                new_arrive_by=change.new_arrive_by,
                note=change.reason,
                reported_by=reporter,
            )
        else:
            change_task = self.report_trip_event(
                trip.trip_id,
                event_type=TripEventType.FLIGHT_CHANGED,
                ref_id=change.ref_id,
                note=f"旅行者自述：{change.reason}",
                reported_by=reporter,
            )
        self._audit(
            task,
            "CHANGE_INTENT_RESOLVED",
            message,
            {
                "kind": change.kind.value,
                "leg_index": change.leg_index,
                "ref_id": change.ref_id,
                "new_arrive_by": (
                    change.new_arrive_by.isoformat() if change.new_arrive_by else None
                ),
                "date_evidence": change.date_evidence,
                "opened_task_id": change_task.task_id,
            },
        )
        # 这句话是改期任务的起因，带过去：它的聊天里第一条就该是旅行者说的这句。
        change_task.messages.append(
            ConversationMessage(role="user", content=message, created_at=now)
        )
        self._audit(
            change_task,
            "CHANGE_MESSAGE_CARRIED",
            {"from_task_id": task.task_id},
            {"kind": change.kind.value},
        )
        return change_task

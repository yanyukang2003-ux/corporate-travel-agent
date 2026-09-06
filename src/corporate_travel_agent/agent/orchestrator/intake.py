"""创建：结构化入口、工具循环入口、把循环终局落成任务状态、请求修订。

从 `TripWorkflowOrchestrator` 按职责拆出来的一块；方法原文逐字来自拆分前的 orchestrator.py。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from corporate_travel_agent.agent.orchestrator.core import (
    LanguageModelUnavailable,
    ToolBudgetExceeded,
    WorkflowError,
)
from corporate_travel_agent.agent.orchestrator.state import OrchestratorState
from corporate_travel_agent.agent.ports import LanguageModelError
from corporate_travel_agent.agent.tool_loop import ExchangeRecordingModel
from corporate_travel_agent.domain.enums import (
    ApprovalStatus,
    BookingScope,
    IntentEntrypoint,
    LodgingRequirement,
    TaskState,
    TripLegRole,
)
from corporate_travel_agent.domain.models import (
    ConversationMessage,
    EmployeeProfileSnapshot,
    PolicySnapshot,
    ScopedRequirement,
    TransportOffer,
    TripEvent,
    TripLeg,
    TripRequestVersion,
    TripStay,
    TripTask,
)
from corporate_travel_agent.domain.validation import validate_trip_request
from corporate_travel_agent.providers.base import ProviderError, TransportSearchQuery
from corporate_travel_agent.services.outbox_events import OutboxEventDraft
from corporate_travel_agent.services.provider_resilience import PROVIDER_RETRY_METADATA_KEY


def _json_safe_result(value: Any) -> Any:
    """工具结果原样落库前转成 JSON 安全形态（Decimal/日期转字符串，不丢字段）。"""
    import json as _json

    return _json.loads(_json.dumps(value, ensure_ascii=False, default=str))


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _empty_corridor_question(query: TransportSearchQuery) -> str:
    """一段搜空时对旅行者说的话：不编火车票，也不把整趟行程停掉。"""
    return (
        f"{query.origin}→{query.destination} 这段没有可用机票。"
        "短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，"
        "或者告诉我换一个日期再搜。"
    )


class IntakeMixin(OrchestratorState):
    """创建：结构化入口、工具循环入口、把循环终局落成任务状态、请求修订。

    混入 `TripWorkflowOrchestrator`；状态都在宿主实例上，这里只放方法。
    """

    def create_task(
        self,
        request: TripRequestVersion,
        *,
        requester_id: str | None = None,
        trip_id: str | None = None,
        parent_task_id: str | None = None,
        change_event: TripEvent | None = None,
    ) -> TripTask:
        """用结构化 TripRequest 建任务并立即搜索规划。

        不传 `trip_id` 就是一趟新差旅的第一个任务；`report_trip_event` 传进来时，
        这是挂在已有差旅下的改期任务（`parent_task_id` 指向被改的那个任务）。
        """
        request = self._canonicalize_request_cities(request)
        validate_trip_request(request).require_valid()
        employee = self.employees.snapshot(request.traveler_id)
        requester = self._requester_for(employee, requester_id)
        policy = self.policies.current()
        metadata: dict[str, Any] = {
            "policy_content_hash": policy.content_hash,
            "intent_entrypoint": IntentEntrypoint.STRUCTURED.value,
        }
        if change_event is not None:
            # 航司说这张票变了/没了，就别再端上来——排除的是那一张票，不是改库存。
            excluded = [change_event.ref_id] if change_event.ref_id else []
            metadata["change_event"] = {
                "event_id": change_event.event_id,
                "event_type": change_event.event_type.value,
                "ref_id": change_event.ref_id,
                "note": change_event.note,
                "new_depart_at": _iso_or_none(change_event.new_depart_at),
                "new_arrive_by": _iso_or_none(change_event.new_arrive_by),
                "excluded_refs": excluded,
                "leg_index": change_event.leg_index,
                "reported_by": change_event.reported_by,
                # watch worker 算出的影响（取消 / 赶不上 / 接不上）；手工报的事件没有。
                "impact": (
                    {
                        "verdict": change_event.impact.verdict.value,
                        "reasons": list(change_event.impact.reasons),
                        "delay_minutes": change_event.impact.delay_minutes,
                        "new_arrive_at": _iso_or_none(change_event.impact.new_arrive_at),
                        "latest_acceptable_arrival": _iso_or_none(
                            change_event.impact.latest_acceptable_arrival
                        ),
                        "buffer_minutes": change_event.impact.buffer_minutes,
                    }
                    if change_event.impact is not None
                    else None
                ),
            }
            metadata["excluded_refs"] = excluded
        task = TripTask(
            task_id=request.task_id,
            state=TaskState.DRAFT,
            request=request,
            employee=employee,
            requester_id=requester,
            policy_snapshot_id=policy.snapshot_id,
            tool_call_limit=self.max_tool_calls,
            metadata=metadata,
            trip_id=trip_id or str(uuid4()),
            parent_task_id=parent_task_id,
            change_event_id=change_event.event_id if change_event is not None else None,
        )
        self._add_task_with_trip(task, change_event=change_event)
        outbox: tuple[OutboxEventDraft, ...] = ()
        if change_event is not None:
            outbox = (
                OutboxEventDraft(
                    event_type="TRIP_CHANGE_REQUESTED",
                    payload={
                        "trip_id": task.trip_id,
                        "task_id": task.task_id,
                        "parent_task_id": parent_task_id,
                        "employee_id": employee.employee_id,
                        "event_type": change_event.event_type.value,
                        "ref_id": change_event.ref_id,
                        "note": change_event.note,
                    },
                ),
            )
        self.recorder.audit(
            task,
            "TASK_CREATED",
            request,
            {
                "state": task.state.value,
                "requester_id": task.requested_by,
                "trip_id": task.trip_id,
                "parent_task_id": parent_task_id,
                "change_event_id": task.change_event_id,
            },
            outbox=outbox,
        )
        return self._search_and_plan(task, policy)

    # ------------------------------------------------------------------
    # 工具循环入口（ADR-0002 的并行迁移方式：新入口另起一条，旧的一个字不动）
    # ------------------------------------------------------------------

    def create_task_from_agentic_message(
        self,
        message: str,
        *,
        traveler_id: str,
        task_id: str | None = None,
        requester_id: str | None = None,
    ) -> TripTask:
        """用工具循环入口创建任务。

        和语义入口的差别只有一条，但那一条是结构性的：**没有全局必填表。**
        模型每轮挑一个带类型的工具，够不够往下走由那个工具自己的签名决定。
        """
        message = self._validate_message(message)
        if self.tool_calling_language_model is None:
            raise LanguageModelUnavailable("No tool-calling language model adapter is configured")
        employee = self.employees.snapshot(traveler_id)
        requester = self._requester_for(employee, requester_id)
        policy = self.policies.current()
        task = TripTask(
            task_id=task_id or str(uuid4()),
            state=TaskState.DRAFT,
            request=None,
            employee=employee,
            requester_id=requester,
            policy_snapshot_id=policy.snapshot_id,
            intent_fields=self._empty_intent_fields(),
            # 用任务时钟盖戳：其余每条记录（工具调用、搜索、审计）都用它，
            # 对话不能例外，否则冻结时钟的评测里"过程记录"会把原话排到动作后面。
            messages=[ConversationMessage(role="user", content=message, created_at=self.clock())],
            tool_call_limit=self.agentic_tool_call_limit,
            metadata={
                "policy_content_hash": policy.content_hash,
                "intent_entrypoint": IntentEntrypoint.AGENTIC.value,
            },
            trip_id=str(uuid4()),
        )
        self._add_task_with_trip(task)
        self.recorder.audit(
            task,
            "AGENTIC_TASK_CREATED_FROM_MESSAGE",
            message,
            {"state": task.state.value, "requester_id": task.requested_by},
        )
        return self._run_tool_loop(task)

    def submit_agentic_message(self, task_id: str, message: str) -> TripTask:
        """向工具循环任务追加消息；整段对话重跑一次循环。

        不做"补一格字段"那种增量修补——旧链路那样做会把上一轮的错误一起带下来。
        """
        message = self._validate_message(message)
        task = self.tasks.get(task_id)
        self._require_intent_entrypoint(task, IntentEntrypoint.AGENTIC)
        if self.tool_calling_language_model is None:
            raise LanguageModelUnavailable("No tool-calling language model adapter is configured")
        allowed = {
            TaskState.NEEDS_CLARIFICATION,
            TaskState.NEEDS_STRUCTURED_INPUT,
            TaskState.WAITING_FOR_USER,
            TaskState.NO_FEASIBLE_OPTION,
            TaskState.PROVIDER_FAILED,
            TaskState.WAITING_FOR_PROVIDER,
            TaskState.OUT_OF_SCOPE,
        }
        if task.state not in allowed:
            raise WorkflowError(f"Cannot submit agentic message in {task.state.value}")
        prior_state = task.state
        task.failure = None
        task.clarification_question = None
        if prior_state is TaskState.NEEDS_CLARIFICATION:
            self.recorder.audit(
                task, "AGENTIC_CLARIFICATION_RECEIVED", message, task.clarification_rounds
            )
        else:
            task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
            self.recorder.audit(
                task,
                "AGENTIC_REVISION_RECEIVED",
                message,
                {"from_state": prior_state.value},
            )
        self.recorder.transition(task, TaskState.DRAFT)
        task.messages.append(
            ConversationMessage(role="user", content=message, created_at=self.clock())
        )
        return self._run_tool_loop(task)

    def _run_tool_loop(self, task: TripTask) -> TripTask:
        """跑一轮有界工具循环，把它的终局动作落成任务状态。

        循环里的**每一次**调用（含每轮选工具的模型调用）都走 `_invoke_tool`：
        工具预算、审计轨迹、有界重试、并发闸和其余两条入口共用同一套，不另起炉灶。
        """
        from corporate_travel_agent.agent.semantic_intent import ConversationLedger
        from corporate_travel_agent.agent.tool_loop import (
            ToolExecutor,
            ToolLoopAborted,
            ToolLoopRunner,
        )

        policy = self._policy_for(task)
        ledger = ConversationLedger.from_messages(task.messages)
        executor = ToolExecutor(
            provider=self.provider,
            # 和规划器**同一个**政策引擎实例：政策结论只有一个来源。
            policy_engine=self.planner.policy_engine,
            employee=task.employee,
            policy=policy,
            city_normalizer=self.city_normalizer,
            now=self.clock(),
            fallback_timezone=self.timezone_name,
            # 每一段的日期都要能在这段原文里逐字找到出处，编的对不上。
            conversation=ledger.render(),
        )
        model = self.tool_calling_language_model
        if model is None:
            raise LanguageModelUnavailable("No tool-calling language model adapter is configured")
        runner = ToolLoopRunner(
            model=model,
            executor=executor,
            # 一轮最多花 2 次预算（选工具 1 次 + 执行工具 1 次），所以轮数上限要按
            # 剩余预算的一半算。此前直接用 max_tool_calls，循环永远是被预算掐断的，
            # "最后一轮只给终局工具"那道保险根本轮不到生效。
            max_iterations=max(1, task.tool_calls_remaining // 2),
            invoke=lambda *, tool_name, tool_kind, operation: self._invoke_tool(
                task,
                tool_name=tool_name,
                tool_kind=tool_kind,
                input_value={"task_id": task.task_id, "turns": len(ledger.turns)},
                operation=operation,
            ),
        )
        context = {
            "reference_time": self.clock().isoformat(),
            "timezone": self.timezone_name,
            "clarification_round": task.clarification_rounds,
            "max_clarification_rounds": self.max_clarification_rounds,
        }
        # 函数级过程记录：这一轮循环发生的每一件事（对话装配原文、每次发给模型的
        # 完整报文和返回、每次工具调用的参数和结果、终局）都要落到任务上。
        # 适配器的 exchanges 是跨任务累计的，先记下起点，结束后只切走本轮的那一段。
        exchanges_before = len(model.exchanges) if isinstance(model, ExchangeRecordingModel) else 0
        run_started_at = self.clock()
        # 循环有自己的格子：AGENT_RUNNING（ADR-0009）。理解和搜索在里面交织进行，收场时按
        # 终局动作迁出——问、越界、空库存、失败各有各的出边；搜到了东西才进 PLANNING。
        # 此前循环期间留在 DRAFT，事后补 SEARCHING → PLANNING 两次迁移只为让边合法。
        self.recorder.transition(task, TaskState.AGENT_RUNNING)
        try:
            outcome = runner.run(ledger.render(), context=context)
        except ToolBudgetExceeded:
            # 预算耗尽时**把已经查到的事实带出来**。真模型实测会在一条没货的航线上
            # 反复搜到预算见底；只回一句"预算用完了"，用户根本不知道发生过什么。
            #
            # 状态仍然是 TOOL_BUDGET_EXHAUSTED——预算确实用光了，这是运维要看见的
            # 事实，不该被包装成"没有方案"。但话要说清楚。
            self._capture_process_log(
                task,
                conversation=ledger.render(),
                context=context,
                started_at=run_started_at,
                exchanges_before=exchanges_before,
                error="ToolBudgetExceeded: 工具预算耗尽",
            )
            findings = self._loop_findings(executor)
            task.metadata["agentic_partial_findings"] = findings
            task = self._stop_for_tool_budget(task, "agentic.tool_loop")
            empty = [
                f"{item['origin']}→{item['destination']}"
                for item in findings["searched_legs"]
                if item["option_count"] == 0
            ]
            if empty:
                task.failure = (
                    f"{task.failure}。已经查明：{'、'.join(dict.fromkeys(empty))} "
                    "在试过的时间窗里都没有库存。"
                )
            return task
        except ToolLoopAborted as exc:
            # 循环没收敛不是"没方案"，是这条链路没走完——别把它伪装成结论。
            self._capture_process_log(
                task,
                conversation=ledger.render(),
                context=context,
                started_at=run_started_at,
                exchanges_before=exchanges_before,
                transcript=exc.transcript,
                error=f"ToolLoopAborted: {exc}",
            )
            task.metadata["agentic_partial_findings"] = self._loop_findings(executor)
            task.metadata["agentic_transcript"] = [
                {
                    "tool": exchange.invocation.name,
                    "arguments": dict(exchange.invocation.arguments),
                    "ok": exchange.ok,
                    "error": None if exchange.ok else str(exchange.result.get("error", ""))[:300],
                }
                for exchange in exc.transcript
            ]
            task.metadata["agentic_final_action"] = None
            task.failure = str(exc)
            task.clarification_question = None
            self.recorder.transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self.recorder.audit(
                task, "AGENTIC_LOOP_ABORTED", {"turns": len(ledger.turns)}, task.failure
            )
            return task
        except LanguageModelError as exc:
            self._capture_process_log(
                task,
                conversation=ledger.render(),
                context=context,
                started_at=run_started_at,
                exchanges_before=exchanges_before,
                error=f"{type(exc).__name__}: {exc}",
            )
            task.failure = str(exc)
            task.metadata["agentic_loop_failure"] = exc.trace_details()
            task.clarification_question = None
            self.recorder.transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self.recorder.audit(
                task,
                "AGENTIC_LOOP_FAILED",
                {"turns": len(ledger.turns)},
                task.metadata["agentic_loop_failure"],
            )
            return task
        except ProviderError as exc:
            self._capture_process_log(
                task,
                conversation=ledger.render(),
                context=context,
                started_at=run_started_at,
                exchanges_before=exchanges_before,
                error=f"{type(exc).__name__}: {exc}",
            )
            task.failure = str(exc)
            task.options = []
            self.recorder.transition(task, TaskState.PROVIDER_FAILED)
            self.recorder.audit(
                task, "AGENTIC_PROVIDER_FAILED", {"turns": len(ledger.turns)}, task.failure
            )
            return task

        self._note_llm_success()
        self._capture_process_log(
            task,
            conversation=ledger.render(),
            context=context,
            started_at=run_started_at,
            exchanges_before=exchanges_before,
            transcript=outcome.transcript,
            outcome=outcome,
        )
        # 宿主替旅行者定下来的事必须当面说出口。循环里唯一这样的推导就是
        # "只说了几点前到，搜索窗口从时限往前扩，覆盖前一晚出发"。
        task.assumptions = tuple(dict.fromkeys(executor.assumptions))
        task.metadata["agentic_transcript"] = [
            {
                "tool": exchange.invocation.name,
                "arguments": dict(exchange.invocation.arguments),
                "ok": exchange.ok,
            }
            for exchange in outcome.transcript
        ]
        # 出口工具（问 / 交付）不进 transcript；评测比"三轮是不是做了同样的事"要知道
        # 这一轮最后选的是哪个出口，单独记一个只读字段。不喂回模型，不进公开视图。
        task.metadata["agentic_final_action"] = outcome.kind
        if outcome.kind == "ask_traveler":
            if outcome.out_of_scope and not executor.leg_searches and not executor.stay_searches:
                return self._stop_for_out_of_scope(task, outcome.question)
            if executor.leg_searches and not executor.seen_transport:
                # 真的去搜了、每一段都是空的、模型于是开口问：这不是"还没问清"，
                # 是"没有可行方案"。状态机本来就有这个格子，用它——前端会摆出原因和
                # "重新规划"，也不占澄清轮数。问题原文留着，那是给旅行者看的解释。
                return self._stop_for_empty_inventory(task, executor, outcome.question)
            return self._pause_for_agentic_question(task, outcome.question)
        return self._plan_from_tool_loop(task, executor, outcome, policy)

    def _capture_process_log(
        self,
        task: TripTask,
        *,
        conversation: str,
        context: dict[str, Any],
        started_at: Any,
        exchanges_before: int,
        transcript: tuple[Any, ...] | None = None,
        outcome: Any = None,
        error: str | None = None,
    ) -> None:
        """把这一轮循环的函数级过程记录落到任务上（`metadata["process_log"]`）。

        内容全是**当场发生过的原文**：喂给循环的对话装配、每次发给模型的完整报文和
        返回（从适配器的 `exchanges` 切本轮那一段）、每次工具调用的参数与完整结果、
        终局动作或错误。预算耗尽 / 模型或供应商故障时拿不到工具往返（异常没带出来），
        记 null 并说明，不假装有。

        体量上限心里有数：报文按轮重复对话与工具结果，演示规模每轮几十 KB；
        生产要落库前应换成按哈希去重存储——这里先把"记全"做对。
        """
        adapter = self.tool_calling_language_model
        exchanges = (
            list(adapter.exchanges)[exchanges_before:]
            if isinstance(adapter, ExchangeRecordingModel)
            else []
        )
        run: dict[str, Any] = {
            "run": len(task.metadata.get("process_log") or ()) + 1,
            "entry_function": (
                "TripWorkflowOrchestrator.create_task_from_agentic_message"
                if len([m for m in task.messages if m.role == "user"]) <= 1
                else "TripWorkflowOrchestrator.submit_agentic_message"
            ),
            "started_at": started_at.isoformat(),
            "finished_at": self.clock().isoformat(),
            "conversation_function": "ConversationLedger.render",
            "conversation": conversation,
            "context": dict(context),
            "prompt_version": getattr(adapter, "prompt_version", None),
            "llm_exchanges": exchanges,
            "tool_exchanges": (
                None
                if transcript is None
                else [
                    {
                        "function": f"ToolExecutor.{exchange.invocation.name}",
                        "tool": exchange.invocation.name,
                        "arguments": dict(exchange.invocation.arguments),
                        "ok": exchange.ok,
                        "result": _json_safe_result(exchange.result),
                    }
                    for exchange in transcript
                ]
            ),
            "tool_exchanges_note": (
                "这条路径的异常没有携带工具往返记录（预算耗尽/传输故障），如实记 null"
                if transcript is None
                else None
            ),
            "outcome": (
                {
                    "kind": outcome.kind,
                    "summary": outcome.summary,
                    "question": outcome.question,
                    "open_questions": list(outcome.open_questions),
                    "transport_refs": list(outcome.transport_refs),
                    "hotel_refs": list(outcome.hotel_refs),
                }
                if outcome is not None
                else None
            ),
            "error": error,
        }
        log = list(task.metadata.get("process_log") or ())
        log.append(run)
        task.metadata["process_log"] = log

    @staticmethod
    def _loop_findings(executor: Any) -> dict[str, Any]:
        """循环没走完时，把它已经查到的事实留下来，别让证据跟着失败一起消失。"""
        return {
            "searched_legs": [
                {
                    "origin": query.origin,
                    "destination": query.destination,
                    "depart_after": query.depart_after.isoformat(),
                    "arrive_before": (
                        query.arrive_before.isoformat() if query.arrive_before else None
                    ),
                    "option_count": len(snapshot.items),
                }
                for query, snapshot in executor.leg_searches
            ],
            "searched_stays": [
                {
                    "city": query.city,
                    "check_in": query.check_in.isoformat(),
                    "check_out": query.check_out.isoformat(),
                    "option_count": len(snapshot.items),
                }
                for query, snapshot in executor.stay_searches
            ],
            "assumptions": list(dict.fromkeys(executor.assumptions)),
        }

    def _stop_for_out_of_scope(self, task: TripTask, question: str | None) -> TripTask:
        """模型说这根本不是差旅请求：和另外两条入口一样落成 OUT_OF_SCOPE。

        这条状态是可以重开的（`submit_agentic_message` 接受它）——判错了，旅行者
        再说一句话就回到 DRAFT。它不占澄清轮数：越界不是"没问清"。
        """
        task.request = None
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.failure = "The request is outside the corporate travel planning scope"
        task.clarification_question = question
        if question:
            task.messages.append(ConversationMessage(role="assistant", content=question))
        self.recorder.transition(task, TaskState.OUT_OF_SCOPE)
        self.recorder.audit(task, "AGENTIC_OUT_OF_SCOPE", None, question)
        return task

    def _stop_for_empty_inventory(
        self, task: TripTask, executor: Any, question: str | None
    ) -> TripTask:
        """循环搜过、每一段都空、模型开口问：落成 NO_FEASIBLE_OPTION，而不是一轮澄清。

        搜索的出处和快照照记——"我们搜过这条航线、结果是空的"本身就是证据。
        """
        empty = [
            f"{query.origin}→{query.destination}" for query, _ in executor.leg_searches
        ]
        reasons = tuple(
            dict.fromkeys(
                [
                    *(
                        f"no inventory matched {route} in the requested time window"
                        for route in empty
                    ),
                    *([question] if question else []),
                ]
            )
        )
        self.recorder.record_searches(task, executor.searches)
        self._audit_snapshots(task, list(executor.captured_snapshots))
        task.request = None
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.metadata["no_feasible_reasons"] = reasons
        task.failure = "; ".join(reasons)
        task.clarification_question = question
        if question:
            task.messages.append(ConversationMessage(role="assistant", content=question))
        self.recorder.transition(task, TaskState.NO_FEASIBLE_OPTION)
        self.recorder.audit(task, "NO_FEASIBLE_OPTION", {"searched_legs": empty}, reasons)
        return task

    def _pause_for_agentic_question(self, task: TripTask, question: str | None) -> TripTask:
        """模型主动调用 `ask_traveler` 收的场。

        **和旧链路的追问不是同一种东西。** 旧的是编译失败之后按字段名查表拼出来的；
        这里提问是一个模型可以主动选的动作，所以它能引用已经搜到的真实选项。
        """
        task.request = None
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.failure = None
        task.clarification_rounds += 1
        if task.clarification_rounds > self.max_clarification_rounds:
            task.clarification_question = None
            task.failure = (
                "Agentic clarification limit reached; use the structured form"
            )
            self.recorder.transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self.recorder.audit(task, "AGENTIC_CLARIFICATION_EXHAUSTED", None, task.failure)
            return task
        task.clarification_question = question or "请确认我对这次出行的理解。"
        task.messages.append(
            ConversationMessage(role="assistant", content=task.clarification_question)
        )
        self.recorder.transition(task, TaskState.NEEDS_CLARIFICATION)
        self.recorder.audit(
            task, "AGENTIC_CLARIFICATION_REQUESTED", None, task.clarification_question
        )
        return task

    def _plan_from_tool_loop(
        self,
        task: TripTask,
        executor: Any,
        outcome: Any,
        policy: PolicySnapshot,
    ) -> TripTask:
        """把循环真实搜到的库存交给现成的规划器，**不再搜一遍**。

        这里构造的 `TripRequestVersion` 是**事后记录**，不是事前关卡：模型已经决定
        并且已经搜过了，这个结构体只是把"它实际做了什么"写下来，好让规划器、政策
        引擎和后续的预订/审批链路照常工作。旧链路那张表的问题从来不是"存在一个结构
        体"，而是"填不满就不许往下走"。
        """
        settled, empty_queries = self._settled_leg_searches(executor)
        if not settled:
            task.failure = "the tool loop proposed options without searching any transport leg"
            self.recorder.transition(task, TaskState.NEEDS_STRUCTURED_INPUT)
            self.recorder.audit(task, "AGENTIC_PROPOSAL_WITHOUT_SEARCH", None, task.failure)
            return task

        leg_snapshots = [snapshot for _, snapshot in settled]
        hotel_snapshots = [snapshot for _, snapshot in executor.stay_searches]
        # 搜索出处从执行器抄到任务上。**抄全部，不只抄用上的那几次**：
        # "我们还搜过这条航线、结果是空的"本身就是溯源的一部分。
        self.recorder.record_searches(task, executor.searches)
        invalid = self._invalid_snapshot_ids([*leg_snapshots, *hotel_snapshots])
        if invalid:
            task.failure = "provider returned expired or invalid inventory snapshots: " + ", ".join(
                invalid
            )
            self.recorder.transition(task, TaskState.PROVIDER_FAILED)
            self.recorder.audit(task, "INVENTORY_SNAPSHOT_REJECTED", tuple(invalid), task.failure)
            return task

        version = task.request.version + 1 if task.request is not None else 1
        task.request = self._request_from_tool_loop(
            task,
            settled,
            executor.stay_searches,
            version=version,
            hard_constraints=outcome.hard_constraints,
            soft_preferences=outcome.soft_preferences,
        )
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.clarification_question = None
        task.metadata.pop("agentic_open_questions", None)
        task.failure = None
        task.selected_option_id = None
        task.booking_intent = None
        task.approval = None
        self.recorder.audit(task, "SEARCH_COMMAND_COMPILED", outcome.summary, task.request)
        self._record_coverage_notices(task, [*leg_snapshots, *hotel_snapshots])
        # 存档认**每一次真实搜索**，不认跨日合并出来的那个：合并快照的原始响应
        # 只覆盖第一天，却装着后面几天的报价，按哈希核对不上。见 `search_transport`。
        self._audit_snapshots(task, list(executor.captured_snapshots))
        self.recorder.transition(task, TaskState.PLANNING)

        task.options = self.planner.plan(
            request=task.request,
            employee=task.employee,
            policy=policy,
            leg_offers=[
                self._without_excluded(task, self._transports(item)) for item in leg_snapshots
            ],
            hotel_offers=[self._hotels(item) for item in hotel_snapshots],
            profile=self._travel_profile(task),
            budget=self._budget_snapshot(task),
            now=self.clock(),
        )
        if not task.options:
            # **模型说"这几条可以"不算数。** 政策与可行性由确定性代码判，判不过就是没有。
            reasons = self._no_feasible_reasons(
                task.request, leg_snapshots, hotel_snapshots, policy=policy
            )
            task.metadata["no_feasible_reasons"] = reasons
            task.failure = "; ".join(reasons)
            self.recorder.transition(task, TaskState.NO_FEASIBLE_OPTION)
            self.recorder.audit(task, "NO_FEASIBLE_OPTION", task.request.version, reasons)
            return task

        gap_questions = tuple(
            _empty_corridor_question(query) for query in empty_queries
        )
        open_questions = tuple(
            dict.fromkeys((*outcome.open_questions, *gap_questions))
        )
        if gap_questions:
            # 空段说明写进每张方案的摘要。只放在追问里的话，看方案的人
            # 会以为北京→上海这几张票就是全程，上海→杭州被静默丢掉了。
            task.options = [
                replace(
                    option,
                    explanation_facts=(*gap_questions, *option.explanation_facts),
                )
                for option in task.options
            ]
        task.metadata["agentic_proposal"] = {
            "summary": outcome.summary,
            "transport_refs": list(outcome.transport_refs),
            "hotel_refs": list(outcome.hotel_refs),
            "open_questions": list(open_questions),
        }
        # **方案和未决问题可以同时存在。** 三段行程里两段的日期写在原话里、一段没写，
        # 正确做法是把能定的两段排出来、只问剩下那一段——不是整轮停住什么都不给。
        # 任务因此停在 WAITING_FOR_USER：方案可看可选，问题也摆在那里等回答。
        if open_questions:
            task.clarification_question = "\n".join(open_questions)
            task.messages.append(
                ConversationMessage(role="assistant", content=task.clarification_question)
            )
            task.metadata["agentic_open_questions"] = list(open_questions)
            self.recorder.audit(
                task,
                "AGENTIC_PARTIAL_ITINERARY",
                {"legs_settled": len(settled)},
                list(open_questions),
            )
        self.recorder.transition(task, TaskState.OPTIONS_READY)
        self.recorder.audit(
            task,
            "OPTIONS_VERIFIED",
            task.request.version,
            [item.option_id for item in task.options],
            tuple(ref for item in task.options for ref in item.inventory_refs),
        )
        self.recorder.transition(task, TaskState.WAITING_FOR_USER)
        return task

    @staticmethod
    def _settled_leg_searches(executor: Any) -> tuple[list[Any], list[Any]]:
        """有货的段拿去规划；没货的段变成未决问题，不让整单一起死。"""
        collapsed: dict[tuple[str, str], Any] = {}
        order: list[tuple[str, str]] = []
        for query, snapshot in executor.leg_searches:
            key = (query.origin, query.destination)
            if key not in collapsed:
                order.append(key)
                collapsed[key] = (query, snapshot)
                continue
            previous_query, previous_snapshot = collapsed[key]
            previous_count = sum(
                1 for item in previous_snapshot.items if isinstance(item, TransportOffer)
            )
            current_count = sum(
                1 for item in snapshot.items if isinstance(item, TransportOffer)
            )
            if current_count >= previous_count:
                collapsed[key] = (query, snapshot)
        settled: list[Any] = []
        empty: list[Any] = []
        for key in order:
            query, snapshot = collapsed[key]
            if any(isinstance(item, TransportOffer) for item in snapshot.items):
                settled.append((query, snapshot))
            else:
                empty.append(query)
        return settled, empty

    def _request_from_tool_loop(
        self,
        task: TripTask,
        settled: Sequence[Any],
        stay_searches: Sequence[Any],
        *,
        version: int,
        hard_constraints: Sequence[str] = (),
        soft_preferences: Sequence[str] = (),
    ) -> TripRequestVersion:
        """把循环实际搜到货的段与站写成一个请求版本。

        **段数是数出来的**：数有库存的段，空搜不进规划器。

        硬要求和偏好来自交付时的声明（`propose_options.hard_constraints /
        soft_preferences`）。此前这里一个都不带——"只要直飞"到了规划器就没了，
        "优先高铁"也不参与排序，模型在提示词里被告知的那两张词表根本没有出口。
        声明按"管全程"落到请求上；分段作用域工具循环今天表达不了，不假装。
        """
        legs = tuple(
            TripLeg(
                # 第一段是去程，其余按顺序算返程/续程；角色只影响文案，不影响搜索。
                role=TripLegRole.OUTBOUND if index == 0 else TripLegRole.RETURN,
                origin=query.origin,
                destination=query.destination,
                depart_after=query.depart_after,
                arrive_before=query.arrive_before,
            )
            for index, (query, _) in enumerate(settled)
        )
        stays = tuple(
            TripStay(city=query.city, check_in=query.check_in, check_out=query.check_out)
            for query, _ in stay_searches
        )
        hard = tuple(dict.fromkeys(hard_constraints))
        soft = tuple(dict.fromkeys(soft_preferences))
        return TripRequestVersion(
            task_id=task.task_id,
            version=version,
            traveler_id=task.employee.employee_id,
            journey=legs,
            stays=stays,
            scoped_hard_constraints=tuple(ScopedRequirement(name=item) for item in hard),
            scoped_soft_preferences=tuple(ScopedRequirement(name=item) for item in soft),
            booking_scope=(
                BookingScope.ROUND_TRIP if len(legs) > 1 else BookingScope.OUTBOUND_ONLY
            ),
            created_at=self.clock(),
        )

    def complete_with_structured_request(
        self, task_id: str, request: TripRequestVersion
    ) -> TripTask:
        """在缺字段状态下提交完整结构化请求并继续搜索。"""
        task = self.tasks.get(task_id)
        if task.state not in {
            TaskState.NEEDS_CLARIFICATION,
            TaskState.NEEDS_STRUCTURED_INPUT,
        }:
            raise WorkflowError(f"Cannot complete a structured request in {task.state.value}")
        if request.task_id != task.task_id or request.traveler_id != task.employee.employee_id:
            raise WorkflowError("Structured request identity does not match the draft task")
        request = self._canonicalize_request_cities(request)
        validate_trip_request(request).require_valid()
        self.recorder.transition(task, TaskState.DRAFT)
        task.request = request
        task.missing_required_fields = ()
        task.intent_conflicts = ()
        task.clarification_question = None
        task.failure = None
        self.recorder.audit(task, "STRUCTURED_FALLBACK_SUBMITTED", request, request.version)
        return self._search_and_plan(task, self._policy_for(task))

    @staticmethod
    def _empty_intent_fields() -> dict[str, Any]:
        """返回空意图字段模板。"""
        return {
            "origin": None,
            "destination": None,
            "departure_after": None,
            "arrive_by": None,
            "return_after": None,
            "return_before": None,
            "hotel_check_in": None,
            "hotel_check_out": None,
            "client_location": None,
            "booking_scope": None,
            "lodging_requirement": LodgingRequirement.UNSPECIFIED.value,
            "hard_constraints": [],
            "soft_preferences": [],
        }

    @staticmethod
    def _timezone_aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    @staticmethod
    def _require_intent_entrypoint(
        task: TripTask, expected: IntentEntrypoint
    ) -> None:
        actual = task.metadata.get("intent_entrypoint")
        # Persisted tasks created before entrypoint separation are legacy-compatible.
        if actual is None and expected is IntentEntrypoint.LEGACY:
            return
        if actual != expected.value:
            raise WorkflowError(
                f"Task uses {actual or 'unknown'} intent entrypoint; "
                f"continue it through the {expected.value} entrypoint"
            )

    def _canonicalize_request_cities(self, request: TripRequestVersion) -> TripRequestVersion:
        """规范化请求中的城市名。

        **航段与住宿站的城市也要规范化。** 它们和 `origin` / `destination` 一样
        会被原样送去 Provider 查询——只规范化扁平字段的话，多城行程里
        "去上海、再去东京"的后两段会拿着没规范化的名字去搜。
        """
        canonical = self.city_normalizer.canonicalize
        return replace(
            request,
            journey=tuple(
                replace(
                    leg,
                    origin=canonical(leg.origin),
                    destination=canonical(leg.destination),
                )
                for leg in request.journey
            ),
            stays=tuple(
                replace(stay, city=canonical(stay.city)) for stay in request.stays
            ),
        )

    @staticmethod
    def _validate_message(message: str) -> str:
        """校验用户消息非空等基本约束。"""
        normalized = message.strip()
        if not normalized:
            raise WorkflowError("Message cannot be empty")
        if len(normalized) > 4000:
            raise WorkflowError("Message exceeds the 4000 character limit")
        return normalized

    def _requester_for(
        self, traveler: EmployeeProfileSnapshot, requester_id: str | None
    ) -> str:
        """定下这趟任务的发起人，并把"能不能替他订"挡在这里。

        - 没给发起人（旧调用方、内部脚本）：就是旅行者本人。
        - 发起人是本系统认识的员工：要么是本人，要么在旅行者的委托名单上，否则拒绝。
          这是防御纵深——API 层已经按身份查过一次，编排器不信它。
        - 发起人不是员工（管理员、系统身份）：照记不拦。他们能不能建任务由角色管，
          不由委托名单管。
        """
        if requester_id is None:
            return traveler.employee_id
        if self.employees.knows(requester_id) and not self.employees.may_book_for(
            requester_id, traveler.employee_id
        ):
            raise WorkflowError(
                f"{requester_id} is not allowed to book on behalf of {traveler.employee_id}"
            )
        return requester_id

    def revise_request(self, task_id: str, request: TripRequestVersion) -> TripTask:
        """用新版结构化请求替换并重新搜索。"""
        task = self.tasks.get(task_id)
        request = self._canonicalize_request_cities(request)
        validate_trip_request(request).require_valid()
        current_request = self._request(task)
        if request.task_id != task.task_id or request.version <= current_request.version:
            raise WorkflowError("A revision must keep task_id and increase request version")
        if task.state not in {
            TaskState.WAITING_FOR_USER,
            TaskState.WAITING_FOR_APPROVAL,
            TaskState.PROVIDER_FAILED,
            TaskState.NO_FEASIBLE_OPTION,
            TaskState.RECONFIRMATION_REQUIRED,
            TaskState.WAITING_FOR_PROVIDER,
        }:
            raise WorkflowError(f"Cannot revise task in {task.state.value}")
        old_version = current_request.version
        if task.approval:
            task.approval.status = ApprovalStatus.INVALIDATED
            self.recorder.audit(
                task, "APPROVAL_INVALIDATED", task.approval.subject_hash, request.version
            )
        task.request = request
        task.options = []
        task.selected_option_id = None
        task.approval = None
        task.booking_intent = None
        task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
        self.recorder.audit(task, "REQUEST_REVISED", old_version, request.version)
        return self._search_and_plan(task, self._policy_for(task))

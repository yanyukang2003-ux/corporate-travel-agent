"""确定性工具循环替身：评测 harness 用的离线 ``ToolCallingLanguageModelPort``。

它存在的唯一理由，是让**产品入口**（`/agentic/trip-tasks`，工具循环）能在不计费、
不联网的前提下逐条跑冻结评测集——此前冻结集只经过 legacy 和 semantic 两条入口，
CI 守的是产品已经不用的门。

对齐原则（与 ADR-0002 / ADR-0003 的 removal gate 对应）：

1. **理解能力完全复用** ``DeterministicSemanticInterpreter`` 和 ``compile_search_command``。
   三条入口拿到的"模型能力"因此相同，观测到的差异只能来自架构——工具循环 vs 一次性
   编译——而不是来自解析器强弱。
2. **绝不猜测**：编译不出来就 ``ask_traveler``；一段的日期在对话里找不到出处就不搜那一段。
3. **每轮只做真模型会做的事**：第一轮按编译结果发搜索（每段一次、每站一次），第二轮把
   真实搜到的引用交出去（``propose_options``），搜空了就开口问。不重试、不换窗——
   这是替身，不是策略。

它不是产品能力，只在评测里用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

from .deterministic_semantic_interpreter import (
    DeterministicSemanticInterpreter,
    _reference_time,
)
from .search_command import SearchCommandCompilation, compile_search_command
from .semantic_intent import IntentDecision, IntentDecisionStatus
from .tool_loop import ModelTurn, ToolExchange, ToolInvocation, ToolSpec

TOOL_LOOP_STANDIN_VERSION = "deterministic-tool-loop-v1"

#: 请求不在差旅范围内时对旅行者说的话。工具循环没有"越界"这个终局动作——
#: 真模型也只能开口说；替身照做。
OUT_OF_SCOPE_QUESTION = "这个请求不在差旅规划的范围内。你需要安排一趟出差吗？"

_SEARCH_TOOLS = frozenset({"search_transport", "search_hotels"})


class DeterministicToolCallingModel:
    """离线工具循环替身；实现 ``ToolCallingLanguageModelPort``。"""

    prompt_version = TOOL_LOOP_STANDIN_VERSION

    def __init__(
        self,
        *,
        city_normalizer: CityNormalizer | None = None,
        model_name: str = "deterministic-tool-calling-model",
    ) -> None:
        self.model_name = model_name
        self.calls = 0
        self._interpreter = DeterministicSemanticInterpreter()
        self._city_normalizer = city_normalizer or CityNormalizer(
            load_policy_configuration().city_aliases
        )

    def next_turn(
        self,
        *,
        conversation: str,
        transcript: Sequence[ToolExchange],
        tools: Sequence[ToolSpec],
        context: Mapping[str, Any],
    ) -> ModelTurn:
        self.calls += 1
        offered = {spec.name for spec in tools}
        searched = [item for item in transcript if item.invocation.name in _SEARCH_TOOLS]
        if not searched:
            return self._open(conversation, dict(context), offered)
        return self._close(conversation, transcript, dict(context), offered)

    # -- 第一轮：理解 → 编译 → 搜索 -----------------------------------------

    def _open(
        self, conversation: str, context: dict[str, Any], offered: set[str]
    ) -> ModelTurn:
        decision, compiled = self._understand(conversation, context)
        if decision.status is IntentDecisionStatus.OUT_OF_SCOPE:
            return ModelTurn(
                calls=(
                    ToolInvocation(
                        "ask_traveler",
                        {"question": OUT_OF_SCOPE_QUESTION, "out_of_scope": True},
                    ),
                )
            )
        if not compiled.ready:
            return _ask(compiled.clarification_question or "请确认我对这次出行的理解。")
        assert compiled.command is not None
        request = compiled.command.request
        if "search_transport" not in offered:
            # 最后一轮只剩终局工具，而我们还什么都没搜：只能开口。
            return _ask("工具预算不够完成搜索，请把行程要求再说一遍。")

        calls: list[ToolInvocation] = []
        for index, leg in enumerate(request.transport_legs()):
            quote = _date_quote(decision, index)
            if quote is None:
                # 这一段的日期在对话里找不到原话出处：不搜。第一段搜不了就没什么可交付，开口问。
                if index == 0:
                    return _ask(
                        compiled.clarification_question or "这趟行程哪天出发、最晚什么时候要到？"
                    )
                continue
            calls.append(
                ToolInvocation(
                    "search_transport",
                    {
                        "origin": leg.origin,
                        "destination": leg.destination,
                        "arrive_by": leg.arrive_before.isoformat(),
                        "depart_after": leg.depart_after.isoformat(),
                        "date_evidence": quote,
                    },
                )
            )
        if "search_hotels" in offered:
            for stay in request.lodging_stays():
                calls.append(
                    ToolInvocation(
                        "search_hotels",
                        {
                            "city": stay.city,
                            "check_in": stay.check_in.isoformat(),
                            "check_out": stay.check_out.isoformat(),
                        },
                    )
                )
        return ModelTurn(calls=tuple(calls))

    # -- 第二轮：把真实搜到的交出去 --------------------------------------------

    def _close(
        self,
        conversation: str,
        transcript: Sequence[ToolExchange],
        context: dict[str, Any],
        offered: set[str],
    ) -> ModelTurn:
        transport_refs: list[str] = []
        hotel_refs: list[str] = []
        empty_routes: list[str] = []
        errors: list[str] = []
        rejected_proposals = 0
        for exchange in transcript:
            name = exchange.invocation.name
            if name == "propose_options" and not exchange.ok:
                rejected_proposals += 1
                continue
            if name not in _SEARCH_TOOLS:
                continue
            if not exchange.ok:
                errors.append(str(exchange.result.get("error", "")))
                continue
            options = exchange.result.get("options") or ()
            refs = [str(item.get("ref_id")) for item in options if item.get("ref_id")]
            if name == "search_transport":
                transport_refs.extend(ref for ref in refs if ref not in transport_refs)
                if not refs:
                    empty_routes.append(
                        f"{exchange.result.get('origin')}→{exchange.result.get('destination')}"
                    )
            else:
                hotel_refs.extend(ref for ref in refs if ref not in hotel_refs)

        if not transport_refs:
            where = "、".join(dict.fromkeys(empty_routes)) or "这条路线"
            question = (
                f"在你给的时间窗里没有查到 {where} 的可用交通。要不要换个时间或换种交通方式？"
            )
            if errors:
                question += " 另外有搜索没能完成：" + "；".join(errors)[:300]
            return _ask(question)

        decision, compiled = self._understand(conversation, context)
        open_questions = [
            f"{route} 这段在你给的时间窗里没有查到可用交通" for route in dict.fromkeys(empty_routes)
        ]
        open_questions.extend(f"有一次搜索没能完成：{item}"[:300] for item in errors)
        args: dict[str, Any] = {
            "transport_refs": transport_refs,
            "hotel_refs": hotel_refs,
            "summary": decision.intent.summary,
            "open_questions": open_questions,
        }
        # 旅行者说过的硬要求和偏好要跟着方案走：引用本身不带它们。
        # 交付被拒过一次（比如某个名字不在词表里）就不再声明要求；再被拒就开口问。
        if rejected_proposals == 0 and compiled.command is not None:
            request = compiled.command.request
            if request.hard_constraints:
                args["hard_constraints"] = list(request.hard_constraints)
            if request.soft_preferences:
                args["soft_preferences"] = list(request.soft_preferences)
        elif rejected_proposals >= 2:
            return _ask("我查到了几条方案，但交付时被系统拒绝了两次；请再说一遍你的要求。")
        return ModelTurn(calls=(ToolInvocation("propose_options", args),))

    # -- 共用 ------------------------------------------------------------------

    def _understand(
        self, conversation: str, context: dict[str, Any]
    ) -> tuple[IntentDecision, SearchCommandCompilation]:
        """同一段对话每轮重算一次；替身跨用例复用，不能把上一趟的理解带进来。"""
        decision = self._interpreter.interpret_trip_intent(
            conversation, task_id="tool-loop-standin", traveler_id="tool-loop-standin",
            context=context,
        ).decision
        compiled = compile_search_command(
            decision,
            task_id="tool-loop-standin",
            traveler_id="tool-loop-standin",
            version=1,
            city_normalizer=self._city_normalizer,
            created_at=_reference_time(context),
        )
        return decision, compiled


#: 每一段的日期出处该引哪个字段：去程引"最晚到达"，没有就引"最早出发"；返程同理。
#: 三段以上的行程确定性解释器从不产出，这里也就不假装能处理。
_LEG_EVIDENCE_FIELDS: tuple[tuple[str, ...], ...] = (
    ("arrive_by", "departure_after"),
    ("return_before", "return_after"),
)


def _date_quote(decision: IntentDecision, leg_index: int) -> str | None:
    if leg_index >= len(_LEG_EVIDENCE_FIELDS):
        return None
    for field in _LEG_EVIDENCE_FIELDS[leg_index]:
        for item in decision.evidence:
            if item.field == field and item.quote.strip():
                return item.quote
    return None


def _ask(question: str) -> ModelTurn:
    return ModelTurn(calls=(ToolInvocation("ask_traveler", {"question": question}),))

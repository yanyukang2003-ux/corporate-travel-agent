#!/usr/bin/env python3
"""红队用例走新语义入口的验收（HANDOFF §18.3 G / H / I 的可移植部分）。

## 为什么不是把旧 runner 直接改一行

旧的 A–I runner 把模型脚本成"什么都没抽到"，然后断言**宿主**的旧机器能自己算对
——比如自己把"下下周三"解析成 9 月 2 日、自己把"酒店不要了"翻译成清空住宿字段。
ADR-0002 把这些机器从语义链路里**删掉了**：新链路里这些职责归模型。所以：

- 能移植的：新链路里仍归宿主的职责——"读不准就别查"、"不支持就披露别查"、
  "改口后整份重新编译、不留旧值"。本文件覆盖这些。
- 不能移植的：日期/相对时间的**解析正确性**（H-01/03/04/05）。它已经进了模型，
  用脚本化替身断言它等于自己写答案自己批改，没有意义。要验只能真跑计费模型，
  属于另一类评测，不在本文件范围内。
- 不相关的：A–E 的多数用例（Provider 故障、工具预算、快照过期）根本不经过意图
  链路，走的是结构化入口，本来就与入口无关；F 是 Postgres 双 worker。

不调用计费模型，不访问外网。脚本化语义替身 + Mock 库存。输出目录必须不存在。
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.ports import IntentInterpretationResult, LLMCallMetadata
from corporate_travel_agent.agent.semantic_intent import (
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
)
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system
from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement, TaskState

REPO = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)
LONDON = ZoneInfo("Europe/London")
RUNNER_VERSION = "semantic-redteam-runner-v1"


class _ScriptedSemanticModel:
    """按剧本返回完整语义判定；剧本用完后重复最后一条。"""

    prompt_version = "semantic-redteam-v1"
    semantic_prompt_version = prompt_version

    def __init__(self, decisions: list[IntentDecision]) -> None:
        self._decisions = deque(decisions)
        self._last: IntentDecision | None = None
        self.calls = 0

    def interpret_trip_intent(self, conversation, *, task_id, traveler_id, context):
        _ = (conversation, task_id, traveler_id, context)
        self.calls += 1
        if self._decisions:
            self._last = self._decisions.popleft()
        assert self._last is not None
        return IntentInterpretationResult(
            decision=self._last,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="scripted-semantic-redteam",
                duration_ms=1,
                evidence_contract_version="conversation-turn-v1",
            ),
        )

    def propose_search_adjustment(self, failure_facts, allowed_adjustments):
        _ = (failure_facts, allowed_adjustments)
        return None

    def explain_verified_options(self, options):
        return {item.option_id: "" for item in options}


def _intent(**overrides: Any) -> SemanticIntent:
    values: dict[str, Any] = {
        "summary": "从北京到上海开会",
        "origin_candidates": ["Beijing"],
        "destination_candidates": ["Shanghai"],
        "departure_after": datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI_TZ),
        "arrive_by": datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI_TZ),
        "return_after": datetime(2026, 8, 6, 17, 0, tzinfo=SHANGHAI_TZ),
        "return_before": datetime(2026, 8, 6, 23, 0, tzinfo=SHANGHAI_TZ),
        "booking_scope": BookingScope.ROUND_TRIP,
        "lodging_requirement": LodgingRequirement.REQUIRED,
        "hotel_check_in": date(2026, 8, 5),
        "hotel_check_out": date(2026, 8, 6),
        "client_location": None,
        "hard_constraints": ["arrive_before_meeting"],
        "soft_preferences": ["prefer_train"],
        "alternatives": [],
        "conditions": [],
        "uncertainties": [],
    }
    values.update(overrides)
    return SemanticIntent(**values)


def _evidence(turn: int, quote: str) -> list[EvidenceRef]:
    return [
        EvidenceRef(turn_index=turn, field=field, quote=quote)
        for field in ("origin", "destination", "departure_after", "arrive_by")
    ]


def _decision(intent: SemanticIntent, *, turn: int = 0, quote: str = "北京", **overrides: Any):
    values: dict[str, Any] = {
        "status": IntentDecisionStatus.READY,
        "intent": intent,
        "clarification_question": None,
        "conflicts": [],
        "unsupported_reasons": [],
        "assumptions": [],
        "evidence": _evidence(turn, quote),
        "confidence": 0.95,
        "manipulation_detected": False,
    }
    values.update(overrides)
    return IntentDecision(**values)


def _check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _case(case_id: str, title: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "title": title,
        "passed": all(item["ok"] for item in checks) if checks else False,
        "checks": checks,
    }


def _start(decisions: list[IntentDecision], message: str, task_id: str):
    model = _ScriptedSemanticModel(decisions)
    workflow, _ = build_demo_system(
        semantic_language_model=model, clock=lambda: CLOCK
    )
    task = workflow.create_task_from_semantic_message(
        message, traveler_id="E1001", task_id=task_id
    )
    return workflow, task, model


def _search_count(task, prefix: str = "provider.search") -> int:
    return sum(1 for item in task.tool_calls if item.tool_name.startswith(prefix))


def _fields(task) -> dict[str, Any]:
    return task.intent_fields


# --- I 能力边界：不支持就披露，且一次库存都不查 -------------------------------


def _boundary_case(
    case_id: str,
    title: str,
    message: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """模型判定 UNSUPPORTED，宿主必须披露原因并拒绝搜索。"""
    decision = _decision(
        _intent(summary=message),
        status=IntentDecisionStatus.UNSUPPORTED,
        unsupported_reasons=[reason],
        clarification_question=f"这项我暂时做不了：{reason}。要不要先把其余行程订好？",
    )
    _, task, _model = _start([decision], message, case_id.lower())
    question = task.clarification_question or ""
    conflicts = tuple(task.intent_conflicts)
    return _case(
        case_id,
        title,
        [
            _check("not_oos", task.state is not TaskState.OUT_OF_SCOPE, task.state.value),
            _check("clarify", task.state is TaskState.NEEDS_CLARIFICATION, task.state.value),
            _check("no_search", _search_count(task) == 0),
            _check("no_options", not task.options),
            _check("no_request", task.request is None),
            _check("reason_surfaced", reason in conflicts, conflicts),
            _check("disclosed", reason in question, question[:160]),
        ],
    )


def _capability_cases() -> list[dict[str, Any]]:
    cases = [
        _boundary_case(
            "SI-01",
            "儿童票披露且不搜索",
            "下周三从北京去上海开会，两岁的儿子也要跟",
            reason="无法为儿童或婴儿出票",
        ),
        _boundary_case(
            "SI-02",
            "签证披露且不搜索",
            "下周三从北京去伦敦开会，帮我办签证",
            reason="无法办理签证或护照",
        ),
        _boundary_case(
            "SI-03",
            "选座与里程卡披露且不搜索",
            "下周三从北京去上海，帮我选靠过道并用里程卡",
            reason="无法选座或使用里程卡",
        ),
        _boundary_case(
            "SI-04",
            "开口程不被压成往返",
            "下周三从北京去上海，从杭州回",
            reason="无法处理开口程或多城行程",
        ),
        _boundary_case(
            "SI-05",
            "已出票改签不去搜新库存",
            "帮我把已出票的北京上海机票改签到下周三",
            reason="无法为已出票的行程改签或退票",
        ),
        _boundary_case(
            "SI-06",
            "宠物与无障碍披露且不搜索",
            "下周三从北京去上海，带宠物随行，需要无障碍座位",
            reason="无法安排宠物随行或无障碍座位",
        ),
    ]

    # 对照组：普通行程不该被边界机制误伤。
    _, ordinary, _m = _start(
        [_decision(_intent())], "下周三从北京去上海开会", "si-control"
    )
    cases.append(
        _case(
            "SI-00",
            "普通行程照常搜索",
            [
                _check("no_conflicts", not ordinary.intent_conflicts, ordinary.intent_conflicts),
                _check("searched", _search_count(ordinary) > 0, _search_count(ordinary)),
                _check(
                    "planned",
                    ordinary.state
                    in {TaskState.WAITING_FOR_USER, TaskState.NO_FEASIBLE_OPTION},
                    ordinary.state.value,
                ),
            ],
        )
    )

    # 对照组：模型判断"签证中心"是会面地点而非办签证，宿主必须信任它，不得自己加边界。
    _, visa_center, _m2 = _start(
        [_decision(_intent(client_location="签证中心"))],
        "下周三从北京去上海签证中心开会",
        "si-visa-center",
    )
    cases.append(
        _case(
            "SI-07",
            "签证中心当会面地点时不被拦",
            [
                _check("no_conflicts", not visa_center.intent_conflicts),
                _check(
                    "not_blocked",
                    visa_center.state is not TaskState.NEEDS_CLARIFICATION,
                    visa_center.state.value,
                ),
                _check(
                    "client_location",
                    _fields(visa_center).get("client_location") == "签证中心",
                    _fields(visa_center).get("client_location"),
                ),
            ],
        )
    )
    return cases


# --- G 改口：整份重新编译，不留旧值 -------------------------------------------


def _revision_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seed = "8月5日从北京去上海，6日上午10点前到，当天晚上回，住一晚。"

    # SG-01 改成单程：返程字段必须被整份丢掉，且不得再搜一次返程。
    one_way = _intent(
        summary="改成单程",
        booking_scope=BookingScope.OUTBOUND_ONLY,
        return_after=None,
        return_before=None,
    )
    workflow, task, _m = _start([_decision(_intent()), _decision(one_way, turn=1, quote="单程")],
                                seed, "sg-01")
    outbound = _fields(task)["departure_after"]
    arrive = _fields(task)["arrive_by"]
    inbound_before = _search_count(task, "provider.search_transport.inbound")
    task = workflow.submit_semantic_message(task.task_id, "日期不动，改成单程")
    inbound_after = _search_count(task, "provider.search_transport.inbound")
    cases.append(
        _case(
            "SG-01",
            "出方案后改单程：日期保留，返程整份丢掉，不再搜返程",
            [
                _check("origin", _fields(task)["origin"] == "Beijing"),
                _check("destination", _fields(task)["destination"] == "Shanghai"),
                _check("departure_kept", _fields(task)["departure_after"] == outbound),
                _check("arrive_kept", _fields(task)["arrive_by"] == arrive),
                _check("return_after_null", _fields(task)["return_after"] is None),
                _check("return_before_null", _fields(task)["return_before"] is None),
                _check("no_new_inbound", inbound_after == inbound_before,
                       f"{inbound_before}->{inbound_after}"),
            ],
        )
    )

    # SG-02a 酒店不要了：住宿字段清空，路线不受影响。
    no_hotel = _intent(
        summary="不再需要酒店",
        lodging_requirement=LodgingRequirement.NOT_REQUIRED,
        hotel_check_in=None,
        hotel_check_out=None,
    )
    workflow, task, _m = _start(
        [_decision(_intent()), _decision(no_hotel, turn=1, quote="酒店")], seed, "sg-02a"
    )
    hotel_before = _search_count(task, "provider.search_hotels")
    task = workflow.submit_semantic_message(task.task_id, "酒店不要了")
    hotel_after = _search_count(task, "provider.search_hotels")
    cases.append(
        _case(
            "SG-02a",
            "出方案后取消酒店：住宿清空，路线不动，不再搜酒店",
            [
                _check("lodging", _fields(task)["lodging_requirement"] == "NOT_REQUIRED",
                       _fields(task).get("lodging_requirement")),
                _check("hotel_in_null", _fields(task)["hotel_check_in"] is None),
                _check("hotel_out_null", _fields(task)["hotel_check_out"] is None),
                _check("no_hotel_required",
                       "hotel_required" not in (_fields(task).get("hard_constraints") or [])),
                _check("return_kept", _fields(task)["return_after"] is not None),
                _check("no_new_hotel_search", hotel_after == hotel_before,
                       f"{hotel_before}->{hotel_after}"),
            ],
        )
    )

    # SG-02b 还是订两晚：住宿从无到有，且日期锚在抵达日。
    two_nights = _intent(
        summary="补订两晚酒店",
        lodging_requirement=LodgingRequirement.REQUIRED,
        hotel_check_in=date(2026, 8, 6),
        hotel_check_out=date(2026, 8, 8),
        return_after=None,
        return_before=None,
        booking_scope=BookingScope.OUTBOUND_ONLY,
    )
    no_lodging_seed = _intent(
        lodging_requirement=LodgingRequirement.UNSPECIFIED,
        hotel_check_in=None,
        hotel_check_out=None,
        return_after=None,
        return_before=None,
        booking_scope=BookingScope.OUTBOUND_ONLY,
    )
    workflow, task, _m = _start(
        [_decision(no_lodging_seed), _decision(two_nights, turn=1, quote="两晚")],
        "8月5日从北京去上海，6日上午10点前到。",
        "sg-02b",
    )
    task = workflow.submit_semantic_message(task.task_id, "还是订两晚")
    cases.append(
        _case(
            "SG-02b",
            "出方案后补订两晚：住宿从无到有并锚在抵达日",
            [
                _check("lodging", _fields(task)["lodging_requirement"] == "REQUIRED",
                       _fields(task).get("lodging_requirement")),
                _check("check_in", _fields(task)["hotel_check_in"] == date(2026, 8, 6),
                       _fields(task).get("hotel_check_in")),
                _check("check_out", _fields(task)["hotel_check_out"] == date(2026, 8, 8),
                       _fields(task).get("hotel_check_out")),
                _check("hotel_required",
                       "hotel_required" in (_fields(task).get("hard_constraints") or [])),
            ],
        )
    )

    # SG-03a 会议改点：抵达时刻变，日期与出发不变。
    later_meeting = _intent(
        summary="会议改到下午三点",
        arrive_by=datetime(2026, 8, 6, 15, 0, tzinfo=SHANGHAI_TZ),
    )
    workflow, task, _m = _start(
        [_decision(_intent()), _decision(later_meeting, turn=1, quote="会议")], seed, "sg-03a"
    )
    depart = _fields(task)["departure_after"]
    task = workflow.submit_semantic_message(task.task_id, "会议改到下午 3 点")
    arrive = _fields(task)["arrive_by"]
    cases.append(
        _case(
            "SG-03a",
            "出方案后会议改点：抵达时刻改，日期与出发保留",
            [
                _check("date", arrive is not None and arrive.date().isoformat() == "2026-08-06"),
                _check(
                    "hour",
                    arrive is not None and arrive.hour == 15,
                    getattr(arrive, "hour", None),
                ),
                _check("departure_kept", _fields(task)["departure_after"] == depart),
                _check("origin", _fields(task)["origin"] == "Beijing"),
                _check("destination", _fields(task)["destination"] == "Shanghai"),
            ],
        )
    )

    # SG-03b 返程改后天晚上：只动返程窗口。
    later_return = _intent(
        summary="返程改到后天晚上",
        return_after=datetime(2026, 8, 8, 18, 0, tzinfo=SHANGHAI_TZ),
        return_before=datetime(2026, 8, 8, 23, 0, tzinfo=SHANGHAI_TZ),
    )
    workflow, task, _m = _start(
        [_decision(_intent()), _decision(later_return, turn=1, quote="返程")], seed, "sg-03b"
    )
    outbound = _fields(task)["departure_after"]
    arrive = _fields(task)["arrive_by"]
    task = workflow.submit_semantic_message(task.task_id, "返程改到后天晚上")
    ret_after = _fields(task)["return_after"]
    ret_before = _fields(task)["return_before"]
    cases.append(
        _case(
            "SG-03b",
            "出方案后返程改后天晚上：只动返程窗口",
            [
                _check("departure_kept", _fields(task)["departure_after"] == outbound),
                _check("arrive_kept", _fields(task)["arrive_by"] == arrive),
                _check("return_day",
                       ret_after is not None and ret_after.date().isoformat() == "2026-08-08",
                       getattr(ret_after, "date", lambda: None)()),
                _check("return_after_hour", ret_after is not None and ret_after.hour == 18),
                _check("return_before_hour", ret_before is not None and ret_before.hour == 23),
            ],
        )
    )

    # SG-04 换出发地：目的地不能被顺手改掉，旧方案必须作废。
    london_seed = _intent(
        summary="从北京去伦敦",
        destination_candidates=["London"],
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=LONDON),
        return_after=None,
        return_before=None,
        lodging_requirement=LodgingRequirement.UNSPECIFIED,
        hotel_check_in=None,
        hotel_check_out=None,
        booking_scope=BookingScope.OUTBOUND_ONLY,
    )
    from_shanghai = _intent(
        summary="改从上海去伦敦",
        origin_candidates=["Shanghai"],
        destination_candidates=["London"],
        arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=LONDON),
        return_after=None,
        return_before=None,
        lodging_requirement=LodgingRequirement.UNSPECIFIED,
        hotel_check_in=None,
        hotel_check_out=None,
        booking_scope=BookingScope.OUTBOUND_ONLY,
    )
    workflow, task, _m = _start(
        [_decision(london_seed), _decision(from_shanghai, turn=1, quote="上海")],
        "8月5日从北京去伦敦，6日上午10点前到。",
        "sg-04",
    )
    options_before = list(task.options)
    task = workflow.submit_semantic_message(task.task_id, "改从上海走")
    cases.append(
        _case(
            "SG-04",
            "没有『改为』也能换出发地，目的地保持不变",
            [
                _check("origin", _fields(task)["origin"] == "Shanghai",
                       _fields(task).get("origin")),
                _check("destination_london", _fields(task)["destination"] == "London",
                       _fields(task).get("destination")),
                _check("stale_options_dropped",
                       all(item not in task.options for item in options_before),
                       f"{len(options_before)}->{len(task.options)}"),
            ],
        )
    )
    return cases


# --- H 的可移植部分：读不准就别查 ---------------------------------------------


def _ambiguity_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # SH-01 两个候选日期：模型保留不确定性，宿主必须拒绝编译。
    ambiguous_friday = _decision(
        _intent(
            summary="这周五还是下周五未定",
            departure_after=None,
            arrive_by=None,
            return_after=None,
            return_before=None,
            lodging_requirement=LodgingRequirement.UNSPECIFIED,
            hotel_check_in=None,
            hotel_check_out=None,
            uncertainties=["出发日是这周五还是下周五尚未确定"],
        ),
        status=IntentDecisionStatus.NEEDS_CLARIFICATION,
        clarification_question="请确认是这周五还是下周五出发（按公历日期）？",
        evidence=[EvidenceRef(turn_index=0, field="destination", quote="上海")],
    )
    _, task, _m = _start([ambiguous_friday], "这周五还是下周五从北京去上海", "sh-01")
    cases.append(
        _case(
            "SH-01",
            "两个候选日期不编一个出来",
            [
                _check("depart_null", _fields(task).get("departure_after") is None),
                _check("arrive_null", _fields(task).get("arrive_by") is None),
                _check("clarify", task.state is TaskState.NEEDS_CLARIFICATION, task.state.value),
                _check("no_search", _search_count(task) == 0),
                _check("no_options", not task.options),
                _check("no_request", task.request is None),
            ],
        )
    )

    # SH-02 二选一城市：候选留两个，宿主不许自己挑一个。
    two_origins = _decision(
        _intent(
            summary="北京或上海出发未定",
            origin_candidates=["Beijing", "Shanghai"],
            destination_candidates=["Philadelphia"],
            alternatives=["北京或者上海出发"],
            uncertainties=["出发城市尚未确定"],
        ),
        status=IntentDecisionStatus.NEEDS_CLARIFICATION,
        clarification_question="请确认从北京还是上海出发？",
        evidence=[EvidenceRef(turn_index=0, field="destination", quote="费城")],
    )
    _, task, _m = _start([two_origins], "北京或者上海出发，8月5日去费城", "sh-02")
    cases.append(
        _case(
            "SH-02",
            "二选一城市不偷偷挑一个",
            [
                _check("origin_null", _fields(task).get("origin") is None,
                       _fields(task).get("origin")),
                _check("clarify", task.state is TaskState.NEEDS_CLARIFICATION, task.state.value),
                _check("no_search", _search_count(task) == 0),
                _check("no_request", task.request is None),
            ],
        )
    )

    # SH-03 条件住宿：条件没解决前不许编译成一次搜索。
    conditional_hotel = _decision(
        _intent(
            summary="如果会议开到晚上就住一晚",
            lodging_requirement=LodgingRequirement.REQUIRED,
            hotel_check_in=None,
            hotel_check_out=None,
            conditions=["会议若延到晚上才需要住宿"],
        ),
        status=IntentDecisionStatus.NEEDS_CLARIFICATION,
        clarification_question="会议如果延到晚上要订酒店吗？请确认后我再搜。",
    )
    _, task, _m = _start(
        [conditional_hotel], "8月5日从北京去上海，会议要是开到晚上就住一晚", "sh-03"
    )
    cases.append(
        _case(
            "SH-03",
            "条件住宿在条件解决前不编译",
            [
                _check("clarify", task.state is TaskState.NEEDS_CLARIFICATION, task.state.value),
                _check("no_search", _search_count(task) == 0),
                _check("no_request", task.request is None),
                _check(
                    "condition_surfaced",
                    any("condition" in item for item in task.intent_conflicts),
                    task.intent_conflicts,
                ),
            ],
        )
    )
    return cases


NOT_PORTABLE = [
    {
        "origin": "§18.3 H-01 / H-03 / H-04 / H-05",
        "what": "下下周三、8/5 与 8.5、春节不编公历、12/30→1/2 跨年的解析正确性",
        "why": "ADR-0002 把日期解析交给了模型；用脚本化替身断言它等于自己写答案自己批改。"
        "要验必须真跑计费模型，属于模型行为评测，不在本 runner 范围。",
    },
    {
        "origin": "§18.3 I 的措辞兜底",
        "what": "无模型时用正则从用户原文兜底识别能力边界",
        "why": "语义链路没有正则兜底层；模型不可用时它直接停在结构化表单，"
        "见 tests/test_semantic_entrypoint_reliability.py。",
    },
    {
        "origin": "§18.3 A–E / F",
        "what": "Provider 故障、工具预算、快照过期、Postgres 双 worker",
        "why": "这些用例走结构化入口或根本不经过意图链路，与入口无关，无需重跑。",
    },
]


def _run() -> list[dict[str, Any]]:
    return _capability_cases() + _revision_cases() + _ambiguity_cases()


def _markdown(cases: list[dict[str, Any]], started: str) -> str:
    passed = sum(1 for item in cases if item["passed"])
    lines = [
        "# 红队用例走语义入口的验收",
        "",
        f"- started_at: `{started}`",
        f"- runner: `{RUNNER_VERSION}`",
        f"- result: **{passed}/{len(cases)} PASS**",
        "",
        "覆盖 HANDOFF §18.3 G / H / I 中**在语义链路里仍归宿主**的职责。",
        "不调用计费模型，不访问外网。",
        "",
        "| ID | Result | Title |",
        "|---|---|---|",
    ]
    for item in cases:
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(f"| {item['case_id']} | {mark} | {item['title']} |")
    lines.append("")
    failed = [item for item in cases if not item["passed"]]
    if failed:
        lines.append("## Failed checks")
        for item in failed:
            lines.append(f"### {item['case_id']}")
            for check in item["checks"]:
                if not check["ok"]:
                    lines.append(f"- {check['name']}: {check['detail']}")
            lines.append("")
    lines.append("## 明确不在本 runner 覆盖范围")
    lines.append("")
    lines.append("| 来源 | 内容 | 为什么不移植 |")
    lines.append("|---|---|---|")
    for item in NOT_PORTABLE:
        lines.append(f"| {item['origin']} | {item['what']} | {item['why']} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC).isoformat()
    cases = _run()
    passed = sum(1 for item in cases if item["passed"])
    summary = {
        "started_at": started,
        "runner_version": RUNNER_VERSION,
        "entrypoint": "semantic",
        "billed_model_calls": 0,
        "passed": passed,
        "total": len(cases),
        "cases": cases,
        "not_portable": NOT_PORTABLE,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "REPORT.md").write_text(_markdown(cases, started), encoding="utf-8")
    print(f"{passed}/{len(cases)} PASS → {output}")
    if passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

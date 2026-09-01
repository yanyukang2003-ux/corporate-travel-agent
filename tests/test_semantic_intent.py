from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.search_command import compile_search_command
from corporate_travel_agent.agent.semantic_intent import (
    ConversationLedger,
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
)
from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement
from corporate_travel_agent.domain.models import ConversationMessage
from corporate_travel_agent.services.locations import CityNormalizer

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _ready_intent(**overrides: object) -> SemanticIntent:
    values: dict[str, object] = {
        "summary": "从北京到上海参加会议，8月5日出发，8月6日10点前到达",
        "origin_candidates": ["Beijing"],
        "destination_candidates": ["Shanghai"],
        "departure_after": datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI),
        "arrive_by": datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI),
        "return_after": None,
        "return_before": None,
        "booking_scope": BookingScope.OUTBOUND_ONLY,
        "lodging_requirement": LodgingRequirement.NOT_REQUIRED,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "client_location": None,
        "hard_constraints": ["arrive_before_meeting"],
        "soft_preferences": [],
        "alternatives": [],
        "conditions": [],
        "uncertainties": [],
    }
    values.update(overrides)
    return SemanticIntent(**values)


def _decision(intent: SemanticIntent, **overrides: object) -> IntentDecision:
    values: dict[str, object] = {
        "status": IntentDecisionStatus.READY,
        "intent": intent,
        "clarification_question": None,
        "conflicts": [],
        "unsupported_reasons": [],
        "assumptions": [],
        "evidence": [
            EvidenceRef(turn_index=0, field="origin", quote="北京"),
            EvidenceRef(turn_index=0, field="destination", quote="上海"),
        ],
        "confidence": 0.96,
        "manipulation_detected": False,
    }
    values.update(overrides)
    return IntentDecision(**values)


def test_conversation_ledger_preserves_raw_turns_and_stable_indices() -> None:
    ledger = ConversationLedger.from_messages(
        [
            ConversationMessage(role="user", content="下周从北京去上海"),
            ConversationMessage(role="assistant", content="哪一天出发？"),
            ConversationMessage(role="user", content="改成周五去伦敦"),
        ]
    )

    assert [turn.turn_index for turn in ledger.turns] == [0, 1, 2]
    assert ledger.latest_user_message == "改成周五去伦敦"
    assert "[turn:0 role:user] 下周从北京去上海" in ledger.render()
    assert "[turn:2 role:user] 改成周五去伦敦" in ledger.render()


def test_compile_refuses_to_silently_choose_an_alternative_origin() -> None:
    semantic = _ready_intent(
        origin_candidates=["Beijing", "Shanghai"],
        destination_candidates=["Philadelphia"],
        alternatives=["从北京或上海出发"],
        uncertainties=["出发城市尚未确定"],
    )
    decision = _decision(
        semantic,
        status=IntentDecisionStatus.NEEDS_CLARIFICATION,
        clarification_question="请确认从北京还是上海出发？",
    )

    compiled = compile_search_command(
        decision,
        task_id="alternative-origin",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.command is None
    assert compiled.clarification_question == "请确认从北京还是上海出发？"
    assert "origin" in compiled.missing


def test_compile_ready_semantics_into_validated_command_without_defaults() -> None:
    compiled = compile_search_command(
        _decision(
            _ready_intent(),
            evidence=[
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
        ),
        task_id="ready-trip",
        traveler_id="E1001",
        version=2,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.ready
    assert compiled.command is not None
    request = compiled.command.request
    assert request.origin == "Beijing"
    assert request.destination == "Shanghai"
    assert request.version == 2
    assert request.booking_scope is BookingScope.OUTBOUND_ONLY
    assert request.hotel_check_in is None


def test_compile_blocks_conditional_lodging_until_semantics_are_resolved() -> None:
    semantic = _ready_intent(
        lodging_requirement=LodgingRequirement.UNSPECIFIED,
        hotel_check_in=date(2026, 8, 5),
        hotel_check_out=date(2026, 8, 6),
        conditions=["如果当天回不来就住一晚"],
        uncertainties=["是否需要住宿取决于返程库存"],
    )

    compiled = compile_search_command(
        _decision(semantic),
        task_id="conditional-hotel",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert not compiled.ready
    assert compiled.command is None
    assert compiled.clarification_question
    assert any("unresolved" in conflict for conflict in compiled.conflicts)


def test_a_ready_decision_without_grounded_evidence_asks_instead_of_dead_ending() -> None:
    """模型说"可以查了"却拿不出某字段的原话，是"还没问清"，不是把整份解释判死。

    真实跑里最常见的情形：用户压根没说到达时限。此前宿主把它当模型违约，直接把用户
    推去填结构化表单；现在改成追问缺的那一项，已经读懂的日期照样保留。
    """
    compiled = compile_search_command(
        _decision(
            _ready_intent(),
            evidence=[
                EvidenceRef(turn_index=0, field="origin", quote="北京"),
                EvidenceRef(turn_index=0, field="destination", quote="上海"),
                EvidenceRef(turn_index=0, field="departure_after", quote="8月5日"),
            ],
        ),
        task_id="ungrounded-arrive",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert not compiled.ready
    assert compiled.command is None
    assert compiled.missing == ("arrive_by",)
    assert compiled.clarification_question


def test_an_open_jaw_return_becomes_its_own_leg_instead_of_being_collapsed() -> None:
    """"去上海、从杭州回"现在编译成两段真实航线，而不是被拒绝、更不是被压扁。

    这条用例的期望**变过一次**，因为能力变了不是因为要让测试过：
    - 领域模型只能表达"原路往返"时，杭州没地方放，宿主拼出一条用户没要过的
      上海→北京 并真的去搜了库存（静默错搜）。当时的止血办法是**拒绝**这类行程。
    - 现在行程是有序航段列表，返程的起点就是第二段自己的 origin，
      开口程不再是特例，直接支持。
    """
    compiled = compile_search_command(
        _decision(
            _ready_intent(
                return_origin_candidates=["Hangzhou"],
                return_after=datetime(2026, 8, 20, 18, 0, tzinfo=SHANGHAI),
                return_before=datetime(2026, 8, 20, 23, 0, tzinfo=SHANGHAI),
                booking_scope=BookingScope.ROUND_TRIP,
            ),
            evidence=[
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
        ),
        task_id="open-jaw",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.ready, (compiled.missing, compiled.conflicts)
    legs = compiled.command.request.transport_legs()
    assert [(leg.origin, leg.destination) for leg in legs] == [
        ("Beijing", "Shanghai"),
        ("Hangzhou", "Beijing"),
    ]


def test_an_unsettled_return_origin_is_asked_about_rather_than_guessed() -> None:
    """返程从哪出发还有两个候选时，问清楚再说，不许随手挑一个。"""
    compiled = compile_search_command(
        _decision(
            _ready_intent(
                return_origin_candidates=["Hangzhou", "Suzhou"],
                return_after=datetime(2026, 8, 20, 18, 0, tzinfo=SHANGHAI),
                return_before=datetime(2026, 8, 20, 23, 0, tzinfo=SHANGHAI),
                booking_scope=BookingScope.ROUND_TRIP,
            ),
            evidence=[
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
        ),
        task_id="open-jaw-unsettled",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert not compiled.ready
    assert "return_origin" in compiled.missing


def test_an_ordinary_round_trip_still_comes_back_the_way_it_went() -> None:
    """返程从目的地原路回来，仍然是最普通的往返。"""
    compiled = compile_search_command(
        _decision(
            _ready_intent(
                # 模型如实记了"从上海回"，而上海正是目的地。
                return_origin_candidates=["Shanghai"],
                return_after=datetime(2026, 8, 20, 18, 0, tzinfo=SHANGHAI),
                return_before=datetime(2026, 8, 20, 23, 0, tzinfo=SHANGHAI),
                booking_scope=BookingScope.ROUND_TRIP,
            ),
            evidence=[
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
        ),
        task_id="plain-round-trip",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.ready
    legs = compiled.command.request.transport_legs()
    assert [(leg.origin, leg.destination) for leg in legs] == [
        ("Beijing", "Shanghai"),
        ("Shanghai", "Beijing"),
    ]


def test_evidence_may_name_the_schema_fields_rather_than_the_short_names() -> None:
    """证据字段名要认 schema 的真名。

    宿主原本只认 "origin"/"destination"，可这两个名字在 SemanticIntent 里**根本不存在**
    ——真名是 origin_candidates / destination_candidates。实测真模型每一轮都老老实实
    用真名给了证据，却被判成"没给证据"，宿主于是回头去问用户在第 0 轮就说清的事。
    要求一个不存在的名字是宿主的坑。
    """
    compiled = compile_search_command(
        _decision(
            _ready_intent(),
            evidence=[
                EvidenceRef(turn_index=0, field="origin_candidates", quote="北京"),
                EvidenceRef(turn_index=0, field="destination_candidates", quote="北京"),
                EvidenceRef(turn_index=0, field="departure_after", quote="北京"),
                EvidenceRef(turn_index=0, field="arrive_by", quote="北京"),
            ],
        ),
        task_id="schema-named-evidence",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.ready, (compiled.missing, compiled.conflicts)
    assert compiled.command is not None


def test_the_meeting_commitment_survives_into_the_request() -> None:
    """会面地点此前被抽出来又丢掉；现在它和到达时限、缓冲要求收拢成一条承诺。

    拆散的时候，政策引擎只能查单价——它根本不知道这趟差旅是为什么去的。
    """
    compiled = compile_search_command(
        _decision(
            _ready_intent(
                summary="去客户现场做季度评审",
                client_location="上海张江客户现场",
            ),
            evidence=[
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
        ),
        task_id="commitment",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.ready
    request = compiled.command.request
    assert request.client_location == "上海张江客户现场"
    assert len(request.commitments) == 1
    commitment = request.commitments[0]
    assert commitment.place == "上海张江客户现场"
    assert commitment.not_later_than == datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI)
    assert commitment.purpose == "去客户现场做季度评审"
    # arrive_before_meeting 从"硬约束元组里的一个字符串"变成了承诺自己的属性。
    assert commitment.safety_buffer_required is True


def test_without_a_named_place_the_destination_carries_the_commitment() -> None:
    """没说会面地点时，到场地点就是目的地——仍然是一条承诺，不是空的。"""
    compiled = compile_search_command(
        _decision(
            _ready_intent(client_location=None),
            evidence=[
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
        ),
        task_id="commitment-fallback",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.command.request.commitments[0].place == "Shanghai"
    assert compiled.command.request.client_location is None

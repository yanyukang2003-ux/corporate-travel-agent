"""第 06 步：只问那些**答案会改变结论**的事。

此前的追问依据是「字段填满了没有」——那是填表不是规划。实测里一段五轮的对话，
系统一轮问一个字段问了五次，其中好几个问题的答案根本不改变最终推荐哪班车。

**行程轮廓**：不查库存就能算出来的东西——走哪几段、每段的时间窗、住不住、
受哪些要求管。具体选哪一班、多少钱不在里面，那要等报价，而报价要花钱。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.search_command import compile_search_command
from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement
from corporate_travel_agent.planning.divergence import (
    OpenQuestion,
    SketchInput,
    resolve_open_questions,
    sketch,
)
from corporate_travel_agent.services.locations import CityNormalizer
from tests.semantic_fixtures import semantic_decision, semantic_intent

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=SH)

NORMALIZER = CityNormalizer({"上海": "Shanghai", "北京": "Beijing", "杭州": "Hangzhou"})


def _compile(intent, **overrides):
    return compile_search_command(
        semantic_decision(intent, **overrides),
        task_id="divergence-task",
        traveler_id="E-DIV",
        version=1,
        city_normalizer=NORMALIZER,
        created_at=NOW,
    )


def _base(**overrides) -> SketchInput:
    values: dict[str, object] = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "return_origin": None,
        "departure_after": datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        "arrive_by": datetime(2026, 8, 20, 18, 0, tzinfo=SH),
        "return_after": None,
        "return_before": None,
        "booking_scope": BookingScope.OUTBOUND_ONLY,
        "lodging_required": False,
        "hotel_check_in": None,
        "hotel_check_out": None,
    }
    values.update(overrides)
    return SketchInput(**values)  # type: ignore[arg-type]


# --- 轮廓本身 ---------------------------------------------------------------


def test_an_unsettled_field_leaves_a_placeholder_instead_of_voiding_the_sketch() -> None:
    """出发地是不是同一座城市，和到达时限还没问到，是两件事，不该互相牵连。"""
    outline = sketch(_base(arrive_by=None))
    assert outline.legs[0][0] == "Beijing"
    assert outline.legs[0][3] == "?"


def test_saying_a_hotel_is_needed_without_dates_is_not_the_same_as_settled_dates() -> None:
    """一件真的没定的事不能被判成"不影响结论"。"""
    unsettled = sketch(_base(lodging_required=True))
    settled = sketch(
        _base(
            lodging_required=True,
            hotel_check_in=date(2026, 8, 20),
            hotel_check_out=date(2026, 8, 21),
        )
    )
    assert unsettled != settled


# --- 分歧判定 ---------------------------------------------------------------


def test_two_readings_that_end_up_at_the_same_outline_are_not_worth_asking() -> None:
    verdict = resolve_open_questions(
        _base(origin=None),
        [
            OpenQuestion(
                field="origin",
                readings=({"origin": "Beijing"},),
                assumption="按北京处理。",
            )
        ],
    )
    assert verdict.must_ask == ()
    assert verdict.settled == {"origin": "Beijing"}
    assert verdict.assumptions == ("按北京处理。",)


def test_two_readings_that_change_the_outline_are_the_one_thing_to_ask() -> None:
    verdict = resolve_open_questions(
        _base(destination=None),
        [
            OpenQuestion(
                field="destination",
                readings=({"destination": "Shanghai"}, {"destination": "Hangzhou"}),
            )
        ],
    )
    assert verdict.must_ask == ("destination",)
    assert verdict.settled == {}


def test_something_with_no_reading_at_all_must_be_asked() -> None:
    """用户压根没提，连读法都没有——这只能问。"""
    verdict = resolve_open_questions(_base(), [OpenQuestion(field="origin", readings=())])
    assert verdict.must_ask == ("origin",)


# --- 编译期的实际效果 -------------------------------------------------------


def test_two_names_for_one_city_are_not_worth_a_question() -> None:
    """用户写「上海」、系统里叫 Shanghai，问"你说的是哪个"没有意义。"""
    compiled = _compile(semantic_intent(destination_candidates=["上海", "Shanghai"]))
    assert compiled.ready, compiled.missing + compiled.conflicts
    assert compiled.command is not None
    assert compiled.command.request.destination == "Shanghai"
    assert any("同一座城市" in item for item in compiled.assumptions)


def test_two_genuinely_different_cities_still_stop_the_search() -> None:
    compiled = _compile(semantic_intent(destination_candidates=["上海", "杭州"]))
    assert not compiled.ready
    assert "destination" in compiled.missing


def test_hotel_dates_the_journey_already_pins_are_computed_not_asked() -> None:
    """往返行程把到达日和返程日钉死了，入住退房只有一种算法——这是算术不是编日期。"""
    compiled = _compile(
        semantic_intent(
            booking_scope=BookingScope.ROUND_TRIP,
            arrive_by=datetime(2026, 8, 20, 18, 0, tzinfo=SH),
            return_after=datetime(2026, 8, 22, 13, 0, tzinfo=SH),
            return_before=datetime(2026, 8, 22, 23, 0, tzinfo=SH),
            lodging_requirement=LodgingRequirement.REQUIRED,
            hard_constraints=[],
        )
    )
    assert compiled.ready, compiled.missing + compiled.conflicts
    assert compiled.command is not None
    assert compiled.command.request.hotel_check_in == date(2026, 8, 20)
    assert compiled.command.request.hotel_check_out == date(2026, 8, 22)
    assert any("推算住宿" in item for item in compiled.assumptions)


def test_a_same_day_trip_that_wants_a_hotel_is_still_asked() -> None:
    """当天往返却说要住店：这不是算得出来的事，是真的要问。"""
    compiled = _compile(
        semantic_intent(
            booking_scope=BookingScope.ROUND_TRIP,
            arrive_by=datetime(2026, 8, 20, 12, 0, tzinfo=SH),
            return_after=datetime(2026, 8, 20, 19, 0, tzinfo=SH),
            return_before=datetime(2026, 8, 20, 23, 0, tzinfo=SH),
            lodging_requirement=LodgingRequirement.REQUIRED,
            hard_constraints=[],
        )
    )
    assert not compiled.ready
    assert "hotel_check_in" in compiled.missing


# --- 问什么由宿主定，怎么问才交给模型 ---------------------------------------


def test_the_host_asks_for_everything_at_once_instead_of_one_field_per_round() -> None:
    """模型一次只问一件事，于是五轮对话被拆成一轮一个字段问了五次。"""
    compiled = _compile(
        semantic_intent(
            origin_candidates=[],
            destination_candidates=[],
            departure_after=None,
            arrive_by=None,
        ),
        clarification_question="请问您从哪座城市出发？",
    )
    assert not compiled.ready
    assert len(compiled.missing) > 1
    question = compiled.clarification_question or ""
    assert "从哪出发" in question
    assert "去哪" in question
    assert "最晚什么时候要到" in question


def test_the_model_wording_survives_when_the_host_adds_the_rest() -> None:
    """真跑抓到的退步：宿主补全不能把模型那句盖掉。

    只有模型说得出这次到底哪里有歧义——"这周五还是下周五"这种话，宿主手上
    只有一个字段名，再怎么措辞也问不出那个"周五"。所以模型那句必须原样打头。
    """
    compiled = _compile(
        semantic_intent(departure_after=None, arrive_by=None),
        clarification_question="您说的是这周五还是下周五？",
    )
    assert not compiled.ready
    question = compiled.clarification_question or ""
    assert question.startswith("您说的是这周五还是下周五？")
    assert "最晚什么时候要到" in question


def test_the_model_keeps_the_wording_when_only_one_thing_is_open() -> None:
    """只剩一件事时两边说的是同一件事，模型说得更像人话。"""
    compiled = _compile(
        semantic_intent(arrive_by=None),
        clarification_question="您最晚什么时候需要抵达上海？",
    )
    assert not compiled.ready
    assert compiled.missing == ("arrive_by",)
    assert compiled.clarification_question == "您最晚什么时候需要抵达上海？"


def test_the_host_never_shows_internal_field_names_to_the_traveler() -> None:
    compiled = _compile(
        semantic_intent(origin_candidates=[], departure_after=None),
    )
    question = compiled.clarification_question or ""
    assert "origin" not in question
    assert "departure_after" not in question

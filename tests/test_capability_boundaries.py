from corporate_travel_agent.agent.capability_boundaries import (
    boundaries_from_model,
    capability_boundary_conflicts,
    detect_capability_boundaries,
    format_capability_conflict,
    is_capability_conflict,
    resolve_capability_boundaries,
)
from corporate_travel_agent.agent.param_extraction_loop import is_blocking_conflict
from corporate_travel_agent.services.locations import CityNormalizer


def _codes(message: str) -> set[str]:
    return {item.code for item in detect_capability_boundaries(message, CityNormalizer())}


def test_children_and_infant_language_is_detected() -> None:
    assert "children" in _codes("下周三北京上海开会，带2岁小孩")
    assert "children" in _codes("需要婴儿票")


def test_visa_and_passport_language_is_detected() -> None:
    assert "visa" in _codes("去伦敦开会，帮我办签证")
    assert "visa" in _codes("护照快过期了也一起办")


def test_seat_and_mileage_language_is_detected() -> None:
    assert "seat_mileage" in _codes("帮我选靠过道，用里程卡")


def test_open_jaw_and_multicity_are_detected() -> None:
    assert "open_jaw" in _codes("开口票北京去上海杭州回")
    assert "open_jaw" in _codes("下周三从北京去上海，从杭州回")
    assert "open_jaw" in _codes("先去上海再去杭州最后回北京")


def test_ticketed_change_is_detected() -> None:
    assert "ticket_change" in _codes("帮我把已出票的机票改签到下周三")
    assert "ticket_change" in _codes("这张票退了吧")
    assert "ticket_change" not in _codes("改从上海走")
    assert "ticket_change" not in _codes("日期不动，改成单程")


def test_accessibility_and_pets_are_detected() -> None:
    assert "accessibility_pet" in _codes("需要无障碍座位")
    assert "accessibility_pet" in _codes("带宠物随行")


def test_ordinary_trip_is_not_a_boundary() -> None:
    assert _codes("下周三从北京去上海开会") == set()


def test_capability_conflicts_are_blocking() -> None:
    conflicts = capability_boundary_conflicts("带小孩去上海开会")
    assert conflicts
    assert all(is_capability_conflict(item) for item in conflicts)
    assert all(is_blocking_conflict(item) for item in conflicts)
    assert format_capability_conflict(conflicts[0])
    assert "儿童" in format_capability_conflict(conflicts[0])


def test_model_codes_are_the_live_judge() -> None:
    modeled = boundaries_from_model(["children"], [])
    assert {item.code for item in modeled} == {"children"}
    resolved = resolve_capability_boundaries(
        user_message="下周三去上海签证中心开会",
        model_codes=(),
        model_conflicts=(),
        llm_judged=True,
    )
    assert resolved == ()


def test_regex_fallback_only_when_model_did_not_judge() -> None:
    fallback = resolve_capability_boundaries(
        user_message="下周三北京上海开会，带2岁小孩",
        model_codes=(),
        model_conflicts=(),
        llm_judged=False,
    )
    assert "children" in {item.code for item in fallback}
    trusted_empty = resolve_capability_boundaries(
        user_message="下周三北京上海开会，带2岁小孩",
        model_codes=(),
        model_conflicts=(),
        llm_judged=True,
    )
    assert trusted_empty == ()


def test_model_conflict_text_can_carry_a_code() -> None:
    modeled = boundaries_from_model(
        [],
        ["unsupported_capability:ticket_change:当前不支持已出票改签或退票"],
    )
    assert {item.code for item in modeled} == {"ticket_change"}

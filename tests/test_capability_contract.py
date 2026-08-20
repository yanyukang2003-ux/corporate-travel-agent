from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.capability_contract import (
    completeness_gaps,
    supporting_slots_for_state,
)
from corporate_travel_agent.agent.clarification_questions import detect_uncertain_slots

FIXED = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_gaps_follow_extracted_preference_not_user_phrasing() -> None:
    fields = {
        "origin": "New York",
        "destination": "Philadelphia",
        "soft_preferences": ["hotel_near_client"],
        "hard_constraints": ["hotel_required"],
        "client_location": None,
    }
    assert completeness_gaps(fields) == ("client_location",)
    names = {slot.name for slot in supporting_slots_for_state(fields)}
    assert names == {"client_location"}

    skipped = dict(fields, client_location="SKIPPED")
    assert completeness_gaps(skipped) == ()
    located = dict(fields, client_location="Center City")
    assert completeness_gaps(located) == ()


def test_uncertain_slots_use_state_even_when_followup_has_no_proximity_words() -> None:
    fields = {
        "origin": "New York",
        "destination": "Philadelphia",
        "departure_after": FIXED,
        "arrive_by": FIXED,
        "hotel_check_in": FIXED.date(),
        "hotel_check_out": FIXED.date(),
        "hard_constraints": ["hotel_required"],
        "soft_preferences": ["hotel_near_client"],
    }
    uncertain = detect_uncertain_slots(
        fields,
        user_message="晚上九点",
        assumptions=(),
    )
    assert "client_location" in uncertain

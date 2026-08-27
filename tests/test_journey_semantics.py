from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.journey_semantics import (
    infer_booking_scope,
    normalize_journey_fields,
)
from corporate_travel_agent.domain.enums import BookingScope, TripLegRole
from corporate_travel_agent.domain.models import TripRequestVersion

SH = ZoneInfo("Asia/Shanghai")


def test_return_from_city_is_a_single_real_direction_leg() -> None:
    assert infer_booking_scope("下周三从北京回来") is BookingScope.RETURN_ONLY

    normalized, notes, provenance = normalize_journey_fields(
        {
            "origin": None,
            "destination": "Beijing",
            "departure_after": None,
            "arrive_by": None,
            "return_after": datetime(2026, 8, 26, 13, 0, tzinfo=SH),
            "return_before": datetime(2026, 8, 26, 22, 0, tzinfo=SH),
        },
        user_message="下周三从北京回来",
        provenance={
            "destination": "model_extract",
            "return_after": "model_extract",
            "return_before": "model_extract",
        },
        return_from_city="Beijing",
    )

    assert normalized["booking_scope"] == BookingScope.RETURN_ONLY.value
    assert normalized["origin"] == "Beijing"
    assert normalized["destination"] is None
    assert normalized["departure_after"] == datetime(2026, 8, 26, 13, 0, tzinfo=SH)
    assert normalized["arrive_by"] == datetime(2026, 8, 26, 22, 0, tzinfo=SH)
    assert normalized["return_after"] is None
    assert normalized["return_before"] is None
    assert provenance["origin"] == "journey_semantics"
    assert notes


def test_old_canonical_round_trip_pair_is_reoriented_for_return_only() -> None:
    normalized, _, _ = normalize_journey_fields(
        {
            "origin": "Shanghai",
            "destination": "Beijing",
            "booking_scope": BookingScope.RETURN_ONLY.value,
        },
        user_message="从北京回来",
        return_from_city="Beijing",
    )

    assert normalized["origin"] == "Beijing"
    assert normalized["destination"] == "Shanghai"


def test_round_trip_followup_keeps_existing_scope() -> None:
    scope = infer_booking_scope(
        "返程改到下周三",
        prior_fields={
            "booking_scope": BookingScope.ROUND_TRIP.value,
            "origin": "Shanghai",
            "destination": "Beijing",
            "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=SH),
        },
    )
    assert scope is BookingScope.ROUND_TRIP


def test_trip_request_projects_return_only_without_reversing_it_again() -> None:
    request = TripRequestVersion(
        task_id="return-only",
        version=1,
        traveler_id="E1001",
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 26, 13, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 26, 22, 0, tzinfo=SH),
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
        booking_scope=BookingScope.RETURN_ONLY,
    )

    legs = request.transport_legs()
    assert len(legs) == 1
    assert legs[0].role is TripLegRole.RETURN
    assert (legs[0].origin, legs[0].destination) == ("Beijing", "Shanghai")


def test_round_trip_projects_two_real_direction_legs() -> None:
    request = TripRequestVersion(
        task_id="round-trip",
        version=1,
        traveler_id="E1001",
        origin="Shanghai",
        destination="Beijing",
        departure_after=datetime(2026, 8, 25, 8, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 25, 12, 0, tzinfo=SH),
        return_after=datetime(2026, 8, 26, 13, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 26, 22, 0, tzinfo=SH),
        hotel_check_in=None,
        hotel_check_out=None,
        booking_scope=BookingScope.ROUND_TRIP,
    )

    outbound, returning = request.transport_legs()
    assert outbound.role is TripLegRole.OUTBOUND
    assert (outbound.origin, outbound.destination) == ("Shanghai", "Beijing")
    assert returning.role is TripLegRole.RETURN
    assert (returning.origin, returning.destination) == ("Beijing", "Shanghai")

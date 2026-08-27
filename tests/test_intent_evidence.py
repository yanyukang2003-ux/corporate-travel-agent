from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.intent_evidence import (
    build_intent_evidence,
    validate_model_field_evidence,
)
from corporate_travel_agent.domain.enums import BookingScope

SH = ZoneInfo("Asia/Shanghai")
REF = datetime(2026, 8, 19, 15, 0, tzinfo=SH)


def test_return_scoped_span_rejects_outbound_slot_but_accepts_return_slot() -> None:
    message = "返程8月25日从上海回北京"
    evidence = build_intent_evidence(message, reference_time=REF)
    day = datetime(2026, 8, 25, 18, 0, tzinfo=SH)

    result = validate_model_field_evidence(
        extracted_fields={"departure_after": day, "return_after": day},
        provided_fields={"departure_after", "return_after"},
        prior_fields={},
        evidence=evidence,
    )

    assert result.accepted_fields == frozenset({"return_after"})
    assert result.rejected_fields == ("departure_after:source_scope_mismatch",)
    assert result.field_evidence["return_after"]["raw"] == "8月25日"
    assert result.field_evidence["return_after"]["start"] == 2
    assert result.field_evidence["return_after"]["end"] == 7


def test_return_scoped_span_supports_primary_slot_for_return_only_booking() -> None:
    message = "返程8月25日从上海回北京"
    evidence = build_intent_evidence(message, reference_time=REF)
    day = datetime(2026, 8, 25, 18, 0, tzinfo=SH)

    result = validate_model_field_evidence(
        extracted_fields={"departure_after": day},
        provided_fields={"departure_after"},
        prior_fields={"booking_scope": BookingScope.RETURN_ONLY.value},
        evidence=evidence,
    )

    assert result.accepted_fields == frozenset({"departure_after"})
    assert result.rejected_fields == ()


def test_unmentioned_date_is_rejected_as_untraceable() -> None:
    evidence = build_intent_evidence("从北京去上海", reference_time=REF)
    result = validate_model_field_evidence(
        extracted_fields={
            "departure_after": datetime(2026, 8, 25, 8, 0, tzinfo=SH)
        },
        provided_fields={"departure_after"},
        prior_fields={},
        evidence=evidence,
    )
    assert result.accepted_fields == frozenset()
    assert result.rejected_fields == ("departure_after:untraceable_source_span",)


def test_same_day_round_trip_span_supports_both_leg_dates() -> None:
    message = "8月25日从北京去上海，当天往返"
    evidence = build_intent_evidence(message, reference_time=REF)
    value = datetime(2026, 8, 25, 13, 0, tzinfo=SH)
    result = validate_model_field_evidence(
        extracted_fields={"departure_after": value, "return_after": value},
        provided_fields={"departure_after", "return_after"},
        prior_fields={},
        evidence=evidence,
    )
    assert result.accepted_fields == frozenset({"departure_after", "return_after"})
    assert result.rejected_fields == ()

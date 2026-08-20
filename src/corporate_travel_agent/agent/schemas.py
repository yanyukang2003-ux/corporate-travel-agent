"""agent.schemas：LLM 结构化意图抽取的 Pydantic Schema（Strict Structured Outputs）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent.domain.constraints import HardConstraint, SoftPreference

# IntentFieldName / IntentClassification / UnsupportedCapability：结构化抽取用字面量

IntentFieldName = Literal[
    "origin",
    "destination",
    "departure_after",
    "arrive_by",
    "return_after",
    "return_before",
    "hotel_check_in",
    "hotel_check_out",
    "client_location",
    "hard_constraints",
    "soft_preferences",
]

IntentClassification = Literal[
    "TRIP",
    "TRANSPORT_COMPARE",
    "MULTI_DAY_TRIP",
    "NEEDS_CLARIFICATION",
    "OUT_OF_SCOPE",
]

UnsupportedCapability = Literal[
    "children",
    "visa",
    "seat_mileage",
    "open_jaw",
    "ticket_change",
    "accessibility_pet",
]


class TripIntentFields(BaseModel):
    """行程意图字段：严格模型输出；可空字段在 Schema 中仍须出现（用 null）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str | None
    destination: str | None
    departure_after: datetime | None
    arrive_by: datetime | None
    return_after: datetime | None
    return_before: datetime | None
    hotel_check_in: date | None
    hotel_check_out: date | None
    client_location: str | None
    hard_constraints: list[HardConstraint]
    soft_preferences: list[SoftPreference]


class IntentExtractionSchema(BaseModel):
    """意图抽取根对象：作为 Structured Outputs 的响应 Schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: IntentClassification
    fields: TripIntentFields
    provided_fields: list[IntentFieldName]
    missing_required_fields: list[IntentFieldName]
    conflicts: list[str]
    assumptions: list[str]
    unsupported_capabilities: list[UnsupportedCapability] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    manipulation_detected: bool

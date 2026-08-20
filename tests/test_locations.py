from __future__ import annotations

import pytest

from corporate_travel_agent.services.locations import (
    CityNormalizer,
    resolve_location_timezone,
    route_timezones,
)


def test_resolve_location_timezone_for_us_and_china_cities() -> None:
    assert resolve_location_timezone("New York") == "America/New_York"
    assert resolve_location_timezone("费城") == "America/New_York"
    assert resolve_location_timezone("Beijing") == "Asia/Shanghai"
    assert resolve_location_timezone("伦敦") == "Europe/London"
    assert resolve_location_timezone("Los Angeles") == "America/Los_Angeles"


def test_unknown_location_keeps_fallback() -> None:
    assert (
        resolve_location_timezone("Somewhere Invented", fallback="Europe/Paris")
        == "Europe/Paris"
    )


def test_route_timezones_prefer_origin_then_destination() -> None:
    origin_tz, dest_tz = route_timezones(
        "New York",
        "Philadelphia",
        fallback="Asia/Shanghai",
    )
    assert origin_tz == "America/New_York"
    assert dest_tz == "America/New_York"

    origin_tz, dest_tz = route_timezones(
        "Shanghai",
        "London",
        fallback="Asia/Shanghai",
    )
    assert origin_tz == "Asia/Shanghai"
    assert dest_tz == "Europe/London"


def test_invalid_fallback_raises() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        resolve_location_timezone("New York", fallback="Not/AZone")


def test_city_normalizer_preserves_iata_location_specificity() -> None:
    normalizer = CityNormalizer(
        {
            "LHR": "London",
            "london": "London",
            "PEK": "Beijing",
        }
    )

    assert normalizer.canonicalize("lhr") == "LHR"
    assert normalizer.canonicalize("PEK") == "PEK"
    assert normalizer.canonicalize("london") == "London"

"""Ride times (dbf.py) from HAFAS trips."""

from custom_components.spdbf_nearby.dbf import _ride_on


def _trip(leave: str, arrive: str) -> dict:
    return {
        "times": True,
        "line": "N1",
        "stops": [
            {"name": "S Westkreuz", "eva": "1", "departure": leave},
            {"name": "S Ostkreuz", "eva": "2", "arrival": arrive},
        ],
    }


def test_ride_across_the_autumn_dst_change() -> None:
    """02:50 CEST to 02:05 CET is a 15-minute ride, not about 23 hours."""
    ride = _ride_on(
        _trip("2026-10-25T02:50:00+02:00", "2026-10-25T02:05:00+01:00"),
        {"eva": "1"},
        {"eva": "2"},
    )
    assert ride["minutes"] == 15
    assert (ride["departure"], ride["arrival"]) == ("02:50", "02:05")


def test_ride_past_midnight() -> None:
    """Test a ride that starts before and ends after midnight."""
    ride = _ride_on(
        _trip("2026-10-23T23:55:00+02:00", "2026-10-24T00:10:00+02:00"),
        {"eva": "1"},
        {"eva": "2"},
    )
    assert ride["minutes"] == 15
    # Clock times only (no date): still wraps past midnight.
    assert (
        _ride_on(_trip("23:55", "00:10"), {"eva": "1"}, {"eva": "2"})["minutes"] == 15
    )

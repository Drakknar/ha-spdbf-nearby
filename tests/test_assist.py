"""Assist answers (assist.py) on a fake coordinator: no Home Assistant needed."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from custom_components.spdbf_nearby import assist
from custom_components.spdbf_nearby.stations import Board, Stop


def _dep(line: str, minutes: int, platform: str = "", destination: str = "X") -> dict[str, Any]:
    when = dt_util.now() + timedelta(minutes=minutes, seconds=30)
    return {
        "line": line,
        "destination": destination,
        "platform": platform,
        "scheduled": when.strftime("%H:%M"),
        "delay": 0,
        "minutes": minutes,
        "cancelled": False,
        "expected": when.isoformat(),
    }


class FakeService:
    degraded = False

    def __init__(self, stops: dict[str, Stop], ride_minutes: int = 10) -> None:
        self.stops = stops
        self.ride_minutes = ride_minutes

    async def async_search(self, query: str, pos: Any) -> tuple[list[Stop], str, None]:
        return [self.stops[query]], "dbf", None

    async def async_ride(self, start: Any, end: Any, lines: Any) -> dict[str, Any]:
        return {"minutes": self.ride_minutes}

    async def async_trip(self, trip: Any) -> dict[str, Any]:
        return {"stops": [], "messages": []}


class FakeCoordinator:
    def __init__(self, boards: dict[str, list[dict[str, Any]] | None], ride_minutes: int = 10) -> None:
        stops = {
            name: Stop(name=name, eva=str(i), source="BVG", latitude=52.5, longitude=13.3)
            for i, name in enumerate(boards)
        }
        self.boards = {stops[name].eva: deps for name, deps in boards.items()}
        self.client = FakeService(stops, ride_minutes)

    async def async_board(self, eva: str, source: str, linked: Any = None, **_: Any) -> Board:
        if self.boards[eva] is None:
            # DBF unreachable, Transitous off or failing, nothing kept.
            return Board(error="Your DBF server isn't reachable", unreachable=True)
        return Board(departures=self.boards[eva])


PLACE = assist.Place(None, "nowhere")


async def test_line_status_accepts_one_line_as_text() -> None:
    """The schema allows lines: "S41"; it must not be read letter by letter."""
    coordinator = FakeCoordinator({"Westkreuz": [_dep("S41", 3), _dep("S42", 5)]})
    result = await assist.line_status(
        coordinator, PLACE, stop="Westkreuz", lines="S41", within_minutes=60
    )
    assert [item["line"] for item in result["lines"]] == ["S41"]
    assert "not_running_in_window" not in result


async def test_connection_keeps_a_zero_minute_change() -> None:
    """walk_minutes 0 on a later leg (same-platform change) is not the default 3."""
    coordinator = FakeCoordinator(
        {
            "A": [_dep("S5", 2)],
            # Arrival at B is 12.5 min from now; the U2 at +13 is catchable
            # with a 0-minute change, not with the default 3.
            "B": [_dep("U2", 13), _dep("U2", 30)],
        },
        ride_minutes=10,
    )
    legs = [
        {"stop": "A", "lines": "S5"},
        {"stop": "B", "lines": "U2", "walk_minutes": 0},
    ]
    result = await assist.connection(coordinator, PLACE, legs, None, 0, 1)
    steps = result["options"][0]["legs"]
    assert steps[1]["in_minutes"] == 13


async def test_per_platform_answer_is_bounded() -> None:
    """Grouped by platform, the answer still holds at most ASSIST_MAX_ROWS rows."""
    deps = [_dep("S1", i % 60, platform=str(i % 20)) for i in range(80)]
    coordinator = FakeCoordinator({"Hbf": deps})
    result = await assist.departures(
        coordinator,
        PLACE,
        stop="Hbf",
        filters=assist.Filters.of({}),
        towards=None,
        per_platform=10,
        count=8,
        within_minutes=180,
    )
    total = sum(len(rows) for rows in result["platforms"].values())
    assert total <= assist.ASSIST_MAX_ROWS
    assert "truncated" in result


async def test_no_data_is_not_reported_as_no_service() -> None:
    """With no source delivering, lines aren't "not running" and data isn't "Live."."""
    coordinator = FakeCoordinator({"Westkreuz": None})
    status = await assist.line_status(
        coordinator, PLACE, stop="Westkreuz", lines=["S41"], within_minutes=60
    )
    assert status["data"] != "Live."
    assert "not_running_in_window" not in status
    deps = await assist.departures(
        coordinator,
        PLACE,
        stop="Westkreuz",
        filters=assist.Filters.of({}),
        towards=None,
        per_platform=None,
        count=8,
        within_minutes=60,
    )
    assert deps["data"] != "Live."
    assert "note" not in deps
    assert deps["error"]

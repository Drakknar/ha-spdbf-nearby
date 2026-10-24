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
    def __init__(self, boards: dict[str, list[dict[str, Any]]], ride_minutes: int = 10) -> None:
        stops = {
            name: Stop(name=name, eva=str(i), source="BVG", latitude=52.5, longitude=13.3)
            for i, name in enumerate(boards)
        }
        self.boards = {stops[name].eva: deps for name, deps in boards.items()}
        self.client = FakeService(stops, ride_minutes)

    async def async_board(self, eva: str, source: str, linked: Any = None, **_: Any) -> Board:
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

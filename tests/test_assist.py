"""Assist answers (assist.py) on a fake coordinator: no Home Assistant needed."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.spdbf_nearby import assist
from custom_components.spdbf_nearby.stations import Board, Stop


def _dep(
    line: str,
    minutes: int,
    platform: str = "",
    destination: str = "X",
    trip: dict[str, Any] | None = None,
) -> dict[str, Any]:
    when = dt_util.now() + timedelta(minutes=minutes, seconds=30)
    dep = {
        "line": line,
        "destination": destination,
        "platform": platform,
        "scheduled": when.strftime("%H:%M"),
        "delay": 0,
        "minutes": minutes,
        "cancelled": False,
        "expected": when.isoformat(),
    }
    if trip:
        dep["trip"] = trip
    return dep


class FakeService:
    degraded = False

    def __init__(self, stops: dict[str, Stop], ride_minutes: int = 10) -> None:
        self.stops = stops
        self.ride_minutes = ride_minutes
        self.trip_messages: dict[str, list[str]] = {}
        self.rides: list[tuple[Any, Any, Any]] = []

    async def async_search(self, query: str, pos: Any) -> tuple[list[Stop], str, None]:
        return [self.stops[query]], "dbf", None

    async def async_ride(self, start: Any, end: Any, lines: Any) -> dict[str, Any]:
        self.rides.append((start, end, lines))
        return {"minutes": self.ride_minutes}

    async def async_trip(self, trip: Any) -> dict[str, Any]:
        return {"stops": [], "messages": self.trip_messages.get(trip.get("id"), [])}


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


async def test_line_status_notices_are_short_and_not_repeated() -> None:
    """A station-wide notice on every line's trip is listed once; long ones are cut."""
    coordinator = FakeCoordinator(
        {
            "Westkreuz": [
                _dep("S41", 3, trip={"id": "a", "hafas": "BVG"}),
                _dep("S42", 5, trip={"id": "b", "hafas": "BVG"}),
            ]
        }
    )
    lift = "Lift at Westkreuz out of service"
    coordinator.client.trip_messages = {"a": [lift, "x" * 2000], "b": [lift]}
    result = await assist.line_status(
        coordinator, PLACE, stop="Westkreuz", lines=None, within_minutes=60
    )
    s41, s42 = result["lines"]
    assert s41["notices"][0] == lift
    assert len(s41["notices"][1]) <= assist.ASSIST_MAX_NOTICE
    assert "notices" not in s42
    assert s42["same_notices_as"] == ["S41"]


class FakeStates:
    def __init__(self, states: list[State]) -> None:
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)


class FakeHass:
    def __init__(self, states: list[State]) -> None:
        self.states = FakeStates(states)


def _sensor_dep(line: str, minutes: int) -> dict[str, Any]:
    when = dt_util.now() + timedelta(minutes=minutes, seconds=30)
    return {
        "train": line,
        "destination": "X",
        "platform": "1",
        "scheduledTime": when.strftime("%Y-%m-%d %H:%M:%S"),
        "departure_timestamp": int(when.timestamp()),
        "delay": 0,
    }


def _sensor(entity_id: str, station: str, deps: list[dict[str, Any]], name: str) -> State:
    return State(
        entity_id,
        str(len(deps)),
        {"station": station, "next_departures": deps, "friendly_name": name},
    )


async def test_commute_ride_is_asked_like_the_card() -> None:
    """The commute asks the ride with the card's start, end and lines, so a
    ride the card got is found in last known while DBF is degraded."""
    hass = FakeHass(
        [
            _sensor("sensor.a", "900023201", [_sensor_dep("S5", 4), _sensor_dep("S7", 6)], "A sensor"),
            _sensor("sensor.b", "900100003", [_sensor_dep("U2", 20)], "B sensor"),
        ]
    )
    coordinator = FakeCoordinator({})
    commute = assist.Commute(
        name="Work",
        dashboard="overview",
        cards=[
            {"type": assist.CONNECTION_CARD, "entity": "sensor.a", "ride_to": "sensor.b"},
            {"type": assist.CONNECTION_CARD, "entity": "sensor.b", "title": "Alex"},
        ],
        visibility=None,
    )
    legs = assist._commute_legs(hass, coordinator, commute)
    result = await assist.plan(coordinator, legs, options=1)
    # What spdbf-connection-card's _updateRide sends for the first card:
    # its _stop() (no title: the stop id), the next card's _stop(next, ""),
    # and the lines of the rows it shows.
    start = {"eva": "900023201", "name": "900023201", "network": "BVG"}
    end = {"eva": "900100003", "name": "900100003"}
    assert coordinator.client.rides[0] == (start, end, ["S5", "S7"])
    # The answer still names the next stop by its card.
    assert result["options"][0]["legs"][0]["arrives"].endswith(" at Alex")


async def test_sensor_without_departures_keeps_its_network(hass) -> None:
    """A BVG db_infoscreen sensor with no departures (at night) stays BVG."""
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    registry = er.async_get(hass)
    for name, source in (
        ("bvg", "BVG – Berliner Verkehrsbetriebe"),
        ("legacy", "hafas=BVG"),
        ("iris", "IRIS-TTS"),
        ("efa", "MVV – Münchener Verkehrs- und Tarifverbund"),
    ):
        entry = MockConfigEntry(domain="db_infoscreen", data={"data_source": source})
        entry.add_to_hass(hass)
        registry.async_get_or_create(
            "sensor", "db_infoscreen", name, config_entry=entry, suggested_object_id=name
        )
    networks = {}
    for name in ("bvg", "legacy", "iris", "efa", "unregistered"):
        state = State(f"sensor.{name}", "0", {"station": "900023201", "next_departures": []})
        networks[name] = assist._sensor_stop(hass, state, "")["network"]
    assert networks == {
        "bvg": "BVG", "legacy": "BVG", "iris": "DB", "efa": "DB", "unregistered": "DB"
    }

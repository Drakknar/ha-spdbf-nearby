"""Answers for Assist: what the LLM tools in llm.py ask.

The questions people ask about public transport around them: the departures
of a stop (filtered, per platform, towards a station), how its lines are
doing, which stops are near, a connection with changes, and the commutes on
their dashboards (sections of connection cards). Everything goes through
TransportService, so answers fall back like the cards do, and each says where
its departures come from.

"Near me" is the asking phone, home for a device at home (a voice
satellite), else the asking user's person, else the entry's tracked entity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    ASSIST_MAX_OPTIONS,
    ASSIST_MAX_ROWS,
    ASSIST_MAX_TRIPS,
    ASSIST_MAX_WINDOW,
    DOMAIN,
    SOURCE_DB,
)
from .coordinator import SpdbfCoordinator, position_of
from .dbf import DbfError, distance_m
from .service import ORIGIN_LAST_KNOWN, ORIGIN_TRANSITOUS
from .stations import Board, Position, Stop, line_key, station_names, tidy_line

CONNECTION_CARD = "custom:spdbf-connection-card"


class AssistError(Exception):
    """A question that can't be answered; the message says why."""


# --- Who asks, and from where ---------------------------------------------


def coordinator_for(hass: HomeAssistant, user_id: str | None) -> SpdbfCoordinator:
    """Return the entry following the asking user, else the first loaded one."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not entries:
        raise AssistError("spdbf Public Transport isn't set up or not ready yet")
    for entry in entries:
        tracked = hass.states.get(entry.runtime_data.tracked)
        if user_id and tracked is not None and tracked.attributes.get("user_id") == user_id:
            return entry.runtime_data
    return entries[0].runtime_data


@dataclass
class Place:
    """Where "near me" is."""

    position: Position | None
    label: str


def _state_position(state: State) -> Position | None:
    # Zones have no GPS accuracy; people and trackers do.
    return position_of(state)


def _person_of_user(hass: HomeAssistant, user_id: str | None) -> State | None:
    if not user_id:
        return None
    return next(
        (s for s in hass.states.async_all("person") if s.attributes.get("user_id") == user_id),
        None,
    )


def person_state(
    hass: HomeAssistant, coordinator: SpdbfCoordinator, user_id: str | None
) -> State | None:
    """Return the asking user's person, else the tracked entity."""
    return _person_of_user(hass, user_id) or hass.states.get(coordinator.tracked)


def _find_place_state(hass: HomeAssistant, near: str) -> State | None:
    """Return the person, zone or tracker with this entity id or name."""
    if "." in near and (state := hass.states.get(near.strip())):
        return state
    wanted = near.strip().casefold()
    candidates = [
        *hass.states.async_all("person"),
        *hass.states.async_all("zone"),
        *hass.states.async_all("device_tracker"),
    ]
    for exact in (True, False):
        for state in candidates:
            names = {state.name.casefold(), state.object_id.replace("_", " ")}
            if exact and wanted in names:
                return state
            if not exact and any(wanted in name for name in names):
                return state
    return None


def locate(
    hass: HomeAssistant,
    coordinator: SpdbfCoordinator,
    near: str | None,
    device_id: str | None,
    user_id: str | None,
) -> Place:
    """Return where "near me" is for this request."""
    if near:
        state = _find_place_state(hass, near)
        if state is None:
            raise AssistError(f"No person, zone or device tracker called '{near}'")
        return Place(_state_position(state), state.name)
    if device_id:
        # A phone asking has a tracker of its own; any other device asking
        # (a voice satellite) is at home. A phone without a usable fix falls
        # through to its user.
        registry = er.async_get(hass)
        trackers = [
            entry.entity_id
            for entry in er.async_entries_for_device(registry, device_id)
            if entry.domain == "device_tracker"
        ]
        for entity_id in trackers:
            if (state := hass.states.get(entity_id)) and (pos := _state_position(state)):
                return Place(pos, f"{state.name} (the asking phone)")
        if not trackers and (home := hass.states.get("zone.home")):
            return Place(_state_position(home), "home (asked from a device at home)")
    if person := _person_of_user(hass, user_id):
        return Place(_state_position(person), person.name)
    tracked = hass.states.get(coordinator.tracked)
    return Place(
        coordinator.tracked_position(), tracked.name if tracked else coordinator.tracked
    )


# --- Departures in one shape, from boards and db_infoscreen sensors --------


@dataclass
class Row:
    """One departure, from a board or a db_infoscreen sensor."""

    line: str
    destination: str
    platform: str
    expected: datetime | None
    scheduled: str
    delay: int
    cancelled: bool
    trip: dict[str, Any] | None


def mode_of(line: str) -> str:
    """Return the kind of a line from its name; DBF sends no product type."""
    key = line_key(line)
    if re.fullmatch(r"S\d+[A-Z]?", key):
        return "S-Bahn"
    if re.fullmatch(r"U\d+", key):
        return "U-Bahn"
    if re.match(r"(RE|RB|IRE|MEX|RS|FEX)\d", key):
        return "regional train"
    if re.match(r"(ICE|IC|EC|ECE|EN|NJ|FLX|RJX?|TGV)\d*$", key):
        return "long-distance train"
    return "bus or tram"


def _board_row(dep: dict[str, Any]) -> Row:
    expected = dt_util.parse_datetime(dep["expected"]) if dep.get("expected") else None
    return Row(
        line=str(dep.get("line") or ""),
        destination=str(dep.get("destination") or ""),
        platform=str(dep.get("platform") or "").strip(),
        expected=expected,
        scheduled=str(dep.get("scheduled") or ""),
        delay=int(dep.get("delay") or 0),
        cancelled=bool(dep.get("cancelled")),
        trip=dep.get("trip"),
    )


def _hhmm(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else text[:5]


def _sensor_row(dep: dict[str, Any], stop: dict[str, str]) -> Row:
    """A db_infoscreen row; IRIS rows have scheduledDeparture, HAFAS rows
    scheduledTime (as the connection card reads them)."""
    iris = "scheduledDeparture" in dep
    timestamp = dep.get("departure_timestamp")
    trip = None
    if iris:
        kind = str(dep.get("train") or "").split()[:1]
        if kind and dep.get("trainNumber"):
            trip = {"train": f"{kind[0]} {dep['trainNumber']}", "station": stop["eva"]}
    elif dep.get("trip_id") or dep.get("tripId"):
        trip = {"id": str(dep.get("trip_id") or dep.get("tripId")), "hafas": stop["network"]}
    return Row(
        line=tidy_line(dep.get("train"), dep.get("trainNumber")),
        destination=str(dep.get("destination") or ""),
        platform=str(dep.get("platform") or "").strip(),
        expected=dt_util.utc_from_timestamp(timestamp) if timestamp else None,
        scheduled=_hhmm(dep.get("scheduledDeparture") or dep.get("scheduledTime")),
        delay=int(dep.get("delayDeparture") or dep.get("delay") or 0),
        cancelled=bool(dep.get("is_cancelled") or dep.get("isCancelled")),
        trip=trip,
    )


def _minutes_from(when: datetime | None, now: datetime) -> int | None:
    if when is None:
        return None
    return max(0, int((when - now).total_seconds() // 60))


def _clock(when: datetime | None) -> str | None:
    return dt_util.as_local(when).strftime("%H:%M") if when else None


def row_json(row: Row, now: datetime) -> dict[str, Any]:
    """Return a departure as short as the LLM needs it."""
    out: dict[str, Any] = {
        "line": row.line,
        "mode": mode_of(row.line),
        "destination": row.destination,
        "in_minutes": _minutes_from(row.expected, now),
        "time": _clock(row.expected) or row.scheduled,
    }
    if row.platform:
        out["platform"] = row.platform
    if row.delay:
        out["delay_minutes"] = row.delay
    if row.cancelled:
        out["cancelled"] = True
    return out


# --- Filters ---------------------------------------------------------------


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


@dataclass
class Filters:
    """What a question or a connection card keeps."""

    lines: list[str]
    platforms: list[str]
    destinations: list[str]
    hide_destinations: list[str]

    @classmethod
    def of(cls, config: dict[str, Any]) -> Filters:
        """Return filters from a card config or tool arguments."""
        return cls(
            lines=[line_key(tidy_line(line)) for line in _as_list(config.get("lines"))],
            platforms=[p.lower() for p in _as_list(config.get("platforms"))],
            destinations=[d.lower() for d in _as_list(config.get("destinations"))],
            hide_destinations=[d.lower() for d in _as_list(config.get("hide_destinations"))],
        )

    def __call__(self, row: Row) -> bool:
        """Return whether a row passes, like the connection card's filter:
        platform "2" also matches BVG's "2 (U2)"; destinations match a part
        of the name."""
        platform = row.platform.lower()
        destination = row.destination.lower()
        return (
            (not self.platforms or any(
                platform == p or platform.startswith(f"{p} ") for p in self.platforms
            ))
            and (not self.lines or line_key(row.line) in self.lines)
            and (not self.destinations or any(d in destination for d in self.destinations))
            and not any(d in destination for d in self.hide_destinations)
        )


def _same_name(a: str | None, b: str | None) -> bool:
    if not (a and b):
        return False
    return bool(
        station_names(a, drop_city=True) & station_names(b, drop_city=False)
        or station_names(a, drop_city=False) & station_names(b, drop_city=True)
    )


async def _goes_towards(
    coordinator: SpdbfCoordinator,
    stop: dict[str, Any],
    rows: list[Row],
    towards: str,
) -> tuple[list[Row], int]:
    """Keep the rows whose trip calls at `towards` after this stop.

    BVG rows from DBF carry no route, so each line and destination asks for
    one trip (at most ASSIST_MAX_TRIPS); a row whose destination is that
    station passes without one. Returns the rows and how many couldn't be
    checked.
    """
    verdicts: dict[tuple[str, str], bool | None] = {}
    here = {stop.get("eva"), *(stop.get("linked_evas") or [])} - {None}
    lookups = 0
    unknown = 0
    kept: list[Row] = []
    for row in rows:
        key = (line_key(row.line), row.destination)
        if key not in verdicts:
            if _same_name(row.destination, towards):
                verdicts[key] = True
            elif row.trip is None or lookups >= ASSIST_MAX_TRIPS:
                verdicts[key] = None
            else:
                lookups += 1
                try:
                    trip = await coordinator.client.async_trip(row.trip)
                except DbfError:
                    verdicts[key] = None
                else:
                    verdicts[key] = _calls_after(trip["stops"], here, stop.get("name"), towards)
        verdict = verdicts[key]
        if verdict:
            kept.append(row)
        elif verdict is None:
            unknown += 1
    return kept, unknown


def _calls_after(
    stops: list[dict[str, Any]], here: set[str], name: str | None, towards: str
) -> bool:
    start = next(
        (
            i
            for i, s in enumerate(stops)
            if s.get("current") or (s.get("eva") and s["eva"] in here) or _same_name(s.get("name"), name)
        ),
        -1,
    )
    return any(_same_name(s.get("name"), towards) for s in stops[start + 1 :])


# --- Stops and boards ------------------------------------------------------


def _source_note(origin: str, as_of: datetime | str | None) -> str:
    if origin == ORIGIN_TRANSITOUS:
        return (
            "Fallback data from Transitous because DBF can't deliver right now: "
            "delays may be missing and platforms too."
        )
    if origin == ORIGIN_LAST_KNOWN:
        when = dt_util.parse_datetime(as_of) if isinstance(as_of, str) else as_of
        return (
            f"Not live: the last known departures, as of {_clock(when) or 'earlier'}, "
            "because neither DBF nor Transitous can deliver right now."
        )
    return "Live."


def _stop_ref(stop: Stop) -> dict[str, Any]:
    return {
        "eva": stop.eva,
        "name": stop.name,
        "network": stop.source,
        "linked_evas": [s.eva for s in stop.linked],
    }


async def _resolve_stop(
    coordinator: SpdbfCoordinator, name: str | None, place: Place
) -> Stop:
    if name:
        stops, _origin, _as_of = await coordinator.client.async_search(name, place.position)
        if not stops:
            raise AssistError(f"No stop called '{name}' found")
        return stops[0]
    if place.position is None:
        raise AssistError(
            f"The position of {place.label} isn't known; name a stop or a place instead"
        )
    stops = await coordinator.async_nearby(place.position)
    if not stops:
        raise AssistError(f"No stops found near {place.label}")
    return stops[0]


async def _stop_board(coordinator: SpdbfCoordinator, stop: Stop) -> Board:
    return await coordinator.async_board(
        stop.eva,
        stop.source,
        [(s.eva, s.source) for s in stop.linked],
        name=stop.name,
        pos=(stop.latitude, stop.longitude),
    )


def _upcoming(rows: list[Row], now: datetime, within: int) -> list[Row]:
    end = now + timedelta(minutes=within)
    return [
        row for row in rows
        if row.expected is None or now - timedelta(minutes=1) <= row.expected <= end
    ]


def _stop_json(stop: Stop, place: Place) -> dict[str, Any]:
    meters = distance_m(stop, place.position)
    out: dict[str, Any] = {"stop": stop.name}
    if meters is not None:
        out["distance_m"] = round(meters)
        out["distance_from"] = place.label
    return out


async def departures(
    coordinator: SpdbfCoordinator,
    place: Place,
    *,
    stop: str | None,
    filters: Filters,
    towards: str | None,
    per_platform: int | None,
    count: int,
    within_minutes: int,
) -> dict[str, Any]:
    """Return a stop's next departures, filtered."""
    found = await _resolve_stop(coordinator, stop, place)
    board = await _stop_board(coordinator, found)
    now = dt_util.now()
    within = min(within_minutes, ASSIST_MAX_WINDOW)
    rows = [row for row in _upcoming([_board_row(d) for d in board.departures], now, within) if filters(row)]
    result = _stop_json(found, place)
    result["data"] = _source_note(board.origin, board.as_of)
    if board.error and not board.departures:
        result["error"] = board.error
    if towards:
        rows, unknown = await _goes_towards(coordinator, _stop_ref(found), rows, towards)
        if unknown:
            result["not_checked"] = (
                f"{unknown} departures couldn't be checked for calling at {towards}"
            )
    if per_platform:
        platforms: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            group = platforms.setdefault(row.platform or "no platform", [])
            if len(group) < per_platform:
                group.append(row_json(row, now))
        result["platforms"] = dict(sorted(platforms.items()))
    else:
        result["departures"] = [row_json(row, now) for row in rows[: min(count, ASSIST_MAX_ROWS)]]
    if not rows:
        result["note"] = f"No matching departures in the next {within} minutes."
    return result


async def line_status(
    coordinator: SpdbfCoordinator,
    place: Place,
    *,
    stop: str | None,
    lines: list[str],
    within_minutes: int,
) -> dict[str, Any]:
    """Return how each line at a stop is running: next departure, delays,
    cancellations, disruption notices, and asked lines that don't run."""
    found = await _resolve_stop(coordinator, stop, place)
    board = await _stop_board(coordinator, found)
    now = dt_util.now()
    within = min(within_minutes, ASSIST_MAX_WINDOW)
    rows = _upcoming([_board_row(d) for d in board.departures], now, within)
    wanted = [line_key(tidy_line(line)) for line in lines]
    if wanted:
        rows = [row for row in rows if line_key(row.line) in wanted]
    by_line: dict[str, list[Row]] = {}
    for row in rows:
        by_line.setdefault(row.line, []).append(row)
    summary = []
    trips = 0
    for line, on_line in by_line.items():
        running = [row for row in on_line if not row.cancelled]
        delays = [row.delay for row in running if row.delay > 0]
        item: dict[str, Any] = {
            "line": line,
            "mode": mode_of(line),
            "destinations": list(dict.fromkeys(row.destination for row in on_line))[:4],
            "departures": len(on_line),
            "cancelled": len(on_line) - len(running),
            "delayed_3_min_or_more": sum(1 for d in delays if d >= 3),
        }
        if delays:
            item["max_delay_minutes"] = max(delays)
        if running:
            item["next_in_minutes"] = _minutes_from(running[0].expected, now)
        # Disruption notices travel with trips (BVG/HAFAS only).
        with_trip = next((row for row in on_line if row.trip and "id" in row.trip), None)
        if with_trip and trips < ASSIST_MAX_TRIPS:
            trips += 1
            try:
                trip = await coordinator.client.async_trip(with_trip.trip)
            except DbfError:
                pass
            else:
                if trip.get("messages"):
                    item["notices"] = trip["messages"][:3]
        summary.append(item)
    result = _stop_json(found, place)
    result["data"] = _source_note(board.origin, board.as_of)
    result["window_minutes"] = within
    result["lines"] = summary
    seen = {line_key(line) for line in by_line}
    if missing := [line for line in lines if line_key(tidy_line(line)) not in seen]:
        result["not_running_in_window"] = missing
    if board.error and not board.departures:
        result["error"] = board.error
    return result


async def nearby_stops(
    coordinator: SpdbfCoordinator, place: Place, *, count: int
) -> dict[str, Any]:
    """Return the stops near a place, with the lines seen there."""
    if place.position is None:
        raise AssistError(f"The position of {place.label} isn't known")
    stops = await coordinator.async_nearby(place.position)
    out = []
    for stop in stops[:count]:
        item = _stop_json(stop, place)
        item["networks"] = stop.networks
        if lines := list(dict.fromkeys(d.get("line") for d in stop.board.departures if d.get("line"))):
            item["lines_seen"] = lines
        out.append(item)
    return {"near": place.label, "stops": out}


# --- Connections: legs with rides and changes ------------------------------


@dataclass
class Leg:
    """One leg: where it starts, its departures, and how it ends."""

    title: str
    stop: dict[str, Any]
    rows: Callable[[], Awaitable[tuple[list[Row], str, Any]]]
    filters: Filters
    towards: str | None = None
    # Walk to this leg's stop before boarding: from where you are for the
    # first leg, the change from the previous leg otherwise.
    walk_before: int = 0
    # Where it ends: the next leg's stop, or a station by name (last leg).
    ride_to: dict[str, Any] | None = None
    walk_after: int = 0


async def _leg_rows(coordinator: SpdbfCoordinator, leg: Leg, now: datetime) -> tuple[list[Row], str]:
    rows, origin, as_of = await leg.rows()
    rows = [row for row in _upcoming(rows, now, ASSIST_MAX_WINDOW) if leg.filters(row)]
    if leg.towards:
        rows, _unknown = await _goes_towards(coordinator, leg.stop, rows, leg.towards)
    return rows, _source_note(origin, as_of)


async def plan(
    coordinator: SpdbfCoordinator, legs: list[Leg], *, options: int
) -> dict[str, Any]:
    """Return the next ways through the legs: which departure, ride time,
    arrival, the change, and the next leg's catchable departure."""
    now = dt_util.now()
    leg_rows = [await _leg_rows(coordinator, leg, now) for leg in legs]
    notes = list(dict.fromkeys(note for _, note in leg_rows))
    first_rows, _ = leg_rows[0]
    ready = now + timedelta(minutes=legs[0].walk_before)
    starts = [row for row in first_rows if not row.cancelled and row.expected and row.expected >= ready]
    result: dict[str, Any] = {"data": notes[0] if len(notes) == 1 else notes}
    if legs[0].walk_before:
        result["walk_to_first_stop_minutes"] = legs[0].walk_before
    found = []
    for start in starts[: min(options, ASSIST_MAX_OPTIONS)]:
        found.append(await _follow(coordinator, legs, leg_rows, start, now))
    result["options"] = found
    if not starts:
        result["note"] = f"No catchable departures for the first leg ({legs[0].title})."
    return result


async def _follow(
    coordinator: SpdbfCoordinator,
    legs: list[Leg],
    leg_rows: list[tuple[list[Row], str]],
    start: Row,
    now: datetime,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    row: Row | None = start
    option: dict[str, Any] = {}
    if legs[0].walk_before:
        assert start.expected is not None
        minutes = _minutes_from(start.expected, now) or 0
        option["leave_in_minutes"] = max(0, minutes - legs[0].walk_before)
    for i, leg in enumerate(legs):
        assert row is not None and row.expected is not None
        step: dict[str, Any] = {"at": leg.title, **row_json(row, now)}
        steps.append(step)
        if leg.ride_to is None:
            break
        ride = await coordinator.client.async_ride(
            {k: leg.stop[k] for k in ("eva", "name", "network") if leg.stop.get(k)},
            {k: leg.ride_to[k] for k in ("eva", "name") if leg.ride_to.get(k)},
            [row.line],
        )
        if ride is None:
            step["ride"] = "unknown"
            option["note"] = "The ride time couldn't be found, so the later legs are left out."
            break
        arrival = row.expected + timedelta(minutes=ride["minutes"])
        step["ride_minutes"] = ride["minutes"]
        step["arrives"] = f"{_clock(arrival)} at {leg.ride_to.get('name')}"
        if i + 1 == len(legs):
            option["arrival"] = _clock(arrival)
            if leg.walk_after:
                option["walk_after_minutes"] = leg.walk_after
                option["done_at"] = _clock(arrival + timedelta(minutes=leg.walk_after))
            break
        nxt = legs[i + 1]
        catchable = arrival + timedelta(minutes=nxt.walk_before)
        rows, _note = leg_rows[i + 1]
        row = next(
            (r for r in rows if not r.cancelled and r.expected and r.expected >= catchable), None
        )
        if row is None:
            option["note"] = f"No catchable departure at {nxt.title} after {_clock(catchable)} yet."
            break
        assert row.expected is not None
        step["change_minutes"] = int((row.expected - arrival).total_seconds() // 60)
    option["legs"] = steps
    return option


def board_leg(
    coordinator: SpdbfCoordinator, stop: Stop
) -> Callable[[], Awaitable[tuple[list[Row], str, Any]]]:
    """Return a leg's departures from the stop's board."""

    async def rows() -> tuple[list[Row], str, Any]:
        board = await _stop_board(coordinator, stop)
        return [_board_row(d) for d in board.departures], board.origin, board.as_of

    return rows


async def connection(
    coordinator: SpdbfCoordinator,
    place: Place,
    legs: list[dict[str, Any]],
    to: str | None,
    walk_after: int,
    options: int,
) -> dict[str, Any]:
    """Plan a connection the user describes: stops, lines and changes."""
    if not legs:
        raise AssistError("A connection needs at least one leg")
    stops = [await _resolve_stop(coordinator, leg.get("stop"), place) for leg in legs]
    built: list[Leg] = []
    for i, (spec, stop) in enumerate(zip(legs, stops, strict=True)):
        if i + 1 < len(stops):
            ride_to: dict[str, Any] | None = {"eva": stops[i + 1].eva, "name": stops[i + 1].name}
        else:
            ride_to = {"name": to} if to else None
        built.append(
            Leg(
                title=stop.name,
                stop=_stop_ref(stop),
                rows=board_leg(coordinator, stop),
                filters=Filters.of(spec),
                towards=spec.get("towards"),
                walk_before=int(spec.get("walk_minutes") or (0 if i == 0 else 3)),
                ride_to=ride_to,
                walk_after=walk_after if i + 1 == len(stops) else 0,
            )
        )
    result = await plan(coordinator, built, options=options)
    if not legs[0].get("stop"):
        result["first_stop"] = _stop_json(stops[0], place)
    return result


# --- Commutes: the dashboard sections of connection cards ------------------


@dataclass
class Commute:
    """A dashboard section of connection cards, under its heading."""

    name: str
    dashboard: str
    cards: list[dict[str, Any]]
    visibility: list[dict[str, Any]] | None


async def dashboard_commutes(hass: HomeAssistant) -> list[Commute]:
    """Return the sections with connection cards on all storage dashboards."""
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return []
    found: list[Commute] = []
    for url_path, dashboard in data.dashboards.items():
        try:
            config = await dashboard.async_load(False)
        except Exception:  # noqa: BLE001 - a dashboard without config, YAML errors
            continue
        for view in config.get("views") or []:
            sections = view.get("sections") or [
                {"cards": view.get("cards") or [], "title": view.get("title")}
            ]
            for section in sections:
                cards = section.get("cards") or []
                connection_cards = [
                    c for c in cards if c.get("type") == CONNECTION_CARD and c.get("entity")
                ]
                if not connection_cards:
                    continue
                heading = next(
                    (c.get("heading") for c in cards if c.get("type") == "heading" and c.get("heading")),
                    None,
                )
                found.append(
                    Commute(
                        name=heading or section.get("title") or view.get("title") or "Connections",
                        dashboard=url_path or "overview",
                        cards=connection_cards,
                        visibility=section.get("visibility"),
                    )
                )
    return found


def _condition(hass: HomeAssistant, cond: dict[str, Any], person: State | None) -> bool | None:
    """Evaluate a dashboard visibility condition; None if it can't be here."""
    kind = cond.get("condition")
    if kind in ("and", "or", "not"):
        results = [_condition(hass, c, person) for c in cond.get("conditions") or []]
        if None in results:
            return None
        if kind == "and":
            return all(results)
        if kind == "or":
            return any(results)
        return not all(results)
    if kind == "state":
        state = hass.states.get(cond.get("entity", ""))
        if state is None:
            return False
        if "state" in cond:
            return state.state in _as_list(cond["state"])
        if "state_not" in cond:
            return state.state not in _as_list(cond["state_not"])
        return None
    if kind == "location":
        if person is None:
            return None
        return person.state in _as_list(cond.get("locations"))
    if kind == "screen":
        return True
    return None


def relevant_now(
    hass: HomeAssistant, commute: Commute, person: State | None
) -> bool | None:
    """Return whether the dashboard shows this section now (for the person)."""
    results = [_condition(hass, c, person) for c in commute.visibility or []]
    if None in results:
        return None
    return all(results)


def _sensor_stop(state: State | None, title: str) -> dict[str, Any] | None:
    """The station of a db_infoscreen sensor, as the connection card finds it."""
    if state is None:
        return None
    station = str(state.attributes.get("station") or "")
    if not station:
        return None
    first = (state.attributes.get("next_departures") or [None])[0]
    # HAFAS stop ids are numbers (BVG: 900...); IRIS stations are names.
    network = "BVG" if isinstance(first, dict) and "scheduledDeparture" not in first else SOURCE_DB
    name = (title or station) if station.isdigit() else station
    return {"eva": station, "name": name, "network": network}


def sensor_leg(
    hass: HomeAssistant, coordinator: SpdbfCoordinator, entity: str, stop: dict[str, Any]
) -> Callable[[], Awaitable[tuple[list[Row], str, Any]]]:
    """Return a leg's departures from its db_infoscreen sensor; from the
    station's board while DBF can't deliver (the sensor then keeps
    recalculating its last board and never goes unavailable)."""

    async def rows() -> tuple[list[Row], str, Any]:
        state = hass.states.get(entity)
        sensor_rows = state.attributes.get("next_departures") if state else None
        if isinstance(sensor_rows, list) and not coordinator.client.degraded:
            return [_sensor_row(d, stop) for d in sensor_rows if isinstance(d, dict)], "dbf", None
        board = await coordinator.async_board(stop["eva"], stop["network"], name=stop["name"])
        return [_board_row(d) for d in board.departures], board.origin, board.as_of

    return rows


def _commute_legs(
    hass: HomeAssistant, coordinator: SpdbfCoordinator, commute: Commute
) -> list[Leg] | None:
    """Return the commute's legs, or None if its cards aren't linked by rides
    (then they are separate boards)."""
    if not any(c.get("ride_to") or c.get("ride_to_station") for c in commute.cards):
        return None
    legs: list[Leg] = []
    by_entity = {c["entity"]: c for c in commute.cards}
    card: dict[str, Any] | None = commute.cards[0]
    seen: set[str] = set()
    walk_before = 0
    while card is not None and card["entity"] not in seen:
        seen.add(card["entity"])
        state = hass.states.get(card["entity"])
        title = card.get("title") or (state.name if state else card["entity"])
        stop = _sensor_stop(state, title)
        if stop is None:
            raise AssistError(f"The sensor of '{title}' ({card['entity']}) has no station right now")
        nxt = by_entity.get(card.get("ride_to") or "")
        ride_to = None
        if card.get("ride_to"):
            nxt_state = hass.states.get(card["ride_to"])
            ride_to = _sensor_stop(nxt_state, (nxt or {}).get("title", ""))
        elif card.get("ride_to_station"):
            ride_to = {"name": card["ride_to_station"]}
        walk = int(card.get("walk_minutes") or 0)
        legs.append(
            Leg(
                title=title,
                stop=stop,
                rows=sensor_leg(hass, coordinator, card["entity"], stop),
                filters=Filters.of(card),
                walk_before=walk_before,
                ride_to=ride_to,
                walk_after=walk if nxt is None else 0,
            )
        )
        walk_before = walk
        card = nxt
    return legs


async def commutes(
    hass: HomeAssistant,
    coordinator: SpdbfCoordinator,
    person: State | None,
    *,
    name: str | None,
    options: int,
) -> dict[str, Any]:
    """Return the dashboard commutes: matching ones (by name), else those the
    dashboard shows now, worked out; the rest listed by name."""
    found = await dashboard_commutes(hass)
    if not found:
        raise AssistError("No dashboard has connection cards (custom:spdbf-connection-card)")
    if name:
        wanted = name.casefold()
        chosen = [c for c in found if wanted in c.name.casefold()]
        if not chosen:
            raise AssistError(
                f"No commute matches '{name}'; known: {', '.join(c.name for c in found)}"
            )
    else:
        chosen = [c for c in found if relevant_now(hass, c, person)]
    now = dt_util.now()
    answers = []
    for commute in chosen:
        item: dict[str, Any] = {"commute": commute.name}
        try:
            legs = _commute_legs(hass, coordinator, commute)
            if legs is None:
                item["boards"] = await _commute_boards(hass, coordinator, commute, now)
            else:
                item.update(await plan(coordinator, legs, options=options))
        except (AssistError, DbfError) as err:
            item["error"] = str(err)
        answers.append(item)
    result: dict[str, Any] = {"commutes": answers}
    others = [c.name for c in found if c not in chosen]
    if others:
        result["other_commutes"] = others
    if not chosen:
        result["note"] = "None of the commutes is shown on the dashboard right now; name one."
    return result


async def _commute_boards(
    hass: HomeAssistant, coordinator: SpdbfCoordinator, commute: Commute, now: datetime
) -> list[dict[str, Any]]:
    boards = []
    for card in commute.cards:
        state = hass.states.get(card["entity"])
        title = card.get("title") or (state.name if state else card["entity"])
        stop = _sensor_stop(state, title)
        if stop is None:
            boards.append({"at": title, "error": "its sensor has no station right now"})
            continue
        rows, origin, as_of = await sensor_leg(hass, coordinator, card["entity"], stop)()
        rows = [r for r in _upcoming(rows, now, ASSIST_MAX_WINDOW) if Filters.of(card)(r)]
        boards.append({
            "at": title,
            "data": _source_note(origin, as_of),
            "departures": [row_json(r, now) for r in rows[: int(card.get("departures") or 5)]],
        })
    return boards


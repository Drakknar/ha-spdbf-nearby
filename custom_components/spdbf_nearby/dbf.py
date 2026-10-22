"""Talking to DBF: requests, outages, boards, trips, status checks.

Shared by every feature of the integration (the map's nearby stops, the
stop search card, the departure dialog and its trip view). What to show
when DBF can't deliver (Transitous, last known data) is service.py's job.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, tzinfo
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.location import distance as geo_distance

from .const import (
    BOARD_CACHE_TTL,
    BOARD_DEPARTURES,
    BOARD_ROWS,
    CONF_MERGE_NETWORKS,
    CONF_PUBLIC_URL,
    CONF_SERVER_URL,
    CONF_SOURCES,
    CONNECT_TIMEOUT,
    DB_FALLBACK_HAFAS,
    DEFAULT_MERGE_NETWORKS,
    DOMAIN,
    IRIS_TIME_ZONE,
    ISSUE_MISSING_COORDINATES,
    MAX_PARALLEL_REQUESTS,
    PRIMARY_RETRY_AFTER,
    REQUEST_TIMEOUT,
    RIDE_CACHE_TTL,
    RIDE_MAX_TRIPS,
    SEARCH_MAX_RESULTS,
    SOURCE_DB,
    SOURCE_TRANSITOUS,
    STATUS_NOCACHE_PARAM,
    STATUS_PROBE_ROWS,
)
from .stations import (
    Board,
    Position,
    Stop,
    combine_boards,
    line_key,
    link_networks,
    rank_search,
    station_names,
    tidy_line,
)

_LOGGER = logging.getLogger(__name__)

# Gateway errors mean the server behind a proxy is down. Other HTTP errors
# come from DBF itself, which is up.
_UNREACHABLE_STATUS = {502, 503, 504}


class DbfError(Exception):
    """DBF gave an unusable answer."""


class ServerUnreachable(DbfError):
    """Your DBF server can't be asked right now."""


class DbfClient:
    """Your DBF server, with fast failure while it is down."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        """Initialize the client from the entry's settings."""
        self.hass = hass
        self._session = async_get_clientsession(hass)
        self.server = config[CONF_SERVER_URL].rstrip("/")
        # Links default to the server URL when no public URL is set.
        self._public = (config.get(CONF_PUBLIC_URL) or self.server).rstrip("/")
        self.sources: list[str] = config[CONF_SOURCES]
        self.merge_networks = bool(config.get(CONF_MERGE_NETWORKS, DEFAULT_MERGE_NETWORKS))
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
        self._iris_tz: tzinfo | None = None
        self._coordinate_issue: bool | None = None
        self._boards: dict[str, tuple[datetime, Board]] = {}
        self._rides: dict[str, tuple[datetime, dict[str, Any] | None]] = {}
        # Reachability of your server.
        self._down_since: datetime | None = None
        self._down_reason = ""
        self._retry_at: datetime | None = None

    async def async_setup(self) -> None:
        """Load the IRIS time zone without blocking the event loop."""
        self._iris_tz = await dt_util.async_get_time_zone(IRIS_TIME_ZONE)

    @property
    def down(self) -> bool:
        """Return whether your server failed to answer lately."""
        return self._down_since is not None

    def board_url(self, eva: str, source: str, via: str | None = None) -> str | None:
        """Return the departure board URL for a stop (none for Transitous stops)."""
        if source == SOURCE_TRANSITOUS:
            return None
        url = f"{self._public}/{quote(eva)}"
        hafas = via or (None if source == SOURCE_DB else source)
        if hafas:
            url += f"?hafas={quote(hafas)}"
        return url

    # --- Requests -------------------------------------------------------

    async def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Ask your DBF server.

        After a failure it is skipped for PRIMARY_RETRY_AFTER, so an outage
        costs one timeout, not one per stop.
        """
        if self._retry_at is None or dt_util.utcnow() >= self._retry_at:
            try:
                payload = await self._send(method, self.server + path, **kwargs)
            except ServerUnreachable as err:
                self._mark_down(str(err))
            else:
                self._mark_up()
                return payload
        raise ServerUnreachable(
            f"Your DBF server at {self.server} isn't reachable ({self._down_reason})"
        )

    async def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            async with (
                self._semaphore,
                self._session.request(
                    method,
                    url,
                    timeout=aiohttp.ClientTimeout(
                        total=REQUEST_TIMEOUT, sock_connect=CONNECT_TIMEOUT
                    ),
                    **kwargs,
                ) as resp,
            ):
                if resp.status in _UNREACHABLE_STATUS:
                    raise ServerUnreachable(f"HTTP {resp.status}")
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientConnectionError, TimeoutError) as err:
            raise ServerUnreachable(str(err) or type(err).__name__) from err
        except (aiohttp.ClientError, ValueError) as err:
            raise DbfError(str(err) or type(err).__name__) from err

    @callback
    def _mark_down(self, reason: str) -> None:
        now = dt_util.utcnow()
        self._down_reason = reason
        self._retry_at = now + PRIMARY_RETRY_AFTER
        if self._down_since is None:
            self._down_since = now
            _LOGGER.warning(
                "DBF server at %s isn't reachable (%s); retrying every minute",
                self.server,
                reason,
            )

    @callback
    def _mark_up(self) -> None:
        if self._down_since is None:
            return
        _LOGGER.info("DBF server at %s is reachable again", self.server)
        self._down_since = self._retry_at = None
        self._down_reason = ""

    async def async_check(self, eva: str, source: str) -> str | None:
        """Fetch one live board; return why it failed, or None if DBF works.

        The URL is unique per check, so a cache in front of DBF (which keeps
        serving old boards while DBF fails) can't answer it. DBF's own
        "no data" answers (HTTP 300) count as working; server errors,
        timeouts and gateway errors don't. A check always reaches the
        server, also inside the fast-fail window, and ends it on success.
        """
        params = {
            "mode": "json",
            "limit": str(STATUS_PROBE_ROWS),
            STATUS_NOCACHE_PARAM: str(int(dt_util.utcnow().timestamp())),
        }
        if source != SOURCE_DB:
            params["hafas"] = source
        try:
            await self._send("GET", f"{self.server}/{quote(eva)}", params=params)
        except ServerUnreachable as err:
            self._mark_down(str(err))
            return str(err)
        except DbfError as err:
            return str(err)
        self._mark_up()
        return None

    # --- Stops ----------------------------------------------------------

    async def fetch_candidates(self, form: dict[str, str]) -> list[Stop]:
        """Ask /_geolocation once per HAFAS source.

        Returns the stops of the configured sources in DBF's order.
        """
        # Every /_geolocation call also returns the IRIS (DB) stations, so one
        # call per HAFAS service covers DB too.
        services = [s for s in self.sources if s != SOURCE_DB]
        candidates: list[dict[str, Any]] = []
        for service in services or [None]:
            data = dict(form)
            if service:
                data["hafas"] = service
            payload = await self.request_json("POST", "/_geolocation", data=data)
            if not isinstance(payload, dict):
                raise DbfError("Unexpected answer from DBF")
            if payload.get("error"):
                raise DbfError(str(payload["error"]))
            if payload.get("warning"):
                _LOGGER.debug("spdbf warning for %s: %s", service, payload["warning"])
            candidates.extend(payload.get("candidates") or [])

        stops: dict[str, Stop] = {}
        missing_coordinates = False
        for cand in candidates:
            source = SOURCE_DB if cand.get("hafas") == 0 else cand.get("hafas")
            if source not in self.sources:
                continue
            if cand.get("lat") is None or cand.get("lon") is None:
                missing_coordinates = True
                continue
            stop = Stop(
                name=cand["name"],
                eva=str(cand["eva"]),
                source=source,
                latitude=float(cand["lat"]),
                longitude=float(cand["lon"]),
            )
            stops.setdefault(stop.key, stop)
        if "lat" in form:
            self._set_coordinate_issue(missing_coordinates)
        return list(stops.values())

    async def async_search(self, query: str, pos: Position | None) -> list[Stop]:
        """Return the stops whose name matches, best matches first."""
        try:
            stops = await self.fetch_candidates({"q": query})
        except ServerUnreachable:
            raise
        except DbfError as err:
            if "lon/lat" in str(err):
                raise DbfError(
                    "This DBF server can't search by name. Use the patched image"
                    " ghcr.io/drakknar/db-fakedisplay (station-name-search)."
                ) from err
            raise
        if self.merge_networks:
            stops = link_networks(stops)
        return rank_search(stops, query, lambda stop: distance_m(stop, pos))[
            :SEARCH_MAX_RESULTS
        ]

    @callback
    def _set_coordinate_issue(self, missing: bool) -> None:
        if missing == self._coordinate_issue:
            return
        self._coordinate_issue = missing
        if missing:
            _LOGGER.warning(
                "spdbf at %s returns HAFAS stops without coordinates; they are"
                " hidden from the map until the container is patched",
                self.server,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_MISSING_COORDINATES,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_MISSING_COORDINATES,
                translation_placeholders={"server": self.server},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_MISSING_COORDINATES)

    # --- Departures -----------------------------------------------------

    async def async_board(
        self, eva: str, source: str, linked: list[tuple[str, str]]
    ) -> Board:
        """Return the full departure board of any stop, briefly cached.

        linked lists the same station in other networks as (eva, source);
        their missing lines are included.
        """
        key = "+".join(f"{s}_{e}" for e, s in [(eva, source), *linked])
        now = dt_util.utcnow()
        if (cached := self._boards.get(key)) and now - cached[0] < BOARD_CACHE_TTL:
            return cached[1]
        board = await self.fetch_departures(
            eva, source, BOARD_ROWS, BOARD_DEPARTURES, linked
        )
        self._boards = {
            k: v for k, v in self._boards.items() if now - v[0] < BOARD_CACHE_TTL
        }
        if not board.unreachable:
            self._boards[key] = (now, board)
        return board

    async def fetch_departures(
        self,
        eva: str,
        source: str,
        rows: int,
        keep: int,
        linked: list[tuple[str, str]] | None = None,
    ) -> Board:
        """Return a stop's departures, with the missing lines of linked stops."""
        board, *others = await asyncio.gather(
            self._fetch_stop_departures(eva, source, rows, keep),
            *(self._fetch_stop_departures(e, s, rows, keep) for e, s in linked or []),
        )
        return combine_boards(board, others, keep) if others else board

    async def _fetch_stop_departures(
        self, eva: str, source: str, rows: int, keep: int
    ) -> Board:
        if source == SOURCE_DB:
            # DB's IRIS feed is empty for some stations (e.g. Berlin
            # Zoologischer Garten). ÖBB's HAFAS knows DB station numbers and
            # fills the gap.
            attempts = [(None, self._parse_iris), (DB_FALLBACK_HAFAS, self._parse_hafas)]
        else:
            attempts = [(source, self._parse_hafas)]

        errors = []
        failed = False
        for hafas, parse in attempts:
            try:
                departures, error = await self._fetch_board(eva, hafas, parse, rows)
            except ServerUnreachable as err:
                # No point trying the next backend on the same server.
                return Board(error=str(err), unreachable=True)
            except DbfError as err:
                _LOGGER.debug("Departures for %s (%s) failed: %s", eva, hafas, err)
                errors.append(str(err))
                failed = True
                continue
            if departures:
                return Board(
                    departures=departures[:keep],
                    via=hafas if hafas != source else None,
                )
            if error:
                errors.append(error)
        return Board(error="; ".join(errors) or None, failed=failed)

    async def _fetch_board(
        self, eva: str, hafas: str | None, parse: Any, rows: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a stop's departures from one backend, and its error if any."""
        params = {"mode": "json", "limit": str(rows)}
        if hafas:
            params["hafas"] = hafas
        payload = await self.request_json("GET", f"/{quote(eva)}", params=params)
        if not isinstance(payload, dict):
            return [], "unexpected response from spdbf"

        now = dt_util.now()
        departures = [
            dep
            for raw in payload.get("departures") or []
            if (dep := parse(raw, now, eva, hafas)) is not None
        ]
        departures.sort(key=lambda dep: dep["minutes"])
        # DBF answers HTTP 300 with an "error" field when a backend has no data.
        return departures, payload.get("error")

    def _parse_iris(
        self, raw: dict[str, Any], now: datetime, eva: str, hafas: str | None
    ) -> dict[str, Any] | None:
        scheduled = raw.get("scheduledDeparture")
        if not scheduled:
            return None  # terminates here
        try:
            hour, minute = (int(part) for part in scheduled.split(":"))
        except ValueError:
            return None
        local_now = now.astimezone(self._iris_tz)
        when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Boards only span a few hours, so pick the closest day around midnight.
        if when < local_now - timedelta(hours=12):
            when += timedelta(days=1)
        elif when > local_now + timedelta(hours=12):
            when -= timedelta(days=1)
        train = str(raw.get("train") or "").split()
        trip = None
        if train and raw.get("trainNumber"):
            # DB trips are found by train type and number at this station.
            trip = {"train": f"{train[0]} {raw['trainNumber']}", "station": eva}
        line = tidy_line(raw.get("train"), raw.get("trainNumber"))
        return _departure(raw, line, when, raw.get("delayDeparture") or 0, now, trip)

    def _parse_hafas(
        self, raw: dict[str, Any], now: datetime, eva: str, hafas: str | None
    ) -> dict[str, Any] | None:
        if raw.get("scheduledTime") is None:
            return None
        when = dt_util.as_local(dt_util.utc_from_timestamp(raw["scheduledTime"]))
        # tripId needs the patched DBF image (hafas-trip-id.pl).
        trip = {"id": raw["tripId"], "hafas": hafas} if raw.get("tripId") else None
        line = tidy_line(raw.get("train"))
        return _departure(raw, line, when, raw.get("delay") or 0, now, trip)

    # --- Trips ----------------------------------------------------------

    async def async_trip(self, trip: dict[str, Any]) -> dict[str, Any]:
        """Return a trip's stops, from the trip reference of a departure."""
        if trip.get("id"):
            path = f"/z/{quote(trip['id'], safe='')}.json"
            payload = await self.request_json("GET", path, params={"hafas": trip["hafas"]})
            return _hafas_trip(payload)
        path = f"/z/{quote(trip['train'], safe='')}/{quote(trip['station'], safe='')}.json"
        return _iris_trip(await self.request_json("GET", path))

    async def async_ride(
        self, start: dict[str, str], end: dict[str, str], lines: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Return the scheduled ride from one station to another, or None.

        start and end are {eva?, name?, network?}. DB's trip data has no
        reliable per-stop times for S-Bahn trains (only names, or another
        train's times), so the ride comes from HAFAS (BVG) trips: the next
        trains from start's HAFAS stop, until one calls at end. A DB start
        station is matched to its BVG stop with the DB + BVG rules. Needs
        tripId from the patched DBF image; without it, None.
        """
        key = "ride:" + "|".join(
            f"{station.get('eva')}/{station.get('name')}" for station in (start, end)
        ) + "|" + ",".join(lines or [])
        now = dt_util.utcnow()
        if (cached := self._rides.get(key)) and now - cached[0] < RIDE_CACHE_TTL:
            return cached[1]
        ride = await self._find_ride(start, end, lines or [])
        self._rides = {k: v for k, v in self._rides.items() if now - v[0] < RIDE_CACHE_TTL}
        self._rides[key] = (now, ride)
        return ride

    async def _find_ride(
        self, start: dict[str, str], end: dict[str, str], lines: list[str]
    ) -> dict[str, Any] | None:
        hafas = await self._hafas_stop(start)
        if hafas is None:
            return None
        eva, service = hafas
        board = await self.fetch_departures(eva, service, 40, 40)
        if board.unreachable:
            raise ServerUnreachable(board.error or "DBF isn't reachable")
        wanted = {line_key(line) for line in lines}
        tried = 0
        for dep in board.departures:
            if not (dep.get("trip") or {}).get("id"):
                continue
            if wanted and line_key(dep["line"]) not in wanted:
                continue
            tried += 1
            if tried > RIDE_MAX_TRIPS:
                break
            data = await self.async_trip(dep["trip"])
            ride = _ride_on(data, {"eva": eva, "name": start.get("name")}, end)
            if ride:
                return ride
        return None

    async def _hafas_stop(self, station: dict[str, str]) -> tuple[str, str] | None:
        """Return the HAFAS (eva, service) of a station."""
        network = station.get("network")
        if network and network != SOURCE_DB and station.get("eva"):
            return station["eva"], network
        if not station.get("name"):
            return None
        stops = await self.fetch_candidates({"q": station["name"]})
        for stop in link_networks(stops):
            members = [stop, *stop.linked]
            if stop.source != SOURCE_DB and any(
                _same_station({"name": m.name, "eva": m.eva}, station) for m in members
            ):
                return stop.eva, stop.source
        return None


def distance_m(stop: Stop, pos: Position | None) -> float | None:
    """Return the distance from a position to a stop."""
    if pos is None:
        return None
    return geo_distance(*pos, stop.latitude, stop.longitude)


def _departure(
    raw: dict[str, Any],
    line: str,
    scheduled: datetime,
    delay: int,
    now: datetime,
    trip: dict[str, Any] | None,
) -> dict[str, Any] | None:
    expected = scheduled + timedelta(minutes=delay)
    if expected < now - timedelta(minutes=1):
        return None
    dep = {
        "line": line,
        "destination": raw.get("destination"),
        "platform": raw.get("platform"),
        "scheduled": dt_util.as_local(scheduled).strftime("%H:%M"),
        "delay": delay,
        "minutes": max(0, int((expected - now).total_seconds() // 60)),
        "cancelled": bool(raw.get("isCancelled")),
        # Absolute, so a kept board can be shown later (last_known.py).
        "expected": dt_util.as_local(expected).isoformat(),
    }
    if trip:
        dep["trip"] = trip
    return dep


def _ride_on(
    data: dict[str, Any], start: dict[str, str], end: dict[str, str]
) -> dict[str, Any] | None:
    """Return the scheduled ride on a trip from start to end, or None."""
    if not data["times"]:
        return None
    stops = data["stops"]
    first = next(
        (i for i, stop in enumerate(stops) if stop.get("current") or _same_station(stop, start)),
        None,
    )
    if first is None:
        return None
    last = next(
        (i for i in range(first + 1, len(stops)) if _same_station(stops[i], end)), None
    )
    if last is None:
        return None
    leave = _minute_of_day(stops[first].get("departure") or stops[first].get("arrival"))
    arrive = _minute_of_day(stops[last].get("arrival") or stops[last].get("departure"))
    if leave is None or arrive is None:
        return None
    return {
        "minutes": (arrive - leave) % (24 * 60),
        "line": data["line"],
        "departure": f"{leave // 60:02d}:{leave % 60:02d}",
        "arrival": f"{arrive // 60:02d}:{arrive % 60:02d}",
    }


def _same_station(stop: dict[str, Any], station: dict[str, str]) -> bool:
    if station.get("eva") and stop.get("eva") and str(stop["eva"]) == str(station["eva"]):
        return True
    a, b = stop.get("name"), station.get("name")
    if not (a and b):
        return False
    # One of the names may start with the town (DB: "Berlin Alexanderplatz").
    # Dropping the first word of both would make "S Potsdam Hauptbahnhof"
    # equal "Berlin Hbf".
    return bool(
        station_names(a, drop_city=True) & station_names(b, drop_city=False)
        or station_names(a, drop_city=False) & station_names(b, drop_city=True)
    )


def _minute_of_day(value: Any) -> int | None:
    """Return the minute of the day of "HH:MM" or an ISO date-time."""
    text = str(value or "")
    clock = text[11:16] if len(text) >= 16 else text[:5]
    try:
        hour, minute = (int(part) for part in clock.split(":"))
    except ValueError:
        return None
    return hour * 60 + minute


def _iso(epoch: Any) -> str | None:
    if epoch is None:
        return None
    return dt_util.as_local(dt_util.utc_from_timestamp(int(epoch))).isoformat()


def _hafas_trip(payload: Any) -> dict[str, Any]:
    journey = payload.get("journey") if isinstance(payload, dict) else None
    if not isinstance(journey, dict):
        raise DbfError(
            str(payload.get("error")) if isinstance(payload, dict) else "No trip found"
        )
    stops = []
    for stop in journey.get("route") or []:
        loc = stop.get("loc") or {}
        stops.append({
            "name": loc.get("name"),
            "eva": str(loc["eva"]) if loc.get("eva") is not None else None,
            "arrival": _iso(stop.get("sched_arr")),
            "departure": _iso(stop.get("sched_dep")),
            "arrival_delay": stop.get("arr_delay"),
            "departure_delay": stop.get("dep_delay"),
            "platform": stop.get("rt_platform") or stop.get("platform")
            or stop.get("sched_platform"),
            "cancelled": bool(stop.get("arr_cancelled") or stop.get("dep_cancelled")),
        })
    messages = list(dict.fromkeys(
        msg["text"] for msg in journey.get("messages") or []
        if msg.get("is_him") and msg.get("text")
    ))
    return {
        "line": tidy_line(journey.get("line") or journey.get("name")),
        "direction": journey.get("direction"),
        "stops": stops,
        "times": True,
        "messages": messages,
    }


def _iris_trip(payload: Any) -> dict[str, Any]:
    """Return a DB trip.

    The stop names (route_pre, route_post) come from IRIS and belong to this
    train. The times (route_*_diff) come from a separate lookup by train
    number, which can pick another train with the same number (an S7 at
    Berlin Westkreuz once got the times of a train near Stuttgart), and for
    S-Bahn trains often hold names only. They are used only when they carry
    times and the stops next to this station match IRIS's.
    """
    dep = payload.get("departure") if isinstance(payload, dict) else None
    if not isinstance(dep, dict):
        raise DbfError(
            str(payload.get("error")) if isinstance(payload, dict) else "No trip found"
        )
    pre, post = list(dep.get("route_pre") or []), list(dep.get("route_post") or [])
    pre_diff, post_diff = dep.get("route_pre_diff") or [], dep.get("route_post_diff") or []

    def names(stops: list[Any]) -> list[str]:
        return [s.get("name") if isinstance(s, dict) else s for s in stops]

    # The timed lists may reach further than IRIS's (back to the train's
    # origin), so compare the neighbours of this station: the stop just
    # before and just after must be the same, and at least one must exist.
    # At Savignyplatz two of three S-Bahn trains got the times of trains
    # near Mannheim and Cologne (2026-10-14); this rejects those.
    neighbours = [
        (names(pre_diff)[-1:], pre[-1:]),
        (names(post_diff)[:1], post[:1]),
    ]
    timed = all(a == b for a, b in neighbours) and any(b for _, b in neighbours)
    # For S-Bahn trains the "timed" lists often hold only names.
    timed = timed and any(
        isinstance(item, dict) and (item.get("sched_arr") or item.get("sched_dep"))
        for item in [*pre_diff, *post_diff]
    )

    def stop_of(item: Any) -> dict[str, Any]:
        if not (timed and isinstance(item, dict)):
            return {"name": item if isinstance(item, str) else item.get("name")}
        return {
            "name": item.get("name"),
            "eva": str(item["eva"]) if item.get("eva") is not None else None,
            "arrival": item.get("sched_arr"),
            "departure": item.get("sched_dep"),
            "arrival_delay": item.get("arr_delay"),
            "departure_delay": item.get("dep_delay"),
            "platform": item.get("platform") or item.get("sched_platform"),
            "cancelled": bool(item.get("isCancelled")),
        }

    here = {
        "name": None,  # the station the trip was opened from; the dialog knows it
        "eva": str(dep["eva"]) if dep.get("eva") is not None else None,
        "departure": dep.get("sched_departure"),
        "departure_delay": dep.get("departure_delay"),
        "platform": dep.get("platform") or dep.get("scheduled_platform"),
        "cancelled": bool(dep.get("is_cancelled")),
        "current": True,
    }
    stops = [stop_of(s) for s in (pre_diff if timed else pre)]
    stops.append(here)
    stops.extend(stop_of(s) for s in (post_diff if timed else post))
    return {
        "line": tidy_line(f"{dep.get('train_type') or ''} {dep.get('train_line') or ''}",
                          dep.get("train_no")),
        "direction": dep.get("destination"),
        "stops": stops,
        "times": timed,
        "messages": [],
    }

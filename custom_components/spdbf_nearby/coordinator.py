"""Data coordinator for spdbf Nearby Stops."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_GPS_ACCURACY, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.location import distance as geo_distance

from .const import (
    BOARD_CACHE_TTL,
    BOARD_DEPARTURES,
    BOARD_ROWS,
    CONF_DEPARTURES,
    CONF_FALLBACK,
    CONF_FALLBACK_URL,
    CONF_MAX_STOPS,
    CONF_MERGE_DUPLICATES,
    CONF_PUBLIC_URL,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    CONF_SERVER_URL,
    CONF_SOURCES,
    CONF_TRACKED_ENTITY,
    CONNECT_TIMEOUT,
    DB_FALLBACK_HAFAS,
    DEFAULT_FALLBACK,
    DEFAULT_FALLBACK_URL,
    DEFAULT_MERGE_DUPLICATES,
    DOMAIN,
    FALLBACK_MAX_REQUESTS,
    FALLBACK_STOPS,
    FALLBACK_WINDOW,
    IRIS_TIME_ZONE,
    ISSUE_MISSING_COORDINATES,
    ISSUE_SERVER_UNREACHABLE,
    MAX_GPS_ACCURACY_METERS,
    MAX_PARALLEL_REQUESTS,
    MERGE_CANDIDATE_FACTOR,
    MERGE_MIN_DEPARTURES,
    MIN_MOVE_METERS,
    PRIMARY_RETRY_AFTER,
    RAIL_NAME_PREFIXES,
    REQUEST_TIMEOUT,
    SEARCH_MAX_RESULTS,
    SOURCE_DB,
    STOP_LOOKUP_MAX_AGE,
    UNREACHABLE_ISSUE_AFTER,
)

_LOGGER = logging.getLogger(__name__)

type Position = tuple[float, float]

# Gateway errors mean the server behind a proxy is down. Other HTTP errors
# come from DBF itself, which is up.
_UNREACHABLE_STATUS = {502, 503, 504}


class DbfError(Exception):
    """DBF gave an unusable answer."""


class ServerUnreachable(DbfError):
    """No DBF server could be asked: yours is down and no fallback could step in."""


@dataclass
class Board:
    """Departures of one stop."""

    departures: list[dict[str, Any]] = field(default_factory=list)
    # Why no departures could be fetched, if no source had any.
    error: str | None = None
    # HAFAS service the departures came from, when it differs from the source.
    via: str | None = None
    # The departures came from the public fallback instance.
    fallback: bool = False
    # No server could be asked (the error says why).
    unreachable: bool = False


@dataclass
class Stop:
    """A stop returned by DBF's /_geolocation endpoint."""

    name: str
    eva: str
    source: str
    latitude: float
    longitude: float
    board: Board = field(default_factory=Board)
    # Names of nearby stops with the same departure board, merged into this one.
    merged: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Return a key that is unique across sources."""
        return f"{self.source}_{self.eva}"

    @property
    def kind(self) -> str:
        """Return the marker kind: train, bus or stop."""
        if self.source == SOURCE_DB or self.name.startswith(RAIL_NAME_PREFIXES):
            return "train"
        if self.source == "BVG":
            return "bus"
        return "stop"


def position_of(state: State | None) -> Position | None:
    """Return a usable (lat, lon) from a person/device_tracker state."""
    if state is None:
        return None
    lat = state.attributes.get(ATTR_LATITUDE)
    lon = state.attributes.get(ATTR_LONGITUDE)
    if lat is None or lon is None:
        return None
    accuracy = state.attributes.get(ATTR_GPS_ACCURACY)
    if accuracy is not None and accuracy > MAX_GPS_ACCURACY_METERS:
        return None
    return float(lat), float(lon)


class SpdbfCoordinator(DataUpdateCoordinator[dict[str, Stop]]):
    """Look up stops near the tracked entity and keep their departures fresh.

    It also answers the dashboard's stop search and departure dialog, which
    ask for any stop rather than only the nearby ones.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.config = {**entry.data, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=int(self.config[CONF_SCAN_INTERVAL])),
        )
        self._session = async_get_clientsession(hass)
        self._server = self.config[CONF_SERVER_URL].rstrip("/")
        # Links default to the server URL when no public URL is set.
        self._public = (self.config.get(CONF_PUBLIC_URL) or self._server).rstrip("/")
        self._fallback: str | None = None
        if self.config.get(CONF_FALLBACK, DEFAULT_FALLBACK):
            self._fallback = (
                self.config.get(CONF_FALLBACK_URL) or DEFAULT_FALLBACK_URL
            ).rstrip("/")
        self.tracked = self.config[CONF_TRACKED_ENTITY]
        self.sources: list[str] = self.config[CONF_SOURCES]
        self._radius_m = float(self.config[CONF_RADIUS]) * 1000
        self._max_stops = int(self.config[CONF_MAX_STOPS])
        self._departures = int(self.config[CONF_DEPARTURES])
        self._merge = bool(
            self.config.get(CONF_MERGE_DUPLICATES, DEFAULT_MERGE_DUPLICATES)
        )
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
        self._iris_tz: tzinfo | None = None
        self._stops: dict[str, Stop] = {}
        self._lookup_pos: Position | None = None
        self._lookup_time: datetime | None = None
        self._coordinate_issue: bool | None = None
        self._boards: dict[str, tuple[datetime, Board]] = {}
        # Reachability of your server, and the fallback's request budget.
        self._down_since: datetime | None = None
        self._down_reason = ""
        self._retry_at: datetime | None = None
        self._unreachable_issue = False
        self._fallback_requests: deque[datetime] = deque()
        self.position: Position | None = None

    async def _async_setup(self) -> None:
        """Load the IRIS time zone without blocking the event loop."""
        self._iris_tz = await dt_util.async_get_time_zone(IRIS_TIME_ZONE)

    @callback
    def async_start_tracking(self) -> CALLBACK_TYPE:
        """Follow the tracked entity; returns the unsubscribe callback."""
        return async_track_state_change_event(
            self.hass, [self.tracked], self._handle_location_change
        )

    @callback
    def _handle_location_change(self, event: Event[EventStateChangedData]) -> None:
        pos = position_of(event.data["new_state"])
        if pos is None:
            return
        self.position = pos
        if self._moved_since_lookup(pos):
            self.hass.async_create_task(self.async_request_refresh())
        else:
            # Small move: only the distances change, no need to ask spdbf.
            self.async_update_listeners()

    @property
    def fallback_active(self) -> bool:
        """Return whether your server is down and the public fallback steps in."""
        return self._fallback is not None and self._down_since is not None

    def board_url(
        self, eva: str, source: str, via: str | None = None, fallback: bool = False
    ) -> str:
        """Return the departure board URL for a stop."""
        base = self._fallback if fallback and self._fallback else self._public
        url = f"{base}/{quote(eva)}"
        hafas = via or (None if source == SOURCE_DB else source)
        if hafas:
            url += f"?hafas={quote(hafas)}"
        return url

    def distance_m(self, stop: Stop, pos: Position | None = None) -> float | None:
        """Return the distance from a position (default: the tracked one)."""
        pos = pos or self.position
        if pos is None:
            return None
        return geo_distance(*pos, stop.latitude, stop.longitude)

    def _moved_since_lookup(self, pos: Position) -> bool:
        if self._lookup_pos is None:
            return True
        moved = geo_distance(*pos, *self._lookup_pos)
        return moved is None or moved >= MIN_MOVE_METERS

    def _needs_lookup(self) -> bool:
        assert self.position is not None
        return (
            self._lookup_time is None
            or dt_util.utcnow() - self._lookup_time > STOP_LOOKUP_MAX_AGE
            or self._moved_since_lookup(self.position)
        )

    async def _async_update_data(self) -> dict[str, Stop]:
        pos = position_of(self.hass.states.get(self.tracked))
        if pos is not None:
            self.position = pos
        if self.position is None:
            return {}

        fresh = False
        if self._needs_lookup():
            try:
                # The fallback only looks stops up while the map has none: it
                # has no coordinates for BVG stops, and requests are scarce.
                candidates, via_fallback = await self._fetch_stops(
                    self.position, allow_fallback=not self._stops
                )
            except DbfError as err:
                if not self._stops:
                    raise UpdateFailed(str(err)) from err
                _LOGGER.debug("Stop lookup failed, keeping previous stops: %s", err)
            else:
                if via_fallback:
                    # Not remembered as a lookup, so a full one follows as
                    # soon as your server is back.
                    self._stops = {s.key: s for s in candidates[: self._max_stops]}
                else:
                    self._stops = await self._pick_stops(candidates)
                    fresh = self._merge and bool(self._departures)
                    self._lookup_pos = self.position
                    self._lookup_time = dt_util.utcnow()

        if self._departures and not fresh:
            stops = list(self._stops.values())
            if self.fallback_active:
                stops.sort(key=lambda stop: self.distance_m(stop) or 0)
                for stop in stops[FALLBACK_STOPS:]:
                    stop.board = Board(
                        error="Paused while your DBF server is unreachable"
                        " (the public fallback only updates the nearest stops)",
                        unreachable=True,
                    )
                stops = stops[:FALLBACK_STOPS]
            await asyncio.gather(*(self._update_departures(stop) for stop in stops))
        return dict(self._stops)

    # --- Talking to DBF -------------------------------------------------

    async def _request_json(
        self, method: str, path: str, *, allow_fallback: bool = True, **kwargs: Any
    ) -> tuple[Any, bool]:
        """Ask your DBF server, or the public fallback while yours is down.

        Returns the JSON answer and whether it came from the fallback. After
        a failure your server is skipped for PRIMARY_RETRY_AFTER, so an outage
        costs one timeout, not one per stop.
        """
        if self._retry_at is None or dt_util.utcnow() >= self._retry_at:
            try:
                payload = await self._send(method, self._server + path, **kwargs)
            except ServerUnreachable as err:
                self._mark_down(str(err))
            else:
                self._mark_up()
                return payload, False

        message = f"Your DBF server at {self._server} isn't reachable ({self._down_reason})"
        if not (self._fallback and allow_fallback):
            raise ServerUnreachable(message)
        self._take_fallback_slot()
        try:
            return await self._send(method, self._fallback + path, **kwargs), True
        except ServerUnreachable as err:
            raise ServerUnreachable(
                f"{message}, and neither is the fallback {self._fallback} ({err})"
            ) from err

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

    def _take_fallback_slot(self) -> None:
        """Count a fallback request, or refuse it if the budget is used up."""
        now = dt_util.utcnow()
        while self._fallback_requests and now - self._fallback_requests[0] > FALLBACK_WINDOW:
            self._fallback_requests.popleft()
        if len(self._fallback_requests) >= FALLBACK_MAX_REQUESTS:
            raise ServerUnreachable(
                f"Your DBF server at {self._server} isn't reachable, and the public"
                f" fallback's limit of {FALLBACK_MAX_REQUESTS} requests per minute"
                " is used up; try again shortly"
            )
        self._fallback_requests.append(now)

    @callback
    def _mark_down(self, reason: str) -> None:
        now = dt_util.utcnow()
        self._down_reason = reason
        self._retry_at = now + PRIMARY_RETRY_AFTER
        if self._down_since is None:
            self._down_since = now
            _LOGGER.warning(
                "DBF server at %s isn't reachable (%s); %s",
                self._server,
                reason,
                f"using {self._fallback} until it is back"
                if self._fallback
                else "retrying every minute",
            )
        if not self._unreachable_issue and now - self._down_since >= UNREACHABLE_ISSUE_AFTER:
            self._unreachable_issue = True
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_SERVER_UNREACHABLE}_{self.config_entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SERVER_UNREACHABLE,
                translation_placeholders={
                    "server": self._server,
                    "reason": reason,
                    "fallback": (
                        f"Departures come from the public fallback {self._fallback}"
                        " for now, for the nearest stops only."
                        if self._fallback
                        else "You can turn on the public fallback in the"
                        " integration's options to keep departures coming meanwhile."
                    ),
                },
            )

    @callback
    def _mark_up(self) -> None:
        if self._down_since is None:
            return
        _LOGGER.info("DBF server at %s is reachable again", self._server)
        self._down_since = self._retry_at = None
        self._down_reason = ""
        # Fallback boards and lookups are replaced by your server's.
        self._boards.clear()
        if self._unreachable_issue:
            self._unreachable_issue = False
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{ISSUE_SERVER_UNREACHABLE}_{self.config_entry.entry_id}"
            )

    # --- Stops ----------------------------------------------------------

    async def _fetch_candidates(
        self, form: dict[str, str], *, allow_fallback: bool = True
    ) -> tuple[list[Stop], bool, bool]:
        """Ask /_geolocation once per HAFAS source.

        Returns the stops of the configured sources in DBF's order, whether
        some had to be dropped for lack of coordinates, and whether the
        answer came from the fallback.
        """
        # Every /_geolocation call also returns the IRIS (DB) stations, so one
        # call per HAFAS service covers DB too.
        services = [s for s in self.sources if s != SOURCE_DB]
        candidates: list[dict[str, Any]] = []
        via_fallback = False
        for service in services or [None]:
            data = dict(form)
            if service:
                data["hafas"] = service
            payload, from_fallback = await self._request_json(
                "POST", "/_geolocation", data=data, allow_fallback=allow_fallback
            )
            via_fallback |= from_fallback
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
        return list(stops.values()), missing_coordinates, via_fallback

    async def _fetch_stops(
        self, pos: Position, *, allow_fallback: bool = True
    ) -> tuple[list[Stop], bool]:
        """Return the stops within the radius, nearest first, and whether
        they came from the fallback."""
        stops, missing_coordinates, via_fallback = await self._fetch_candidates(
            {"lat": str(pos[0]), "lon": str(pos[1])}, allow_fallback=allow_fallback
        )
        # The public instance never sends HAFAS coordinates; that's no
        # reason to tell you to patch your own server.
        if not via_fallback:
            self._set_coordinate_issue(missing_coordinates)

        in_range = []
        for stop in stops:
            if previous := self._stops.get(stop.key):
                stop.board = previous.board
            dist = geo_distance(*pos, stop.latitude, stop.longitude)
            if dist is not None and dist <= self._radius_m:
                in_range.append((dist, stop))
        in_range.sort(key=lambda item: item[0])
        return [stop for _, stop in in_range], via_fallback

    async def _pick_stops(self, candidates: list[Stop]) -> dict[str, Stop]:
        """Keep the nearest stops, merging stops that show the same board.

        BVG lists one station several times (S+U Alexanderplatz Bhf, .../
        Dircksenstr., .../Gontardstr., ...), and their boards are identical.
        Only exact duplicates are merged: similar names are no safe guide,
        since a tram stop with the same name can have a board of its own.
        """
        if not (self._merge and self._departures):
            return {stop.key: stop for stop in candidates[: self._max_stops]}

        pool = candidates[: self._max_stops * MERGE_CANDIDATE_FACTOR]
        await asyncio.gather(*(self._update_departures(stop) for stop in pool))

        groups: dict[Any, list[Stop]] = {}
        for stop in pool:
            groups.setdefault(_board_signature(stop) or stop.key, []).append(stop)

        picked: dict[str, Stop] = {}
        # Groups keep the order of their nearest stop.
        for group in list(groups.values())[: self._max_stops]:
            # The shortest name is usually the station itself, without the
            # street of its exit or bus bay.
            keep = min(group, key=lambda stop: len(stop.name))
            keep.merged = [stop.name for stop in group if stop is not keep]
            picked[keep.key] = keep
        return picked

    async def async_search(self, query: str) -> list[Stop]:
        """Return the stops whose name matches, in DBF's order."""
        try:
            # Stock DBF (the public fallback) can't search by name as JSON.
            stops, _, _ = await self._fetch_candidates({"q": query}, allow_fallback=False)
        except ServerUnreachable as err:
            raise ServerUnreachable(f"Search needs your own DBF server. {err}") from err
        except DbfError as err:
            if "lon/lat" in str(err):
                raise DbfError(
                    "This DBF server can't search by name. Use the patched image"
                    " ghcr.io/drakknar/db-fakedisplay (station-name-search)."
                ) from err
            raise
        return stops[:SEARCH_MAX_RESULTS]

    async def async_nearby(self, pos: Position | None) -> list[Stop]:
        """Return the stops near a position, nearest first.

        Near the tracked entity, these are the stops already on the map, with
        duplicates merged. Elsewhere it's a fresh lookup.
        """
        if pos is None or (
            self.position is not None
            and (moved := geo_distance(*pos, *self.position)) is not None
            and moved < MIN_MOVE_METERS
        ):
            stops = list((self.data or {}).values())
            stops.sort(key=lambda stop: self.distance_m(stop) or 0)
            return stops
        stops, _ = await self._fetch_stops(pos)
        return stops[: self._max_stops]

    # --- Departures -----------------------------------------------------

    async def async_board(self, eva: str, source: str) -> Board:
        """Return the full departure board of any stop, briefly cached."""
        key = f"{source}_{eva}"
        now = dt_util.utcnow()
        if (cached := self._boards.get(key)) and now - cached[0] < BOARD_CACHE_TTL:
            return cached[1]
        board = await self._fetch_departures(eva, source, BOARD_ROWS, BOARD_DEPARTURES)
        self._boards = {
            k: v for k, v in self._boards.items() if now - v[0] < BOARD_CACHE_TTL
        }
        if not board.unreachable:
            self._boards[key] = (now, board)
        return board

    async def _update_departures(self, stop: Stop) -> None:
        # IRIS boards include arrival-only entries, so ask for extra rows.
        stop.board = await self._fetch_departures(
            stop.eva, stop.source, self._departures * 3, self._departures
        )

    async def _fetch_departures(
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
        for hafas, parse in attempts:
            try:
                departures, error, fallback = await self._fetch_board(
                    eva, hafas, parse, rows
                )
            except ServerUnreachable as err:
                # No point trying the next backend on the same server.
                return Board(error=str(err), unreachable=True)
            if departures:
                return Board(
                    departures=departures[:keep],
                    via=hafas if hafas != source else None,
                    fallback=fallback,
                )
            if error:
                errors.append(error)
        return Board(error="; ".join(errors) or None)

    async def _fetch_board(
        self, eva: str, hafas: str | None, parse: Any, rows: int
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Return a stop's departures from one backend, its error if any, and
        whether they came from the fallback."""
        params = {"mode": "json", "limit": str(rows)}
        if hafas:
            params["hafas"] = hafas
        try:
            payload, fallback = await self._request_json(
                "GET", f"/{quote(eva)}", params=params
            )
        except ServerUnreachable:
            raise
        except DbfError as err:
            _LOGGER.debug("Departures for %s (%s) failed: %s", eva, hafas, err)
            return [], str(err), False
        if not isinstance(payload, dict):
            return [], "unexpected response from spdbf", fallback

        now = dt_util.now()
        departures = [
            dep
            for raw in payload.get("departures") or []
            if (dep := parse(raw, now)) is not None
        ]
        departures.sort(key=lambda dep: dep["minutes"])
        # DBF answers HTTP 300 with an "error" field when a backend has no data.
        return departures, payload.get("error"), fallback

    def _parse_iris(self, raw: dict[str, Any], now: datetime) -> dict[str, Any] | None:
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
        return _departure(raw, when, raw.get("delayDeparture") or 0, now)

    def _parse_hafas(self, raw: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        if raw.get("scheduledTime") is None:
            return None
        when = dt_util.as_local(dt_util.utc_from_timestamp(raw["scheduledTime"]))
        return _departure(raw, when, raw.get("delay") or 0, now)

    @callback
    def _set_coordinate_issue(self, missing: bool) -> None:
        if missing == self._coordinate_issue:
            return
        self._coordinate_issue = missing
        if missing:
            _LOGGER.warning(
                "spdbf at %s returns HAFAS stops without coordinates; they are"
                " hidden from the map until the container is patched",
                self._server,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_MISSING_COORDINATES,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_MISSING_COORDINATES,
                translation_placeholders={"server": self._server},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_MISSING_COORDINATES)


def _board_signature(stop: Stop) -> tuple[Any, ...] | None:
    """Return what identifies a stop's board, if it has enough departures."""
    if len(stop.board.departures) < MERGE_MIN_DEPARTURES:
        return None
    return (
        stop.source,
        *(
            (dep["line"], dep["destination"], dep["scheduled"])
            for dep in stop.board.departures
        ),
    )


def _departure(
    raw: dict[str, Any], scheduled: datetime, delay: int, now: datetime
) -> dict[str, Any] | None:
    expected = scheduled + timedelta(minutes=delay)
    if expected < now - timedelta(minutes=1):
        return None
    return {
        "line": raw.get("train"),
        "destination": raw.get("destination"),
        "platform": raw.get("platform"),
        "scheduled": dt_util.as_local(scheduled).strftime("%H:%M"),
        "delay": delay,
        "minutes": max(0, int((expected - now).total_seconds() // 60)),
        "cancelled": bool(raw.get("isCancelled")),
    }

"""Data coordinator for spdbf Nearby Stops."""

from __future__ import annotations

import asyncio
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
    CONF_DEPARTURES,
    CONF_MAX_STOPS,
    CONF_PUBLIC_URL,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    CONF_SERVER_URL,
    CONF_SOURCES,
    CONF_TRACKED_ENTITY,
    DB_FALLBACK_HAFAS,
    DOMAIN,
    IRIS_TIME_ZONE,
    ISSUE_MISSING_COORDINATES,
    MAX_GPS_ACCURACY_METERS,
    MAX_PARALLEL_REQUESTS,
    MIN_MOVE_METERS,
    REQUEST_TIMEOUT,
    SOURCE_DB,
    STOP_LOOKUP_MAX_AGE,
)

_LOGGER = logging.getLogger(__name__)

type Position = tuple[float, float]


@dataclass
class Stop:
    """A stop returned by DBF's /_geolocation endpoint."""

    name: str
    eva: str
    source: str
    latitude: float
    longitude: float
    departures: list[dict[str, Any]] = field(default_factory=list)
    # Why no departures could be fetched, if no source had any.
    departures_error: str | None = None
    # HAFAS service the departures came from, when it differs from the source.
    departures_via: str | None = None

    @property
    def key(self) -> str:
        """Return a key that is unique across sources."""
        return f"{self.source}_{self.eva}"


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
    """Look up stops near the tracked entity and keep their departures fresh."""

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
        self._tracked = self.config[CONF_TRACKED_ENTITY]
        self._sources: list[str] = self.config[CONF_SOURCES]
        self._radius_m = float(self.config[CONF_RADIUS]) * 1000
        self._max_stops = int(self.config[CONF_MAX_STOPS])
        self._departures = int(self.config[CONF_DEPARTURES])
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
        self._iris_tz: tzinfo | None = None
        self._stops: dict[str, Stop] = {}
        self._lookup_pos: Position | None = None
        self._lookup_time: datetime | None = None
        self._coordinate_issue: bool | None = None
        self.position: Position | None = None

    async def _async_setup(self) -> None:
        """Load the IRIS time zone without blocking the event loop."""
        self._iris_tz = await dt_util.async_get_time_zone(IRIS_TIME_ZONE)

    @callback
    def async_start_tracking(self) -> CALLBACK_TYPE:
        """Follow the tracked entity; returns the unsubscribe callback."""
        return async_track_state_change_event(
            self.hass, [self._tracked], self._handle_location_change
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

    def board_url(self, stop: Stop) -> str:
        """Return the public departure board URL for a stop."""
        url = f"{self._public}/{quote(stop.eva)}"
        hafas = stop.departures_via or (None if stop.source == SOURCE_DB else stop.source)
        if hafas:
            url += f"?hafas={quote(hafas)}"
        return url

    def distance_m(self, stop: Stop) -> float | None:
        """Return the distance from the current position to a stop."""
        if self.position is None:
            return None
        return geo_distance(*self.position, stop.latitude, stop.longitude)

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
        pos = position_of(self.hass.states.get(self._tracked))
        if pos is not None:
            self.position = pos
        if self.position is None:
            return {}

        if self._needs_lookup():
            try:
                self._stops = await self._fetch_stops(self.position)
            except UpdateFailed:
                if not self._stops:
                    raise
                _LOGGER.warning("Stop lookup failed, keeping previous stops")
            else:
                self._lookup_pos = self.position
                self._lookup_time = dt_util.utcnow()

        if self._departures:
            await asyncio.gather(
                *(self._update_departures(stop) for stop in self._stops.values())
            )
        return dict(self._stops)

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        async with (
            self._semaphore,
            self._session.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                **kwargs,
            ) as resp,
        ):
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _fetch_stops(self, pos: Position) -> dict[str, Stop]:
        # Every /_geolocation call also returns the IRIS (DB) stations, so one
        # call per HAFAS service covers DB too.
        services = [s for s in self._sources if s != SOURCE_DB]
        candidates: list[dict[str, Any]] = []
        for service in services or [None]:
            data = {"lat": str(pos[0]), "lon": str(pos[1])}
            if service:
                data["hafas"] = service
            try:
                payload = await self._request_json(
                    "POST", f"{self._server}/_geolocation", data=data
                )
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise UpdateFailed(f"Stop lookup at {self._server} failed: {err}") from err
            if payload.get("warning"):
                _LOGGER.debug("spdbf warning for %s: %s", service, payload["warning"])
            candidates.extend(payload.get("candidates") or [])

        stops: dict[str, Stop] = {}
        missing_coordinates = False
        for cand in candidates:
            source = SOURCE_DB if cand.get("hafas") == 0 else cand.get("hafas")
            if source not in self._sources:
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
            if previous := self._stops.get(stop.key):
                stop.departures = previous.departures
                stop.departures_error = previous.departures_error
                stop.departures_via = previous.departures_via
            stops.setdefault(stop.key, stop)
        self._set_coordinate_issue(missing_coordinates)

        in_range = []
        for stop in stops.values():
            dist = geo_distance(*pos, stop.latitude, stop.longitude)
            if dist is not None and dist <= self._radius_m:
                in_range.append((dist, stop))
        in_range.sort(key=lambda item: item[0])
        return {stop.key: stop for _, stop in in_range[: self._max_stops]}

    async def _update_departures(self, stop: Stop) -> None:
        if stop.source == SOURCE_DB:
            # DB's IRIS feed is empty for some stations (e.g. Berlin
            # Zoologischer Garten). ÖBB's HAFAS knows DB station numbers and
            # fills the gap.
            attempts = [(None, self._parse_iris), (DB_FALLBACK_HAFAS, self._parse_hafas)]
        else:
            attempts = [(stop.source, self._parse_hafas)]

        errors = []
        for hafas, parse in attempts:
            departures, error = await self._fetch_board(stop, hafas, parse)
            if departures:
                stop.departures = departures
                stop.departures_error = None
                stop.departures_via = hafas if hafas != stop.source else None
                return
            if error:
                errors.append(error)
        stop.departures = []
        stop.departures_error = "; ".join(errors) or None
        stop.departures_via = None

    async def _fetch_board(
        self, stop: Stop, hafas: str | None, parse: Any
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a stop's departures from one backend, and its error if any."""
        # IRIS boards include arrival-only entries, so ask for extra rows.
        params = {"mode": "json", "limit": str(self._departures * 3)}
        if hafas:
            params["hafas"] = hafas
        try:
            payload = await self._request_json(
                "GET", f"{self._server}/{quote(stop.eva)}", params=params
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Departures for %s (%s) failed: %s", stop.name, hafas, err)
            return [], str(err) or type(err).__name__
        if not isinstance(payload, dict):
            return [], "unexpected response from spdbf"

        now = dt_util.now()
        departures = [
            dep
            for raw in payload.get("departures") or []
            if (dep := parse(raw, now)) is not None
        ]
        departures.sort(key=lambda dep: dep["minutes"])
        # DBF answers HTTP 300 with an "error" field when a backend has no data.
        return departures[: self._departures], payload.get("error")

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

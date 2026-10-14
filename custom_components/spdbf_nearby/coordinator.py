"""The map feature: stops near the tracked person, with live departures.

Talking to DBF is in dbf.py and the station rules in stations.py; this
module follows the person and keeps the nearby stops up to date. With the
map turned off it does no polling, and only answers "nearby" requests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

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
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.location import distance as geo_distance

from .const import (
    CONF_DEPARTURES,
    CONF_MAP_STOPS,
    CONF_MAX_STOPS,
    CONF_MERGE_DUPLICATES,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    CONF_TRACKED_ENTITY,
    DEFAULT_MAP_STOPS,
    DEFAULT_MERGE_DUPLICATES,
    DOMAIN,
    FALLBACK_STOPS,
    MAX_GPS_ACCURACY_METERS,
    MERGE_CANDIDATE_FACTOR,
    MIN_MOVE_METERS,
    STOP_LOOKUP_MAX_AGE,
)
from .dbf import DbfClient, DbfError, distance_m
from .stations import (
    Board,
    Position,
    Stop,
    combine_boards,
    link_networks,
    merge_same_board,
)

_LOGGER = logging.getLogger(__name__)


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

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: DbfClient) -> None:
        """Initialize the coordinator."""
        self.config = {**entry.data, **entry.options}
        self.map_stops = bool(self.config.get(CONF_MAP_STOPS, DEFAULT_MAP_STOPS))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            # Without map stops there is nothing to keep up to date.
            update_interval=(
                timedelta(seconds=int(self.config[CONF_SCAN_INTERVAL]))
                if self.map_stops
                else None
            ),
        )
        self.client = client
        self.tracked = self.config[CONF_TRACKED_ENTITY]
        self._radius_m = float(self.config[CONF_RADIUS]) * 1000
        self._max_stops = int(self.config[CONF_MAX_STOPS])
        self._departures = int(self.config[CONF_DEPARTURES])
        self._merge = bool(
            self.config.get(CONF_MERGE_DUPLICATES, DEFAULT_MERGE_DUPLICATES)
        )
        self._stops: dict[str, Stop] = {}
        self._lookup_pos: Position | None = None
        self._lookup_time: datetime | None = None
        self.position: Position | None = None

    async def _async_setup(self) -> None:
        await self.client.async_setup()

    @property
    def sources(self) -> list[str]:
        """Return the configured sources."""
        return self.client.sources

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

    def distance_m(self, stop: Stop, pos: Position | None = None) -> float | None:
        """Return the distance from a position (default: the tracked one)."""
        return distance_m(stop, pos or self.tracked_position())

    def tracked_position(self) -> Position | None:
        """Return the tracked entity's position, also without map stops."""
        return self.position or position_of(self.hass.states.get(self.tracked))

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
        if not self.map_stops:
            return {}
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
                    self._stops, fresh = await self._pick_stops(candidates)
                    self._lookup_pos = self.position
                    self._lookup_time = dt_util.utcnow()

        if self._departures and not fresh:
            stops = list(self._stops.values())
            if self.client.fallback_active:
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

    async def _fetch_stops(
        self, pos: Position, *, allow_fallback: bool = True
    ) -> tuple[list[Stop], bool]:
        """Return the stops within the radius, nearest first, and whether
        they came from the fallback."""
        stops, via_fallback = await self.client.fetch_candidates(
            {"lat": str(pos[0]), "lon": str(pos[1])}, allow_fallback=allow_fallback
        )
        in_range = []
        for stop in stops:
            if previous := self._stops.get(stop.key):
                stop.board = previous.board
            dist = geo_distance(*pos, stop.latitude, stop.longitude)
            if dist is not None and dist <= self._radius_m:
                in_range.append((dist, stop))
        in_range.sort(key=lambda item: item[0])
        return [stop for _, stop in in_range], via_fallback

    async def _pick_stops(self, candidates: list[Stop]) -> tuple[dict[str, Stop], bool]:
        """Keep the nearest stops, merging stops that are one station.

        Returns the stops, and whether their departures were just fetched.
        Stops with the same board merge (stations.merge_same_board), and a
        DB station joins its BVG rail stop (stations.link_networks).
        """
        merge_boards = self._merge and bool(self._departures)
        if not (merge_boards or self.client.merge_networks):
            return {stop.key: stop for stop in candidates[: self._max_stops]}, False

        pool = candidates[: self._max_stops * MERGE_CANDIDATE_FACTOR]
        if merge_boards:
            await asyncio.gather(*(self._update_departures(stop) for stop in pool))
            pool = merge_same_board(pool)
        if self.client.merge_networks:
            pool = link_networks(pool)
        picked = pool[: self._max_stops]
        if merge_boards:
            for stop in picked:
                if stop.linked:
                    stop.board = combine_boards(
                        stop.board, [s.board for s in stop.linked], self._departures
                    )
        return {stop.key: stop for stop in picked}, merge_boards

    async def _update_departures(self, stop: Stop) -> None:
        # IRIS boards include arrival-only entries, so ask for extra rows.
        stop.board = await self.client.fetch_departures(
            stop.eva,
            stop.source,
            self._departures * 3,
            self._departures,
            [(s.eva, s.source) for s in stop.linked],
        )

    async def async_nearby(self, pos: Position | None) -> list[Stop]:
        """Return the stops near a position (default: the tracked one).

        Near the tracked entity, these are the stops already on the map, with
        duplicates merged. Elsewhere, or without map stops, it's a lookup.
        """
        if self.map_stops and (
            pos is None
            or (
                self.position is not None
                and (moved := geo_distance(*pos, *self.position)) is not None
                and moved < MIN_MOVE_METERS
            )
        ):
            stops = list((self.data or {}).values())
            stops.sort(key=lambda stop: self.distance_m(stop) or 0)
            return stops
        pos = pos or self.tracked_position()
        if pos is None:
            return []
        stops, _ = await self._fetch_stops(pos)
        if self.client.merge_networks:
            stops = link_networks(stops)
        return stops[: self._max_stops]

    async def async_board(
        self, eva: str, source: str, linked: list[tuple[str, str]] | None = None
    ) -> Board:
        """Return a stop's full board; a map stop brings its linked stations."""
        if linked is None:
            stop = self._stops.get(f"{source}_{eva}")
            linked = [(s.eva, s.source) for s in stop.linked] if stop else []
        return await self.client.async_board(eva, source, linked)

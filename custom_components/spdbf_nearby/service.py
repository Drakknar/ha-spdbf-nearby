"""Where departures come from: DBF, else Transitous, else the last known ones.

Every feature asks this service, never a source directly:

1. Your DBF server, as long as it delivers. A board that is merely empty
   (no service right now) is an answer too.
2. Transitous (option "fallback"), when DBF is unreachable or answers with
   an error.
3. The last good answer for the same board, trip, search or ride, marked
   with its age.

It also runs DBF's status checks (status.py).
"""

import asyncio
from datetime import datetime, timedelta
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    BOARD_DEPARTURES,
    BOARD_ROWS,
    CONF_FALLBACK,
    CONF_PROBLEM_AFTER,
    DEFAULT_FALLBACK,
    DEFAULT_PROBLEM_AFTER,
    FALLBACK_STOPS,
    LAST_KNOWN_BOARD_REFRESH,
    PAUSED_STOP_REFRESH,
    SEARCH_MAX_RESULTS,
    SOURCE_TRANSITOUS,
    TRANSITOUS_MAX_ROWS,
)
from .dbf import DbfClient, DbfError, ServerUnreachable, distance_m
from .last_known import LastKnown, refresh_departures
from .stations import Board, Position, Stop, rank_search, search_text
from .status import DbfStatus
from .transitous import TransitousClient, TransitousError

_LOGGER = logging.getLogger(__name__)

ORIGIN_DBF = "dbf"
ORIGIN_TRANSITOUS = "transitous"
ORIGIN_LAST_KNOWN = "last_known"


class TransportService:
    """Departures, trips, search and rides, with fallbacks."""

    def __init__(
        self, hass: HomeAssistant, config: dict[str, Any], entry_id: str
    ) -> None:
        """Initialize the service of one config entry."""
        self.dbf = DbfClient(hass, config)
        fallback = bool(config.get(CONF_FALLBACK, DEFAULT_FALLBACK))
        self.transitous = TransitousClient(hass) if fallback else None
        self.last_known = LastKnown(hass, entry_id)
        # Board key -> when a paused map stop last asked Transitous.
        self._paused_asked: dict[str, datetime] = {}
        self.status = DbfStatus(
            hass,
            entry_id,
            self.dbf.server,
            timedelta(
                minutes=int(config.get(CONF_PROBLEM_AFTER, DEFAULT_PROBLEM_AFTER))
            ),
            fallback,
        )

    async def async_setup(self) -> None:
        """Prepare DBF parsing and load the last known answers."""
        await self.dbf.async_setup()
        await self.last_known.async_load()

    async def async_unload(self) -> None:
        """Stop reporting status and save the last known answers."""
        self.status.async_clear()
        await self.last_known.async_flush()

    # --- Passed through to DBF -------------------------------------------

    @property
    def sources(self) -> list[str]:
        """Return the configured networks."""
        return self.dbf.sources

    @property
    def merge_networks(self) -> bool:
        """Return whether DB and BVG stations are merged."""
        return self.dbf.merge_networks

    @property
    def degraded(self) -> bool:
        """Return whether DBF is unreachable or its checks fail right now."""
        return self.dbf.down or self.status.failing()

    def board_url(self, eva: str, source: str, via: str | None = None) -> str | None:
        """Return the link to a stop's board in DBF."""
        return self.dbf.board_url(eva, source, via)

    async def fetch_candidates(self, form: dict[str, str]) -> list[Stop]:
        """Return stops near a position or matching a name (DBF only)."""
        return await self.dbf.fetch_candidates(form)

    # --- Status ----------------------------------------------------------

    async def async_check(self, stations: dict[str, str]) -> None:
        """Check one live board per network ({network: eva}) and record it."""
        results = await asyncio.gather(
            *(self.dbf.async_check(eva, network) for network, eva in stations.items())
        )
        self.status.record(dict(zip(stations, results, strict=True)))

    # --- Boards ----------------------------------------------------------

    async def fetch_departures(
        self,
        eva: str,
        source: str,
        rows: int,
        keep: int,
        linked: list[tuple[str, str]] | None = None,
        *,
        name: str | None = None,
        pos: Position | None = None,
        live: bool = True,
    ) -> Board:
        """Return a stop's departures; live=False gives the last known only."""

        async def from_dbf() -> Board:
            return await self.dbf.fetch_departures(eva, source, rows, keep, linked)

        return await self._board(
            eva, source, linked, from_dbf, rows, keep, name, pos, live
        )

    async def async_board(
        self,
        eva: str,
        source: str,
        linked: list[tuple[str, str]],
        *,
        name: str | None = None,
        pos: Position | None = None,
    ) -> Board:
        """Return the full board of any stop, as the dialog and cards show it."""

        async def from_dbf() -> Board:
            return await self.dbf.async_board(eva, source, linked)

        return await self._board(
            eva, source, linked, from_dbf, BOARD_ROWS, BOARD_DEPARTURES, name, pos, True
        )

    async def _board(
        self,
        eva: str,
        source: str,
        linked: list[tuple[str, str]] | None,
        from_dbf: Any,
        rows: int,
        keep: int,
        name: str | None,
        pos: Position | None,
        live: bool,
    ) -> Board:
        key = "+".join(f"{s}_{e}" for e, s in [(eva, source), *(linked or [])])
        dbf_board: Board | None = None
        if live and source != SOURCE_TRANSITOUS:
            dbf_board = await from_dbf()
            if not (dbf_board.unreachable or dbf_board.failed):
                self._keep_board(key, dbf_board)
                return dbf_board
        # A paused map stop (live=False) shows its last known board while it
        # has one; once that has run out, it asks Transitous now and then.
        kept = None if live else self._kept_board(key, keep)
        if kept:
            return kept
        if self.transitous is not None and (live or self._paused_refresh_due(key)):
            if board := await self._transitous_board(
                eva, source, name, pos, rows, keep
            ):
                self._keep_board(key, board)
                return board
        if live and (kept := self._kept_board(key, keep)):
            return kept
        if dbf_board is not None:
            return dbf_board
        return Board(
            error="Live departures are paused while DBF can't deliver; none are known yet",
            unreachable=True,
        )

    async def _transitous_board(
        self,
        eva: str,
        source: str,
        name: str | None,
        pos: Position | None,
        rows: int,
        keep: int,
    ) -> Board | None:
        assert self.transitous is not None
        try:
            stop_id = await self.transitous.async_stop_id(eva, source, name, pos)
            if stop_id is None:
                _LOGGER.debug(
                    "No Transitous stop matches %s %s (%s)", source, eva, name
                )
                return None
            departures = await self.transitous.async_departures(
                stop_id, min(rows, TRANSITOUS_MAX_ROWS)
            )
        except TransitousError as err:
            _LOGGER.debug("Transitous board for %s %s failed: %s", source, eva, err)
            return None
        return Board(departures=departures[:keep], origin=ORIGIN_TRANSITOUS)

    def _paused_refresh_due(self, key: str) -> bool:
        """Return whether a paused stop may ask Transitous now (and note it)."""
        now = dt_util.utcnow()
        if (asked := self._paused_asked.get(key)) and now - asked < PAUSED_STOP_REFRESH:
            return False
        self._paused_asked[key] = now
        return True

    def _keep_board(self, key: str, board: Board) -> None:
        """Remember a board with departures; a short one doesn't replace a longer recent one (map stops keep a few rows, the dialog many)."""
        if not board.departures:
            return
        if kept := self.last_known.get("boards", key):
            when, value = kept
            if (
                len(value["departures"]) > len(board.departures)
                and dt_util.utcnow() - when < LAST_KNOWN_BOARD_REFRESH
            ):
                return
        self.last_known.put(
            "boards",
            key,
            {"departures": board.departures, "via": board.via, "origin": board.origin},
        )

    def _kept_board(self, key: str, keep: int) -> Board | None:
        if (kept := self.last_known.get("boards", key)) is None:
            return None
        when, value = kept
        departures = refresh_departures(value["departures"], dt_util.now())
        if not departures:
            return None
        return Board(
            departures=departures[:keep],
            via=value.get("via"),
            origin=ORIGIN_LAST_KNOWN,
            as_of=when,
        )

    # --- Search ----------------------------------------------------------

    async def async_search(
        self, query: str, pos: Position | None
    ) -> tuple[list[Stop], str, str | None]:
        """Return (stops, origin, as_of) for a stop search."""
        key = search_text(query)
        try:
            stops = await self.dbf.async_search(query, pos)
        except DbfError as err:
            # DBF's own refusals (e.g. an unpatched server) aren't outages.
            if not (isinstance(err, ServerUnreachable) or self.status.failing()):
                raise
            if self.transitous is not None:
                try:
                    found = await self.transitous.async_search(query, pos)
                except TransitousError as t_err:
                    _LOGGER.debug("Transitous search failed: %s", t_err)
                else:
                    ranked = rank_search(
                        found, query, lambda stop: distance_m(stop, pos)
                    )
                    return ranked[:SEARCH_MAX_RESULTS], ORIGIN_TRANSITOUS, None
            if kept := self.last_known.get("searches", key):
                when, value = kept
                return (
                    [Stop.from_dict(item) for item in value],
                    ORIGIN_LAST_KNOWN,
                    dt_util.as_local(when).isoformat(),
                )
            raise
        self.last_known.put("searches", key, [stop.as_dict() for stop in stops])
        return stops, ORIGIN_DBF, None

    async def async_nearby_fallback(self, pos: Position, radius_m: float) -> list[Stop]:
        """Return the nearest stops from Transitous, for Assist while DBF can't look stops up: at most FALLBACK_STOPS within the radius, as the map asks Transitous for at most that many stops while degraded."""
        if self.transitous is None:
            return []
        try:
            found = await self.transitous.async_nearby(pos)
        except TransitousError as err:
            _LOGGER.debug("Transitous nearby stops failed: %s", err)
            return []
        near = [
            (meters, stop)
            for stop in found
            if (meters := distance_m(stop, pos)) is not None and meters <= radius_m
        ]
        near.sort(key=lambda item: item[0])
        return [stop for _, stop in near[:FALLBACK_STOPS]]

    # --- Trips and rides -------------------------------------------------

    async def async_trip(self, trip: dict[str, Any]) -> dict[str, Any]:
        """Return a trip's stops; Transitous trips come from Transitous."""
        key = json.dumps(trip, sort_keys=True)
        try:
            if "transitous" in trip:
                if self.transitous is None:
                    # Raised inside the try on purpose: the last known trip still answers.
                    raise DbfError("The Transitous fallback is turned off")  # noqa: TRY301
                data = await self.transitous.async_trip(trip["transitous"])
                origin = ORIGIN_TRANSITOUS
            else:
                data = await self.dbf.async_trip(trip)
                origin = ORIGIN_DBF
        except (DbfError, TransitousError) as err:
            if kept := self.last_known.get("trips", key):
                when, value = kept
                return {
                    **value,
                    "origin": ORIGIN_LAST_KNOWN,
                    "as_of": dt_util.as_local(when).isoformat(),
                }
            if isinstance(err, DbfError):
                raise
            raise DbfError(str(err)) from err
        self.last_known.put("trips", key, data)
        return {**data, "origin": origin}

    async def async_ride(
        self, start: dict[str, str], end: dict[str, str], lines: list[str] | None
    ) -> dict[str, Any] | None:
        """Return the scheduled ride between two stations, or None.

        Ride times hardly change, so a known one stands in while DBF fails.
        """
        key = json.dumps([start, end, sorted(lines or [])], sort_keys=True)
        try:
            ride = await self.dbf.async_ride(start, end, lines)
        except DbfError:
            ride = None
        if ride is not None:
            self.last_known.put("rides", key, ride)
            return ride
        if self.degraded and (kept := self.last_known.get("rides", key)):
            return kept[1]
        return None

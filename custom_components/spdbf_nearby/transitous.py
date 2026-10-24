"""Transitous: the fallback while your DBF server can't deliver.

Transitous (api.transitous.org, MOTIS) is a community-run, Europe-wide
journey planner fed by GTFS and GTFS-RT. Its realtime data is thinner than
DBF's (it misses many delays and most S-Bahn platforms in Berlin), so it is
only asked when DBF fails. Answers are turned into the same departure, trip
and stop shapes the rest of the integration uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.location import distance as geo_distance

from .const import (
    CONNECT_TIMEOUT,
    REQUEST_TIMEOUT,
    SOURCE_DB,
    SOURCE_TRANSITOUS,
    TRANSITOUS_BOARD_CACHE_TTL,
    TRANSITOUS_MATCH_METERS,
    TRANSITOUS_NEARBY_CACHE_TTL,
    TRANSITOUS_RADIUS_METERS,
    TRANSITOUS_URL,
    USER_AGENT,
)
from .stations import Position, Stop, station_names, tidy_line

_LOGGER = logging.getLogger(__name__)


class TransitousError(Exception):
    """Transitous couldn't answer."""


class TransitousClient:
    """Boards, trips and stop search from Transitous."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the client."""
        self._session = async_get_clientsession(hass)
        # (network, eva) of a DBF stop -> Transitous stop id; stops don't move.
        self._stop_ids: dict[tuple[str, str], str | None] = {}
        self._boards: dict[tuple[str, int], tuple[datetime, list[dict[str, Any]]]] = {}
        self._nearby: dict[tuple[float, float], tuple[datetime, list[Stop]]] = {}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            async with self._session.get(
                f"{TRANSITOUS_URL}/{path}",
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(
                    total=REQUEST_TIMEOUT, sock_connect=CONNECT_TIMEOUT
                ),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise TransitousError(str(err) or type(err).__name__) from err

    # --- Stops ----------------------------------------------------------

    async def async_search(self, query: str, pos: Position | None) -> list[Stop]:
        """Return stops whose name matches, as Transitous stops."""
        params: dict[str, Any] = {"text": query, "type": "STOP"}
        if pos is not None:
            params["place"] = f"{pos[0]},{pos[1]}"
        return _stops(await self._get("v1/geocode", params))

    async def async_nearby(self, pos: Position) -> list[Stop]:
        """Return the stops closest to a position (Transitous's default 5),
        briefly cached per ~100 m."""
        now = dt_util.utcnow()
        key = (round(pos[0], 3), round(pos[1], 3))
        if (cached := self._nearby.get(key)) and now - cached[0] < TRANSITOUS_NEARBY_CACHE_TTL:
            return cached[1]
        stops = _stops(
            await self._get("v1/reverse-geocode", {"place": f"{pos[0]},{pos[1]}", "type": "STOP"})
        )
        self._nearby = {
            k: v for k, v in self._nearby.items() if now - v[0] < TRANSITOUS_NEARBY_CACHE_TTL
        }
        self._nearby[key] = (now, stops)
        return stops

    async def async_stop_id(
        self, eva: str, network: str, name: str | None, pos: Position | None
    ) -> str | None:
        """Return the Transitous stop of a DBF stop, or None if none matches.

        BVG and VBB numbers are part of the VBB stop ids Transitous uses
        ("de-VBB_de:11000:900110001"), so those match exactly. Other stops
        match by name, and by distance when the position is known.
        """
        if network == SOURCE_TRANSITOUS:
            return eva
        key = (network, eva)
        if key in self._stop_ids:
            return self._stop_ids[key]
        # A card title may carry extras: "Savignyplatz (Eastbound)", "U2 → Pankow".
        plain = re.sub(r"\([^)]*\)|→.*$", " ", name or "").strip()
        for query in dict.fromkeys(q for q in (name, plain) if q):
            stops = await self.async_search(query, pos)
            if stop_id := _pick_stop(stops, eva, network, query, pos):
                # Only sure matches are kept: the BVG number, or near the
                # stop. A name alone may find a namesake elsewhere, and a
                # miss may be a passing gap; both are asked again next time.
                if pos is not None or f":{eva}" in stop_id:
                    self._stop_ids[key] = stop_id
                return stop_id
        return None

    # --- Departures -----------------------------------------------------

    async def async_departures(self, stop_id: str, rows: int) -> list[dict[str, Any]]:
        """Return a stop's next departures, briefly cached."""
        now = dt_util.utcnow()
        key = (stop_id, rows)
        if (cached := self._boards.get(key)) and now - cached[0] < TRANSITOUS_BOARD_CACHE_TTL:
            return cached[1]
        payload = await self._get(
            "v6/stoptimes",
            {"stopId": stop_id, "n": rows, "radius": TRANSITOUS_RADIUS_METERS},
        )
        if not isinstance(payload, dict):
            raise TransitousError("Unexpected answer from Transitous")
        local_now = dt_util.now()
        departures = [
            dep
            for item in payload.get("stopTimes") or []
            if (dep := _departure(item, local_now)) is not None
        ]
        departures.sort(key=lambda dep: dep["minutes"])
        self._boards = {
            k: v for k, v in self._boards.items() if now - v[0] < TRANSITOUS_BOARD_CACHE_TTL
        }
        self._boards[key] = (now, departures)
        return departures

    # --- Trips ----------------------------------------------------------

    async def async_trip(self, trip_id: str) -> dict[str, Any]:
        """Return a trip's stops in the integration's trip shape."""
        payload = await self._get("v6/trip", {"tripId": trip_id})
        legs = payload.get("legs") if isinstance(payload, dict) else None
        if not legs:
            raise TransitousError("No trip found")
        leg = legs[0]
        places = [leg.get("from") or {}, *(leg.get("intermediateStops") or []), leg.get("to") or {}]
        stops = []
        for place in places:
            stops.append({
                "name": place.get("name"),
                "eva": place.get("stopId"),
                "arrival": _local_iso(place.get("scheduledArrival")),
                "departure": _local_iso(place.get("scheduledDeparture")),
                "arrival_delay": _delay(place.get("scheduledArrival"), place.get("arrival")),
                "departure_delay": _delay(
                    place.get("scheduledDeparture"), place.get("departure")
                ),
                "platform": place.get("track") or place.get("scheduledTrack"),
                "cancelled": bool(place.get("cancelled")),
            })
        messages = list(dict.fromkeys(
            alert.get("headerText") or alert.get("descriptionText")
            for alert in leg.get("alerts") or []
            if alert.get("headerText") or alert.get("descriptionText")
        ))
        return {
            "line": tidy_line(leg.get("routeShortName") or leg.get("displayName")),
            "direction": leg.get("headsign"),
            "stops": stops,
            "times": True,
            "messages": messages,
        }


def _stops(matches: Any) -> list[Stop]:
    """Return geocoding matches (v1/geocode, v1/reverse-geocode) as stops."""
    if not isinstance(matches, list):
        raise TransitousError("Unexpected answer from Transitous")
    return [
        Stop(
            name=match["name"],
            eva=match["id"],
            source=SOURCE_TRANSITOUS,
            latitude=float(match["lat"]),
            longitude=float(match["lon"]),
        )
        for match in matches
        if isinstance(match, dict)
        and match.get("id")
        and match.get("name")
        and match.get("lat") is not None
        and match.get("lon") is not None
    ]


def _pick_stop(
    stops: list[Stop], eva: str, network: str, name: str, pos: Position | None
) -> str | None:
    """Return the Transitous stop that is the given DBF stop."""
    if network != SOURCE_DB and eva.isdigit():
        for stop in stops:
            if f":{eva}" in stop.eva:
                return stop.eva
    # Never the first result just because it came first: geocoding is
    # Europe-wide, and "Schönhauser Allee" also finds "Schönhauser Damm".
    names = station_names(name, drop_city=network == SOURCE_DB)
    named = [stop for stop in stops if station_names(stop.name, drop_city=False) & names]
    if pos is None:
        return named[0].eva if named else None
    near = []
    for stop in named:
        dist = geo_distance(*pos, stop.latitude, stop.longitude)
        if dist is not None and dist <= TRANSITOUS_MATCH_METERS:
            near.append((dist, stop))
    return min(near, key=lambda item: item[0])[1].eva if near else None


def _parse(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


def _local_iso(value: Any) -> str | None:
    when = _parse(value)
    return dt_util.as_local(when).isoformat() if when else None


def _delay(scheduled: Any, actual: Any) -> int | None:
    planned, real = _parse(scheduled), _parse(actual)
    if planned is None or real is None:
        return None
    return round((real - planned).total_seconds() / 60)


def _departure(item: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Return a departure in the integration's shape, or None for arrivals."""
    place = item.get("place") or {}
    scheduled = _parse(place.get("scheduledDeparture"))
    if scheduled is None or place.get("pickupType") == "NOT_ALLOWED":
        return None  # the trip ends here
    expected = _parse(place.get("departure")) or scheduled
    if expected < now - timedelta(minutes=1):
        return None
    dep: dict[str, Any] = {
        "line": tidy_line(item.get("routeShortName") or item.get("displayName")),
        "destination": item.get("headsign") or (item.get("tripTo") or {}).get("name"),
        "platform": place.get("track") or place.get("scheduledTrack"),
        "scheduled": dt_util.as_local(scheduled).strftime("%H:%M"),
        "delay": round((expected - scheduled).total_seconds() / 60),
        "minutes": max(0, int((expected - now).total_seconds() // 60)),
        "cancelled": bool(
            item.get("cancelled") or item.get("tripCancelled") or place.get("cancelled")
        ),
        "expected": dt_util.as_local(expected).isoformat(),
    }
    if item.get("tripId"):
        dep["trip"] = {"transitous": item["tripId"]}
    return dep

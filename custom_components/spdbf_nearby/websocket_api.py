"""WebSocket commands for the stop search card and the departure dialog."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, FRONTEND_VERSION, STATIC_URL
from .coordinator import SpdbfCoordinator
from .dbf import DbfError, ServerUnreachable
from .stations import Position, Stop

_ENTRY = vol.Optional("entry_id")
_POSITION = {
    vol.Inclusive("latitude", "position"): vol.Coerce(float),
    vol.Inclusive("longitude", "position"): vol.Coerce(float),
}


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the WebSocket commands."""
    websocket_api.async_register_command(hass, ws_search)
    websocket_api.async_register_command(hass, ws_nearby)
    websocket_api.async_register_command(hass, ws_board)
    websocket_api.async_register_command(hass, ws_trip)
    websocket_api.async_register_command(hass, ws_ride)


def _coordinator(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> SpdbfCoordinator | None:
    """Return the requested entry, else the one following this user, else any."""
    all_entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id := msg.get("entry_id"):
        all_entries = [entry for entry in all_entries if entry.entry_id == entry_id]
    entries = [entry for entry in all_entries if entry.state is ConfigEntryState.LOADED]
    if not entries:
        # Typically the DBF server was down when Home Assistant started.
        retrying = [e for e in all_entries if e.state is ConfigEntryState.SETUP_RETRY]
        if retrying:
            connection.send_error(
                msg["id"],
                "not_ready",
                "spdbf Nearby Stops isn't ready yet and keeps retrying: "
                f"{retrying[0].reason or 'DBF server unreachable'}",
            )
        else:
            connection.send_error(msg["id"], "not_found", "spdbf Nearby Stops is not set up")
        return None
    user_id = connection.user.id if connection.user else None
    for entry in entries:
        tracked = hass.states.get(entry.runtime_data.tracked)
        if tracked is not None and tracked.attributes.get("user_id") == user_id:
            return entry.runtime_data
    return entries[0].runtime_data


def _error_code(err: DbfError) -> str:
    return "server_unreachable" if isinstance(err, ServerUnreachable) else "dbf_error"


async def _reply(
    connection: websocket_api.ActiveConnection, msg: dict[str, Any], call: Awaitable[Any]
) -> None:
    """Send the call's result, or DBF's error."""
    try:
        result = await call
    except DbfError as err:
        connection.send_error(msg["id"], _error_code(err), str(err))
        return
    connection.send_result(msg["id"], result)


def _bad_networks(
    coordinator: SpdbfCoordinator,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    networks: set[str],
    also: frozenset[str] = frozenset(),
) -> bool:
    """Report networks that aren't configured sources; True if there were any."""
    if bad := sorted(networks - {*coordinator.sources, *also}):
        connection.send_error(
            msg["id"], "invalid_network", f"{', '.join(bad)} is not a configured source"
        )
        return True
    return False


def _position(msg: dict[str, Any]) -> Position | None:
    if "latitude" in msg:
        return msg["latitude"], msg["longitude"]
    return None


def _stop_json(
    coordinator: SpdbfCoordinator, stop: Stop, pos: Position | None
) -> dict[str, Any]:
    meters = coordinator.distance_m(stop, pos)
    return {
        "name": stop.name,
        "eva": stop.eva,
        "network": stop.source,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
        "distance_km": None if meters is None else round(meters / 1000, 2),
        "entity_picture": f"{STATIC_URL}/marker-{stop.kind}.svg?v={FRONTEND_VERSION}",
        "url": coordinator.client.board_url(stop.eva, stop.source),
        "merged_stops": stop.merged,
        "networks": stop.networks,
        "linked": [{"eva": s.eva, "network": s.source} for s in stop.linked],
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/search",
        vol.Required("query"): str,
        _ENTRY: str,
        **_POSITION,
    }
)
@websocket_api.async_response
async def ws_search(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Search stops by name."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    pos = _position(msg)

    async def search() -> dict[str, Any]:
        stops = await coordinator.client.async_search(
            msg["query"], pos or coordinator.tracked_position()
        )
        return {"stops": [_stop_json(coordinator, stop, pos) for stop in stops]}

    await _reply(connection, msg, search())


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/nearby", _ENTRY: str, **_POSITION}
)
@websocket_api.async_response
async def ws_nearby(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the stops near a position, or near the tracked entity."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    pos = _position(msg)

    async def nearby() -> dict[str, Any]:
        stops = await coordinator.async_nearby(pos)
        return {"stops": [_stop_json(coordinator, stop, pos) for stop in stops]}

    await _reply(connection, msg, nearby())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/board",
        vol.Required("eva"): str,
        vol.Required("network"): str,
        vol.Optional("linked"): [
            {vol.Required("eva"): str, vol.Required("network"): str}
        ],
        _ENTRY: str,
    }
)
@websocket_api.async_response
async def ws_board(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the full departure board of a stop."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    linked = msg.get("linked")
    networks = {msg["network"], *(item["network"] for item in linked or [])}
    if _bad_networks(coordinator, connection, msg, networks):
        return
    board = await coordinator.async_board(
        msg["eva"],
        msg["network"],
        None if linked is None else [(item["eva"], item["network"]) for item in linked],
    )
    connection.send_result(
        msg["id"],
        {
            "departures": board.departures,
            "departures_error": board.error,
            "departures_via": board.via,
            "fallback": board.fallback,
            "server_unreachable": board.unreachable,
            "url": coordinator.client.board_url(
                msg["eva"], msg["network"], board.via, board.fallback
            ),
        },
    )


_TRIP = vol.Any(
    {vol.Required("id"): str, vol.Required("hafas"): str},
    {vol.Required("train"): str, vol.Required("station"): str},
)
_STATION = {vol.Optional("eva"): str, vol.Optional("name"): str}


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/trip", vol.Required("trip"): _TRIP, _ENTRY: str}
)
@websocket_api.async_response
async def ws_trip(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return a trip's stops, from the trip reference of a departure."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    trip = msg["trip"]
    # ÖBB is the integration's own fallback for empty DB boards.
    if trip.get("hafas") and _bad_networks(
        coordinator, connection, msg, {trip["hafas"]}, frozenset({"ÖBB"})
    ):
        return
    await _reply(connection, msg, coordinator.client.async_trip(trip))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/ride",
        vol.Required("from"): {**_STATION, vol.Optional("network"): str},
        vol.Required("to"): _STATION,
        vol.Optional("lines"): [str],
        _ENTRY: str,
    }
)
@websocket_api.async_response
async def ws_ride(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the scheduled ride between two stations, or null."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    start = msg["from"]
    if start.get("network") and _bad_networks(coordinator, connection, msg, {start["network"]}):
        return
    await _reply(
        connection, msg, coordinator.client.async_ride(start, msg["to"], msg.get("lines"))
    )

"""Nearby stops as geo_location entities, shown on the HA map."""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SpdbfConfigEntry
from .const import DOMAIN, FRONTEND_VERSION, STATIC_URL
from .coordinator import SpdbfCoordinator
from .stations import Stop

# Marker kind -> icon shown in lists and dialogs. The map uses the matching
# www/marker-<kind>.svg picture instead of the name's initials.
MARKER_ICONS = {"train": "mdi:train", "bus": "mdi:bus", "stop": "mdi:bus-stop"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpdbfConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add and remove stop entities as the nearby stops change."""
    coordinator = entry.runtime_data
    entities: dict[str, SpdbfStop] = {}

    @callback
    def _sync_entities() -> None:
        stops = coordinator.data or {}
        for key in [key for key in entities if key not in stops]:
            hass.async_create_task(entities.pop(key).async_remove())
        new = [SpdbfStop(coordinator, stop) for key, stop in stops.items() if key not in entities]
        entities.update((entity.stop_key, entity) for entity in new)
        if new:
            async_add_entities(new)

    _sync_entities()
    entry.async_on_unload(coordinator.async_add_listener(_sync_entities))


class SpdbfStop(CoordinatorEntity[SpdbfCoordinator], GeolocationEvent):
    """A stop near the tracked person.

    No unique_id on purpose: stops come and go as you travel, and registering
    each one would fill the entity registry with every stop you ever passed.
    """

    _attr_source = DOMAIN
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    # Departures change on every refresh. Recording them would write a new
    # attributes row per stop every scan interval, for no useful history.
    _unrecorded_attributes = frozenset(
        {
            "departures",
            "next_departure",
            "departures_via",
            "departures_error",
            "distance_km",
            "url",
            "merged_stops",
            "fallback",
            "networks",
        }
    )

    def __init__(self, coordinator: SpdbfCoordinator, stop: Stop) -> None:
        """Initialize the stop entity."""
        super().__init__(coordinator)
        self.stop_key = stop.key
        self._stop = stop
        self._attr_name = stop.name
        kind = stop.kind
        self._attr_icon = MARKER_ICONS[kind]
        self._attr_entity_picture = (
            f"{STATIC_URL}/marker-{kind}.svg?v={FRONTEND_VERSION}"
        )
        self._attr_latitude = stop.latitude
        self._attr_longitude = stop.longitude

    @callback
    def _handle_coordinator_update(self) -> None:
        stop = (self.coordinator.data or {}).get(self.stop_key)
        if stop is None:
            return  # about to be removed by _sync_entities
        self._stop = stop
        super()._handle_coordinator_update()

    @property
    def distance(self) -> float | None:
        """Return the live distance to the tracked person in km."""
        meters = self.coordinator.distance_m(self._stop)
        return None if meters is None else round(meters / 1000, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return stop details and upcoming departures."""
        stop = self._stop
        board = stop.board
        attrs: dict[str, Any] = {
            "network": stop.source,
            "eva": stop.eva,
            "distance_km": self.distance,
            "url": self.coordinator.client.board_url(
                stop.eva, stop.source, board.via, board.fallback
            ),
            "departures": board.departures,
        }
        if stop.merged:
            attrs["merged_stops"] = stop.merged
        if stop.linked:
            attrs["networks"] = stop.networks
        if board.via:
            attrs["departures_via"] = board.via
        if board.fallback:
            attrs["fallback"] = True
        if board.error:
            attrs["departures_error"] = board.error
        if board.departures:
            dep = board.departures[0]
            attrs["next_departure"] = (
                f"{dep['line']} → {dep['destination']} in {dep['minutes']} min"
            )
        return attrs

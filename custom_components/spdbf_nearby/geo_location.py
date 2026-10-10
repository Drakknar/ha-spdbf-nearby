"""Nearby stops as geo_location entities, shown on the HA map."""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SpdbfConfigEntry
from .const import (
    DOMAIN,
    FRONTEND_VERSION,
    RAIL_NAME_PREFIXES,
    SOURCE_DB,
    STATIC_URL,
)
from .coordinator import SpdbfCoordinator, Stop

# Marker kind -> icon shown in lists and dialogs. The map uses the matching
# www/marker-<kind>.svg picture instead of the name's initials.
MARKER_ICONS = {"train": "mdi:train", "bus": "mdi:bus", "stop": "mdi:bus-stop"}


def _marker_kind(stop: Stop) -> str:
    if stop.source == SOURCE_DB or stop.name.startswith(RAIL_NAME_PREFIXES):
        return "train"
    if stop.source == "BVG":
        return "bus"
    return "stop"


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

    def __init__(self, coordinator: SpdbfCoordinator, stop: Stop) -> None:
        """Initialize the stop entity."""
        super().__init__(coordinator)
        self.stop_key = stop.key
        self._stop = stop
        self._attr_name = stop.name
        kind = _marker_kind(stop)
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
        attrs: dict[str, Any] = {
            "network": stop.source,
            "eva": stop.eva,
            "distance_km": self.distance,
            "url": self.coordinator.board_url(stop),
            "departures": stop.departures,
        }
        if stop.departures_via:
            attrs["departures_via"] = stop.departures_via
        if stop.departures_error:
            attrs["departures_error"] = stop.departures_error
        if stop.departures:
            dep = stop.departures[0]
            attrs["next_departure"] = (
                f"{dep['line']} → {dep['destination']} in {dep['minutes']} min"
            )
        return attrs

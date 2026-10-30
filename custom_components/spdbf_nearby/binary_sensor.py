"""DBF problem sensor: on while DBF has been down for real (status.py)."""

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SpdbfConfigEntry
from .const import DOMAIN
from .status import DbfStatus


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpdbfConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the DBF problem sensor."""
    async_add_entities(
        [DbfProblemSensor(entry.entry_id, entry.runtime_data.client.status)]
    )


class DbfProblemSensor(BinarySensorEntity):
    """On while DBF hasn't delivered departures for the configured time.

    Its attributes say since when, why, and per network. The cards and the
    departure dialog find it by the spdbf_status attribute and show a
    banner while it is on.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "dbf_problem"
    _attr_icon = "mdi:train-variant"
    # Every check rewrites last_check; history needs only the on/off state.
    _unrecorded_attributes = frozenset(
        {"failing", "last_check", "fallback", "spdbf_status"}
    )

    def __init__(self, entry_id: str, status: DbfStatus) -> None:
        """Initialize the sensor."""
        self._status = status
        self._attr_unique_id = f"{entry_id}_dbf_problem"
        # A service device gives the sensor its name prefix and a home on
        # the device page; existing installs keep their entity id.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="spdbf Public Transport",
            manufacturer="spdbf",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Follow the status checks."""
        self.async_on_remove(self._status.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        """Return whether DBF is down for real."""
        return self._status.problem

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return since when and why DBF fails."""
        return {**self._status.attributes(), "spdbf_status": True}

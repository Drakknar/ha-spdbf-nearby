"""Public transport from your DBF (spdbf) server: stops near you on the map,
a stop search card, departure boards and trips.

dbf.py talks to DBF, stations.py holds the station rules, www/dbf-core.js the
shared frontend (line colours, departure dialog, trip view). The map feature
(coordinator.py, geo_location.py) can be turned off per entry.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import DOMAIN, FRONTEND_VERSION, STATIC_URL
from .coordinator import SpdbfCoordinator
from .dbf import DbfClient

PLATFORMS = [Platform.GEO_LOCATION]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SpdbfConfigEntry = ConfigEntry[SpdbfCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the marker icons, the frontend core and the stop search card."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "www"), True)]
    )
    # Loaded on every page, so stops show departures wherever they are opened,
    # cards work without adding a dashboard resource, and other cards can use
    # window.dbfCore.
    add_extra_js_url(hass, f"{STATIC_URL}/dbf-core.js?v={FRONTEND_VERSION}")
    add_extra_js_url(hass, f"{STATIC_URL}/spdbf-stop-search-card.js?v={FRONTEND_VERSION}")
    websocket_api.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> bool:
    """Set up spdbf Nearby Stops from a config entry."""
    client = DbfClient(hass, {**entry.data, **entry.options}, entry.entry_id)
    coordinator = SpdbfCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    if coordinator.map_stops:
        entry.async_on_unload(coordinator.async_start_tracking())
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> bool:
    """Unload a config entry."""
    if not entry.runtime_data.map_stops:
        return True
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: SpdbfConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

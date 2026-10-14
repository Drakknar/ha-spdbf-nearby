"""Show stops near a person on the Home Assistant map, using a DBF (spdbf) server."""

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

PLATFORMS = [Platform.GEO_LOCATION]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SpdbfConfigEntry = ConfigEntry[SpdbfCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the marker icons, the departure dialog and the stop search card."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "www"), True)]
    )
    # Loaded on every page, so stops show departures wherever they are opened,
    # and the card works without adding a dashboard resource.
    add_extra_js_url(hass, f"{STATIC_URL}/spdbf-departures.js?v={FRONTEND_VERSION}")
    add_extra_js_url(hass, f"{STATIC_URL}/spdbf-stop-search-card.js?v={FRONTEND_VERSION}")
    websocket_api.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> bool:
    """Set up spdbf Nearby Stops from a config entry."""
    coordinator = SpdbfCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_start_tracking())
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: SpdbfConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

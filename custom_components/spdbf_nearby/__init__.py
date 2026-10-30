"""Public transport from your DBF (spdbf) server: stops near you on the map, a stop search card, departure boards and trips.

dbf.py talks to DBF, transitous.py to the fallback, service.py decides which
answers, stations.py holds the station rules, status.py tracks whether DBF
delivers, www/dbf-core.js is the shared frontend (line colours, departure
dialog, trip view). The map feature (coordinator.py, geo_location.py) can be
turned off per entry.
"""

from datetime import datetime
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import CONF_FALLBACK, DOMAIN, FRONTEND_VERSION, STATIC_URL, STATUS_INTERVAL
from .coordinator import SpdbfCoordinator
from .frontend import async_register_resource, async_unregister_resource
from .last_known import LastKnown
from .service import TransportService

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SpdbfConfigEntry = ConfigEntry[SpdbfCoordinator]


def _platforms(coordinator: SpdbfCoordinator) -> list[Platform]:
    if coordinator.map_stops:
        return [Platform.BINARY_SENSOR, Platform.GEO_LOCATION]
    return [Platform.BINARY_SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the marker icons, the frontend core and the cards."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "www"), True)]
    )
    # Loaded on every page, so stops show departures wherever they are opened,
    # cards work without adding a dashboard resource, and other cards can use
    # window.dbfCore. The loader imports the core and the cards, retrying
    # until they load (see www/spdbf-loader.js and frontend.py).
    add_extra_js_url(hass, f"{STATIC_URL}/spdbf-loader.js?v={FRONTEND_VERSION}")
    websocket_api.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> bool:
    """Set up spdbf Public Transport from a config entry."""
    service = TransportService(hass, {**entry.data, **entry.options}, entry.entry_id)
    coordinator = SpdbfCoordinator(hass, entry, service)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    if coordinator.map_stops:
        entry.async_on_unload(coordinator.async_start_tracking())

    async def check(_now: datetime | None = None) -> None:
        await service.async_check(coordinator.check_stations())

    entry.async_on_unload(async_track_time_interval(hass, check, STATUS_INTERVAL))
    entry.async_on_unload(service.async_unload)
    entry.async_create_background_task(hass, check(), "spdbf_nearby status check")

    await hass.config_entries.async_forward_entry_setups(entry, _platforms(coordinator))
    try:
        await async_register_resource(hass)
    except Exception:
        _LOGGER.exception("Couldn't add the cards as a dashboard resource")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(
        entry, _platforms(entry.runtime_data)
    )


async def async_remove_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> None:
    """Delete the entry's saved answers; with the last entry, the resource."""
    await LastKnown(hass, entry.entry_id).async_remove()
    others = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if not others:
        await async_unregister_resource(hass)


async def async_migrate_entry(hass: HomeAssistant, entry: SpdbfConfigEntry) -> bool:
    """Move entries from the public DBF fallback to Transitous (0.9.0).

    The old fallback (a public DBF instance, off by default) is gone; its
    on/off choice meant something else, so the Transitous fallback starts on.
    """
    if entry.version == 1 and entry.minor_version < 2:
        data = {k: v for k, v in entry.data.items() if k != "fallback_url"}
        options = {k: v for k, v in entry.options.items() if k != "fallback_url"}
        data[CONF_FALLBACK] = True
        if CONF_FALLBACK in options:
            options[CONF_FALLBACK] = True
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, minor_version=2
        )
    return True


async def _async_reload(hass: HomeAssistant, entry: SpdbfConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

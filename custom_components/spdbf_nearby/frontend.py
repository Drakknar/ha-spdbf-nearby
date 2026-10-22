"""The cards' loader as a dashboard resource, next to the extra module.

Home Assistant imports extra modules (add_extra_js_url) once, when the page
opens. If that one import fails (Firefox restoring tabs after sleep, the
companion app opening before the VPN is up), the cards stay "Configuration
error" until a reload. Dashboard resources are imported again whenever a
dashboard loads, so registering the loader there too gives it another
chance. The loader runs only once per page either way.

Only in storage mode; in YAML mode resources are yours to manage. The
resource is updated with each release and removed with the last entry.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.core import HomeAssistant

from .const import FRONTEND_VERSION, STATIC_URL

_LOGGER = logging.getLogger(__name__)

LOADER_PATH = f"{STATIC_URL}/spdbf-loader.js"
# A URL of its own: a browser may remember a failed import of the extra
# module's URL for the page's lifetime.
RESOURCE_URL = f"{LOADER_PATH}?v={FRONTEND_VERSION}&via=resource"
# Entries set up at the same time must not both add the resource.
_LOCK_KEY = "spdbf_nearby_resource_lock"


def _resources(hass: HomeAssistant):  # noqa: ANN202 - Home Assistant's collection
    data = hass.data.get(LOVELACE_DATA)
    if data is None or data.resource_mode != MODE_STORAGE:
        return None
    return data.resources


async def async_register_resource(hass: HomeAssistant) -> None:
    """Add the loader as a dashboard resource, or bring it to this version."""
    if (resources := _resources(hass)) is None:
        return
    async with hass.data.setdefault(_LOCK_KEY, asyncio.Lock()):
        await _async_register(resources)


async def _async_register(resources) -> None:  # noqa: ANN001 - Home Assistant's collection
    await resources.async_get_info()  # loads the stored resources
    ours = [
        item for item in resources.async_items()
        if str(item.get("url", "")).split("?")[0] == LOADER_PATH
    ]
    if not ours:
        await resources.async_create_item({"res_type": "module", "url": RESOURCE_URL})
        _LOGGER.debug("Added dashboard resource %s", RESOURCE_URL)
        return
    first, *extra = ours
    if first.get("url") != RESOURCE_URL:
        await resources.async_update_item(
            first["id"], {"res_type": "module", "url": RESOURCE_URL}
        )
    for item in extra:
        await resources.async_delete_item(item["id"])


async def async_unregister_resource(hass: HomeAssistant) -> None:
    """Remove the loader's dashboard resource."""
    if (resources := _resources(hass)) is None:
        return
    await resources.async_get_info()
    for item in list(resources.async_items()):
        if str(item.get("url", "")).split("?")[0] == LOADER_PATH:
            await resources.async_delete_item(item["id"])

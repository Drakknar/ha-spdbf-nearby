"""The last good answers, for when neither DBF nor Transitous can answer.

Boards, trips, searches and rides are kept per key, in Home Assistant's
storage so they survive a restart, and shown marked with their age. Old and
surplus entries are dropped.
"""

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LAST_KNOWN_LIMITS, LAST_KNOWN_MAX_AGE, LAST_KNOWN_SAVE_DELAY

STORE_VERSION = 1


class LastKnown:
    """Last good answers by kind ("boards", "trips", "searches", "rides") and key."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store of one config entry."""
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.last_known.{entry_id}"
        )
        self._data: dict[str, dict[str, Any]] = {kind: {} for kind in LAST_KNOWN_LIMITS}

    async def async_load(self) -> None:
        """Load what was saved before the last restart."""
        if stored := await self._store.async_load():
            for kind in LAST_KNOWN_LIMITS:
                self._data[kind] = dict(stored.get(kind) or {})

    async def async_remove(self) -> None:
        """Delete the saved answers (the entry is being removed)."""
        await self._store.async_remove()

    async def async_flush(self) -> None:
        """Save now, ending a pending delayed save (the entry unloads)."""
        await self._store.async_save(self._data)

    @callback
    def put(self, kind: str, key: str, value: Any) -> None:
        """Remember an answer; saved to disk after a short delay."""
        entries = self._data[kind]
        entries.pop(key, None)  # re-insert, so the newest is last
        entries[key] = {"time": dt_util.utcnow().isoformat(), "value": value}
        while len(entries) > LAST_KNOWN_LIMITS[kind]:
            entries.pop(next(iter(entries)))
        self._store.async_delay_save(lambda: self._data, LAST_KNOWN_SAVE_DELAY)

    @callback
    def get(self, kind: str, key: str) -> tuple[datetime, Any] | None:
        """Return (when, answer) if one was kept and isn't too old."""
        entry = self._data[kind].get(key)
        if entry is None:
            return None
        when = dt_util.parse_datetime(entry["time"])
        if when is None or dt_util.utcnow() - when > LAST_KNOWN_MAX_AGE:
            return None
        return when, entry["value"]


def refresh_departures(
    departures: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Return kept departures with minutes counted from now; gone ones dropped."""
    fresh = []
    for dep in departures:
        expected = dt_util.parse_datetime(dep.get("expected") or "")
        if expected is None or expected < now - timedelta(minutes=1):
            continue
        fresh.append(
            {**dep, "minutes": max(0, int((expected - now).total_seconds() // 60))}
        )
    return fresh

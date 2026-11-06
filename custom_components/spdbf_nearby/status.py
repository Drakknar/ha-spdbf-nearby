"""Whether DBF really delivers: live checks, the problem sensor's state, Repairs.

Each network (DB, BVG, ...) is checked on its own, since one backend can
fail while the others work. Short failures are normal (a cache in front of
DBF covers them), so a network only counts as down for real once its checks
have failed for problem_after; one working check ends that.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ISSUE_SERVER_UNREACHABLE

_LOGGER = logging.getLogger(__name__)


def _since_text(since: datetime) -> str:
    """Return when an outage began, local time: "23:40", or "Nov 6, 23:40" before today."""
    local = dt_util.as_local(since)
    if local.date() == dt_util.now().date():
        return f"{local:%H:%M}"
    return f"{local:%b} {local.day}, {local:%H:%M}"


class DbfStatus:
    """The outcome of DBF's live checks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        server: str,
        problem_after: timedelta,
        fallback: bool,
        *,
        map_stops: bool,
    ) -> None:
        """Initialize the status of one config entry.

        With map stops on, a round can be empty because no stop was found;
        with them off, only because nothing can be checked at all.
        """
        self.hass = hass
        self._entry_id = entry_id
        self._issue_id = f"{ISSUE_SERVER_UNREACHABLE}_{entry_id}"
        self._server = server
        self._problem_after = problem_after
        self._fallback = fallback
        self._map_stops = map_stops
        # Network -> since when its checks fail, and why.
        self._failing: dict[str, tuple[datetime, str]] = {}
        self.last_check: datetime | None = None
        # Some network is down for real: failing for problem_after.
        self.problem = False
        self._listeners: list[CALLBACK_TYPE] = []
        self._closed = False

    @property
    def failing_since(self) -> datetime | None:
        """Return since when the longest-failing network fails."""
        return min((since for since, _ in self._failing.values()), default=None)

    def failing(self, network: str | None = None) -> bool:
        """Return whether a network's (default: any network's) checks fail now."""
        return bool(self._failing) if network is None else network in self._failing

    @property
    def reason(self) -> str | None:
        """Return why the checks fail, per network."""
        return (
            "; ".join(f"{net}: {why}" for net, (_, why) in self._failing.items())
            or None
        )

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> Callable[[], None]:
        """Call listener after every round of checks; returns the remover."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def record(self, checks: dict[str, str | None]) -> None:
        """Take one round of checks (network -> error, or None if it worked)."""
        # A check still running when the entry unloaded is dropped.
        if self._closed:
            return
        # No stop found nearby to check: nothing new is known, so the
        # previous state stays.
        if not checks and self._map_stops:
            return
        now = dt_util.utcnow()
        if checks:
            self.last_check = now
        # A network no longer checked (no stop of it around, or none at all by
        # design: no DB/BVG source and map stops off) no longer fails.
        self._failing = {net: v for net, v in self._failing.items() if net in checks}
        for network, why in checks.items():
            if why is None:
                self._failing.pop(network, None)
            else:
                since = self._failing.get(network, (now, ""))[0]
                self._failing[network] = (since, why)
        since = self.failing_since
        problem = since is not None and now - since >= self._problem_after
        if problem and not self.problem:
            _LOGGER.warning(
                "DBF at %s hasn't delivered departures since %s: %s",
                self._server,
                _since_text(since),
                self.reason,
            )
            self._create_issue()
        elif since is None or (self.problem and not problem):
            if self.problem:
                _LOGGER.info("DBF at %s delivers departures again", self._server)
            # Also ends a notice kept from before a reload.
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self.problem = problem
        for listener in list(self._listeners):
            listener()

    def _create_issue(self) -> None:
        # After the entry was removed (e.g. a failed unload left the checks
        # running), there is nothing to link a notice to: HA would raise.
        if self.hass.config_entries.async_get_entry(self._entry_id) is None:
            return
        since = self.failing_since
        assert since is not None
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            config_entry_id=self._entry_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SERVER_UNREACHABLE,
            translation_placeholders={
                "server": self._server,
                "since": _since_text(since),
                "reason": self.reason or "",
                "fallback": (
                    "Meanwhile departures, trips and the stop search come from"
                    " Transitous, or are the last known ones."
                    if self._fallback
                    else "Meanwhile only the last known departures are shown. Turn on"
                    " the Transitous fallback in the integration's options for live"
                    " (if less precise) data."
                ),
            },
        )

    @callback
    def async_close(self) -> None:
        """Stop taking checks (the entry unloads).

        The Repairs notice belongs to the config entry: Home Assistant hides it
        while the entry is disabled and deletes it with the entry. After a
        reload, the first round without failures ends it.
        """
        self._closed = True

    def attributes(self) -> dict[str, Any]:
        """Return the details shown on the problem sensor."""
        since = self.failing_since
        return {
            "failing_since": dt_util.as_local(since).isoformat() if since else None,
            "failing": {
                net: {"since": dt_util.as_local(when).isoformat(), "reason": why}
                for net, (when, why) in self._failing.items()
            },
            "last_check": (
                dt_util.as_local(self.last_check).isoformat()
                if self.last_check
                else None
            ),
            "fallback": "Transitous" if self._fallback else None,
        }

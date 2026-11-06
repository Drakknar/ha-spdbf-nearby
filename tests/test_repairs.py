"""Repairs notices (status.py, dbf.py) belong to their config entry (HA 2026.11)."""

from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant.common import MockConfigEntry

from custom_components.spdbf_nearby.const import (
    CONF_SERVER_URL,
    CONF_SOURCES,
    DOMAIN,
    ISSUE_MISSING_COORDINATES,
    ISSUE_SERVER_UNREACHABLE,
)
from custom_components.spdbf_nearby.dbf import DbfClient
from custom_components.spdbf_nearby.status import DbfStatus
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

SERVER = "http://dbf.local:8092"
POSITION = {"lat": "52.5051", "lon": "13.3193"}


def _config(server: str) -> dict:
    return {CONF_SERVER_URL: server, CONF_SOURCES: ["DB", "BVG"]}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2)
    entry.add_to_hass(hass)
    return entry


def _unreachable_id(entry: MockConfigEntry) -> str:
    return f"{ISSUE_SERVER_UNREACHABLE}_{entry.entry_id}"


def _status(
    hass: HomeAssistant, entry_id: str, *, map_stops: bool = False
) -> DbfStatus:
    # problem_after 0: the first failing round is a problem.
    return DbfStatus(hass, entry_id, SERVER, timedelta(0), True, map_stops=map_stops)


async def test_unreachable_notice_is_linked_to_the_entry(hass: HomeAssistant) -> None:
    """The notice carries its entry, and a working round ends it."""
    entry = _entry(hass)
    status = _status(hass, entry.entry_id)
    issue_id = _unreachable_id(entry)
    registry = ir.async_get(hass)

    status.record({"DB": "timeout"})
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.config_entry_id == entry.entry_id

    status.record({"DB": None})
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_notice_survives_unload_until_a_working_round(
    hass: HomeAssistant,
) -> None:
    """Unloading (a reload) keeps the notice; the new status ends it once DBF works."""
    entry = _entry(hass)
    issue_id = _unreachable_id(entry)
    registry = ir.async_get(hass)

    old = _status(hass, entry.entry_id)
    old.record({"DB": "timeout"})
    old.async_close()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # A late round of the unloaded status changes nothing.
    old.record({"DB": None})
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    _status(hass, entry.entry_id).record({"DB": None})
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_notice_is_removed_with_the_entry(hass: HomeAssistant) -> None:
    """Home Assistant deletes the notice when the entry is removed."""
    entry = _entry(hass)
    _status(hass, entry.entry_id).record({"DB": "timeout"})
    issue_id = _unreachable_id(entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    await hass.config_entries.async_remove(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_missing_coordinates_notice_per_entry(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Each entry has its own notice, so one entry's fix doesn't end another's."""
    registry = ir.async_get(hass)
    # The pre-0.12.0 notice, shared by all entries: dropped at setup.
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_MISSING_COORDINATES,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_MISSING_COORDINATES,
        translation_placeholders={"server": SERVER},
    )
    bvg = {"name": "S Savignyplatz", "eva": "900024203", "hafas": "BVG"}
    unpatched, patched = "http://old.local:8092", "http://new.local:8092"
    aioclient_mock.post(f"{unpatched}/_geolocation", json={"candidates": [bvg]})
    aioclient_mock.post(
        f"{patched}/_geolocation",
        json={"candidates": [{**bvg, "lat": 52.505, "lon": 13.319}]},
    )
    first, second = _entry(hass), _entry(hass)
    for server, entry in ((unpatched, first), (patched, second)):
        client = DbfClient(hass, _config(server), entry.entry_id)
        await client.async_setup()
        await client.fetch_candidates(POSITION)
    assert registry.async_get_issue(DOMAIN, ISSUE_MISSING_COORDINATES) is None

    issue = registry.async_get_issue(
        DOMAIN, f"{ISSUE_MISSING_COORDINATES}_{first.entry_id}"
    )
    assert issue is not None
    assert issue.config_entry_id == first.entry_id
    assert (
        registry.async_get_issue(
            DOMAIN, f"{ISSUE_MISSING_COORDINATES}_{second.entry_id}"
        )
        is None
    )


async def test_no_notice_for_a_removed_entry(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A late answer after the entry is gone creates nothing (and doesn't raise)."""
    aioclient_mock.post(
        f"{SERVER}/_geolocation",
        json={"candidates": [{"name": "X", "eva": "1", "hafas": "BVG"}]},
    )
    client = DbfClient(hass, _config(SERVER), "removed-entry")
    await client.async_setup()
    await client.fetch_candidates(POSITION)
    assert not [domain for domain, _ in ir.async_get(hass).issues if domain == DOMAIN]


async def test_no_coordinate_notice_after_close(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A late answer to an unloaded client (a reload) creates no notice."""
    aioclient_mock.post(
        f"{SERVER}/_geolocation",
        json={"candidates": [{"name": "X", "eva": "1", "hafas": "BVG"}]},
    )
    entry = _entry(hass)
    client = DbfClient(hass, _config(SERVER), entry.entry_id)
    await client.async_setup()
    client.async_close()
    await client.fetch_candidates(POSITION)
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_MISSING_COORDINATES}_{entry.entry_id}"
        )
        is None
    )

    # The same answer to an open client does create it.
    client = DbfClient(hass, _config(SERVER), entry.entry_id)
    await client.async_setup()
    await client.fetch_candidates(POSITION)
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_MISSING_COORDINATES}_{entry.entry_id}"
    )


async def test_notice_ends_when_the_long_failure_recovers(hass: HomeAssistant) -> None:
    """DB recovers while BVG has only just started failing: no problem, no notice."""
    entry = _entry(hass)
    status = DbfStatus(
        hass, entry.entry_id, SERVER, timedelta(minutes=10), True, map_stops=False
    )
    registry = ir.async_get(hass)
    start = dt_util.utcnow()
    for minutes, checks in (
        (0, {"DB": "timeout", "BVG": None}),
        (11, {"DB": "timeout", "BVG": None}),
    ):
        with patch.object(
            dt_util, "utcnow", return_value=start + timedelta(minutes=minutes)
        ):
            status.record(checks)
    assert status.problem
    assert registry.async_get_issue(DOMAIN, _unreachable_id(entry)) is not None

    with patch.object(dt_util, "utcnow", return_value=start + timedelta(minutes=12)):
        status.record({"DB": None, "BVG": "timeout"})
    assert not status.problem
    assert status.failing("BVG")
    assert registry.async_get_issue(DOMAIN, _unreachable_id(entry)) is None


async def test_notice_ends_when_there_is_nothing_to_check(hass: HomeAssistant) -> None:
    """After a reload without stations to check (no DB/BVG, no map stops), the kept notice ends."""
    entry = _entry(hass)
    old = _status(hass, entry.entry_id)
    old.record({"DB": "timeout"})
    old.async_close()
    assert ir.async_get(hass).async_get_issue(DOMAIN, _unreachable_id(entry))

    new = _status(hass, entry.entry_id, map_stops=False)
    new.record({})
    assert not new.problem
    assert new.last_check is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, _unreachable_id(entry)) is None


async def test_empty_round_with_map_stops_keeps_the_state(hass: HomeAssistant) -> None:
    """With map stops on, a round without stops to check changes nothing."""
    entry = _entry(hass)
    status = _status(hass, entry.entry_id, map_stops=True)
    status.record({"DB": "timeout"})
    assert status.problem
    last_check = status.last_check

    status.record({})
    assert status.problem
    assert status.failing("DB")
    assert status.last_check == last_check
    assert ir.async_get(hass).async_get_issue(DOMAIN, _unreachable_id(entry))

    # Nor does it end a notice kept from before a reload.
    status.async_close()
    new = _status(hass, entry.entry_id, map_stops=True)
    new.record({})
    assert not new.problem
    assert ir.async_get(hass).async_get_issue(DOMAIN, _unreachable_id(entry))


async def test_no_unreachable_notice_for_a_removed_entry(hass: HomeAssistant) -> None:
    """Checks still running for a removed entry (failed unload) create nothing and don't raise."""
    status = _status(hass, "removed-entry")
    status.record({"DB": "timeout"})
    assert status.problem
    assert not [domain for domain, _ in ir.async_get(hass).issues if domain == DOMAIN]

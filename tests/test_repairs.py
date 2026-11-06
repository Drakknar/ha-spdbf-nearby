"""Repairs notices (status.py, dbf.py) belong to their config entry (HA 2026.11)."""

from datetime import timedelta

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

SERVER = "http://dbf.local:8092"
POSITION = {"lat": "52.5051", "lon": "13.3193"}


def _config(server: str) -> dict:
    return {CONF_SERVER_URL: server, CONF_SOURCES: ["DB", "BVG"]}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2)
    entry.add_to_hass(hass)
    return entry


def _status(hass: HomeAssistant, entry_id: str) -> DbfStatus:
    # problem_after 0: the first failing round is a problem.
    return DbfStatus(hass, entry_id, SERVER, timedelta(0), True)


async def test_unreachable_notice_is_linked_to_the_entry(hass: HomeAssistant) -> None:
    """The notice carries its entry, and a working round ends it."""
    entry = _entry(hass)
    status = _status(hass, entry.entry_id)
    issue_id = f"{ISSUE_SERVER_UNREACHABLE}_{entry.entry_id}"
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
    issue_id = f"{ISSUE_SERVER_UNREACHABLE}_{entry.entry_id}"
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
    issue_id = f"{ISSUE_SERVER_UNREACHABLE}_{entry.entry_id}"
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
    clients = {
        first.entry_id: DbfClient(hass, _config(unpatched), first.entry_id),
        second.entry_id: DbfClient(hass, _config(patched), second.entry_id),
    }
    for client in clients.values():
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

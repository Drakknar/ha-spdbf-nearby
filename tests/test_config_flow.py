"""Options flow (config_flow.py) through Home Assistant's flow manager."""

from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant.common import MockConfigEntry

from custom_components.spdbf_nearby.config_flow import SpdbfOptionsFlow
from custom_components.spdbf_nearby.const import (
    CONF_DEPARTURES,
    CONF_MAP_STOPS,
    CONF_MAX_STOPS,
    CONF_MERGE_DUPLICATES,
    CONF_PUBLIC_URL,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    CONF_SERVER_URL,
    CONF_SOURCES,
    CONF_TRACKED_ENTITY,
    DEFAULT_DEPARTURES,
    DEFAULT_MERGE_DUPLICATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.spdbf_nearby.coordinator import SpdbfCoordinator
from custom_components.spdbf_nearby.dbf import DbfClient
from homeassistant.data_entry_flow import FlowResultType

SERVER = "http://dbf.local:8092"


async def test_public_url_can_be_cleared(hass, aioclient_mock) -> None:
    """Emptying the public URL in the options stores it empty, so links go to the server URL again instead of the old public URL."""
    aioclient_mock.post(f"{SERVER}/_geolocation", json={"candidates": []})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={
            CONF_SERVER_URL: SERVER,
            CONF_PUBLIC_URL: "https://dbf.example.org",
            CONF_TRACKED_ENTITY: "person.me",
            CONF_SOURCES: ["DB", "BVG"],
        },
    )
    entry.add_to_hass(hass)
    flow = SpdbfOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    with patch.object(
        type(flow), "config_entry", new_callable=lambda: property(lambda self: entry)
    ):
        form = await flow.async_step_init()
        schema = form["data_schema"].schema
        key = next(k for k in schema if k == CONF_PUBLIC_URL)
        # Shown, but not a default the cleared field would fall back to.
        assert key.description == {"suggested_value": "https://dbf.example.org"}
        # The field cleared: the frontend sends no key, and none comes back.
        user_input = form["data_schema"](
            {
                CONF_SERVER_URL: SERVER,
                CONF_TRACKED_ENTITY: "person.me",
                CONF_SOURCES: ["DB", "BVG"],
            }
        )
        assert CONF_PUBLIC_URL not in user_input
        result = await flow.async_step_init(user_input)
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PUBLIC_URL] == ""
    # Stop links then go to the server URL again.
    client = DbfClient(hass, {**entry.data, **result["data"]}, entry.entry_id)
    assert client.board_url("8011160", "DB") == f"{SERVER}/8011160"


async def test_map_settings_hidden_without_map_stops(hass, aioclient_mock) -> None:
    """With map stops off, the map-only fields are hidden: a submitted value is dropped, the saved one kept."""
    aioclient_mock.post(f"{SERVER}/_geolocation", json={"candidates": []})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={
            CONF_SERVER_URL: SERVER,
            CONF_TRACKED_ENTITY: "person.me",
            CONF_SOURCES: ["DB", "BVG"],
            CONF_MAP_STOPS: True,
            CONF_DEPARTURES: 7,
            CONF_SCAN_INTERVAL: 90,
            CONF_MERGE_DUPLICATES: False,
        },
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    visible = {
        str(key): key.visible
        for key in form["data_schema"].schema
        if getattr(key, "visible", None) is not None
    }
    assert set(visible) == {CONF_DEPARTURES, CONF_MERGE_DUPLICATES, CONF_SCAN_INTERVAL}

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SERVER_URL: SERVER,
            CONF_TRACKED_ENTITY: "person.me",
            CONF_SOURCES: ["DB", "BVG"],
            CONF_MAP_STOPS: False,
            CONF_DEPARTURES: 2,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAP_STOPS] is False
    # The hidden fields keep what was saved, for when map stops come back on.
    assert result["data"][CONF_DEPARTURES] == 7
    assert result["data"][CONF_SCAN_INTERVAL] == 90
    assert result["data"][CONF_MERGE_DUPLICATES] is False


async def test_coordinator_without_map_settings(hass) -> None:
    """An entry saved without the map settings (hidden in the form) uses the defaults."""
    data = {
        CONF_SERVER_URL: SERVER,
        CONF_TRACKED_ENTITY: "person.me",
        CONF_SOURCES: ["DB"],
        CONF_RADIUS: 1.0,
        CONF_MAX_STOPS: 12,
    }
    for map_stops, interval in (
        (False, None),
        (True, timedelta(seconds=DEFAULT_SCAN_INTERVAL)),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            version=1,
            minor_version=2,
            data={**data, CONF_MAP_STOPS: map_stops},
        )
        entry.add_to_hass(hass)
        assert SpdbfCoordinator(hass, entry, None).update_interval == interval


async def test_new_entry_stores_the_hidden_map_settings(hass, aioclient_mock) -> None:
    """Set up with map stops off, the entry still stores the map settings (defaults), so 0.11.x can load it after a rollback."""
    aioclient_mock.post(f"{SERVER}/_geolocation", json={"candidates": []})
    # Only the stored data matters here, not setting the entry up.
    with patch("custom_components.spdbf_nearby.async_setup_entry", return_value=None):
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_SERVER_URL: SERVER,
                CONF_TRACKED_ENTITY: "person.me",
                CONF_SOURCES: ["DB"],
                CONF_MAP_STOPS: False,
                CONF_DEPARTURES: 2,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_DEPARTURES] == DEFAULT_DEPARTURES
    assert data[CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL
    assert data[CONF_MERGE_DUPLICATES] is DEFAULT_MERGE_DUPLICATES

"""Options flow (config_flow.py) through Home Assistant's flow manager."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spdbf_nearby.config_flow import SpdbfOptionsFlow
from custom_components.spdbf_nearby.const import (
    CONF_PUBLIC_URL,
    CONF_SERVER_URL,
    CONF_SOURCES,
    CONF_TRACKED_ENTITY,
    DOMAIN,
)
from custom_components.spdbf_nearby.dbf import DbfClient

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
    client = DbfClient(hass, {**entry.data, **result["data"]})
    assert client.board_url("8011160", "DB") == f"{SERVER}/8011160"

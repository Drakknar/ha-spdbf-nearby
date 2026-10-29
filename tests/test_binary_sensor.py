"""The DBF problem sensor names itself the Home Assistant way (since 0.11.1)."""

import json
from pathlib import Path

from custom_components.spdbf_nearby.binary_sensor import DbfProblemSensor
from custom_components.spdbf_nearby.const import DOMAIN
from homeassistant.helpers.device_registry import DeviceEntryType

TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / DOMAIN
    / "translations"
    / "en.json"
)


class FakeStatus:
    """Just enough of DbfStatus for the entity."""

    problem = False

    def async_add_listener(self, listener):
        """Accept a listener and return a no-op unsubscribe."""
        return lambda: None

    def attributes(self) -> dict:
        """Return the status attributes of a working server."""
        return {"failing_since": None}


def test_sensor_uses_native_naming() -> None:
    """Test the sensor's entity naming, unique id and device."""
    sensor = DbfProblemSensor("entry1", FakeStatus())
    assert sensor.has_entity_name
    assert sensor.translation_key == "dbf_problem"
    assert sensor.unique_id == "entry1_dbf_problem"
    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, "entry1")}
    assert info["name"] == "spdbf Public Transport"
    assert info["entry_type"] is DeviceEntryType.SERVICE


def test_translation_key_has_a_name() -> None:
    """Test that the sensor's translation key has an English name."""
    strings = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))
    assert strings["entity"]["binary_sensor"]["dbf_problem"]["name"] == "DBF problem"


def test_attributes_keep_the_frontend_marker() -> None:
    """Test that the attributes keep the marker the frontend looks for."""
    sensor = DbfProblemSensor("entry1", FakeStatus())
    assert sensor.extra_state_attributes["spdbf_status"] is True
    assert sensor.is_on is False

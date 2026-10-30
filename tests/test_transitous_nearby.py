"""Nearest stops from Transitous (Assist's fallback while DBF is down)."""

from typing import Any

from custom_components.spdbf_nearby.const import TRANSITOUS_URL
from custom_components.spdbf_nearby.service import TransportService
from custom_components.spdbf_nearby.stations import Stop
from custom_components.spdbf_nearby.transitous import TransitousClient, TransitousError

POS = (52.5054, 13.3053)
# As api.transitous.org answered for this position (2026-10-24), shortened.
MATCHES = [
    {
        "type": "STOP",
        "id": "de-VBB_de:11000:900024205::1",
        "name": "U Wilmersdorfer Str./S Charlottenburg (Berlin)",
        "lat": 52.505646,
        "lon": 13.306052,
    },
    {
        "type": "STOP",
        "id": "de-DELFI_de:11000:900024101",
        "name": "S Charlottenburg Bhf (Berlin)",
        "lat": 52.50473,
        "lon": 13.303862,
    },
    {
        "type": "STOP",
        "id": "de-VBB_de:11000:900024202",
        "name": "U Wilmersdorfer Str. (Berlin)",
        "lat": 52.506416,
        "lon": 13.306774,
    },
    {
        "type": "STOP",
        "id": "de-VBB_de:11000:900024152::1",
        "name": "Hindemithplatz (Berlin)",
        "lat": 52.50326,
        "lon": 13.307651,
    },
    {"type": "STOP", "id": "far", "name": "Far away", "lat": 52.6, "lon": 13.5},
]


async def test_client_asks_reverse_geocode_once_per_place(hass, aioclient_mock) -> None:
    """Test that the client asks Transitous once per place and caches the answer."""
    aioclient_mock.get(f"{TRANSITOUS_URL}/v1/reverse-geocode", json=MATCHES)
    client = TransitousClient(hass)
    stops = await client.async_nearby(POS)
    assert [s.eva for s in stops][:2] == [
        "de-VBB_de:11000:900024205::1",
        "de-DELFI_de:11000:900024101",
    ]
    assert all(s.source == "Transitous" for s in stops)
    # Asked again for the same place within a minute: from the cache.
    await client.async_nearby((52.50541, 13.30531))
    assert aioclient_mock.call_count == 1
    _method, url, _data, _headers = aioclient_mock.mock_calls[0]
    assert url.query["type"] == "STOP"
    assert url.query["place"] == f"{POS[0]},{POS[1]}"


class FakeTransitous:
    """Stand-in for the Transitous client's stop lookup."""

    def __init__(self, stops: list[Stop] | None) -> None:
        """Return stops from lookups; None makes them fail."""
        self.stops = stops

    async def async_nearby(self, pos: Any) -> list[Stop]:
        """Return the stops, or fail when there are none."""
        if self.stops is None:
            raise TransitousError("down")
        return self.stops


def _service(transitous: Any) -> TransportService:
    service = TransportService.__new__(TransportService)
    service.transitous = transitous
    return service


def _as_stops() -> list[Stop]:
    return [
        Stop(
            name=m["name"],
            eva=m["id"],
            source="Transitous",
            latitude=m["lat"],
            longitude=m["lon"],
        )
        for m in MATCHES
    ]


async def test_service_keeps_the_nearest_three_within_the_radius() -> None:
    """Test that the fallback keeps the nearest three stops within the radius."""
    # Reversed, so the order must come from the distances (about 58, 123,
    # 151 and 240 m; "far" is outside the radius).
    found = _as_stops()[::-1]
    stops = await _service(FakeTransitous(found)).async_nearby_fallback(POS, 1000)
    assert [s.eva for s in stops] == [
        "de-VBB_de:11000:900024205::1",
        "de-DELFI_de:11000:900024101",
        "de-VBB_de:11000:900024202",
    ]


async def test_service_without_transitous_or_when_it_fails() -> None:
    """Test that the fallback returns no stops without Transitous or when it fails."""
    assert await _service(None).async_nearby_fallback(POS, 1000) == []
    assert await _service(FakeTransitous(None)).async_nearby_fallback(POS, 1000) == []

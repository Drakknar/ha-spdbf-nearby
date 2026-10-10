# spdbf Nearby Stops for Home Assistant

Show public transport stops near you on the Home Assistant map, with live departures, using your own [DBF](https://github.com/derf/db-fakedisplay) (db-fakedisplay) server.

- **Stops follow you.** Stops around a person or device tracker appear on the map and update as you move.
- **Train or bus at a glance.** Map markers show a train for DB stations and S-/U-Bahn stops, a bus for BVG stops, and a generic stop icon for other networks.
- **Tap a stop for its departures.** You get the line, destination, platform, minutes until departure, delays and cancellations, plus a link to the full DBF board. This works on the Map panel, on any map card and from entity search. It follows your theme, including light/dark mode.
- **DB + BVG by default.** Any HAFAS network that DBF supports can be added.
- **Local:** Home Assistant only talks to your DBF server.

## Requirements

- Home Assistant 2026.9.0 or newer.
- A DBF server that Home Assistant can reach.
- **For BVG and other HAFAS stops on the map:** the patched image [`ghcr.io/drakknar/db-fakedisplay`](https://github.com/Drakknar/db-fakedisplay-patched). Upstream DBF doesn't send coordinates for HAFAS stops. With an unpatched server, only DB stops appear, and Home Assistant shows a Repairs notice explaining why.
- **For your location:** a person or device tracker with GPS, such as the Home Assistant companion app.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/Drakknar/ha-spdbf-nearby` as type **Integration**.
3. Install **spdbf Nearby Stops**, then restart Home Assistant.

### Manual

Copy `custom_components/spdbf_nearby` into your Home Assistant `config/custom_components/` folder, then restart Home Assistant.

After installing or updating, reload the Home Assistant page or companion app once so it picks up the departure dialog.

## Setup

**Settings → Devices & services → Add integration → spdbf Nearby Stops.**

| Setting | Default | What it does |
|---|---|---|
| Server URL | | Address of your DBF server, for example `http://192.168.1.10:8092`. |
| Public URL | server URL | Where you open DBF in a browser, if different. Each stop links here. |
| Person or device to follow | first person | Stops are searched around this entity's location. |
| Sources | DB, BVG | `DB` is DBF's built-in DB station list. Everything else is a HAFAS network name. |
| Search radius | 1 km | Only stops within this distance are shown. |
| Maximum number of stops | 12 | The nearest stops are kept. |
| Departures per stop | 5 | Set to 0 to show stops only. |
| Departure refresh interval | 60 s | How often departures are updated. |

Stops are searched again after you move more than 100 m, or every 15 minutes. Location fixes less accurate than 250 m are ignored.

## Dashboard

The stops appear on the built-in **Map** panel with no setup. For a dashboard, [`dashboard_card.yaml`](custom_components/spdbf_nearby/dashboard_card.yaml) has two example cards: a map card, and a Markdown card listing departures for the nearest stops.

Each stop is a `geo_location` entity with these attributes:

| Attribute | Content |
|---|---|
| `network` | `DB` or the HAFAS network name |
| `eva` | Station ID |
| `distance_km` | Live distance to you |
| `departures` | List of `line`, `destination`, `platform`, `scheduled`, `delay`, `minutes`, `cancelled` |
| `next_departure` | For example `S3 → Spandau in 4 min` |
| `url` | Link to the DBF departure board |
| `departures_via` | The source the departures came from, when DBF's DB source was empty and the integration used its fallback (ÖBB) |
| `departures_error` | Why no departures could be fetched, if no source had any |

## Notes

- **ÖBB fallback:** DB's IRIS feed, which DBF uses for DB stations, is currently empty for some stations (for example Berlin Zoologischer Garten). For those, the integration asks DBF's ÖBB HAFAS backend, which accepts DB station IDs.
- **Entity registry:** stop entities aren't added to the registry, so stops you pass while travelling don't pile up there.
- **History:** departures, distance and the board link aren't written to the recorder database, since they change on every refresh. Only the stop itself and its distance (the entity state) are recorded. To record nothing at all, exclude `geo_location.*` in your `recorder:` settings.

## Development

There are no automated tests yet. How the integration works, how changes have been checked, and the open items are in [`custom_components/spdbf_nearby/CLAUDE.md`](custom_components/spdbf_nearby/CLAUDE.md).

## License

[MIT](LICENSE). The marker icons are based on [Material Design Icons](https://pictogrammers.com/library/mdi/) (Apache-2.0).

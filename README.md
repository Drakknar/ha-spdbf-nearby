# spdbf Nearby Stops for Home Assistant

Show public transport stops near you on the Home Assistant map, with live departures, using your own [DBF](https://github.com/derf/db-fakedisplay) (db-fakedisplay) server.

- **Stops follow you.** Stops around a person or device tracker appear on the map and update as you move.
- **Train or bus at a glance.** Map markers show a train for DB stations and S-/U-Bahn stops, a bus for BVG stops, and a generic stop icon for other networks.
- **Tap a stop for its departures.** The dialog loads the stop's full board, grouped by line and destination ("U2 → Pankow: 2 · 7 · 12 min"), or sorted by time if you prefer. You get platforms, delays and cancellations, plus a link to the DBF board. This works on the Map panel, on any map card, from entity search and from the stop search card. It follows your theme, including light/dark mode.
- **Search any stop.** A dashboard card finds DB and BVG stops by name. Tap the empty field to list the stops near you.
- **One marker per station.** BVG lists some stations several times (Alexanderplatz has a stop for each exit and bus bay, all with the same board). Stops with identical departures are merged into one.
- **DB and BVG together.** A DB station and the BVG S-/U-Bahn stop of the same station, such as Berlin Savignyplatz and S Savignyplatz, are one marker. It shows every BVG line plus the DB lines BVG doesn't list, such as regional and long-distance trains.
- **DB + BVG by default.** Any HAFAS network that DBF supports can be added.
- **Local:** Home Assistant only talks to your DBF server.

## Requirements

- Home Assistant 2026.9.0 or newer.
- A DBF server that Home Assistant can reach.
- **For BVG and other HAFAS stops on the map, and for the stop search:** the patched image [`ghcr.io/drakknar/db-fakedisplay`](https://github.com/Drakknar/db-fakedisplay-patched). Upstream DBF doesn't send coordinates for HAFAS stops and can't search stops by name as JSON. With an unpatched server, only DB stops appear on the map, Home Assistant shows a Repairs notice explaining why, and the search card says it needs the patched image.
- **For your location:** a person or device tracker with GPS, such as the Home Assistant companion app.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/Drakknar/ha-spdbf-nearby` as type **Integration**.
3. Install **spdbf Nearby Stops**, then restart Home Assistant.

Updates are published as [releases](https://github.com/Drakknar/ha-spdbf-nearby/releases), which HACS offers as they appear.

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
| Departures per stop | 5 | Departures kept on each map stop (for `next_departure` and Markdown cards). The dialog always loads the full board. Set to 0 to show stops only. |
| Merge duplicate stops | on | Show one stop when nearby stops have exactly the same departures. Needs departures turned on. |
| Merge DB and BVG stations | on | Show a DB station and the BVG S-/U-Bahn stop of the same name, at most 300 m apart, as one stop. Bus and tram stops stay separate. |
| Use a public DBF instance while yours is unreachable | off | See [When your DBF server is down](#when-your-dbf-server-is-down). |
| Public DBF instance | `https://dbf.finalrewind.org` | The instance used for that. |
| Departure refresh interval | 60 s | How often departures are updated. |

Stops are searched again after you move more than 100 m, or every 15 minutes. Location fixes less accurate than 250 m are ignored.

## Dashboard

The stops appear on the built-in **Map** panel with no setup. For a dashboard, [`dashboard_card.yaml`](custom_components/spdbf_nearby/dashboard_card.yaml) has example cards: a map card, a Markdown card listing departures for the nearest stops, and the stop search card.

### Stop search card

Dashboard → Edit → **Add card** → **spdbf stop search** (under custom cards). The integration loads the card itself, so there's no dashboard resource to add. Its options have a visual editor. It fits under a heading card in a sections dashboard.

In YAML:

```yaml
type: custom:spdbf-stop-search-card
```

- **Empty field:** tap it to list the stops near you. The card asks the browser for your location. Without it (permission denied, or not on HTTPS), it uses the person or device the integration follows.
- **Typing:** two characters or more search DB and your HAFAS sources by name.
- **Picking a stop** opens its departures. A stop that is also on the map opens as that entity, so "Entity details" works.

| Option | Default | What it does |
|---|---|---|
| `placeholder` | `Search stops` | Text in the empty field. |
| `use_device_location` | `true` | Set to `false` to always use the followed person's location. |
| `entry_id` | | Which integration entry to use, if you have several. By default, the entry that follows the person of the logged-in user, else the first. |

Each stop is a `geo_location` entity with these attributes:

| Attribute | Content |
|---|---|
| `network` | `DB` or the HAFAS network name |
| `eva` | Station ID |
| `distance_km` | Live distance to you |
| `departures` | List of `line`, `destination`, `platform`, `scheduled`, `delay`, `minutes`, `cancelled` |
| `next_departure` | For example `S3 → Spandau in 4 min` |
| `url` | Link to the DBF departure board |
| `merged_stops` | Names of nearby stops merged into this one: the same board, or the same station in DB |
| `networks` | For a merged DB + BVG station: the networks its departures come from, e.g. `["BVG", "DB"]` |
| `departures_via` | The source the departures came from, when DBF's DB source was empty and the integration used its fallback (ÖBB) |
| `departures_error` | Why no departures could be fetched, if no source had any |

## When your DBF server is down

- **Fails fast:** after one failed request, your server is skipped for a minute and then tried again. You don't wait for a timeout at every stop.
- **Stops stay on the map:** each stop's dialog and `departures_error` say the server isn't reachable. If the server is down when Home Assistant starts, the integration retries setup by itself.
- **Long outages:** after 5 minutes a Repairs notice appears. It disappears once the server answers again.
- **You can still change settings:** the options can be saved while your server is down, as long as you keep its URL.

**Public fallback (off by default).** When it's on, departures come from the public instance until your server is back. The stops and the dialog then say "from the public DBF".

The public instance runs stock DBF and asks for at most 10 requests per minute, so:
- **Fewer updates:** only the 3 nearest stops keep updating. The dialog still loads full boards within that budget.
- **No new stops while you have some:** stops already on the map stay. Stops are only looked up there if the map has none, and then only DB stations appear, because stock DBF sends no coordinates for BVG stops.
- **No stop search:** the search card needs your own server.

## Notes

- **ÖBB fallback:** DB's IRIS feed, which DBF uses for DB stations, is currently empty for some stations (for example Berlin Zoologischer Garten). For those, the integration asks DBF's ÖBB HAFAS backend, which accepts DB station IDs.
- **Entity registry:** stop entities aren't added to the registry, so stops you pass while travelling don't pile up there.
- **History:** departures, distance and the board link aren't written to the recorder database, since they change on every refresh. Only the stop itself and its distance (the entity state) are recorded. To record nothing at all, exclude `geo_location.*` in your `recorder:` settings.

## Development

There are no automated tests yet. How the integration works, how changes have been checked, and the open items are in [`custom_components/spdbf_nearby/CLAUDE.md`](custom_components/spdbf_nearby/CLAUDE.md).

## License

[MIT](LICENSE). The marker icons are based on [Material Design Icons](https://pictogrammers.com/library/mdi/) (Apache-2.0).

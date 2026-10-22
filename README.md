# spdbf Public Transport for Home Assistant

Live public transport in Home Assistant, from your own [DBF](https://github.com/derf/db-fakedisplay) (db-fakedisplay) server: stops near you on the map, a stop search, cards for the connections you use with ride and walk times, and trip details. DB and BVG by default.

**Your connections**

- **A card per leg of your trip.** Departures of a [db_infoscreen](https://github.com/FaserF/ha-db_infoscreen) sensor with line colours, platforms, delays and cancellations, filtered to your platform and line. See [Connection card](#connection-card).
- **Ride and walk times between legs.** "↓ 15 min ride · 4 min walk" under each leg, and for the last one the ride to where you get off.

**Stops near you and any stop**

- **Stops follow you.** Stops around a person or device tracker appear on the map and update as you move.
- **Train or bus at a glance.** Map markers show a train for DB stations and S-/U-Bahn stops, a bus for BVG stops, and a generic stop icon for other networks.
- **Search any stop.** A dashboard card finds DB and BVG stops by name. Tap the empty field to list the stops near you.

**Departures and trips**

- **Tap a stop for its departures.** The dialog loads the stop's full board, grouped by line and destination ("U2 → Pankow: 2 · 7 · 12 min"), or sorted by time if you prefer. You get platforms, delays and cancellations, plus a link to the DBF board. This works on the Map panel, on any map card, from entity search, the stop search card and the connection cards. It follows your theme, including light/dark mode.
- **Follow a trip.** Tap a departure to see its stops and times, with delays and disruption notices, from DBF.

**Stations**

- **One marker per station.** BVG lists some stations several times (Alexanderplatz has a stop for each exit and bus bay, all with the same board). Stops with identical departures are merged into one.
- **DB and BVG together.** A DB station and the BVG S-/U-Bahn stop of the same station, such as Berlin Savignyplatz and S Savignyplatz, are one marker. It shows every BVG line plus the DB lines BVG doesn't list, such as regional and long-distance trains.
- **DB + BVG by default.** Any HAFAS network that DBF supports can be added.
- **Local:** Home Assistant only talks to your DBF server.

## Requirements

- Home Assistant 2026.9.0 or newer.
- A DBF server that Home Assistant can reach.
- **For BVG and other HAFAS stops on the map, and for the stop search:** the patched image [`ghcr.io/drakknar/db-fakedisplay`](https://github.com/Drakknar/db-fakedisplay-patched). Upstream DBF doesn't send coordinates for HAFAS stops and can't search stops by name as JSON. With an unpatched server, only DB stops appear on the map, Home Assistant shows a Repairs notice explaining why, and the search card says it needs the patched image.
- **For your location:** a person or device tracker with GPS, such as the Home Assistant companion app.
- **For the connection cards:** [db_infoscreen](https://github.com/FaserF/ha-db_infoscreen) departure sensors for your stops. Their ride times also need the patched image (BVG trip data).

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/Drakknar/ha-spdbf-nearby` as type **Integration**.
3. Install **spdbf Public Transport**, then restart Home Assistant.

Updates are published as [releases](https://github.com/Drakknar/ha-spdbf-nearby/releases), which HACS offers as they appear.

### Manual

Copy `custom_components/spdbf_nearby` into your Home Assistant `config/custom_components/` folder, then restart Home Assistant.

After installing or updating, reload the Home Assistant page or companion app once so it picks up the departure dialog.

## Setup

**Settings → Devices & services → Add integration → spdbf Public Transport.**

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
| Show nearby stops on the map | on | Off: no stop entities and no polling. The search card, departures and trips still work; "near you" uses the device's location or the followed person's. |
| Merge DB and BVG stations | on | Show a DB station and the BVG S-/U-Bahn stop of the same name, at most 300 m apart, as one stop. Bus and tram stops stay separate. |
| Use Transitous while DBF can't deliver | on | See [When DBF can't deliver](#when-dbf-cant-deliver). |
| Report DBF as down after | 10 min | How long DBF must fail before the problem sensor, a Repairs notice and the cards' warning appear. |
| Departure refresh interval | 60 s | How often departures are updated. |

Stops are searched again after you move more than 100 m, or every 15 minutes. Location fixes less accurate than 250 m are ignored.

## Dashboard

The stops appear on the built-in **Map** panel with no setup. For a dashboard, [`dashboard_card.yaml`](custom_components/spdbf_nearby/dashboard_card.yaml) has example cards: a map card, a Markdown card listing departures for the nearest stops, and the stop search card.

### Stop search card

Dashboard → Edit → **Add card** → **spdbf stop search** (under custom cards). The integration loads the card itself and adds its own dashboard resource (see [Card loading](#card-loading)), so there's nothing to add by hand. Its options have a visual editor. It fits under a heading card in a sections dashboard.

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
| `departures` | List of `line`, `destination`, `platform`, `scheduled`, `delay`, `minutes`, `cancelled`, `expected` (ISO time) |
| `next_departure` | For example `S3 → Spandau in 4 min` |
| `url` | Link to the DBF departure board |
| `merged_stops` | Names of nearby stops merged into this one: the same board, or the same station in DB |
| `networks` | For a merged DB + BVG station: the networks its departures come from, e.g. `["BVG", "DB"]` |
| `departures_via` | The source the departures came from, when DBF's DB source was empty and the integration used its fallback (ÖBB) |
| `departures_error` | Why no departures could be fetched, if no source had any |
| `fallback`, `origin`, `as_of` | Only while DBF can't deliver: `origin` is `transitous` or `last_known`, and `as_of` when last known departures were fetched |

### Connection card

For the connections you use: one card per stop, from a [db_infoscreen](https://github.com/FaserF/ha-db_infoscreen) departures sensor. Dashboard → Edit → **Add card** → **spdbf connection**, with a visual editor; no dashboard resource to add by hand.

- **Departures** with line colours (Berlin S- and U-Bahn), platform, minutes, delay and cancellations, the same look as the departure dialog.
- **Tap a departure** for its trip, or the title for the stop's full board.
- **Filters that stay right:** in the editor, pick a platform from a list that shows the lines and destinations stopping there (the platform is the direction). Lines and destinations are suggested from what the sensor has now. A check at the top says what the card would show, and warns about a filter that matches nothing, e.g. a destination the line doesn't reach tonight because it turns back early. Prefer platform and line over destination: lines often end early, and a destination filter then empties the card. At stops without platforms (most bus stops), hide the other direction's destinations instead: a bus that ends early in your direction still shows.
- **Changing trains:** a row under the card leads to the next leg, e.g. "↓ 15 min ride · 4 min walk". Each part can be turned off.
  - **Ride:** set "Ride to" to the next card's sensor. The ride is timed on this card's lines (S-Bahn to S-Bahn, bus to bus), so it ends where that train arrives. It comes from BVG trip data, which needs the patched DBF image; DB's data has no reliable per-stop times for S-Bahn trains.
  - **Last leg:** instead, type a station on the line into "Or ride to a station", e.g. where you get off: "↓ 6 min ride to S Schönhauser Allee · 10 min walk". The name is matched without the S/U prefix and the town in brackets; write the mode as a prefix ("U Schönhauser Allee").
  - **Walk:** the minutes you enter after the ride: the change to the next card's platform, or on from the station.
  - **Before 0.8.0** the row sat above the second card (`ride_from`, the previous card's sensor). That still works on a card without `ride_to` or `ride_to_station`.
- **When the sensor is unavailable** (DBF down for longer than any cache in front of it covers), the card shows the station's departures from the integration instead: from DBF, else Transitous, else the last known ones, and says which. It remembers the station, and the destinations seen on its platforms, while the sensor works. Transitous rarely knows S-Bahn platforms, so those destinations then stand for the direction.

```yaml
type: custom:spdbf-connection-card
entity: sensor.db_infoscreen_berlin_westkreuz_departures
title: Westkreuz        # optional
departures: 6           # optional
platforms: ["11"]       # optional: "2" also matches BVG's "2 (U2)"
lines: [S41]            # optional: only these lines
destinations: [Pankow]  # optional: part of the destination is enough
hide_destinations: [Staaken]  # optional: hide these; for stops without platforms
ride_to: sensor.db_infoscreen_berlin_alexanderplatz_departures  # optional: the next card
ride_to_station: U Schönhauser Allee  # optional, for the last leg instead
walk_minutes: 4         # optional: walk after the ride
show_ride: true         # optional
show_walk: true         # optional
```

## Trips

Tapping a departure in the dialog shows its trip: every stop with its time, delays and platform, and disruption notices. The station you opened it from is highlighted, and earlier stops are dimmed.

- **BVG and other HAFAS networks:** needs the patched DBF image (`hafas-trip-id`).
- **DB trains:** the stops always show. Times show when DBF can match them to the train for sure; otherwise the dialog lists the stops only.

Other cards can use the same dialog, trip view and line colours through `window.dbfCore` (see CLAUDE.md).

## When DBF can't deliver

DBF fails in two ways: your server is unreachable, or it answers with errors because DB's or a transport company's systems fail. For every board, trip, search and ride the integration then uses, in this order:

1. **DBF**, as long as it delivers. A board that is merely empty (no service right now) counts.
2. **[Transitous](https://transitous.org)** (option on by default): a free, community-run, Europe-wide journey planner. Its data is live, but has fewer delays and platforms than DBF (in Berlin it misses many S-Bahn delays and most S-Bahn platforms). Trips of Transitous departures come from Transitous too; a DB or BVG trip can't be looked up there.
3. **The last known answer** for the same board, trip, search or ride, up to 24 hours old, kept in Home Assistant's storage. Departures that have left are dropped, and the minutes are counted from now.

The dialog, the cards and the stop entities say where the data comes from: "from Transitous (DBF can't deliver)" or "last known, as of 14:05".

- **Fails fast:** after one failed request, your server is skipped for a minute and then tried again. You don't wait for a timeout at every stop.
- **Map stops:** stops already on the map stay. Only the 3 nearest ask Transitous; the others show their last known departures. New stops are only looked up in DBF: if it's down when Home Assistant starts, the map starts empty and fills once DBF answers, while the cards, the search and the status checks already work.
- **Status checks:** every minute the integration fetches one live board per network from DBF (Berlin Hbf for DB and BVG, else the nearest map stop of that network), past any cache in front of DBF. When a network's checks have failed for the "Report DBF as down after" time (10 minutes by default):
  - `binary_sensor.spdbf_dbf_problem` turns on (device class *problem*; attributes `failing_since` and, per network, since when and why);
  - a Repairs notice appears;
  - the cards and the dialog show "Live data from DBF unavailable since 14:05".

  All three clear by themselves once DBF delivers again. Use the sensor for your own notifications.
- **You can still change settings:** the options can be saved while your server is down, as long as you keep its URL.

## Card loading

Home Assistant loads the cards through an "extra module" on every page, which the departure dialog needs too. It imports that module only once, when the page opens: if that one import fails (the companion app opening before the network or VPN is up, a browser restoring tabs after sleep), the cards show "Configuration error" until you reload. So the integration also adds the same loader as a dashboard resource, which Home Assistant imports again whenever a dashboard loads. The loader runs once per page either way, and retries the cards' files until they load.

The resource is only added in storage mode (the default; in YAML mode resources are yours to manage), is updated with each release, and is removed when you delete the integration's last entry. Delete the entry before uninstalling through HACS, or remove the resource `/spdbf_nearby/spdbf-loader.js` by hand afterwards.

## Notes

- **ÖBB fallback:** DB's IRIS feed, which DBF uses for DB stations, is currently empty for some stations (for example Berlin Zoologischer Garten). For those, the integration asks DBF's ÖBB HAFAS backend, which accepts DB station IDs.
- **Entity registry:** stop entities aren't added to the registry, so stops you pass while travelling don't pile up there.
- **History:** departures, distance and the board link aren't written to the recorder database, since they change on every refresh. Only the stop itself and its distance (the entity state) are recorded. To record nothing at all, exclude `geo_location.*` in your `recorder:` settings.

## Development

There are no automated tests in the repository; changes are checked with a scratch pytest run against Home Assistant's test harness. How the integration works, how changes have been checked, and the open items are in [`custom_components/spdbf_nearby/CLAUDE.md`](custom_components/spdbf_nearby/CLAUDE.md).

## License

[MIT](LICENSE). The marker icons are based on [Material Design Icons](https://pictogrammers.com/library/mdi/) (Apache-2.0).

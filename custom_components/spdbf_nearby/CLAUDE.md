# CLAUDE.md

Context for picking this project up later. The user-facing docs are in README.md; this file covers how the integration works, what was verified, and what is still open.

## What this is

`spdbf_nearby` is a Home Assistant custom integration, distributed through HACS as `Drakknar/ha-spdbf-nearby`. It shows public transport stops near a tracked person on the Home Assistant map, with live departures, using the author's own [DBF](https://github.com/derf/db-fakedisplay) (db-fakedisplay) server. Home Assistant only talks to that DBF server.

It belongs with two sibling projects:

- **`Drakknar/db-fakedisplay-patched`:** the DBF image this integration expects. Upstream DBF doesn't return coordinates for HAFAS stops (BVG etc.), so without the patched image only DB stations appear.
- **`Drakknar/ha-dbf-departures-card`:** a dashboard card for db_infoscreen sensors. It is unrelated to this integration's map dialog. When the author talks about "the map" or tapping a stop, they mean this integration.

## Layout

This folder is self-contained, like `ha-audiocpp`: code and these notes. Only `hacs.json` and `README.md` sit at the repository root, because HACS needs them there.

```
<repo root>/
  hacs.json, README.md              HACS metadata (min HA 2026.9.0) and user docs
  custom_components/spdbf_nearby/   <- this folder
    __init__.py         Serves www/ as static files, injects both scripts on every page
                        (add_extra_js_url), registers the WebSocket commands, creates the
                        DbfClient and the coordinator, and the geo_location platform if map stops are on
    dbf.py              Core: DbfClient talks to DBF (fast fail, public fallback, Repairs), reads
                        boards, searches, caches full boards, fetches trips
    stations.py         Core, no network: Stop/Board, tidy_line, DB + BVG linking, same-board
                        merging, board combining, search ranking
    coordinator.py      The map feature: follows the tracked entity, keeps nearby stops and their
                        departures; with map stops off it only answers "nearby"
    websocket_api.py    spdbf_nearby/search, /nearby, /board and /trip for the cards and the dialog
    geo_location.py     One SpdbfStop entity per stop; added and removed as the stop list changes
    config_flow.py      Setup and options flow
    const.py            Settings, defaults, FRONTEND_VERSION, thresholds
    www/
      dbf-core.js               Frontend core, window.dbfCore: tidyLine, lineKey, lineStyle (line
                                colours), showStop (departure dialog), showTrip; also replaces
                                more-info for stop entities (window.spdbfShowStop: old name)
      spdbf-stop-search-card.js custom:spdbf-stop-search-card, the stop search card
      spdbf-connection-card.js  custom:spdbf-connection-card: one connection from a db_infoscreen sensor
      marker-{train,bus,stop}.svg   Map marker pictures (entity_picture)
    dashboard_card.yaml Example map card and Markdown departures card
    translations/en.json
    CLAUDE.md           These notes
```

## How it works

- **Following the person:** the coordinator listens to state changes of the tracked entity (a person or device tracker with GPS). Fixes worse than 250 m accuracy are ignored.
  - Moving 100 m or more, or a stop list older than 15 minutes, triggers a new stop lookup.
  - Smaller moves only refresh the distances, without asking DBF.
- **Stop lookup:** `POST <server>/_geolocation` with `lat`, `lon` and `hafas=<service>`. It is called once per HAFAS source. Each call also returns the DB (IRIS) stations, flagged with `hafas == 0`, so DB needs no call of its own.
  - Candidates without `lat`/`lon` are dropped, and a Repairs issue (`missing_coordinates`) explains that the server needs the patched image.
  - Stops are filtered by radius, sorted by distance and capped at the configured maximum.
  - If a lookup fails but stops are already known, the old list is kept.
- **Departures:** `GET <server>/<eva>?mode=json&limit=<3 × departures>[&hafas=<service>]`, refreshed every scan interval (60 s by default). At most 4 requests run in parallel, because DBF's default container has only a few workers.
  - **Why the entity keeps few departures:** they are the next N across all lines, which at a big station is a handful of lines. Alexanderplatz (BVG 900100003) has about 95 departures in 30 minutes across ~20 lines, so 5 rows held 1–2 U2 trains. The dialog therefore loads the full board itself (below) instead of reading the entity.
  - **DB stations:** IRIS first. If IRIS is empty or fails, the integration retries through DBF's ÖBB HAFAS backend, which accepts DB station numbers. Example: Berlin Zoologischer Garten's IRIS board is empty. `departures_via` records when the fallback was used.
  - IRIS boards include arrival-only rows, hence the `3 ×` limit. IRIS times are German local time (`Europe/Berlin`).
- **Merging duplicate stops (since 0.3.0, option `merge_duplicates`, default on):** BVG lists one station several times, and many of these return the same board. Checked 2026-10-14: Alexanderplatz 900100003, …024 (Dircksenstr.) and …026 (Gontardstr.) have identical boards; so do four Hermannplatz stops. Names are no safe guide: the Memhardstr. tram stop has its own board.
  - **How:** on each stop lookup the coordinator fetches departures for up to 2 × max stops candidates and groups stops of the same source whose departure lists (line, destination, scheduled time; at least 2 rows) are identical.
  - **Which stop is kept:** each group keeps its shortest name, usually the station itself. The others go into `merged_stops`, and the groups are capped at max stops.
  - **When:** only at lookup time (move ≥ 100 m or 15 min), so markers don't flicker between refreshes. With departures set to 0 nothing is merged.
- **Merging DB and BVG stations (since 0.4.0, option `merge_networks`, default on):** a DB station and the BVG rail stop of the same station are one stop. Boards can't be compared here: DB (IRIS) has regional and long-distance trains, BVG has U-Bahn, trams and buses, and both have the S-Bahn.
  - **Matching** (`_link_networks`, `_station_names`): the BVG stop's name must start with a rail prefix (`S `, `U `, `S+U `), and the two must be at most 300 m apart.
    - **Name normalisation:** lower case; brackets dropped (`(Berlin)`, `(S)`, `[Bus]`); the rail prefix dropped; `straße`/`str.` → `str`; `Hbf` → `hauptbahnhof`; `Bhf` dropped.
    - **City prefix:** DB names may also match without their first word, the town ("Berlin Savignyplatz" → "savignyplatz").
    - **Effect:** bus and tram stops of the same name stay separate.
  - **Checked against live DBF data (2026-10-14):** Savignyplatz, Alexanderplatz (both DB entries), Hauptbahnhof (`S+U Berlin Hauptbahnhof` ↔ `Berlin Hbf`, `Berlin Hbf (S-Bahn)`), Friedrichstraße, Brandenburger Tor, Zoologischer Garten, Spandau (`Berlin-Spandau`), Ostkreuz, Rummelsburg, Südkreuz, Potsdamer Platz, Gesundbrunnen (`Berlin Gesundbrunnen(S)`) and Humboldthain all matched. No bus or tram stop did.
  - **The kept stop:** the BVG stop stays, with its key, name and link. The DB stations go into `linked` and `merged_stops`, and `networks` becomes e.g. `["BVG", "DB"]`.
  - **Departures** (`_combine_boards`): the BVG board as is, plus the DB rows whose line (spaces ignored: `S 7` = `S7`) isn't on it. Taking each line from one board avoids showing an S-Bahn train twice without matching trips.
    - **Applies to:** the entity's departures and the dialog's `board` command for that stop.
    - **While on the public fallback:** only the own board is fetched.
  - **When:** it runs at lookup time after the same-board merge. Unlike that one, it also works with departures set to 0.
  - **Search and nearby results (since 0.4.1):** the same linking applies to `search` results and to a fresh `nearby` lookup elsewhere, since it needs no departures.
    - **Linked stops:** results carry `linked` (`[{eva, network}]`), and the dialog sends it with `board`, so a searched DB + BVG station shows both networks.
    - **Exit stops** of one station (Dircksenstr., Gontardstr., …) still appear separately in search: telling them apart would take a board per result.
  - **Search ranking (since 0.4.1):** DB's station list matches loosely, and DBF lists DB before BVG. Live on 2026-10-14, "Alexanderplatz" started with "Neanderthal" and "Hauptbahnhof" with Düsseldorf.
    - **Order:** first the station itself (its normalised name equals the query, "Hbf" = "Hauptbahnhof"), then other names containing the query (e.g. its exits), then the rest; nearest first within each group. Distance is measured from the card's position, else the tracked person.
- **Stop search (since 0.3.0):** `POST <server>/_geolocation` with `q=<name>` and `hafas=<service>`, once per HAFAS source like the coordinate lookup. This needs the `station-name-search.pl` patch of `Drakknar/db-fakedisplay-patched`. Upstream answers `{"error": "Invalid lon/lat received"}`, which the coordinator turns into a "use the patched image" message. Upstream's own name search only renders HTML, and its JSON mode returns just an error for ambiguous names.
- **Your server down (since 0.3.0):** all requests go through `_request_json`.
  - **Fast fail:** a connection error, a timeout (5 s to connect, 20 s in total) or a gateway status (502/503/504) marks the server down. Any other HTTP error comes from DBF itself and doesn't count.
  - **Retry:** while down, the server is skipped for `PRIMARY_RETRY_AFTER` (60 s), then the next request probes it. An outage therefore costs one round of at most 4 timeouts, not one per stop.
  - **Logging:** one warning when the server goes down, one info line when it's back. On recovery the board cache is cleared.
  - **What you see:** stops stay on the map, with `departures_error` set. A Repairs issue `server_unreachable_<entry_id>` appears after 5 minutes and is deleted on recovery.
  - **At startup:** if there is no stop yet, `UpdateFailed` makes Home Assistant retry setup. WebSocket commands then answer `not_ready` with the entry's reason.
  - **Options while down:** they can be saved if the server URL is unchanged, so you can turn the fallback on during an outage.
- **Public fallback (option `fallback`, default off; `fallback_url`, default `https://dbf.finalrewind.org`):** only used while your server is marked down.
  - **What the public instance is:** stock DBF, so HAFAS stops come without coordinates and there's no name search. It also asks for at most 10 requests per minute; checked 2026-10-14, its board JSON matches.
  - **Budget:** `_take_fallback_slot` caps requests at 10 per rolling minute. Past that, `ServerUnreachable` is raised with a "try again shortly" message.
  - **Coordinator updates:** only the `FALLBACK_STOPS` (3) nearest stops update. The others get an "unreachable" board saying they are paused.
  - **Stop lookups:** they use the fallback only when the map has no stops. Such a lookup isn't remembered as one, so a full lookup follows once your server is back. No merging, and no `missing_coordinates` issue for the public instance.
  - **Search:** never uses the fallback.
  - **Marking:** boards from the fallback carry `fallback: true`, which becomes an entity attribute and is part of the WebSocket answer. Their links point to the fallback.
- **WebSocket commands (`websocket_api.py`):**
  - **`search`** `{query}` and **`nearby`** `{latitude?, longitude?}` return `stops` with name, eva, network, coordinates, `distance_km`, `entity_picture`, `url`, `merged_stops` and `networks`.
  - **`nearby` without a position,** or within 100 m of the tracked entity, returns the coordinator's current (merged) stops. Elsewhere it does a fresh, unmerged lookup.
  - **`board`** `{eva, network, linked?}` returns up to 80 departures (150 rows asked from DBF), cached for 20 s per stop (not while unreachable). It also returns `fallback` and `server_unreachable`. `network` must be a configured source.
  - **Errors:** `server_unreachable`, `dbf_error`, `not_ready`, `not_found`, `invalid_network`.
  - **Which entry:** each command takes an optional `entry_id`. By default it uses the entry whose tracked person belongs to the logged-in user, else the first loaded entry.
- **Entities:** `geo_location` entities, state = distance in km.
  - **No unique_id on purpose,** so stops passed while travelling never pile up in the entity registry. They also don't appear in `core.restore_state`.
  - **Recorder (since 0.2.3):** `_unrecorded_attributes` keeps `departures`, `next_departure`, `departures_via`, `departures_error`, `distance_km`, `url`, (since 0.3.0) `merged_stops` and `fallback`, and (since 0.4.0) `networks` out of history. Before that, every refresh wrote a new attributes row per stop.
- **Core split (since 0.5.0):** the owner wants one core for everything shown from DBF, and only what each feature needs on top. The map (nearby stops) and the dashboard hub (picked connections, search) stay separate features.
  - **Python:** `dbf.py` and `stations.py` are the core; `coordinator.py` and `geo_location.py` are the map feature.
  - **Frontend:** `www/dbf-core.js` is the core for the search card, and for the connection card planned next. The old `spdbf-departures.js` became `dbf-core.js`.
  - **Optional map:** option `map_stops` (default on). Off means no stop entities and no polling (`update_interval` None). `nearby` then does a lookup around the card's position or the tracked person.
- **Line names (`stations.tidy_line`, `dbfCore.tidyLine`, since 0.5.0):**
  - **What DB sends:** IRIS writes `S S41`, `S 5`, `OE RB10`, `STR M10`, or `ICE` with the number apart. BVG writes `S41`, `RB10`, `M10`.
  - **Rules:** a second word that already contains letters and digits is taken as is. `S/U/RE/RB/IRE/MEX/RS` + number are joined. Long-distance trains keep the space (`ICE 1107`).
  - **Bug fixed:** 0.4.x compared raw names, so the DB + BVG merge kept DB's `S S3` next to BVG's `S3`, and those lines got no colour. Checked live 2026-10-14: the merged Alexanderplatz board now has no S-Bahn train twice.
- **Ride times (since 0.6.0, `DbfClient.async_ride`):** they come from HAFAS (BVG) trips, because DB's are useless for this.
  - **DB data, checked live 2026-10-14 at Savignyplatz:** for S-Bahn trains, `route_*_diff` is either names only, or another train's times (trains near Mannheim and Cologne).
  - **Finding the start stop:** a DB start station is matched to its BVG rail stop via search + `link_networks`.
  - **Trains tried:** the next trains from there on the given lines (at most 6, those with a `tripId`), until one calls at the end station (eva, or name via `station_names`). The result is minutes between scheduled departure and arrival.
  - **Without `tripId`** (unpatched DBF) there is no ride. The card then shows only the walk.
  - **DB trip view** counts times only if the neighbouring stops match IRIS's and the timed stops really carry times.
- **Trips (since 0.5.0, `spdbf_nearby/trip`, DBF only; Transitous is dropped by the owner's choice):** each departure carries `trip`.
  - **BVG/HAFAS:** `trip` is `{id, hafas}`, and the trip is `GET /z/<id>.json?hafas=<service>` (`journey.route`). The id needs the DBF patch `hafas-trip-id.pl`; without it, BVG departures have no `trip`.
  - **DB/IRIS:** `trip` is `{train: "<type> <number>", station: eva, scheduled}`, and the trip is `GET /z/<train>/<eva>.json` (`departure`).
    - **Stop names** (`route_pre`/`route_post`) come from IRIS and are this train's.
    - **Times** (`route_*_diff`) come from a lookup by train number, which can return another train: an S7 at Westkreuz once got the times of a train near Stuttgart. They are used only when their stop names equal IRIS's; otherwise `times: false` and the dialog shows names only.
  - **Result:** `{line, direction, stops: [{name, eva, arrival, departure, *_delay, platform, cancelled, current?}], times, messages, fallback}`. HAFAS times are ISO with offset, IRIS times are naive local ISO, and the IRIS station itself is `HH:MM`. The dialog shows `HH:MM` from either.
  - **Checked live 2026-10-14:** an S42 from Westkreuz returned its ring stops with times.
- **Departure dialog (`www/dbf-core.js`):**
  - It listens for `hass-more-info` in the capture phase on `window`, which runs before Home Assistant's own handler on `<home-assistant>`. For a stop entity it stops the event and opens its own dialog; every other entity keeps the normal dialog.
  - The "Entity details" button re-fires `hass-more-info` with `spdbfDetails: true`, which the listener lets through.
  - **Full board (since 0.3.0):** on open the dialog calls `spdbf_nearby/board` and reloads it every 30 s. Until the first answer it shows the entity's own departures, if it has any.
  - **Views:** "By line" (default) groups by line and destination, with the next 3 departures each, ordered S-Bahn, U-Bahn, then the rest. DBF's JSON has no product type, so trams and buses can't be told apart. "By time" is the old flat list. The choice is kept in `localStorage` (`spdbf-departures-view`).
  - **Line colours:** for BVG and VBB stops, S- and U-Bahn chips use BVG's line colours; everything else uses the theme's primary colour.
  - **`dbfCore.showStop(stop)`** (`window.spdbfShowStop` until 0.6.0) opens the dialog for a stop. If that stop is also a map entity (same eva and network), it opens as the entity. `dbfCore.showTrip(stop, dep)` opens it on a departure's trip.
  - **Trip view (since 0.5.0):** tapping a departure (a row in either view; in "By line", the next departure of that line) opens its trip.
    - **Shown:** the trip's stops with times and delays, and platforms. Stops before the station are dimmed; the station itself is bold, matched by eva, the `current` flag or name.
    - **Also:** HIM messages (e.g. lifts out of order), and a note when DBF has only stop names.
    - **Back:** "← Departures" returns to the board.
- **Connection card (`www/spdbf-connection-card.js`, since 0.6.0):** `custom:spdbf-connection-card` shows one stop's departures from a db_infoscreen sensor. It replaces the owner's fork `Drakknar/ha-dbf-departures-card` on the Public transport dashboard. db_infoscreen stays, by the owner's choice.
  - **Options:** `entity`, `title`, `icon`, `departures`, `platforms` (exact, or the start before a space: "2" matches BVG's "2 (U2)"), `lines`, `destinations` (case-insensitive part of the name). `departureFilter(config)` is shared by the card and its editor, `walk_minutes`.
  - **Link row:** "↓ 15 min ride · 4 min walk" above the card; `show_ride` and `show_walk` turn each part off. It replaces the dashboard's hand-written "↓ N Mins" markdown.
    - **Ride (`ride_from`, the previous card's sensor):** `spdbf_nearby/ride` `{from: {eva, name, network}, to: {eva, name}, lines}` is called with the distinct tidy lines of that sensor's departures. Owner's rule: leaving at an S-Bahn station you arrive at an S-Bahn station, bus to bus, so the ride ends where that train arrives. At Alexanderplatz that's the S-Bahn, then you change to the U2.
    - **Walk (`walk_minutes`, typed in):** the change from there to this card's platform.
    - **Refresh:** the ride is cached in the card and the backend for 10 minutes.
  - **Everything via `window.dbfCore`:** line names (`tidyLine`), Berlin S-/U-Bahn colours (`lineStyle(line, "BVG")`), a tap on a departure (`showTrip`) and a tap on the title (`showStop`).
    - **Berlin colours in the dialog:** the card passes `colors: "berlin"`, so the dialog uses them for DB stations too.
  - **db_infoscreen rows:**
    - **DB (IRIS)** rows have `scheduledDeparture` ("HH:MM"), a route and a train number. Their trip is `{train: "<type> <number>", station: <station name>}`; DBF's `/z/` accepts station names.
    - **HAFAS** rows have `scheduledTime`, and `trip_id` (null until DBF sends `tripId`). Without a trip id, a tap opens the board.
    - **Network:** HAFAS rows are taken as BVG, since db_infoscreen doesn't expose its data source.
    - **Minutes** come from `departure_timestamp`, which is real-time.
  - **Editor (since 0.7.0):** `getConfigElement()` returns `spdbf-connection-card-editor`, which wraps HA's own `ha-form` (so it looks native), because the suggestions depend on the chosen sensor and a fixed `getConfigForm` schema can't do that.
    - **Suggestions:** `select` selectors with `multiple` and `custom_value`, built from the sensor's current departures: platforms (sorted, labelled "line → destinations"), lines on the chosen platforms, destinations of the chosen lines. Chosen values not seen now stay listed as "(not seen now)".
    - **Checks (`ha-alert`):** how many current departures the filters match, on which lines; a warning per filter value that matches nothing at its step (platform, then line, then destination).
    - **Why (owner, 2026-10-14):** a destination filter (Ruhleben, Theodor-Heuss-Platz) emptied a journey card when the U2 turned back at Zoo. The platform is the direction. Compass directions and a "goes via" filter were rejected: BVG rows from DBF have no route.
    - Empty filters are removed from the config; single values (`lines: U2`) become lists for the form.
    - **`ha-form` loading (0.7.1):** HA loads `ha-form` only when an editor needs it, so `getConfigElement` is async and first loads the tile card's editor through `loadCardHelpers()`, the usual custom-card trick.
  - **Checked 2026-10-14** in a Node fake-DOM test on real sensor states (Schönhauser Allee U2, Westkreuz all platforms, Alexanderplatz platform 4).
- **Stop search card (`www/spdbf-stop-search-card.js`, since 0.3.0):** `custom:spdbf-stop-search-card`. It is loaded with `add_extra_js_url` like the dialog, so no Lovelace resource is needed.
  - **Card picker:** listed through `window.customCards`, with `preview: true` and a `documentationURL`.
  - **Visual editor (since 0.3.1):** `static getConfigForm()` returns an `ha-form` schema, which HA's card editor renders natively; no editor element of our own. Checked 2026-10-14 in frontend 20260930.3: the card editor resolves the custom element class and calls its `getConfigForm`, as for built-in cards.
  - **Form fields:** `placeholder` (text), `use_device_location` (boolean), `entry_id` (a `config_entry` selector filtered to `spdbf_nearby`). `getStubConfig` sets `use_device_location: true`, since a boolean field without a value shows as off.
  - **Searching:** 2+ characters search after 300 ms. With an empty field, focusing it lists nearby stops; that list is reused for a minute.
  - **Location:** the browser's location (needs HTTPS and permission, 5 s timeout), else the tracked entity.
  - **Options:** `placeholder`, `use_device_location`, `entry_id`.
  - **Bump `FRONTEND_VERSION` in `const.py` whenever anything in `www/` changes.** It is the cache-busting query string, and without it browsers and the companion app keep the old file.

## Findings

- **HA 2026.10 map (checked 2026-10-10 against frontend `dev`):** `ha-entity-marker` fires `hass-more-info` on click (`fireEvent`, bubbles and composed), so the window capture listener catches marker taps. The map now has a `MapEngine` abstraction with MapLibre and Leaflet engines, kept in the private `ha-map._engine`. The engine API has `getView()`, `addMarker()` and a `moveStart` event, but no public "move ended" event.
- **HA 2026.10.1 (checked 2026-10-12, frontend 20260930.3):** nothing to change.
  - **Validation:** core replaced voluptuous with probatio. `import voluptuous as vol` still works, because `homeassistant/__init__.py` aliases it to probatio's shim. The setup and options forms serialize and validate as before.
  - **Map:** markers still fire `hass-more-info` on click, so the window capture listener runs first. The map card's new overview selects entities instead of opening more-info, but only for `person`, `device_tracker` and `zone`, not `geo_location`. `geo_location_sources` still filters on `attributes.source`.
  - **Runtime:** a scratch pytest (`pytest-homeassistant-custom-component` for 2026.10.1, `aioclient_mock` as a fake DBF) passed. It covered the setup flow, the `already_configured` abort, form errors, entities with departures, the options reload, unload and the Repairs issue, with no deprecation reports.
- **Transitous** (`api.transitous.org/api/v1/map/stops?min=lat,lon&max=lat,lon`) returns all stops in a map area, one entry per platform or bus bay. Not used here; noted for the idea below.

## Testing

There are **no automated tests** yet. What has been done for changes so far:

1. `python -m py_compile custom_components/spdbf_nearby/*.py`
2. Check the DBF server directly:
   ```bash
   curl -X POST -d 'lat=52.5251&lon=13.3694&hafas=BVG' <server>/_geolocation | jq '.candidates[:3]'
   curl '<server>/900100003?mode=json&hafas=BVG' | jq '.departures[:3]'
   ```
3. Install into Home Assistant (HACS, or copy this folder into `config/custom_components/`), restart, then check the Map panel: markers appear, tapping one opens the departure dialog, and "Entity details" opens the normal dialog.
4. After changing `www/`, reload the browser or reset the companion app's frontend cache.

**0.3.0 scratch pytest (2026-10-14, HA 2026.10.1, on the Windows PC):**
- **What it covered** (`aioclient_mock` as fake DBF, 8 tests, all passed):
  - **Merging:** three identical Alexanderplatz boards merge into "S+U Alexanderplatz Bhf"; the tram stop stays; turning the option off keeps all four.
  - **WebSocket commands:** search, the unpatched-server message, nearby (near and far), board (80 rows, 20 s cache), `invalid_network`.
  - **Options flow** and **static scripts**.
  - **Outages:** setup retry while DBF is down; outage with fast fail and no requests within the retry window; the Repairs issue after 5 minutes; recovery.
  - **Fallback:** stock-DBF stops, no coordinate issue, no search, the 10-requests-per-minute cap.
- **Windows workarounds** (the harness assumes Linux):
  - **Stub modules** `fcntl.py` and `resource.py` in the venv's site-packages.
  - **`conftest.py` in the scratch tests folder:**
    - **Socket blocking:** replace `pytest_socket.disable_socket` with a no-op, because the Proactor loop needs a socket pair.
    - **Shadowing:** insert the scratch `custom_components` folder first in `custom_components.__path__`, because the harness's `testing_config` shadows it.
  - **`pip install home-assistant-frontend==<pinned>`**, because the `frontend` dependency needs it.
  - **Teardown errors:** the hass fixture's teardown raises `WinError 10022` in tests that use the HTTP/WebSocket client. That's an environment issue; the tests themselves pass.

**0.3.0 frontend checks (2026-10-14):** both scripts were run under Node (VS Code's `Code.exe` with `ELECTRON_RUN_AS_NODE=1`) against a hand-made fake DOM and `hass`.
- **Dialog:**
  - **By line:** order S7, U2 Pankow, U2 Ruhleben, M4; 3 times per line; U2 colour; cancelled times struck through.
  - **Map entity:** "Includes …", provisional rows and "Entity details".
  - **Unreachable** board message, and a searched stop opening as its entity.
- **Card:** nearby on focus, debounced search, Enter opens the first stop, errors are shown.
  - **Stale answers:** a search still on its way is dropped when the field is cleared and the cached nearby list shows. That bug was found and fixed this way.

## Open items

- **0.3.0 (2026-10-14), installed but not yet tested in Home Assistant:** stop search card, full-board dialog, duplicate merging, outage handling and the public fallback. The DBF image with `station-name-search.pl` is deployed, and its name search answered on the author's server.
  - **Check:** the card's nearby list and search, the dialog's grouping at Alexanderplatz, and merged markers on the map.
  - **Companion app:** whether the browser location works in the companion app's WebView. If it doesn't, the card falls back to the tracked person.

- **Mobile tap shows entity details (reported 2026-10-10, unresolved):** in the Android companion app, tapping a stop on the map opened Home Assistant's entity dialog instead of the departures. The installed files matched the repository. Most likely cause: the app's cached frontend never loaded the departure dialog script (now `dbf-core.js`). First step: reset the app's frontend cache and retry. If it persists, check in the app's WebView whether `window.dbfCore` is set and the console shows "spdbf Nearby Stops: frontend core active".
- **Idea, discussed and not wanted for now:** loading stops for the area the map is showing, instead of around the person. That would mean hooking into `ha-map`'s private engine, which can break with any HA update. It would use temporary markers rather than entities, so one viewer's panning doesn't move everyone's stops.
- **Possible improvements:** pytest tests with a fake DBF server (as in `ha-audiocpp`), diagnostics, translations other than English.

## Releases and HACS

The author's Home Assistant installs this integration through HACS (custom repository `Drakknar/ha-spdbf-nearby`).

- **Releases (since 0.3.0):** each version is a GitHub release tagged `vX.Y.Z`, matching `version` in `manifest.json`. HACS then shows the version and release notes, and offers only releases as updates.
  - **Before 0.3.0** there were no releases. HACS tracked `main` and showed commit hashes; installed was `5cc072e`.
- **To release:**
  1. Bump `version` in `manifest.json`, and `FRONTEND_VERSION` in `const.py` if `www/` changed.
  2. Merge to `main`.
  3. `.github/workflows/release.yml` (since 2026-10-14) publishes `vX.Y.Z` on its own. It runs when `manifest.json` changes on `main`, skips a version that already has a release, and writes notes from the merged pull requests. Edit the notes on GitHub if they need more.
- **Compatibility fixes:** the review prompt tells Claude to bump the patch version whenever it changes integration code. Merging its pull request therefore publishes the fix.
- **Doc-only changes** don't need a release. HACS won't offer them, and nothing changes in Home Assistant.
- **When HACS updates:** it refreshes repository data only every few hours. "Update information" on the repository in HACS checks at once.
- **Copying by hand:** the installed `www/` had an old `.js.gz` next to the dialog script (found 2026-10-14, presumably from HACS). When copying the folder by hand, delete any such `.gz`, or the old script may be served.

## Upstream watch

`.github/workflows/compat-review.yml` runs daily (briefly weekly on 2026-10-14, then daily again at the owner's request). For each new version of a dependency, Claude reviews this integration once, following `.github/prompts/ha-compat.md`.
- **Home Assistant** reviews start at the first beta of each minor release. Each change to DBF's server code (`lib/` in derf/db-fakedisplay) is reviewed too.
- **Results:** a fix comes as a pull request; anything that needs the owner, as an issue labelled `upstream-watch` and assigned to the owner; new features worth using, as an `enhancement` issue.
- **Access:** Claude works with read-only access (secret `CLAUDE_CODE_OAUTH_TOKEN`). A separate job opens the pull request and issues.
- **Isolation (tested 2026-10-11):**
  - **Before Claude runs:** the review job turns on the action's subprocess secret scrub and installs bubblewrap, so Claude's commands don't see the Claude token or any other process. It also blocks reading other processes' memory and removes sudo. Docker stays on because Claude needs it here.
  - **After Claude:** the steps that follow ignore variables and PATH entries its commands could plant, and make the diff in a fresh clone.
  - **Publishing:** the report job won't publish output that contains the token, or a piece of it, and deletes the review's artifact.
  - **What's left:** Claude's commands never see the token, so leaking it would take a way around the sandbox, and Docker, which this job needs, amounts to root and is one. The scan is a second line of defence.
- **Bookkeeping:** `.github/scripts/watch.sh` records what was reviewed on the `watch-state` branch. Run the workflow by hand with `force` to review again.

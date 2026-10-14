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
                        (add_extra_js_url), registers the WebSocket commands, sets up the
                        coordinator and the geo_location platform
    coordinator.py      SpdbfCoordinator: follows the tracked entity, looks up stops, fetches departures,
                        merges duplicate stops; also answers search, nearby and full-board requests
    websocket_api.py    spdbf_nearby/search, /nearby and /board for the card and the dialog
    geo_location.py     One SpdbfStop entity per stop; added and removed as the stop list changes
    config_flow.py      Setup and options flow
    const.py            Settings, defaults, FRONTEND_VERSION, thresholds
    www/
      spdbf-departures.js       Departure dialog that replaces more-info for stop entities;
                                window.spdbfShowStop(stop) opens it for any stop
      spdbf-stop-search-card.js custom:spdbf-stop-search-card, the stop search card
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
  - **`search`** `{query}` and **`nearby`** `{latitude?, longitude?}` return `stops` with name, eva, network, coordinates, `distance_km`, `entity_picture`, `url` and `merged_stops`.
  - **`nearby` without a position,** or within 100 m of the tracked entity, returns the coordinator's current (merged) stops. Elsewhere it does a fresh, unmerged lookup.
  - **`board`** `{eva, network}` returns up to 80 departures (150 rows asked from DBF), cached for 20 s per stop (not while unreachable). It also returns `fallback` and `server_unreachable`. `network` must be a configured source.
  - **Errors:** `server_unreachable`, `dbf_error`, `not_ready`, `not_found`, `invalid_network`.
  - **Which entry:** each command takes an optional `entry_id`. By default it uses the entry whose tracked person belongs to the logged-in user, else the first loaded entry.
- **Entities:** `geo_location` entities, state = distance in km.
  - **No unique_id on purpose,** so stops passed while travelling never pile up in the entity registry. They also don't appear in `core.restore_state`.
  - **Recorder (since 0.2.3):** `_unrecorded_attributes` keeps `departures`, `next_departure`, `departures_via`, `departures_error`, `distance_km`, `url` and (since 0.3.0) `merged_stops` out of history. Before that, every refresh wrote a new attributes row per stop.
- **Departure dialog (`www/spdbf-departures.js`):**
  - It listens for `hass-more-info` in the capture phase on `window`, which runs before Home Assistant's own handler on `<home-assistant>`. For a stop entity it stops the event and opens its own dialog; every other entity keeps the normal dialog.
  - The "Entity details" button re-fires `hass-more-info` with `spdbfDetails: true`, which the listener lets through.
  - **Full board (since 0.3.0):** on open the dialog calls `spdbf_nearby/board` and reloads it every 30 s. Until the first answer it shows the entity's own departures, if it has any.
  - **Views:** "By line" (default) groups by line and destination, with the next 3 departures each, ordered S-Bahn, U-Bahn, then the rest. DBF's JSON has no product type, so trams and buses can't be told apart. "By time" is the old flat list. The choice is kept in `localStorage` (`spdbf-departures-view`).
  - **Line colours:** for BVG and VBB stops, S- and U-Bahn chips use BVG's line colours; everything else uses the theme's primary colour.
  - **`window.spdbfShowStop(stop)`** opens the dialog for a stop from the card. If that stop is also a map entity (same eva and network), it opens as the entity.
- **Stop search card (`www/spdbf-stop-search-card.js`, since 0.3.0):** `custom:spdbf-stop-search-card`, also listed in the card picker. It is loaded with `add_extra_js_url` like the dialog, so no Lovelace resource is needed.
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

- **0.3.0 (2026-10-14), not yet tested in Home Assistant:** stop search card, full-board dialog, duplicate merging, outage handling and the public fallback. Needs the DBF image with `station-name-search.pl`.
  - **Check:** the card's nearby list and search, the dialog's grouping at Alexanderplatz, and merged markers on the map.
  - **Companion app:** whether the browser location works in the companion app's WebView. If it doesn't, the card falls back to the tracked person.

- **Install 0.2.3 (2026-10-10):** the recorder fix is pushed but not yet installed. The integration was copied into `custom_components` by hand rather than through HACS; switching to HACS is planned.
- **Mobile tap shows entity details (reported 2026-10-10, unresolved):** in the Android companion app, tapping a stop on the map opened Home Assistant's entity dialog instead of the departures. The installed files matched the repository. Most likely cause: the app's cached frontend never loaded `spdbf-departures.js`. First step: reset the app's frontend cache and retry. If it persists, check in the app's WebView whether `window.__spdbfDepartures` is set and the console shows "spdbf Nearby Stops: departure list active".
- **Idea, discussed and not wanted for now:** loading stops for the area the map is showing, instead of around the person. That would mean hooking into `ha-map`'s private engine, which can break with any HA update. It would use temporary markers rather than entities, so one viewer's panning doesn't move everyone's stops.
- **Possible improvements:** pytest tests with a fake DBF server (as in `ha-audiocpp`), diagnostics, translations other than English.

## Upstream watch

`.github/workflows/compat-review.yml` runs daily. For each new version of a dependency, Claude reviews this integration once, following `.github/prompts/ha-compat.md`.
- **Home Assistant** reviews start at the first beta of each minor release. Each change to DBF's server code (`lib/` in derf/db-fakedisplay) is reviewed too.
- **Results:** a fix comes as a pull request; anything that needs the owner, as an issue labelled `upstream-watch` and assigned to the owner; new features worth using, as an `enhancement` issue.
- **Access:** Claude works with read-only access (secret `CLAUDE_CODE_OAUTH_TOKEN`). A separate job opens the pull request and issues.
- **Isolation (tested 2026-10-11):**
  - **Before Claude runs:** the review job turns on the action's subprocess secret scrub and installs bubblewrap, so Claude's commands don't see the Claude token or any other process. It also blocks reading other processes' memory and removes sudo. Docker stays on because Claude needs it here.
  - **After Claude:** the steps that follow ignore variables and PATH entries its commands could plant, and make the diff in a fresh clone.
  - **Publishing:** the report job won't publish output that contains the token, or a piece of it, and deletes the review's artifact.
  - **What's left:** Claude's commands never see the token, so leaking it would take a way around the sandbox, and Docker, which this job needs, amounts to root and is one. The scan is a second line of defence.
- **Bookkeeping:** `.github/scripts/watch.sh` records what was reviewed on the `watch-state` branch. Run the workflow by hand with `force` to review again.

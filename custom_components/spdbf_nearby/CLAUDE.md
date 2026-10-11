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
    __init__.py         Serves www/ as static files, injects spdbf-departures.js on every page
                        (add_extra_js_url), sets up the coordinator and the geo_location platform
    coordinator.py      SpdbfCoordinator: follows the tracked entity, looks up stops, fetches departures
    geo_location.py     One SpdbfStop entity per stop; added and removed as the stop list changes
    config_flow.py      Setup and options flow
    const.py            Settings, defaults, FRONTEND_VERSION, thresholds
    www/
      spdbf-departures.js       Departure dialog that replaces more-info for stop entities
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
  - **DB stations:** IRIS first. If IRIS is empty or fails, the integration retries through DBF's ÖBB HAFAS backend, which accepts DB station numbers. Example: Berlin Zoologischer Garten's IRIS board is empty. `departures_via` records when the fallback was used.
  - IRIS boards include arrival-only rows, hence the `3 ×` limit. IRIS times are German local time (`Europe/Berlin`).
- **Entities:** `geo_location` entities, state = distance in km.
  - **No unique_id on purpose,** so stops passed while travelling never pile up in the entity registry. They also don't appear in `core.restore_state`.
  - **Recorder (since 0.2.3):** `_unrecorded_attributes` keeps `departures`, `next_departure`, `departures_via`, `departures_error`, `distance_km` and `url` out of history. Before that, every refresh wrote a new attributes row per stop.
- **Departure dialog (`www/spdbf-departures.js`):**
  - It listens for `hass-more-info` in the capture phase on `window`, which runs before Home Assistant's own handler on `<home-assistant>`. For a stop entity it stops the event and opens its own dialog; every other entity keeps the normal dialog.
  - The "Entity details" button re-fires `hass-more-info` with `spdbfDetails: true`, which the listener lets through.
  - **Bump `FRONTEND_VERSION` in `const.py` whenever anything in `www/` changes.** It is the cache-busting query string, and without it browsers and the companion app keep the old file.

## Findings

- **HA 2026.10 map (checked 2026-10-10 against frontend `dev`):** `ha-entity-marker` fires `hass-more-info` on click (`fireEvent`, bubbles and composed), so the window capture listener catches marker taps. The map now has a `MapEngine` abstraction with MapLibre and Leaflet engines, kept in the private `ha-map._engine`. The engine API has `getView()`, `addMarker()` and a `moveStart` event, but no public "move ended" event.
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

## Open items

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

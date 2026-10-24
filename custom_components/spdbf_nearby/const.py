"""Constants for spdbf Public Transport."""

from datetime import timedelta
import json
from pathlib import Path

DOMAIN = "spdbf_nearby"

CONF_SERVER_URL = "server_url"
CONF_PUBLIC_URL = "public_url"
CONF_TRACKED_ENTITY = "tracked_entity"
CONF_SOURCES = "sources"
CONF_RADIUS = "radius"
CONF_MAX_STOPS = "max_stops"
CONF_DEPARTURES = "departures"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MERGE_DUPLICATES = "merge_duplicates"
CONF_MERGE_NETWORKS = "merge_networks"
CONF_MAP_STOPS = "map_stops"
CONF_FALLBACK = "fallback"
CONF_PROBLEM_AFTER = "problem_after"

DEFAULT_RADIUS = 1.0  # km
DEFAULT_MAX_STOPS = 12
DEFAULT_DEPARTURES = 5
DEFAULT_SCAN_INTERVAL = 60  # seconds
DEFAULT_MERGE_DUPLICATES = True
DEFAULT_MERGE_NETWORKS = True
DEFAULT_MAP_STOPS = True
DEFAULT_FALLBACK = True
DEFAULT_PROBLEM_AFTER = 10  # minutes

# "DB" means the IRIS station list built into DBF (DB InfraGO network).
# Everything else is a HAFAS service name as accepted by DBF's ?hafas= parameter.
SOURCE_DB = "DB"
# Used for DB stations whose IRIS board is empty; it accepts DB station numbers.
DB_FALLBACK_HAFAS = "ÖBB"
DEFAULT_SOURCES = [SOURCE_DB, "BVG"]
HAFAS_SOURCES = [
    "BVG", "VBB", "ÖBB", "AVV", "BART", "BLS", "CFL", "CMTA", "DSB", "IE",
    "KVB", "mobiliteit", "NAHSH", "NASA", "NVV", "PKP", "Resrobot", "RMV",
    "RSAG", "SaarVV", "STV", "TPG", "VBN", "VMT", "VOS", "ZVV",
]
# Served from www/. FRONTEND_VERSION is the cache-busting query string: the
# release version, so every release makes browsers and the companion app fetch
# the new files (they cache them for 31 days).
STATIC_URL = f"/{DOMAIN}"
FRONTEND_VERSION: str = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)["version"]
# Map marker pictures. Names starting like this are S-/U-Bahn stations.
RAIL_NAME_PREFIXES = ("S ", "U ", "S+U ")

# DBF prints German local times for IRIS boards.
IRIS_TIME_ZONE = "Europe/Berlin"

# Re-query nearby stops only after moving this far, or when the list is this old.
MIN_MOVE_METERS = 100
STOP_LOOKUP_MAX_AGE = timedelta(minutes=15)
# Ignore location fixes worse than this.
MAX_GPS_ACCURACY_METERS = 250

# Stops whose next departures are identical show the same board (BVG lists
# a station several times, e.g. "S+U Alexanderplatz Bhf" and ".../Dircksenstr.").
# A lookup checks this many candidates per kept stop, and needs this many
# identical departures to merge two stops.
MERGE_CANDIDATE_FACTOR = 2
MERGE_MIN_DEPARTURES = 2
# A DB station and a HAFAS (BVG) rail stop of the same name, at most this far
# apart, are one station: "Berlin Savignyplatz" and "S Savignyplatz (Berlin)".
MERGE_NETWORK_MAX_METERS = 300

# Full departure board, as shown in the departure dialog.
BOARD_ROWS = 150  # rows asked from DBF; IRIS boards include arrival-only rows
BOARD_DEPARTURES = 80
BOARD_CACHE_TTL = timedelta(seconds=20)
SEARCH_MAX_RESULTS = 20
# Ride times between two stations: trips tried per lookup, and how long a
# result is kept (scheduled ride times hardly change).
RIDE_MAX_TRIPS = 6
RIDE_CACHE_TTL = timedelta(minutes=10)
# Assist (llm.py, assist.py): answers short enough for a local model.
ASSIST_MAX_ROWS = 30  # departures in one answer
ASSIST_MAX_WINDOW = 180  # minutes ahead; a board holds BOARD_DEPARTURES rows anyway
ASSIST_MAX_TRIPS = 8  # trip lookups per answer (towards, notices)
ASSIST_MAX_OPTIONS = 3  # ways through a connection
ASSIST_MAX_NOTICE = 300  # characters of one disruption notice

REQUEST_TIMEOUT = 20  # seconds
CONNECT_TIMEOUT = 5  # seconds; a server that is off fails fast
MAX_PARALLEL_REQUESTS = 4  # DBF's default container runs a few workers

# After your DBF server fails to answer, skip it for this long instead of
# waiting for a timeout on every stop.
PRIMARY_RETRY_AFTER = timedelta(seconds=60)

# DBF status: one live board per network (past any cache in front of DBF)
# every STATUS_INTERVAL. "Down for real" (the problem sensor, a Repairs
# notice) once checks have failed for the problem_after option. Without map
# stops of a network, these stations are asked; other networks are skipped.
STATUS_INTERVAL = timedelta(seconds=60)
STATUS_PROBE_ROWS = 5
STATUS_STATIONS = {SOURCE_DB: "8011160", "BVG": "900003201"}  # Berlin Hbf
# Makes each check's URL unique, so a cache in front of DBF can't answer it.
STATUS_NOCACHE_PARAM = "spdbf_check"

# Transitous, the fallback while DBF can't deliver (option "fallback").
# Community-run: requests carry a User-Agent, boards are cached briefly and,
# on the map, only the nearest stops ask it; the others show last known data.
SOURCE_TRANSITOUS = "Transitous"
TRANSITOUS_URL = "https://api.transitous.org/api"
TRANSITOUS_RADIUS_METERS = 50  # the station's platforms, not nearby bus stops
TRANSITOUS_MATCH_METERS = 500  # a DBF stop matches a Transitous stop this close
TRANSITOUS_BOARD_CACHE_TTL = timedelta(seconds=60)
TRANSITOUS_MAX_ROWS = 100
FALLBACK_STOPS = 3
# The other map stops show last known boards; when those run out (map stops
# keep a few rows), each asks Transitous at most this often.
PAUSED_STOP_REFRESH = timedelta(minutes=5)
USER_AGENT = (
    f"spdbf-nearby-home-assistant/{FRONTEND_VERSION}"
    " (+https://github.com/Drakknar/ha-spdbf-nearby)"
)

# Last good answers, shown when neither DBF nor Transitous answers.
LAST_KNOWN_MAX_AGE = timedelta(hours=24)
LAST_KNOWN_LIMITS = {"boards": 200, "trips": 100, "searches": 50, "rides": 50}
LAST_KNOWN_SAVE_DELAY = 60  # seconds
# A map stop's few rows don't replace a dialog's full board younger than this.
LAST_KNOWN_BOARD_REFRESH = timedelta(minutes=5)

ISSUE_MISSING_COORDINATES = "missing_coordinates"
ISSUE_SERVER_UNREACHABLE = "server_unreachable"

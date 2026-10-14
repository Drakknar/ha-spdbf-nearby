"""Constants for spdbf Nearby Stops."""

from datetime import timedelta

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
CONF_FALLBACK = "fallback"
CONF_FALLBACK_URL = "fallback_url"

DEFAULT_RADIUS = 1.0  # km
DEFAULT_MAX_STOPS = 12
DEFAULT_DEPARTURES = 5
DEFAULT_SCAN_INTERVAL = 60  # seconds
DEFAULT_MERGE_DUPLICATES = True
DEFAULT_MERGE_NETWORKS = True
DEFAULT_FALLBACK = False
DEFAULT_FALLBACK_URL = "https://dbf.finalrewind.org"

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
# Served from www/. Bump FRONTEND_VERSION when anything in www/ changes, so
# browsers and the companion app fetch the new files.
STATIC_URL = f"/{DOMAIN}"
FRONTEND_VERSION = "0.4.1"
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

REQUEST_TIMEOUT = 20  # seconds
CONNECT_TIMEOUT = 5  # seconds; a server that is off fails fast
MAX_PARALLEL_REQUESTS = 4  # DBF's default container runs a few workers

# After your DBF server fails to answer, skip it for this long instead of
# waiting for a timeout on every stop; a Repairs notice appears once it has
# been unreachable for UNREACHABLE_ISSUE_AFTER.
PRIMARY_RETRY_AFTER = timedelta(seconds=60)
UNREACHABLE_ISSUE_AFTER = timedelta(minutes=5)

# Optional public fallback while your server is unreachable. The public
# instance runs stock DBF and asks for at most 10 requests per minute, so
# requests are capped, only the nearest stops keep updating, and nothing is
# looked up there that the map already has.
FALLBACK_MAX_REQUESTS = 10
FALLBACK_WINDOW = timedelta(minutes=1)
FALLBACK_STOPS = 3

ISSUE_MISSING_COORDINATES = "missing_coordinates"
ISSUE_SERVER_UNREACHABLE = "server_unreachable"

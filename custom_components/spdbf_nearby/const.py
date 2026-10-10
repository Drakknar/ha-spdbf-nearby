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

DEFAULT_RADIUS = 1.0  # km
DEFAULT_MAX_STOPS = 12
DEFAULT_DEPARTURES = 5
DEFAULT_SCAN_INTERVAL = 60  # seconds

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
FRONTEND_VERSION = "0.2.2"
# Map marker pictures. Names starting like this are S-/U-Bahn stations.
RAIL_NAME_PREFIXES = ("S ", "U ", "S+U ")

# DBF prints German local times for IRIS boards.
IRIS_TIME_ZONE = "Europe/Berlin"

# Re-query nearby stops only after moving this far, or when the list is this old.
MIN_MOVE_METERS = 100
STOP_LOOKUP_MAX_AGE = timedelta(minutes=15)
# Ignore location fixes worse than this.
MAX_GPS_ACCURACY_METERS = 250

REQUEST_TIMEOUT = 20  # seconds
MAX_PARALLEL_REQUESTS = 4  # DBF's default container runs a few workers

ISSUE_MISSING_COORDINATES = "missing_coordinates"

import os

# --- SQLite (config db: calendars, keyword_rules, tag_definitions) ---
SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/config.db")

# Used to resolve "which calendar day does this sleep session belong
# to" in the user's own local terms, not UTC's. Without this, a
# session starting shortly after local midnight (in any timezone ahead
# of UTC) can resolve to the WRONG day - UTC's date for that instant
# is still the previous day, even though it's already tomorrow
# wherever the person actually is.
TZ_NAME = os.getenv("TZ", "UTC")

# --- InfluxDB (shared with the ring parser stack) ---
INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "home")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "health")

# Measurement this service writes its own data into - deliberately
# separate from the ring parser's measurement (default "colmi") so the
# two never collide on field/tag names.
EVENTS_MEASUREMENT = os.getenv("EVENTS_MEASUREMENT", "events")
SLEEP_MEASUREMENT = os.getenv("SLEEP_MEASUREMENT", "subjective_sleep")

# Measurement the ring parser writes sensor data into - this service
# reads FROM here (read-only) to resolve /sleep POSTs against the most
# recent completed sleep session. Must match INFLUXDB_MEASUREMENT on the
# parser service.
SENSOR_MEASUREMENT = os.getenv("SENSOR_MEASUREMENT", "colmi")

# Same GADGETBRIDGE_USER value the parser tags points with, so sleep-
# session lookups filter to the right person - but now derived per-request
# from the logged-in session's username (see app/auth.py), not a fixed
# env var. Kept here as a documented convention/fallback only: if a
# request somehow reaches sensor-lookup code without a resolved username
# (shouldn't happen once auth is wired through everywhere), this is what
# it falls back to.
DEFAULT_GADGETBRIDGE_USER_FALLBACK = os.getenv("GADGETBRIDGE_USER", "primary")

# --- Background calendar sync ---
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "15"))

# Minimum sleep session duration (seconds) to be eligible for /sleep
# resolution - excludes naps per spec §6.
MIN_SLEEP_SESSION_SECONDS = int(os.getenv("MIN_SLEEP_SESSION_SECONDS", str(3 * 3600)))

# --- Activity session detection (Activity page) ---
# An "active minute" (steps > 0 OR raw_intensity >= this) is the
# trigger for both Stand-hour crediting and derived activity-session
# detection. Confirmed empirically against the watch's own real
# hourly Stand display (see parser/activefit/FIELD_RESEARCH.md) -
# not a guess, though the person's own words on it were "the best I
# can do from one day's data" (the watch only shows one day at a
# time), so this could still be refined with more comparison data
# later.
STAND_INTENSITY_THRESHOLD = int(os.getenv("STAND_INTENSITY_THRESHOLD", "50"))

# How much of a gap (in inactive minutes) is tolerated within one
# session before it's considered ended, and how short a session is
# short enough to discard as noise (a single stray step, a brief arm
# movement) rather than a real activity bout. Both are OUR OWN
# reasonable starting choices, not a replication of Gadgetbridge's own
# StepAnalysis algorithm (its exact source wasn't available to pull
# directly) - worth revisiting once there's enough real data to
# compare our own session list against Gadgetbridge's or the watch's.
SESSION_GAP_MINUTES = int(os.getenv("SESSION_GAP_MINUTES", "5"))
SESSION_MIN_DURATION_MINUTES = int(os.getenv("SESSION_MIN_DURATION_MINUTES", "3"))

# --- Server ---
PORT = int(os.getenv("PORT", "8080"))

# --- Auth ---
# First-run bootstrap only - creates one account if the users table is
# empty. No public signup endpoint exists; after bootstrap, further
# accounts are added by a logged-in user from the UI.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# Set to "Y" if this is served behind TLS (e.g. a reverse proxy doing
# HTTPS) so the session cookie gets the Secure flag. Left off by default
# since the common case here is plain HTTP over Tailscale.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "N") == "Y"
SESSION_MAX_AGE_DAYS = int(os.getenv("SESSION_MAX_AGE_DAYS", "30"))

# Starting tag_definitions seeded for every newly created user, matching
# the manual/one-tap taxonomy from spec §4.
DEFAULT_TAG_DEFINITIONS = [
    # (tag, label, category, is_duration, sort_order)
    ("caffeine", "Caffeine", "substance", False, 10),
    ("alcohol", "Alcohol", "substance", False, 20),
    ("social", "Social", "context", False, 30),
    ("recovery", "Recovery", "restful", False, 40),
    ("nature", "Nature", "restful", False, 50),
]
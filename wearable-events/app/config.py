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

# Upper sanity bound - added 2026-09 after a confirmed real bug: a
# corrupted sleep_session_duration_s value (~1092 hours, reported
# directly against real data) was being treated as a genuine
# completed session since it trivially cleared MIN_SLEEP_SESSION_SECONDS
# (no upper bound existed to catch it). No real sleep session is ever
# this long regardless of root cause - 24 hours is a deliberately
# generous ceiling (well past even an extreme outlier night), not a
# tight one, so this only ever filters out data that's obviously
# corrupted rather than risking hiding a genuine unusual night.
MAX_SLEEP_SESSION_SECONDS = int(os.getenv("MAX_SLEEP_SESSION_SECONDS", str(24 * 3600)))

# Sleep Duration detail page's range-bar goal marker (0 to goal, per
# UI_DESIGN_NOTES.md's "Page: Sleep Duration"). Zepp's own value there
# is a person-configurable in-app setting, not something synced to
# Gadgetbridge's database - no real field to read this from, so this
# is a standalone, independently-configurable default rather than an
# attempt to mirror Zepp's own number. 8 hours is a commonly-cited
# general sleep recommendation, not this app's own opinion on the
# "right" amount - override via env var for a personal target.
SLEEP_DURATION_GOAL_SECONDS = int(os.getenv("SLEEP_DURATION_GOAL_SECONDS", str(8 * 3600)))

# Age bracket for the Sleep Quality panel's published thresholds below -
# not auto-detected (no birthdate is tracked anywhere in this app),
# env-configurable per person. "adult" (26-64) is the broadest,
# most-generally-applicable bracket, used as the default.
SLEEP_QUALITY_AGE_BRACKET = os.getenv("SLEEP_QUALITY_AGE_BRACKET", "adult")

# Published "appropriate" thresholds for the 3 sleep-continuity metrics
# this app can actually compute (efficiency, WASO, awakenings>5min) -
# NOT a composite score, deliberately (see FIELD_RESEARCH.md's "Sleep
# Score" entry for why: the industry's own standards body, ANSI/CTA/
# NSF-2110, states no standardized composite-scoring formula exists,
# and recommends showing individual cited metrics over one opaque
# number). Source: Ohayon et al. 2017 (National Sleep Foundation Sleep
# Quality Consensus Panel, Sleep Health 3(1):6-19), Table 1 as
# reproduced in ANSI/CTA/NSF-2110 (June 2024). A 4th consensus metric,
# sleep latency, is deliberately excluded - not a threshold gap, a
# data gap: this app has no "got into bed" timestamp separate from
# sleep onset to measure latency from (see FIELD_RESEARCH.md's "time
# in bed" future-feature note).
#
# Only the "appropriate" bound is encoded for waso/awakenings - the
# source table gives an inappropriate bound too for some metrics, but
# not consistently across all three for every age bracket, and this
# file would rather state one number with real confidence than three
# with mixed confidence. efficiency_poor_pct is the one exception:
# <75% is independently corroborated across multiple citations of the
# same underlying panel (not just this one table), not just this
# table's own number, so it's included where the other "poor" bounds
# are not.
SLEEP_QUALITY_THRESHOLDS = {
    "young_adult": {"waso_appropriate_max_min": 20, "awakenings_appropriate_max": 1, "efficiency_appropriate_min_pct": 85, "efficiency_poor_max_pct": 75},
    "adult": {"waso_appropriate_max_min": 20, "awakenings_appropriate_max": 1, "efficiency_appropriate_min_pct": 85, "efficiency_poor_max_pct": 75},
    "older_adult": {"waso_appropriate_max_min": 30, "awakenings_appropriate_max": 2, "efficiency_appropriate_min_pct": 85, "efficiency_poor_max_pct": 75},
}

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
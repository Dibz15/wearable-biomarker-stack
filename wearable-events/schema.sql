-- config.db schema
-- Relational config data only (users, calendars, rules, tag button
-- definitions, sessions). Time-series sensor/event/sleep data lives in
-- InfluxDB, not here.
--
-- Multi-user: calendars, keyword_rules, tag_definitions, and
-- calendar_events_cache are all scoped by user_id. Each household
-- member gets their own calendars/rules/tags after logging in - nothing
-- is shared across users by default.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Should match the GADGETBRIDGE_USER value used for this person's
    -- ring-parser instance, so their calendar/sleep-score data and their
    -- HRV/HR/temperature data share the same `user` tag in InfluxDB and
    -- can be correlated later. Not enforced (this service has no
    -- knowledge of the parser's config) - just a convention worth
    -- following when creating accounts.
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token         TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS calendars (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    ics_url       TEXT NOT NULL,
    default_tag   TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_synced   TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, ics_url)
);

CREATE TABLE IF NOT EXISTS keyword_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    keyword     TEXT NOT NULL,
    is_regex    INTEGER NOT NULL DEFAULT 0,
    match_field TEXT NOT NULL DEFAULT 'title',
    tag         TEXT NOT NULL,
    category    TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    -- When 1 (default, matches original behaviour): this rule competes
    -- for its category's single slot - if it matches, and no earlier
    -- (lower-priority-number) exclusive rule in the same category has
    -- already matched, its tag wins that category and any other
    -- exclusive rule in the same category is skipped.
    -- When 0: this rule's tag is added unconditionally whenever it
    -- matches, regardless of what else matched in its category -
    -- lets two rules "stack" onto the same event instead of fighting
    -- for one slot. See classify_event() in ics_sync.py.
    exclusive   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tag_definitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    label       TEXT NOT NULL,
    category    TEXT NOT NULL,
    is_duration INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_keyword_rules_user_category ON keyword_rules(user_id, category, enabled);
CREATE INDEX IF NOT EXISTS idx_calendars_user_enabled ON calendars(user_id, enabled);
CREATE INDEX IF NOT EXISTS idx_tag_definitions_user ON tag_definitions(user_id);

-- Local cache of raw calendar event data (title/description/start/etc)
-- as last seen during a sync, keyed by the same event_id written to
-- Influx. This exists so keyword rule changes can be reprocessed against
-- everything we've ever synced, without depending on the ICS feed still
-- exposing those events (most feeds are a rolling window, not a full
-- archive). For a normal (not excluded) event, applied_tags tracks
-- what's currently written to Influx, so a reprocess pass knows exactly
-- which old tag-points need deleting vs which are unchanged. For an
-- excluded event it's the tag set to write back on restore, and
-- stranded_tags is what may still be in Influx (see below).
CREATE TABLE IF NOT EXISTS calendar_events_cache (
    event_id      TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    calendar_id   INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    start_iso     TEXT NOT NULL,
    duration_min  INTEGER,
    applied_tags  TEXT NOT NULL DEFAULT '[]',
    -- Set when a person manually edits this event's tags via the
    -- Timeline UI. While true, sync_calendar() and reprocess both skip
    -- reclassifying this event, so a manual fix doesn't get silently
    -- reverted by the next scheduled sync or a future keyword-rule
    -- change. Cleared again by "Reset to auto" in the Timeline UI
    -- (POST /calendar_events/{id}/reset_tags).
    manually_tagged INTEGER NOT NULL DEFAULT 0,
    -- Set when a person strikes this event off in the Timeline UI
    -- ("Didn't attend"). While true, the event's points are removed
    -- from Influx and sync_calendar(), reprocess and the tag-edit
    -- endpoints never write them back, so it doesn't count towards tags
    -- or event stats - but the cache row stays, so it's still shown
    -- (struck through) on the timeline and can be restored. Never
    -- touched by upsert_cached_event(), so it survives re-syncs.
    excluded      INTEGER NOT NULL DEFAULT 0,
    -- JSON list of tags whose Influx points may still exist for an
    -- excluded event - i.e. a strike-off (or a failed restore) that
    -- couldn't finish deleting/writing. Kept separate from applied_tags
    -- so editing an excluded event's tags can't lose track of them; the
    -- next exclude retry deletes these too. Empty means the strike-off
    -- is complete. Shown in the Timeline as "needs retry".
    stranded_tags TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cache_user_calendar ON calendar_events_cache(user_id, calendar_id);
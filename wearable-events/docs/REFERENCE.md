# wearable-events reference

Internals that the code relies on and that aren't obvious from a single
file. For the overall stack layout see
[`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md); for setup see
[`docs/SETUP.md`](../../docs/SETUP.md).

## Calendar events

### Where the data lives

- **SQLite `calendar_events_cache`** (`schema.sql`) holds every event
  seen during a sync, including its title, description, start time,
  duration and tags, plus three per-event flags: `manually_tagged`,
  `excluded` and `stranded_tags`. Rows are kept after an event drops out
  of the ICS feed's window, so reprocessing and restores never depend on
  the feed.
- **InfluxDB `events` measurement** holds one point per (event, tag),
  with the tags `tag`, `source=calendar`, `user` and `calendar`. These
  points are what tag and event stats count.
- **Event ids** are a hash of calendar name, start time and title
  (`calendar_event_id`). Renaming the calendar, or retitling or moving
  the event at the source, produces a new id, and any per-event flag on
  the old id is lost.
- **Cache rows are keyed by `(user_id, event_id)`.** Two household
  members who both add a shared calendar under the same name get the
  same event id for the same event, so each needs their own row, with
  their own tags and their own "Didn't attend" flag. Installs created
  before this rebuild the table in place on startup
  (`db._migrate_cache_to_per_user_key`). The rebuild runs in one
  transaction, checks the row count and rolls back on any failure.
- **Recurring events:** RRULE series aren't expanded, so only the master
  VEVENT and any overridden instances are synced.

### Keyword rules and manual tag overrides

- **Sync** runs on startup and then every `SYNC_INTERVAL_MINUTES`. It
  classifies each event with the enabled keyword rules and falls back to
  the calendar's default tag.
- **Edit tags** in the Timeline (`PATCH /calendar_events/{id}/tags`) sets
  `manually_tagged`. From then on, sync and reprocess keep that event's
  tags as they are.
- **Reset to auto** (`POST /calendar_events/{id}/reset_tags`)
  reclassifies the event with the current rules and clears the flag.

### "Didn't attend" (exclusion)

- **Effect:** an excluded event stays on the Timeline, struck through,
  but has no points in Influx, so it doesn't count towards tags or event
  stats.
- **Exclude** (`POST /calendar_events/{id}/exclude`) is pessimistic.
  1. It marks the row `excluded` and records every tag to delete in
     `stranded_tags`.
  2. It looks up which tags the event actually has points for in Influx
     (`find_calendar_event_point_tags`, by its `event_id` field) and adds
     them. The cache can lag behind Influx: a sync that retags an event
     writes the new tag without deleting the old one, and a retag whose
     delete failed leaves the old point behind. If the lookup fails,
     every known tag stays stranded and the endpoint returns 502.
  3. It deletes those points.
  4. It keeps in `stranded_tags` only the tags whose delete failed.
  5. If any delete failed, it returns 502. The Timeline then shows
     "removal failed · may still be counted" with a **Retry removal**
     button. Calling exclude again deletes whatever is still stranded.
- **Restore** (`POST /calendar_events/{id}/restore`):
  - Deletes any stranded points that aren't part of the restored tag set.
  - Writes the restored tags. A manually-tagged event gets its cached
    tags; otherwise the event is reclassified against the current rules.
  - Clears the flag.
  - If Influx fails part-way, the event stays excluded and the tags that
    might have been written are recorded as stranded. The endpoint
    returns 502, and calling it again is safe.
  - Works the same for a disabled calendar. Disabling only stops future
    syncs and doesn't remove the calendar's existing points.
- **While an event is excluded:**
  - Sync and reprocess never write its points.
  - Edit tags and Reset to auto update only the cached tags, which take
    effect on restore, and leave `stranded_tags` alone.
- **Deletes are scoped by `user`.** Household members often share a
  calendar, so one person's points can share a tag, calendar and
  timestamp with another person's.
- **Values in Influx deletes and queries are escaped.** InfluxDB's delete
  predicate reads backslash escapes and rejects a bare `"`, a lone `\` or
  a raw newline, so names containing those used to make every delete
  fail. OSS InfluxDB has no parameterized Flux queries, so Flux strings
  are escaped too.

### Reprocessing

Reprocessing (`app/reprocess.py`) reclassifies every cached event after
keyword rules change, and skips manually-tagged and excluded events.

- The "would the tags change?" check runs against a snapshot taken at the
  start of the run.
- Only an event whose tags change is re-read under the lock and rewritten
  (see below).

### Concurrency: `calendar_points_lock`

Several code paths read a cache row (its excluded flag and tags) and then
write or delete that event's Influx points: the scheduled sync, the
reprocess thread and the per-event Timeline endpoints. They all hold
`calendar_points_lock` (`app/queries/calendar_events.py`) from the read
through the write. Without the lock, a sync could read `excluded=0` just
before a "Didn't attend" click and write the points back after they had
been deleted.

The lock is **in-process only**. That works because the app runs as a
single uvicorn process with its scheduler thread inside it (see the
`Dockerfile` CMD). Running with `--workers N` would start N schedulers
and need a cross-process guard, for example re-checking the flag inside
a SQLite write transaction.

## Sleep entry addressing (`entry_id`, not `sleep_date`)

Subjective sleep scores are keyed by `sleep_entry_id(user, session_start)`
(`app/queries/sleep_journal.py`), which hashes the sleep **session's**
start instant, not its calendar date. This protects against two
previously shipped bugs:

- Two sessions on the same date silently overwrote each other.
- A saved edit didn't stick, because the same instant hashed differently
  depending on the timezone of the datetime it came from.

`tests/test_sleep_entry_id.py` covers both.

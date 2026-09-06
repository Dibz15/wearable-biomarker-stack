"""sleep_entry_id() (app/queries/sleep_journal.py) is a pure function -
deterministic id for a subjective sleep score entry, derived from the
sleep SESSION's own start time rather than its calendar date. These
tests protect the two real, previously-shipped bugs its own docstring
describes: two different sessions silently overwriting each other when
they shared a calendar date, and a saved edit silently not sticking
because the same instant hashed differently depending on which
timezone the datetime object happened to carry.
"""

from datetime import datetime, timedelta, timezone

from app.queries.sleep_journal import sleep_entry_id


def test_same_user_and_session_start_always_produces_the_same_id():
    dt = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
    assert sleep_entry_id("alice", dt) == sleep_entry_id("alice", dt)


def test_different_users_get_different_ids_for_the_same_instant():
    dt = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
    assert sleep_entry_id("alice", dt) != sleep_entry_id("bob", dt)


def test_two_sessions_on_the_same_calendar_date_get_different_ids():
    """The exact bug this scheme fixes: one session starting just
    after local midnight, another just before the NEXT local midnight -
    both resolve to the same calendar day, but they're different
    nights and must not collide.
    """
    just_after_midnight = datetime(2026, 9, 5, 0, 15, tzinfo=timezone.utc)
    just_before_next_midnight = datetime(2026, 9, 5, 23, 45, tzinfo=timezone.utc)
    assert sleep_entry_id("alice", just_after_midnight) != sleep_entry_id("alice", just_before_next_midnight)


def test_same_instant_hashes_identically_regardless_of_timezone_representation():
    """The second real bug: the same real instant must hash the same
    way whether the datetime object carries UTC or some other offset -
    otherwise a PATCH computed from a round-tripped (always-UTC) Influx
    timestamp silently orphans the original local-timezone-tagged entry
    instead of overwriting it.
    """
    utc_dt = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
    # Same real instant, expressed in a UTC+2 offset instead.
    plus_two = utc_dt.astimezone(timezone(timedelta(hours=2)))
    assert sleep_entry_id("alice", utc_dt) == sleep_entry_id("alice", plus_two)


def test_a_different_start_time_produces_a_different_id():
    dt1 = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
    dt2 = dt1 + timedelta(minutes=1)
    assert sleep_entry_id("alice", dt1) != sleep_entry_id("alice", dt2)


def test_id_is_a_short_hex_string():
    dt = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
    entry_id = sleep_entry_id("alice", dt)
    assert len(entry_id) == 12
    int(entry_id, 16)  # raises ValueError if not valid hex

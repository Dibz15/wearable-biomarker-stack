"""classify_event() is a pure function (app/ics_sync.py) - no DB or
network involved, so these are genuine, fast unit tests. It assumes
its `rules` argument is already filtered to enabled rules and already
sorted by ascending priority - callers (app/ics_sync.py's own
sync_calendar, app/reprocess.py) are responsible for both; these tests
construct already-sorted lists directly rather than re-testing that
sorting here.
"""

from app.ics_sync import classify_event


def rule(keyword, tag, category, *, priority=0, is_regex=False, match_field="title", exclusive=True):
    return {
        "keyword": keyword, "tag": tag, "category": category,
        "priority": priority, "is_regex": is_regex,
        "match_field": match_field, "exclusive": exclusive,
    }


def test_simple_keyword_match():
    rules = [rule("standup", "meeting", "context")]
    assert classify_event("Daily Standup", "", rules, "default") == ["meeting"]


def test_case_insensitive_match():
    rules = [rule("STANDUP", "meeting", "context")]
    assert classify_event("daily standup", "", rules, "default") == ["meeting"]


def test_no_match_falls_back_to_default_tag():
    rules = [rule("standup", "meeting", "context")]
    assert classify_event("Dentist Appointment", "", rules, "default") == ["default"]


def test_matches_against_description_when_configured():
    rules = [rule("gym", "exercise", "context", match_field="description")]
    assert classify_event("Block", "gym session with a friend", rules, "default") == ["exercise"]
    # Same keyword in the TITLE shouldn't match a description-scoped rule.
    assert classify_event("gym", "unrelated", rules, "default") == ["default"]


def test_regex_match():
    rules = [rule(r"\bsync\b", "meeting", "context", is_regex=True)]
    assert classify_event("Weekly Sync", "", rules, "default") == ["meeting"]
    assert classify_event("Synchronize files", "", rules, "default") == ["default"]  # no word boundary


def test_first_match_wins_within_an_exclusive_category():
    """Rules pre-sorted by ascending priority - the first hit for a
    category wins, later rules for the same category are skipped even
    if they'd also match."""
    rules = [
        rule("meeting", "meeting", "context", priority=10),
        rule("standup", "standup", "context", priority=20),
    ]
    assert classify_event("Standup Meeting", "", rules, "default") == ["meeting"]


def test_non_exclusive_rule_stacks_alongside_the_exclusive_winner():
    rules = [
        rule("meeting", "meeting", "context", priority=10, exclusive=True),
        rule("standup", "quick", "meta", priority=20, exclusive=False),
    ]
    tags = classify_event("Standup Meeting", "", rules, "default")
    assert set(tags) == {"meeting", "quick"}


def test_non_exclusive_context_match_does_not_suppress_the_default_fallback():
    """A non-exclusive rule matching in the context category doesn't
    count as "context is covered" - exclusive and non-exclusive rules
    answer different questions (see classify_event's own docstring)."""
    rules = [rule("standup", "extra", "context", exclusive=False)]
    tags = classify_event("Standup", "", rules, "default")
    assert "default" in tags  # fallback still applied
    assert "extra" in tags


def test_multiple_categories_combine():
    rules = [
        rule("meeting", "meeting", "context"),
        rule("coffee", "caffeine", "substance"),
    ]
    tags = classify_event("Coffee Meeting", "", rules, "default")
    assert set(tags) == {"meeting", "caffeine"}


def test_extra_tag_deduped_if_already_a_category_winner():
    rules = [
        rule("standup", "quick", "context", priority=10, exclusive=True),
        rule("standup", "quick", "meta", priority=20, exclusive=False),
    ]
    tags = classify_event("Standup", "", rules, "default")
    assert tags.count("quick") == 1


def test_empty_title_and_description_do_not_crash():
    rules = [rule("standup", "meeting", "context")]
    assert classify_event(None, None, rules, "default") == ["default"]

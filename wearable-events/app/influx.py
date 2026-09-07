""" Backward-compatible re-export shim.

influx.py used to be a single ~3200-line file holding every InfluxDB
query/write function this app uses. It's been split into app/queries/
(client, calendar_events, sleep_journal, sleep_sessions, naps, vitals,
activity, workouts - one module per feature domain, see each module's
own docstring), grouped by feature domain the same way app/routes/
groups main.py's own endpoints.

This file re-exports every name the split modules define, unchanged,
so `import app.influx as influx` / `from app.influx import X` keeps
working exactly as before for anything not yet updated to import from
app.queries.* directly (this app's own test suite, in particular,
still imports this way in several places) - a pure convenience/
compatibility layer, not where any real logic lives anymore.
"""

from app.queries.client import (
    get_client,
    list_distinct_sensor_users,
)
from app.queries.calendar_events import (
    calendar_event_id,
    manual_event_id,
    delete_event_tag_point,
    find_manual_event_by_id,
    write_event_points,
    find_manual_events_in_range,
)
from app.queries.sleep_journal import (
    sleep_entry_id,
    write_sleep_point,
    _split_sleep_entry_fields,
    find_sleep_entries_in_range,
    get_sleep_journal_rollup,
    find_sleep_entry_by_id,
    find_sleep_entry_for_wake_date,
    write_sleep_entry_for_wake_date,
    delete_sleep_entry,
)
from app.queries.sleep_sessions import (
    find_last_completed_sleep_session,
    find_sleep_sessions_in_range,
    _sleep_sessions_by_wake_date,
    _sleep_session_for_night,
    _session_windows,
    _nightly_means_in_bulk,
    get_nightly_baseline_comparison,
    _primary_device_session,
    get_sleep_overview_for_night,
    get_sleep_timing_trend,
    get_sleep_regularity_index,
    get_sleep_stage_trend,
    get_sleep_vitals_trend,
    get_sleep_vitals_series,
    _sleep_stage_timeline,
    _bulk_stage_segments,
    get_sleep_hypnogram_for_night,
    get_nightly_differential_series,
    get_sleep_stage_breakdown,
    count_wake_events,
    get_sleep_quality_indicators,
    get_sleep_duration_recommendation,
)
from app.queries.naps import (
    get_naps_for_date,
    get_nap_trend,
)
from app.queries.vitals import (
    local_today_bounds,
    _device_stat_by_field,
    get_today_vitals,
    get_today_series,
    _grouped_series,
    _MAX_WINDOWS_PER_QUERY,
    _merge_windows,
    _windowed_series,
    _slice_values,
    get_manual_readings,
    get_period_range_series,
    get_rolling_mean_series,
    _daily_values,
    _zscore_comparison,
    get_baseline_comparison,
)
from app.queries.activity import (
    SITTING_EXCLUDED_LABELS,
    _get_pivoted_activity_minutes,
    _finalize_session,
    get_activity_sessions,
    get_precomputed_activity_sessions,
    get_combined_activity_sessions,
    get_sitting_minutes,
    get_stood_hours,
    get_hourly_activity_breakdown,
    get_activity_time_range_series,
    get_today_steps,
)
from app.queries.workouts import (
    TRAINING_EFFECT_SCALE,
    HR_ZONE_DISPLAY_NAMES,
    HR_ZONE_ORDER,
    WORKOUT_LAP_FIELDS,
    get_training_effect_label,
    get_workout_summary_detail,
    get_workout_detail_series,
    get_workout_laps,
    get_workout_raw_intensity,
)
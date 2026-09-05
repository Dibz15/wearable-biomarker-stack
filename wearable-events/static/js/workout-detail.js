// --- Workout Detail page (opened from a tappable row in the Activity
// page's own "Activities Today" session list - only "precomputed"
// entries, i.e. real BASE_ACTIVITY_SUMMARY rows, have one of these to
// open at all) ---
//
// CORE SUBSET ONLY, deliberately - summary stats, HR Zone breakdown,
// Training Effect, and an HR-over-time chart (when per-sample data
// exists). Deferred to a later pass: laps table, elevation/speed/
// cadence charts, GPS map (see wearable-events/UI_DESIGN_NOTES.md's
// own "Individual Activity/Workout detail" page notes for the full
// documented layout this is working toward).
import { escapeHtml, api, formatNum } from "./core.js";
import { openDetailScreen, registerActiveChart, clearActiveCharts } from "./metric-detail.js";
import { buildLineChart } from "./metric-charts.js";

// The person's own real watch setting (Zepp: Settings -> interval type
// -> "lactate threshold heart rate zone") - matches
// HR_ZONE_DISPLAY_NAMES in app/influx.py exactly; the backend already
// applies this naming, so this frontend module just renders whatever
// `display_name` each zone comes back with rather than hardcoding a
// second copy of the mapping here.

function formatSecondsAsMinSec(totalSeconds) {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function renderStatsRow(workout) {
  const items = [
    ["Duration", workout.active_seconds !== null && workout.active_seconds !== undefined
      ? formatSecondsAsMinSec(workout.active_seconds) : "\u2013"],
    ["Calories", workout.calories_kcal !== null && workout.calories_kcal !== undefined
      ? `${workout.calories_kcal} kcal` : "\u2013"],
    ["Avg Heart Rate", workout.hr_avg !== null && workout.hr_avg !== undefined
      ? `${workout.hr_avg} bpm` : "\u2013"],
    ["Training Load", workout.training_load !== null && workout.training_load !== undefined
      ? workout.training_load : "\u2013"],
  ];
  return `
    <div class="activity-stats-row">
      ${items.map(([label, value]) => `
        <div class="activity-stat-item">
          <span class="activity-stat-label">${escapeHtml(label)}</span>
          <span class="activity-stat-value">${escapeHtml(String(value))}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function renderHeartRateSummary(workout) {
  if (workout.hr_avg === null && workout.hr_max === null && workout.hr_min === null) return "";
  const cell = (label, value) => `
    <div class="activity-stat-item">
      <span class="activity-stat-label">${escapeHtml(label)}</span>
      <span class="activity-stat-value">${value !== null && value !== undefined ? `${value} bpm` : "\u2013"}</span>
    </div>
  `;
  return `
    <p class="today-section-label">Heart Rate</p>
    <div class="activity-stats-row">
      ${cell("Avg", workout.hr_avg)}
      ${cell("Max", workout.hr_max)}
      ${cell("Min", workout.hr_min)}
    </div>
    <div class="detail-chart-card">
      <canvas id="workout-hr-chart"></canvas>
    </div>
  `;
}

// Each zone gets its own row: name, BPM range (from the real,
// per-workout hr_zone_*_max_bpm thresholds - see
// parser/activefit/FIELD_RESEARCH.md for how these were confirmed,
// and why they're used instead of a fixed/guessed formula), a
// proportional bar, and the time spent in that zone. Zones with zero
// duration AND no real bpm data at all are skipped entirely rather
// than shown as an empty row - matches this app's own "don't
// fabricate rows for absent data" convention elsewhere.
//
// BPM range math, verified directly against the real Hybrid Training
// screenshot before shipping (TWO earlier attempts at this got it
// wrong in different ways and were both caught before committing -
// worth being extra careful with fresh eyes here, not just trusting
// the third attempt either without checking again): each zone's own
// `max_bpm` field IS that same zone's own upper bound (off by one, an
// inclusive/exclusive convention - "132" closes off a range ending at
// "131") - EXCEPT the very last (highest) zone, which uses its own
// max_bpm directly with no adjustment (there's no zone above it to
// imply an exclusive upper edge). The LOWER bound of any zone (other
// than the first) is the PREVIOUS zone's own max_bpm value, unmodified
// - e.g. Active Recovery's range (106-131) has 106 as ITS OWN lower
// bound but that number is stored on the zone BELOW it (N/A), and 131
// comes from Active Recovery's own max_bpm (132) minus one. Computed
// here over the zones array in its ORIGINAL low-to-high order (as
// returned by the backend), deliberately BEFORE reversing for
// display, to keep the index arithmetic straightforward.
function computeZoneBpmRanges(zones) {
  return zones.map((z, i) => {
    if (z.max_bpm === null || z.max_bpm === undefined) return { ...z, bpmRangeLabel: "" };
    const lower = i === 0 ? null : zones[i - 1].max_bpm;
    const upper = i === zones.length - 1 ? z.max_bpm : z.max_bpm - 1;
    const label = lower === null ? `Under ${z.max_bpm} bpm` : `${lower}\u2013${upper} bpm`;
    return { ...z, bpmRangeLabel: label };
  });
}

function renderHeartRateZones(zones) {
  if (!zones || zones.length === 0) return "";
  const totalSeconds = zones.reduce((sum, z) => sum + (z.seconds || 0), 0);
  const withRanges = computeZoneBpmRanges(zones);
  // Reversed only for DISPLAY order (highest-intensity zone first,
  // matching both Zepp's and Gadgetbridge's own real screenshots) -
  // the range math above has already been fully computed against the
  // original low-to-high order, so this reverse can't disturb it.
  const rows = [...withRanges].reverse().map(z => {
    const pct = totalSeconds > 0 ? Math.round((z.seconds / totalSeconds) * 100) : 0;
    return `
      <div class="workout-zone-row">
        <div class="workout-zone-header">
          <span class="workout-zone-name">${escapeHtml(z.display_name)}</span>
          <span class="metric-sub">${escapeHtml(z.bpmRangeLabel)}</span>
        </div>
        <div class="workout-zone-bar-track">
          <div class="workout-zone-bar-fill" style="width: ${pct}%"></div>
        </div>
        <div class="workout-zone-footer">
          <span class="metric-sub">${pct}%</span>
          <span class="metric-sub">${formatSecondsAsMinSec(z.seconds || 0)}</span>
        </div>
      </div>
    `;
  }).join("");
  return `
    <p class="today-section-label">Heart Rate Zones</p>
    <div class="sleep-summary-card">${rows}</div>
  `;
}

function renderTrainingEffectCard(workout) {
  const aerobic = workout.aerobic_training_effect;
  const anaerobic = workout.anaerobic_training_effect;
  // Zepp's own real screenshots hide a gauge entirely when its value
  // is negligible for a given activity (e.g. no Anaerobic gauge shown
  // at all for a low-intensity walk) - matched here by only rendering
  // a gauge when the backend actually returned one (get_training_effect_label
  // returns None for a workout with no HasField("trainingEffect") at
  // all in its own RAW_SUMMARY_DATA - a missing FIELD, not a genuine
  // 0.0, which DOES still render).
  if (!aerobic && !anaerobic) return "";
  const gauge = (title, te) => te ? `
    <div class="activity-stat-item">
      <span class="activity-stat-label">${escapeHtml(title)}</span>
      <span class="activity-stat-value">${formatNum(te.value, 1)}</span>
      <span class="metric-sub">${escapeHtml(te.label)}</span>
    </div>
  ` : "";
  return `
    <p class="today-section-label">Training Effect</p>
    <div class="activity-stats-row">
      ${gauge("Aerobic", aerobic)}
      ${gauge("Anaerobic", anaerobic)}
    </div>
  `;
}

export async function openWorkoutDetail(startMs) {
  openDetailScreen("Workout");
  const content = document.getElementById("detail-content");
  content.innerHTML = `<p class="muted">Loading...</p>`;
  clearActiveCharts();

  let workout;
  try {
    workout = await api(`/activity/workout/${startMs}`);
  } catch (e) {
    content.innerHTML = `<p class="status">Error loading workout: ${escapeHtml(e.message)}</p>`;
    return;
  }

  document.getElementById("detail-title").textContent = workout.name || "Workout";

  const dateLabel = new Date(workout.start).toLocaleString([], {
    weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });

  content.innerHTML = `
    <p class="metric-sub" style="margin-bottom: 0.75rem;">${escapeHtml(dateLabel)} \u00b7 ${escapeHtml(workout.device || "")}</p>
    ${renderStatsRow(workout)}
    ${renderHeartRateSummary(workout)}
    ${renderHeartRateZones(workout.hr_zones)}
    ${renderTrainingEffectCard(workout)}
  `;

  // Per-sample HR chart, only if a real FIT/GPX export exists for this
  // workout. An empty series here is a real, expected, already-
  // documented case - most commonly a workout recorded BEFORE
  // Gadgetbridge's own "Auto export GPX/FIT tracks" automations were
  // enabled at all (that export only fires when a NEW activity syncs,
  // never retroactively for one already recorded - confirmed directly
  // this session against two real workouts, one from before enabling
  // the automations and one from after). The message says as much,
  // rather than a bare "no data" that reads like something's broken -
  // Zepp/Gadgetbridge's own apps can still show a chart for that same
  // older workout because they read the raw per-workout details file
  // directly and locally; this app can only reach that data via the
  // separate export automation, which has nothing to export for a
  // workout that predates it.
  const showHrPlaceholder = (message) => {
    const canvas = document.getElementById("workout-hr-chart");
    if (canvas) {
      canvas.replaceWith(Object.assign(document.createElement("p"), {
        className: "metric-card-empty", textContent: message,
      }));
    }
  };
  const NO_HR_DATA_MESSAGE = "No per-sample heart rate data for this workout \u2013 likely recorded before GPX/FIT export was enabled";

  if (workout.hr_avg !== null && workout.hr_avg !== undefined) {
    try {
      const hrSeries = await api(`/activity/workout/${startMs}/heart-rate`);
      const canvas = document.getElementById("workout-hr-chart");
      const points = hrSeries.filter(p => p.hr !== undefined).map(p => ({ t: p.time, v: p.hr }));
      if (canvas && points.length > 0) {
        const deviceName = workout.device || "device";
        registerActiveChart(buildLineChart(canvas, { [deviceName]: points }, [deviceName], 0));
      } else {
        showHrPlaceholder(NO_HR_DATA_MESSAGE);
      }
    } catch (e) {
      // A failed per-sample fetch shouldn't take down the whole page -
      // the summary content above is already rendered and useful on
      // its own. Deliberately a DIFFERENT message than the "no data"
      // case above - this one means the request itself broke
      // (network/server error), not that the export simply doesn't
      // exist for this workout.
      showHrPlaceholder("Could not load per-sample heart rate data");
    }
  }
}
// --- Workout Detail page (opened from a tappable row in the Activity
// page's own "Activities Today" session list - only "precomputed"
// entries, i.e. real BASE_ACTIVITY_SUMMARY rows, have one of these to
// open at all) ---
//
// Originally a deliberately smaller core subset (summary stats, HR
// Zone breakdown, Training Effect, HR-over-time chart); this pass
// adds the deferred pieces - Lap Details table, elevation/speed/
// cadence charts, and a real GPS map (Leaflet + OpenStreetMap tiles,
// loaded via CDN in index.html - see UI_DESIGN_NOTES.md's own
// "Individual Activity/Workout detail" page notes for the full
// documented layout this is working toward).
import { escapeHtml, api, formatNum } from "./core.js";
import { openDetailScreen, registerActiveChart, clearActiveCharts } from "./metric-detail.js";
import { buildLineChart, buildCategoryPieChart, buildTieredBarChart, renderTierLegend, INTENSITY_BANDS } from "./metric-charts.js";
import { openZoomChart } from "./zoom-chart.js";

// The person's own real watch setting (Zepp: Settings -> interval type
// -> "lactate threshold heart rate zone") - matches
// HR_ZONE_DISPLAY_NAMES in app/influx.py exactly; the backend already
// applies this naming, so this frontend module just renders whatever
// `display_name` each zone comes back with rather than hardcoding a
// second copy of the mapping here.

// Speed is stored (and comes back from the API) in m/s regardless of
// source (summary-level avg_speed_mps/max_speed_mps, or per-sample
// speed_mps) - converted to mph only here, at display time, matching
// what the person's own real Zepp screenshots show them (their device
// is configured for mph, confirmed directly against real workout
// screenshots earlier this session) - this app has no other existing
// distance/speed unit convention to match instead.
const MPS_TO_MPH = 2.23694;
const METERS_TO_MILES = 1 / 1609.344;

// A short, deliberate palette for this page - previously every chart/
// bar used the same DEVICE_CHART_COLORS[0] (a real reported gap:
// buildLineChart colors by DEVICE index, and every chart here has
// exactly one device, so they all silently landed on the same first
// color). Each metric gets its own color instead, and HR Zones get a
// genuine low-to-high intensity gradient (cool -> hot) rather than one
// flat fill for every zone regardless of intensity.
const WORKOUT_COLORS = {
  hr: "#ff6b6b",
  elevation: "#4fd8b8",
  speed: "#6ea8fe",
  cadence: "#f0c674",
  stride: "#b39ddb",
  gpsTrack: "#f4a261",
  // Distinct from every INTENSITY_BANDS tier color too (those clash
  // with speed/cadence's own colors above) - only matters as a single
  // representative line color in the zoom view, since the dedicated
  // Activity Intensity panel itself uses the full tiered-bar coloring.
  intensity: "#9b8fd4",
};
// Indexed low-to-high intensity, matching HR_ZONE_ORDER in app/influx.py
// (na/warm_up/fat_burn/aerobic/anaerobic/extreme) - a cool-to-hot
// progression (gray for the untracked "N/A" zone, then blue -> teal ->
// yellow -> orange -> red as intensity rises), not the same flat color
// repeated for every zone regardless of how hard that zone actually is.
const HR_ZONE_COLORS = {
  na: "#8a8d99",
  warm_up: "#6ea8fe",
  fat_burn: "#4fd8b8",
  aerobic: "#f0c674",
  anaerobic: "#f4a261",
  extreme: "#ff6b6b",
};

function formatSecondsAsMinSec(totalSeconds) {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function renderStatsRow(workout, samples) {
  // max_speed_mps comes from the summary blob's own Pace field - a
  // GENUINELY SEPARATE data source from the per-sample FIT export
  // (see parser/activefit/FIELD_RESEARCH.md's own Pace/Speed
  // solving entry) - it's entirely possible for a workout to have
  // real per-sample speed data (the chart fills in fine) while the
  // summary blob itself never recorded a Pace field at all, a real
  // reported case, not hypothetical. Falls back to the max of the
  // already-fetched per-sample speed_mps values in that case, same
  // "derive from samples when no summary field exists" pattern
  // already used for Stride's own max value.
  let maxSpeedMps = workout.max_speed_mps;
  if (maxSpeedMps === null || maxSpeedMps === undefined) {
    const sampleSpeeds = samples.filter(p => p.speed_mps !== undefined).map(p => p.speed_mps);
    if (sampleSpeeds.length > 0) maxSpeedMps = Math.max(...sampleSpeeds);
  }
  const maxSpeedMph = maxSpeedMps !== null && maxSpeedMps !== undefined
    ? `${formatNum(maxSpeedMps * MPS_TO_MPH, 1)} mph` : "\u2013";
  const cell = (label, value) => `
    <div class="activity-stat-item">
      <span class="activity-stat-label">${escapeHtml(label)}</span>
      <span class="activity-stat-value">${escapeHtml(String(value))}</span>
    </div>
  `;
  // Two rows of three, not one row of six - .activity-stats-row's own
  // CSS has no wrapping behavior (a plain flex row), and this class is
  // shared with other 2-4 item stat rows elsewhere on this page and
  // in the app more broadly - changing it to wrap could shift those
  // other rows' own layout in ways not actually asked for here.
  // Keeping to the already-proven 3-item width (matching the existing
  // Heart Rate summary row exactly) avoids touching shared CSS at all.
  return `
    <div class="activity-stats-row">
      ${cell("Duration", workout.active_seconds !== null && workout.active_seconds !== undefined
        ? formatSecondsAsMinSec(workout.active_seconds) : "\u2013")}
      ${cell("Calories", workout.calories_kcal !== null && workout.calories_kcal !== undefined
        ? `${workout.calories_kcal} kcal` : "\u2013")}
      ${cell("Avg Heart Rate", workout.hr_avg !== null && workout.hr_avg !== undefined
        ? `${workout.hr_avg} bpm` : "\u2013")}
    </div>
    <div class="activity-stats-row">
      ${cell("Training Load", workout.training_load !== null && workout.training_load !== undefined
        ? workout.training_load : "\u2013")}
      ${cell("Max Speed", maxSpeedMph)}
      ${cell("Steps", workout.steps !== null && workout.steps !== undefined ? workout.steps : "\u2013")}
    </div>
  `;
}

function renderHeartRateSummary(workout, samples) {
  if (workout.hr_avg === null && workout.hr_max === null && workout.hr_min === null) return "";
  const hasChart = samples.some(p => p.hr !== undefined);
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
      ${hasChart ? zoomTriggerHtml("hr") : ""}
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
          <div class="workout-zone-bar-fill" style="width: ${pct}%; background: ${HR_ZONE_COLORS[z.key] || WORKOUT_COLORS.hr}"></div>
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

// Lap Details table. Columns are a deliberately smaller, fixed subset
// of what a lap can carry (see WORKOUT_LAP_FIELDS in app/influx.py for
// the full list) - Lap/Duration/Distance/Avg HR/Avg Speed, matching
// the spirit of both Zepp's and Gadgetbridge's own real lap tables
// (which also show a handful of columns, not every field at once).
// Distance/Speed columns are only shown at all if AT LEAST ONE lap has
// that data - an indoor workout's own laps (HR/duration only, no GPS)
// shouldn't show two permanently-empty columns.
function renderLapsTable(laps) {
  if (!laps || laps.length === 0) return "";
  const hasDistance = laps.some(l => l.distance_m !== undefined);
  const hasSpeed = laps.some(l => l.avg_speed_mps !== undefined);
  const hasHr = laps.some(l => l.avg_hr !== undefined);

  const headerCells = ["Lap", "Time"];
  if (hasDistance) headerCells.push("Distance");
  if (hasHr) headerCells.push("Avg HR");
  if (hasSpeed) headerCells.push("Avg Speed");

  const rows = laps.map(l => {
    const cells = [
      String(l.lap_number !== undefined ? l.lap_number : ""),
      l.duration_s !== undefined ? formatSecondsAsMinSec(l.duration_s) : "\u2013",
    ];
    if (hasDistance) cells.push(l.distance_m !== undefined ? `${formatNum(l.distance_m * METERS_TO_MILES, 2)} mi` : "\u2013");
    if (hasHr) cells.push(l.avg_hr !== undefined ? `${l.avg_hr} bpm` : "\u2013");
    if (hasSpeed) cells.push(l.avg_speed_mps !== undefined ? `${formatNum(l.avg_speed_mps * MPS_TO_MPH, 1)} mph` : "\u2013");
    return `<tr>${cells.map(c => `<td>${escapeHtml(c)}</td>`).join("")}</tr>`;
  }).join("");

  return `
    <p class="today-section-label">Lap Details</p>
    <div class="sleep-summary-card workout-laps-card">
      <table class="workout-laps-table">
        <thead><tr>${headerCells.map(h => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

// A single reusable chart-card shape for chart-only sections (Speed
// specifically - Elevation/Cadence/Stride below get their own
// dedicated panels since they also show summary stats above their own
// chart, a shape this generic version doesn't have). Same "canvas now,
// filled in or replaced with a message once the actual per-sample
// fetch resolves" pattern the HR chart already established. Returns
// "" (renders nothing at all, not an empty-state message) when the
// field is completely absent from every sample - an indoor workout's
// own Yoga/Hybrid Training session genuinely has no speed data at
// all, and showing an empty "no data" card for something that was
// never going to exist for that activity type would just be noise
// (unlike HR, which is expected for virtually every workout and so
// gets its own explanatory placeholder instead of disappearing).
// Small per-panel zoom trigger - an overlay INSIDE the chart card
// itself (see .detail-chart-zoom-btn's own comment in style.css for
// why: a direct report that a button placed below the card looked
// visually disconnected from the chart it belongs to). Every button
// opens the SAME shared multi-series zoom view (all 5 metrics still
// toggleable together there, per the original spec) - the
// data-zoom-key just decides which series starts focused/visible when
// THAT particular panel's own button was the one tapped, wired
// generically below via a single querySelectorAll rather than one
// bespoke handler per panel.
function zoomTriggerHtml(key) {
  return `<button class="detail-chart-zoom-btn workout-zoom-trigger" data-zoom-key="${escapeHtml(key)}">Zoom \u2197</button>`;
}

function renderPerSampleChartCard(canvasId, title, samples, field, zoomKey = null) {
  const hasAny = samples.some(p => p[field] !== undefined);
  if (!hasAny) return "";
  return `
    <p class="today-section-label">${escapeHtml(title)}</p>
    <div class="detail-chart-card">
      <canvas id="${canvasId}"></canvas>
      ${zoomKey ? zoomTriggerHtml(zoomKey) : ""}
    </div>
  `;
}

// Elevation: summary-level Avg/Min/Max/Gain stats (from RAW_SUMMARY_DATA
// - available for any workout with a location field, regardless of
// whether a per-sample FIT/GPX export exists at all) shown above the
// per-sample chart (only when that DOES exist). Rendered independently
// of each other - a workout can have summary altitude stats with no
// per-sample chart (no export), though not the reverse in practice.
function renderElevationPanel(workout, samples) {
  const hasStats = workout.altitude_avg_m !== null && workout.altitude_avg_m !== undefined;
  const hasChart = samples.some(p => p.altitude_m !== undefined);
  if (!hasStats && !hasChart) return "";

  const cell = (label, value) => `
    <div class="activity-stat-item">
      <span class="activity-stat-label">${escapeHtml(label)}</span>
      <span class="activity-stat-value">${value !== null && value !== undefined ? `${formatNum(value, 1)} m` : "\u2013"}</span>
    </div>
  `;
  const statsHtml = hasStats ? `
    <div class="activity-stats-row">
      ${cell("Avg", workout.altitude_avg_m)}
      ${cell("Min", workout.altitude_min_m)}
      ${cell("Max", workout.altitude_max_m)}
      ${cell("Gain", workout.elevation_gain_m)}
    </div>
  ` : "";
  const chartHtml = hasChart ? `
    <div class="detail-chart-card">
      <canvas id="workout-elevation-chart"></canvas>
      ${zoomTriggerHtml("elevation")}
    </div>
  ` : "";

  return `
    <p class="today-section-label">Elevation</p>
    ${statsHtml}
    ${chartHtml}
  `;
}

// Cadence: same pattern as Elevation above - summary-level Avg/Max
// (from RAW_SUMMARY_DATA) shown above the per-sample chart.
function renderCadencePanel(workout, samples) {
  const hasStats = workout.avg_cadence_per_min !== null && workout.avg_cadence_per_min !== undefined;
  const hasChart = samples.some(p => p.cadence_rpm !== undefined);
  if (!hasStats && !hasChart) return "";

  const cell = (label, value) => `
    <div class="activity-stat-item">
      <span class="activity-stat-label">${escapeHtml(label)}</span>
      <span class="activity-stat-value">${value !== null && value !== undefined ? `${formatNum(value, 0)} spm` : "\u2013"}</span>
    </div>
  `;
  const statsHtml = hasStats ? `
    <div class="activity-stats-row">
      ${cell("Avg", workout.avg_cadence_per_min)}
      ${cell("Max", workout.max_cadence_per_min)}
    </div>
  ` : "";
  const chartHtml = hasChart ? `
    <div class="detail-chart-card">
      <canvas id="workout-cadence-chart"></canvas>
      ${zoomTriggerHtml("cadence")}
    </div>
  ` : "";

  return `
    <p class="today-section-label">Cadence</p>
    ${statsHtml}
    ${chartHtml}
  `;
}

// Gradient Distribution: a pie chart (Uphill/Flat/Downhill time) fully
// computed from already-extracted summary fields - no new parser work
// needed (confirmed directly earlier this session: this exact
// workout's own ascent_seconds/descent_seconds/active_seconds sum to
// its own total duration exactly, and match the real Zepp screenshot's
// own shown 8%/76%/16% breakdown for this workout). Only rendered when
// BOTH ascent_seconds and descent_seconds are present - an indoor
// workout with no elevation data at all has nothing to show here.
//
// Colors chosen for terrain intuition (tan/earthy=effort, gray=neutral,
// cool blue=ease/coasting) rather than reused from WORKOUT_COLORS above
// - this is a genuinely different concept (terrain, not metric type),
// and reusing another section's own color here (an earlier attempt at
// this DID collide - uphill and downhill accidentally matched
// gpsTrack/speed exactly, caught only by testing the actual hex values
// against each other rather than eyeballing the two color sets) would
// make two unrelated things on the same page look deliberately linked
// when they aren't.
const GRADIENT_COLORS = { uphill: "#c9976b", flat: "#8a8d99", downhill: "#7fb3d5" };

function renderGradientDistribution(workout) {
  const { ascent_seconds, descent_seconds, active_seconds } = workout;
  if (ascent_seconds === null || ascent_seconds === undefined
    || descent_seconds === null || descent_seconds === undefined
    || active_seconds === null || active_seconds === undefined) {
    return "";
  }
  const flatSeconds = Math.max(0, active_seconds - ascent_seconds - descent_seconds);
  const legendRow = (label, seconds, color) => {
    const pct = active_seconds > 0 ? Math.round((seconds / active_seconds) * 100) : 0;
    return `
      <div class="workout-gradient-legend-row">
        <span class="workout-gradient-dot" style="background:${color}"></span>
        <span class="workout-gradient-legend-label">${escapeHtml(label)}</span>
        <span class="metric-sub">${pct}%</span>
        <span class="metric-sub">${formatSecondsAsMinSec(seconds)}</span>
      </div>
    `;
  };
  return `
    <p class="today-section-label">Gradient Distribution</p>
    <div class="sleep-summary-card workout-gradient-card">
      <div class="workout-gradient-chart-wrap">
        <canvas id="workout-gradient-chart"></canvas>
      </div>
      <div class="workout-gradient-legend">
        ${legendRow("Uphill", ascent_seconds, GRADIENT_COLORS.uphill)}
        ${legendRow("Flat", flatSeconds, GRADIENT_COLORS.flat)}
        ${legendRow("Downhill", descent_seconds, GRADIENT_COLORS.downhill)}
      </div>
    </div>
  `;
}

// Stride: summary-level Avg (avg_stride_cm, the only stride stat
// RAW_SUMMARY_DATA itself carries) alongside a per-sample-derived Max
// (RAW_SUMMARY_DATA has no max-stride field at all - computed here
// directly from the already-fetched per-sample step_length_mm values,
// no new backend work needed for this specific piece). Both converted
// to inches, matching Zepp's own real "STRIDE (in)" unit choice seen
// in an earlier screenshot this session - the same locale-display
// reasoning already applied to Speed's own mph conversion.
const CM_TO_INCHES = 1 / 2.54;

function renderStridePanel(workout, samples) {
  const hasAvg = workout.avg_stride_cm !== null && workout.avg_stride_cm !== undefined;
  const strideValues = samples.filter(p => p.step_length_mm !== undefined).map(p => p.step_length_mm);
  const hasChart = strideValues.length > 0;
  if (!hasAvg && !hasChart) return "";

  const avgInches = hasAvg ? workout.avg_stride_cm * CM_TO_INCHES : null;
  const maxInches = hasChart ? Math.max(...strideValues) / 10 * CM_TO_INCHES : null; // mm -> cm -> in

  const cell = (label, inches) => `
    <div class="activity-stat-item">
      <span class="activity-stat-label">${escapeHtml(label)}</span>
      <span class="activity-stat-value">${inches !== null ? `${formatNum(inches, 1)} in` : "\u2013"}</span>
    </div>
  `;
  const statsHtml = `
    <div class="activity-stats-row">
      ${cell("Avg", avgInches)}
      ${cell("Max", maxInches)}
    </div>
  `;
  const chartHtml = hasChart ? `
    <div class="detail-chart-card">
      <canvas id="workout-stride-chart"></canvas>
      ${zoomTriggerHtml("stride")}
    </div>
  ` : "";

  return `
    <p class="today-section-label">Stride</p>
    ${statsHtml}
    ${chartHtml}
  `;
}

// Raw activity intensity during the workout's own real time window -
// same continuous background monitoring stream and same tiered-bar
// style as the Activity page's own daily intensity chart (see
// INTENSITY_BANDS's own comment in metric-charts.js), just scoped to
// the workout instead of a full day. A separate data source from every
// other per-sample chart on this page (those all come from a GPX/FIT
// export correlated by workout_start_time; this comes from the watch's
// own always-on monitoring, scoped by the workout's real start/duration
// instead - see get_workout_raw_intensity's own docstring) - so this
// can be present even for a workout with no FIT/GPX export at all, and
// absent even when the others aren't.
function renderIntensityPanel(intensitySeries) {
  const hasData = Object.values(intensitySeries).some(points => points.length > 0);
  if (!hasData) return "";
  return `
    <p class="today-section-label">Activity Intensity</p>
    <div class="detail-chart-card">
      <canvas id="workout-intensity-chart"></canvas>
      ${zoomTriggerHtml("intensity")}
    </div>
    ${renderTierLegend(INTENSITY_BANDS, "")}
  `;
}

function renderGpsMapCard(samples) {
  const hasGps = samples.some(p => p.latitude !== undefined && p.longitude !== undefined);
  if (!hasGps) return "";
  return `
    <p class="today-section-label">Route</p>
    <div class="detail-chart-card workout-map-card">
      <div id="workout-map"></div>
    </div>
  `;
}

export async function openWorkoutDetail(startMs, onBack = null) {
  openDetailScreen("Workout", onBack);
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

  // Fetched up front, in parallel, before any of the per-sample/per-lap
  // sections are rendered - lets renderPerSampleChartCard/
  // renderGpsMapCard/renderLapsTable decide up front whether a given
  // section has anything to show at all (and skip it entirely, rather
  // than rendering a card and then immediately replacing it), and
  // avoids an earlier version's own pattern of only fetching HR after
  // the rest of the page was already drawn.
  //
  // Always attempted, regardless of workout.hr_avg - a real bug found
  // and fixed here: an earlier version gated this ENTIRE fetch on
  // workout.hr_avg being set, using it as a proxy for "this workout
  // likely has per-sample data worth fetching at all". That's an
  // imperfect proxy - hr_avg is a SUMMARY stat, independent of whether
  // a GPX/FIT export with per-sample data exists (see the HR chart's
  // own comment below on that same distinction) - so a workout with
  // real per-sample elevation/speed/cadence data but no HR sensor
  // paired (e.g. an outdoor walk tracked by GPS alone) would never get
  // its samples fetched at all, silently hiding EVERY per-sample chart
  // AND the zoom trigger below, not just the HR one. The existing
  // try/catch and every render function's own "empty means nothing to
  // show" handling already degrade gracefully for a workout with truly
  // no per-sample data at all, so there's no real downside to always
  // attempting this.
  let samples = [];
  let laps = [];
  try {
    [samples, laps] = await Promise.all([
      api(`/activity/workout/${startMs}/samples`),
      api(`/activity/workout/${startMs}/laps`),
    ]);
  } catch (e) {
    // A failed per-sample/lap fetch shouldn't take down the whole
    // page - the summary content above is still useful on its own.
    // samples/laps just stay empty, which every render function
    // below already treats as "nothing to show for this section".
  }

  // Separate fetch, separate data source (see
  // get_workout_raw_intensity's own docstring) - the watch's own
  // always-on background monitoring, scoped by the workout's real
  // start/duration rather than a GPX/FIT export tag, so it can be
  // present even when samples above is empty (no export at all) and
  // absent even when samples isn't. A failure here shouldn't take
  // down the rest of the page either, same reasoning as above.
  let intensitySeries = {};
  try {
    intensitySeries = await api(`/activity/workout/${startMs}/raw-intensity`);
  } catch (e) {
    // intensitySeries stays {}, which renderIntensityPanel already
    // treats as "nothing to show for this section".
  }

  content.innerHTML = `
    <p class="metric-sub" style="margin-bottom: 0.75rem;">${escapeHtml(dateLabel)} \u00b7 ${escapeHtml(workout.device || "")}</p>
    ${renderStatsRow(workout, samples)}
    ${renderGpsMapCard(samples)}
    ${renderHeartRateSummary(workout, samples)}
    ${renderHeartRateZones(workout.hr_zones)}
    ${renderIntensityPanel(intensitySeries)}
    ${renderTrainingEffectCard(workout)}
    ${renderElevationPanel(workout, samples)}
    ${renderGradientDistribution(workout)}
    ${renderPerSampleChartCard("workout-speed-chart", "Speed", samples, "speed_mps", "speed")}
    ${renderCadencePanel(workout, samples)}
    ${renderStridePanel(workout, samples)}
    ${samples.length === 0
      ? `<p class="metric-card-empty">No per-sample data recorded for this workout \u2013 likely recorded before GPX/FIT export was enabled</p>`
      : ""}
    ${renderLapsTable(laps)}
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
  //
  // HR gets this explanatory treatment (unlike elevation/speed/
  // cadence/GPS below, which simply don't render a card at all when
  // absent) because it's the one per-sample series expected for
  // virtually every workout, indoor or outdoor - a missing HR chart
  // is worth explaining, a missing elevation chart for a Yoga session
  // is not.
  if (workout.hr_avg !== null && workout.hr_avg !== undefined) {
    const hrCanvas = document.getElementById("workout-hr-chart");
    if (hrCanvas) {
      const points = samples.filter(p => p.hr !== undefined).map(p => ({ t: p.time, v: p.hr }));
      if (points.length > 0) {
        const deviceName = workout.device || "device";
        registerActiveChart(buildLineChart(hrCanvas, { [deviceName]: points }, [deviceName], 0, "bpm", false, WORKOUT_COLORS.hr));
      } else {
        hrCanvas.replaceWith(Object.assign(document.createElement("p"), {
          className: "metric-card-empty",
          textContent: "No per-sample heart rate data for this workout \u2013 likely recorded before GPX/FIT export was enabled",
        }));
      }
    }
  }

  renderPerSampleChart("workout-elevation-chart", samples, "altitude_m", workout.device, v => v, "m", false, WORKOUT_COLORS.elevation);
  renderPerSampleChart("workout-speed-chart", samples, "speed_mps", workout.device, v => v * MPS_TO_MPH, "mph", true, WORKOUT_COLORS.speed);
  renderPerSampleChart("workout-cadence-chart", samples, "cadence_rpm", workout.device, v => v, "spm", true, WORKOUT_COLORS.cadence);
  renderPerSampleChart("workout-stride-chart", samples, "step_length_mm", workout.device, v => v / 10 * CM_TO_INCHES, "in", true, WORKOUT_COLORS.stride);
  renderGpsMap(samples);
  renderGradientChart(workout);

  const intensityCanvas = document.getElementById("workout-intensity-chart");
  if (intensityCanvas) {
    const intensityDevices = Object.keys(intensitySeries);
    registerActiveChart(buildTieredBarChart(
      intensityCanvas, intensitySeries, intensityDevices,
      { bands: INTENSITY_BANDS, yMax: 255, unit: "", decimals: 0 }
    ));
  }

  document.querySelectorAll(".workout-zoom-trigger").forEach(btn => {
    btn.onclick = () => openWorkoutZoom(workout, samples, intensitySeries, btn.dataset.zoomKey);
  });
}

// Opens the shared zoom view for a workout, focused on whichever
// panel's own button was tapped - all 6 metrics are still toggleable
// together there (the original spec this shares with the sleep
// hypnogram's zoom), `focusKey` just decides which one starts already
// visible instead of requiring an extra tap to see the very chart the
// person was just looking at. HR stays on too whenever it's available
// and isn't already the focus, as a familiar baseline reference (the
// same pairing buildWorkoutZoomSeries's own default-on set already
// used) - so tapping "Zoom" on Cadence, say, opens with HR + Cadence
// visible, not Cadence alone.
function openWorkoutZoom(workout, samples, intensitySeries, focusKey) {
  const series = buildWorkoutZoomSeries(workout, samples, intensitySeries).map(s => ({
    ...s,
    defaultOn: s.key === focusKey || (s.key === "hr" && focusKey !== "hr"),
  }));
  openZoomChart({ title: workout.name || "Workout", series, windowMinutes: 15 });
}

// Fills in the Gradient Distribution pie chart, if its own card was
// rendered at all (renderGradientDistribution already decided whether
// this workout has the ascent/descent/duration data needed for it).
function renderGradientChart(workout) {
  const canvas = document.getElementById("workout-gradient-chart");
  if (!canvas) return;
  const { ascent_seconds, descent_seconds, active_seconds } = workout;
  const flatSeconds = Math.max(0, active_seconds - ascent_seconds - descent_seconds);
  registerActiveChart(buildCategoryPieChart(canvas, [
    { label: "Uphill", value: ascent_seconds, color: GRADIENT_COLORS.uphill },
    { label: "Flat", value: flatSeconds, color: GRADIENT_COLORS.flat },
    { label: "Downhill", value: descent_seconds, color: GRADIENT_COLORS.downhill },
  ]));
}

// Fills in one of the three optional per-sample chart canvases -
// mirrors the HR chart's own buildLineChart call above, just
// parameterized over field/unit-conversion so the three don't need
// three near-identical copies of this logic. Does nothing if the
// canvas doesn't exist at all (renderPerSampleChartCard already
// decided not to render it, because this field is completely absent
// for this workout - Yoga has no elevation/speed data, for instance).
function renderPerSampleChart(canvasId, samples, field, deviceName, convert, unit, minZero = false, colorOverride = null) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const points = samples.filter(p => p[field] !== undefined).map(p => ({ t: p.time, v: convert(p[field]) }));
  if (points.length === 0) return;
  registerActiveChart(buildLineChart(canvas, { [deviceName || "device"]: points }, [deviceName || "device"], 1, unit, minZero, colorOverride));
}

// The zoom view's own series config for a workout - one entry per
// per-sample metric this workout actually has data for (a Yoga
// session has no elevation/speed/cadence/stride at all, so those are
// simply omitted rather than offered as an empty toggle). Colors match
// WORKOUT_COLORS exactly, so a series looks the same whether it's
// shown in its own dedicated card above or overlaid in the zoom view.
// defaultOn here (HR + Elevation) is only the FALLBACK - openWorkoutZoom
// overrides it per call based on which panel's own button was tapped,
// so this only matters if this function is ever called directly
// without going through that.
//
// Activity Intensity is handled separately from the other 5 - it
// comes from `intensitySeries` (get_workout_raw_intensity's own
// {"<device>": [{t,v}]} shape, the watch's own always-on background
// stream), not from `samples` (a GPX/FIT export correlated by tag) -
// so it needs its own per-device flattening rather than fitting the
// shared per-sample-field pattern the other 5 all follow. Only the
// FIRST reporting device's points are used, same "realistically
// single-device" simplification used elsewhere in this app (e.g.
// sleep-overview.js's own HR/respiratory zoom series).
function buildWorkoutZoomSeries(workout, samples, intensitySeries = {}) {
  const fieldConfigs = [
    { key: "hr", label: "Heart Rate", color: WORKOUT_COLORS.hr, unit: "bpm", field: "hr", convert: v => v, decimals: 0, defaultOn: true },
    { key: "elevation", label: "Elevation", color: WORKOUT_COLORS.elevation, unit: "m", field: "altitude_m", convert: v => v, decimals: 0, defaultOn: true },
    { key: "speed", label: "Speed", color: WORKOUT_COLORS.speed, unit: "mph", field: "speed_mps", convert: v => v * MPS_TO_MPH, decimals: 1, defaultOn: false },
    { key: "cadence", label: "Cadence", color: WORKOUT_COLORS.cadence, unit: "spm", field: "cadence_rpm", convert: v => v, decimals: 0, defaultOn: false },
    { key: "stride", label: "Stride", color: WORKOUT_COLORS.stride, unit: "in", field: "step_length_mm", convert: v => v / 10 * CM_TO_INCHES, decimals: 1, defaultOn: false },
  ];
  const series = fieldConfigs
    .map(c => ({
      key: c.key,
      label: c.label,
      color: c.color,
      unit: c.unit,
      decimals: c.decimals,
      defaultOn: c.defaultOn,
      points: samples.filter(p => p[c.field] !== undefined).map(p => ({ t: p.time, v: c.convert(p[c.field]) })),
    }))
    .filter(s => s.points.length > 0);

  const intensityPoints = Object.values(intensitySeries)[0] || [];
  if (intensityPoints.length > 0) {
    series.push({
      key: "intensity",
      label: "Activity Intensity",
      color: WORKOUT_COLORS.intensity,
      unit: "",
      decimals: 0,
      defaultOn: false,
      points: intensityPoints,
    });
  }

  return series;
}


// Renders the real GPS track on a Leaflet map (OpenStreetMap tiles,
// loaded via CDN in index.html - the person's own explicit choice
// over a track-only/no-basemap rendering) - does nothing if the map
// container doesn't exist (renderGpsMapCard already decided this
// workout has no GPS data at all).
function renderGpsMap(samples) {
  const container = document.getElementById("workout-map");
  if (!container) return;
  const points = samples
    .filter(p => p.latitude !== undefined && p.longitude !== undefined)
    .map(p => [p.latitude, p.longitude]);
  if (points.length === 0) return;

  const map = L.map(container, { attributionControl: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "\u00a9 OpenStreetMap contributors",
  }).addTo(map);

  const polyline = L.polyline(points, { color: WORKOUT_COLORS.gpsTrack, weight: 4 }).addTo(map);
  map.fitBounds(polyline.getBounds(), { padding: [16, 16] });
}
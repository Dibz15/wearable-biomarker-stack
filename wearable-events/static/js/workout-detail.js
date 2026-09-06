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
import { buildLineChart, buildCategoryPieChart } from "./metric-charts.js";

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
function renderPerSampleChartCard(canvasId, title, samples, field) {
  const hasAny = samples.some(p => p[field] !== undefined);
  if (!hasAny) return "";
  return `
    <p class="today-section-label">${escapeHtml(title)}</p>
    <div class="detail-chart-card">
      <canvas id="${canvasId}"></canvas>
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
    </div>
  ` : "";

  return `
    <p class="today-section-label">Stride</p>
    ${statsHtml}
    ${chartHtml}
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

  // Both fetched up front, in parallel, before any of the per-sample/
  // per-lap sections are rendered - lets renderPerSampleChartCard/
  // renderGpsMapCard/renderLapsTable decide up front whether a given
  // section has anything to show at all (and skip it entirely,
  // rather than rendering a card and then immediately replacing it),
  // and avoids the earlier version's own pattern of only fetching HR
  // after the rest of the page was already drawn.
  let samples = [];
  let laps = [];
  if (workout.hr_avg !== null && workout.hr_avg !== undefined) {
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
  }

  content.innerHTML = `
    <p class="metric-sub" style="margin-bottom: 0.75rem;">${escapeHtml(dateLabel)} \u00b7 ${escapeHtml(workout.device || "")}</p>
    ${renderStatsRow(workout, samples)}
    ${renderHeartRateSummary(workout)}
    ${renderHeartRateZones(workout.hr_zones)}
    ${renderTrainingEffectCard(workout)}
    ${renderGpsMapCard(samples)}
    ${renderElevationPanel(workout, samples)}
    ${renderGradientDistribution(workout)}
    ${renderPerSampleChartCard("workout-speed-chart", "Speed", samples, "speed_mps")}
    ${renderCadencePanel(workout, samples)}
    ${renderStridePanel(workout, samples)}
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
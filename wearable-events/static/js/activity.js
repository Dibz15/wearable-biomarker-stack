// --- Activity page (opened from a Today card, not a tab) ---
import { escapeHtml, api, todayISO } from "./core.js";
import {
  openDetailScreen, registerActiveChart, clearActiveCharts,
  renderDateNav, renderPeriodButtons, wireDetailControls,
} from "./metric-detail.js";
import {
  buildRangeBarChart, buildTieredBarChart, buildStackedMinutesChart,
  buildActivityTimeChart, renderTierLegend,
} from "./metric-charts.js";

// Our own bands, anchored on real confirmed data (STAND_INTENSITY_THRESHOLD=50,
// confirmed against the watch's own hourly Stand display; the ~0-255
// raw scale, confirmed against real values) - not an official Zepp/
// Gadgetbridge scheme the way Stress's tiers are. See
// parser/activefit/FIELD_RESEARCH.md for the full reasoning behind
// these specific boundaries.
const INTENSITY_BANDS = [
  { max: 24, label: "Resting", color: "#6ea8fe" },
  { max: 49, label: "Light", color: "#6ecf97" },
  { max: 99, label: "Active", color: "#f0c674" },
  { max: 255, label: "Vigorous", color: "#e88a8a" },
];

// A single flat band spanning the whole range - reuses
// buildTieredBarChart's per-point bar rendering (sparse, discrete
// bars, one per actual reading, not hourly-aggregated) for the Steps
// day chart without needing a near-identical function just to drop
// the tier-coloring UI_DESIGN_NOTES.md never asked for on this chart.
const STEPS_FLAT_BAND = [{ max: Infinity, label: "Steps", color: "#6ea8fe" }];

export function formatMinutes(min) {
  const hr = Math.floor(min / 60);
  const rest = min % 60;
  return hr > 0 ? `${hr}h ${rest}m` : `${rest}m`;
}

// "outdoor_running" -> "Outdoor Running" - only used for known labels
// (never "unknown", which is handled separately in renderSessionLabel
// so it isn't title-cased into "Unknown" twice over).
function titleCase(label) {
  return label.split("_").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

// Every session shows its raw code alongside the label, known or not -
// "outdoor_running" is a real decoded name but confirmed unreliable
// for this device (see FIELD_RESEARCH.md), so it doesn't get shown
// with any more confidence than an unmapped code. Keeping the raw
// code visible either way is also what makes it possible to build an
// eventual internal map by eye, the same way the "Hybrid training"
// correspondence was found.
function renderSessionLabel(session) {
  const hasKnownLabel = session.label && session.label !== "unknown";
  const text = hasKnownLabel ? titleCase(session.label) : "Unknown";
  const code = (session.raw_code === null || session.raw_code === undefined) ? "" : ` (${session.raw_code})`;
  return `${escapeHtml(text)}${escapeHtml(code)}`;
}

function renderSessionList(sessions) {
  if (!sessions.length) {
    return `<p class="metric-card-empty">No activity sessions recorded for this day</p>`;
  }
  const rows = sessions.map(s => {
    const start = new Date(s.start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const end = new Date(s.end).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const hrText = (s.avg_heart_rate === null || s.avg_heart_rate === undefined) ? "" : `${s.avg_heart_rate} bpm avg`;
    return `
      <div class="activity-session-row">
        <div class="activity-session-main">
          <span class="activity-session-label">${renderSessionLabel(s)}</span>
          <span class="metric-sub">${escapeHtml(start)} \u2013 ${escapeHtml(end)}</span>
        </div>
        <div class="activity-session-side">
          ${hrText ? `<span class="metric-sub">${escapeHtml(hrText)}</span>` : ""}
          <span class="metric-device-name">${escapeHtml(s.device)}</span>
        </div>
      </div>
    `;
  }).join("");
  return `<div class="activity-session-list">${rows}</div>`;
}

function replaceWithEmptyState(canvasId, message) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  canvas.replaceWith(Object.assign(document.createElement("p"), {
    className: "metric-card-empty", textContent: message,
  }));
}

export async function openActivityDetail(anchorDate = todayISO()) {
  openDetailScreen("Activity");
  await renderActivityPeriod("day", anchorDate);
}

async function renderActivityPeriod(period, anchorDate) {
  const content = document.getElementById("detail-content");
  content.innerHTML = renderPeriodButtons(period, null) + renderDateNav(period, anchorDate) + `<p class="muted">Loading...</p>`;
  wireActivityControls(period, anchorDate);

  clearActiveCharts();

  try {
    if (period === "day") {
      await renderActivityDay(anchorDate);
    } else {
      await renderActivityRange(period, anchorDate);
    }
  } catch (e) {
    content.innerHTML = renderPeriodButtons(period, null) + renderDateNav(period, anchorDate) + `<p class="status">Error loading activity data: ${escapeHtml(e.message)}</p>`;
    wireActivityControls(period, anchorDate);
  }
}

function wireActivityControls(period, anchorDate) {
  wireDetailControls((p, d) => renderActivityPeriod(p, d), period, anchorDate);
}

async function renderActivityDay(anchorDate) {
  const content = document.getElementById("detail-content");

  const [intensitySeries, stepsSeries, sittingMinutes, stoodHours, hourlyBreakdown, sessions] = await Promise.all([
    api(`/today/series/raw_intensity?date=${anchorDate}`),
    api(`/today/series/steps?date=${anchorDate}`),
    api(`/activity/sitting-minutes?date=${anchorDate}`),
    api(`/activity/stood-hours?date=${anchorDate}`),
    api(`/activity/hourly-breakdown?date=${anchorDate}`),
    api(`/activity/sessions?date=${anchorDate}`),
  ]);

  const intensityDevices = Object.keys(intensitySeries);
  const stepsDevices = Object.keys(stepsSeries);
  const hourlyDevices = Object.keys(hourlyBreakdown);

  // The two quick stat cards use whichever device reported anything
  // today - this app is realistically single-device right now (the
  // ring has been unbound, see FIELD_RESEARCH.md), so picking the
  // first reporting device rather than trying to merge multiple
  // devices' minutes into one combined number.
  const statsDevice = intensityDevices[0] || stepsDevices[0];
  const sittingVal = statsDevice ? (sittingMinutes[statsDevice] ?? 0) : null;
  const stoodVal = statsDevice ? (stoodHours[statsDevice] ?? 0) : null;

  content.innerHTML = renderPeriodButtons("day", null) + renderDateNav("day", anchorDate) + `
    <p class="today-section-label">Activity</p>
    <div class="detail-chart-card">
      <canvas id="activity-intensity-chart"></canvas>
    </div>
    ${renderTierLegend(INTENSITY_BANDS, "")}

    <p class="today-section-label">Steps</p>
    <div class="detail-chart-card">
      <canvas id="activity-steps-chart"></canvas>
    </div>

    <div class="activity-stats-row">
      <div class="activity-stat-item">
        <span class="activity-stat-label">Sitting Time</span>
        <span class="activity-stat-value">${sittingVal === null ? "\u2013" : formatMinutes(sittingVal)}</span>
      </div>
      <div class="activity-stat-item">
        <span class="activity-stat-label">Hours Stood</span>
        <span class="activity-stat-value">${stoodVal === null ? "\u2013" : stoodVal}</span>
      </div>
    </div>

    <p class="today-section-label">Sitting vs Standing</p>
    <div class="detail-chart-card">
      <canvas id="activity-hourly-chart"></canvas>
    </div>

    <p class="today-section-label">Activities Today</p>
    ${renderSessionList(sessions)}
  `;

  wireActivityControls("day", anchorDate);

  if (intensityDevices.length > 0) {
    registerActiveChart(buildTieredBarChart(
      document.getElementById("activity-intensity-chart"), intensitySeries, intensityDevices,
      { bands: INTENSITY_BANDS, yMax: 255, unit: "", decimals: 0 }
    ));
  } else {
    replaceWithEmptyState("activity-intensity-chart", "No data for this day");
  }

  if (stepsDevices.length > 0) {
    registerActiveChart(buildTieredBarChart(
      document.getElementById("activity-steps-chart"), stepsSeries, stepsDevices,
      { bands: STEPS_FLAT_BAND, yMax: null, unit: "", decimals: 0 }
    ));
  } else {
    replaceWithEmptyState("activity-steps-chart", "No data for this day");
  }

  if (hourlyDevices.length > 0) {
    registerActiveChart(buildStackedMinutesChart(
      document.getElementById("activity-hourly-chart"), hourlyBreakdown, hourlyDevices,
      { labelFormat: "hour" }
    ));
  } else {
    replaceWithEmptyState("activity-hourly-chart", "No data for this day");
  }
}

async function renderActivityRange(period, anchorDate) {
  const content = document.getElementById("detail-content");

  const [stepsSeries, timeRangeSeries] = await Promise.all([
    api(`/vitals/range/steps?period=${period}&end_date=${anchorDate}`),
    api(`/activity/time-range?period=${period}&end_date=${anchorDate}`),
  ]);

  const stepsDevices = Object.keys(stepsSeries);
  const timeRangeDevices = Object.keys(timeRangeSeries);
  const labelFormat = period === "year" ? "month" : "date";

  content.innerHTML = renderPeriodButtons(period, null) + renderDateNav(period, anchorDate) + `
    <p class="today-section-label">Steps</p>
    <div class="detail-chart-card">
      <canvas id="activity-range-steps-chart"></canvas>
    </div>

    <p class="today-section-label">Total Activity Time</p>
    <div class="detail-chart-card">
      <canvas id="activity-range-time-chart"></canvas>
    </div>

    <p class="today-section-label">Sitting vs Standing</p>
    <div class="detail-chart-card">
      <canvas id="activity-range-stacked-chart"></canvas>
    </div>
  `;

  wireActivityControls(period, anchorDate);

  if (stepsDevices.length > 0) {
    registerActiveChart(buildRangeBarChart(
      document.getElementById("activity-range-steps-chart"), stepsSeries, stepsDevices, period, {}, undefined, 0
    ));
  } else {
    replaceWithEmptyState("activity-range-steps-chart", "No data for this period");
  }

  if (timeRangeDevices.length > 0) {
    registerActiveChart(buildActivityTimeChart(
      document.getElementById("activity-range-time-chart"), timeRangeSeries, timeRangeDevices, { labelFormat }
    ));
    registerActiveChart(buildStackedMinutesChart(
      document.getElementById("activity-range-stacked-chart"), timeRangeSeries, timeRangeDevices, { labelFormat }
    ));
  } else {
    replaceWithEmptyState("activity-range-time-chart", "No data for this period");
    replaceWithEmptyState("activity-range-stacked-chart", "No data for this period");
  }
}
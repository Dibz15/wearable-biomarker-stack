// --- Sleep tab's objective-data overview (day view) ---
// Separate module from sleep.js (the existing subjective 1-5 journal
// feature, which keeps its own name/tab-loading role) - this renders
// the device-derived content Zepp's own Sleep tab shows ABOVE its
// "Sleep Tags" journal-style input, matching that same top-to-bottom
// order: objective data first, subjective input below it (see
// UI_DESIGN_NOTES.md's "Sleep tab" entry).
import { escapeHtml, api, todayISO, shiftISODate } from "./core.js";
import { renderDateNav } from "./metric-detail.js";
import { buildHypnogramSVG } from "./metric-charts.js";

const SLEEP_STAGE_ORDER = ["deep", "light", "rem", "awake"];
const SLEEP_STAGE_LABELS = { deep: "Deep", light: "Light", rem: "REM", awake: "Awake" };

function formatDuration(totalSeconds) {
  const totalMin = Math.round(totalSeconds / 60);
  const hr = Math.floor(totalMin / 60);
  const min = totalMin % 60;
  return hr > 0 ? `${hr}<span class="unit">hr</span> ${min}<span class="unit">min</span>` : `${min}<span class="unit">min</span>`;
}

// Legend beneath the hypnogram - same per-stage total-minutes
// breakdown as before, still useful alongside the real hypnogram.
function renderHypnogramLegend(segments) {
  const presentStages = SLEEP_STAGE_ORDER.filter(stage => segments.some(s => s.stage === stage));
  return presentStages.map(stage => {
    const stageMin = segments.filter(s => s.stage === stage).reduce((sum, s) => sum + s.duration_min, 0);
    return `
      <span class="sleep-stage-legend-item">
        <span class="sleep-stage-dot sleep-stage-seg ${stage}"></span>
        ${SLEEP_STAGE_LABELS[stage]} ${stageMin}m
      </span>
    `;
  }).join("");
}

function renderQualityMetricRow(label, valueText, meetsThreshold, thresholdText, notMetLabel) {
  const tierClass = meetsThreshold === null ? "" : meetsThreshold ? "sleep-quality-good" : "sleep-quality-attention";
  const tierLabel = meetsThreshold === null ? "" : meetsThreshold ? "Meets guideline" : notMetLabel;
  return `
    <div class="sleep-quality-row">
      <div class="sleep-quality-row-main">
        <span class="sleep-quality-label">${escapeHtml(label)}</span>
        <span class="sleep-quality-value">${escapeHtml(valueText)}</span>
      </div>
      <div class="sleep-quality-row-side">
        ${tierLabel ? `<span class="sleep-quality-tier ${tierClass}">${escapeHtml(tierLabel)}</span>` : ""}
        <span class="sleep-quality-threshold">${escapeHtml(thresholdText)}</span>
      </div>
    </div>
  `;
}

// Deliberately 3 individually-cited metrics, NOT a blended score - see
// FIELD_RESEARCH.md's "Sleep Score" entry for why: no standardized
// composite-scoring formula exists in the literature (confirmed via
// the industry's own ANSI/CTA/NSF-2110 standard), which recommends
// showing individual metrics with their own basis over a single
// opaque number - exactly what this renders.
function renderSleepQuality(quality) {
  if (!quality) return "";
  const bracketLabel = { young_adult: "young adult", adult: "adult", older_adult: "older adult" }[quality.age_bracket] || quality.age_bracket;

  const rows = [
    // Efficiency is a >=-type guideline (higher is better) - not
    // meeting it means the value fell BELOW the threshold, so "Below
    // guideline" is the correct direction here.
    renderQualityMetricRow(
      "Sleep Efficiency",
      quality.efficiency_pct !== null ? `${quality.efficiency_pct}%` : "\u2013",
      quality.efficiency_meets_threshold,
      "Guideline: \u226585%",
      "Below guideline"
    ),
    // WASO and Awakenings are <=-type guidelines (lower is better) -
    // not meeting them means the value came in ABOVE the threshold,
    // the opposite direction from efficiency. Real bug fixed here:
    // this used to say "Below guideline" for these two as well, which
    // is backwards - too much wake time or too many awakenings is
    // "above", not "below", the published guideline.
    renderQualityMetricRow(
      "Wake After Sleep Onset",
      `${quality.waso_min}m`,
      quality.waso_meets_threshold,
      `Guideline: <${quality.age_bracket === "older_adult" ? 30 : 20}m`,
      "Above guideline"
    ),
    renderQualityMetricRow(
      "Awakenings (\u22655min)",
      `${quality.awakenings_5min}`,
      quality.awakenings_meets_threshold,
      `Guideline: \u2264${quality.age_bracket === "older_adult" ? 2 : 1}`,
      "Above guideline"
    ),
  ].join("");

  return `
    <div class="sleep-quality-card">
      <p class="today-section-label">Sleep Quality</p>
      ${rows}
      <p class="sleep-quality-source">
        Individually assessed against published research thresholds (${escapeHtml(quality.source)}),
        ${escapeHtml(bracketLabel)} bracket - not a blended score. No standardized formula for a single
        composite sleep score exists in the literature.
      </p>
    </div>
  `;
}

function renderSleepStatsRow(overview) {
  const wakeHours = Math.floor(overview.duration_s / 3600);
  const wakeMin = Math.round((overview.duration_s % 3600) / 60);
  return `
    <div class="sleep-stats-row">
      <div class="sleep-stat-item">
        <span class="sleep-stat-value">${wakeHours}h ${wakeMin}m</span>
        <span class="sleep-stat-label">Duration</span>
      </div>
      <div class="sleep-stat-item">
        <span class="sleep-stat-value">${overview.wake_events}</span>
        <span class="sleep-stat-label">Wake Events</span>
      </div>
      <div class="sleep-stat-item">
        <span class="sleep-stat-value">${overview.avg_heart_rate !== null ? overview.avg_heart_rate : "\u2013"}</span>
        <span class="sleep-stat-label">Avg HR</span>
      </div>
      <div class="sleep-stat-item">
        <span class="sleep-stat-value">${overview.avg_respiratory_rate !== null ? overview.avg_respiratory_rate : "\u2013"}</span>
        <span class="sleep-stat-label">Avg BRPM</span>
      </div>
    </div>
  `;
}

export async function loadSleepOverview(anchorDate = todayISO()) {
  const container = document.getElementById("sleep-overview");
  container.innerHTML = `${renderDateNav("day", anchorDate)}<p class="muted">Loading...</p>`;
  wireSleepOverviewDateNav(anchorDate);

  try {
    const [overview, hypnogram] = await Promise.all([
      api(`/sleep/overview?date=${anchorDate}`),
      api(`/sleep/hypnogram?date=${anchorDate}`),
    ]);

    if (overview === null) {
      container.innerHTML = `
        ${renderDateNav("day", anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep session recorded for this night.</p>
        </div>
      `;
      wireSleepOverviewDateNav(anchorDate);
      return;
    }

    const hasStageData = hypnogram.length > 0;

    container.innerHTML = `
      ${renderDateNav("day", anchorDate)}
      <div class="sleep-summary-card">
        <div class="sleep-summary-top">
          <span class="sleep-summary-duration">${formatDuration(overview.duration_s)}</span>
          <span class="sleep-summary-date">${escapeHtml(anchorDate)}</span>
        </div>
        ${hasStageData
          ? `<div class="sleep-hypnogram-card">${buildHypnogramSVG(hypnogram, { width: 800, height: 190 })}</div><div class="sleep-stage-legend">${renderHypnogramLegend(hypnogram)}</div>`
          : `<p class="metric-card-empty">No stage data for this night</p>`}
      </div>
      ${renderSleepStatsRow(overview)}
      ${renderSleepQuality(overview.sleep_quality)}
    `;
    wireSleepOverviewDateNav(anchorDate);
  } catch (e) {
    container.innerHTML = `${renderDateNav("day", anchorDate)}<p class="status">Error loading sleep data: ${escapeHtml(e.message)}</p>`;
    wireSleepOverviewDateNav(anchorDate);
  }
}

// Same trimmed-down date-nav pattern as Today's own wireTodayDateNav -
// prev/next/date-picker only, scoped to THIS container specifically
// (see today.js's own wireTodayDateNav docstring for why an unscoped
// document-wide query is a real, previously-hit bug here - the Sleep
// tab sits alongside Today's own date-nav and the detail-screen
// overlay's, both of which render the identical .date-nav-btn markup).
function wireSleepOverviewDateNav(anchorDate) {
  const container = document.getElementById("sleep-overview");
  const prevBtn = container.querySelector('.date-nav-btn[data-nav="prev"]');
  const nextBtn = container.querySelector('.date-nav-btn[data-nav="next"]');
  if (prevBtn) {
    prevBtn.addEventListener("click", () => loadSleepOverview(shiftISODate(anchorDate, -1)));
  }
  if (nextBtn && !nextBtn.disabled) {
    nextBtn.addEventListener("click", () => loadSleepOverview(shiftISODate(anchorDate, 1)));
  }

  const dateInput = container.querySelector(".date-nav-input");
  const dateLabel = container.querySelector(".date-nav-label");
  if (dateInput) {
    dateInput.addEventListener("change", () => {
      if (dateInput.value) loadSleepOverview(dateInput.value);
    });
  }
  if (dateLabel && dateInput) {
    dateLabel.addEventListener("click", (e) => {
      if (typeof dateInput.showPicker === "function") {
        e.preventDefault();
        try {
          dateInput.showPicker();
        } catch (err) {
          // Rare - fallback click-passthrough still works.
        }
      }
    });
  }
}
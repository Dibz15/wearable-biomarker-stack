// --- Today tab ---
import { escapeHtml, api, todayISO, shiftISODate } from "./core.js";
import { openMetricDetail, renderDateNav } from "./metric-detail.js";

const METRIC_FIELDS = [
  { key: "heart_rate", label: "Heart Rate", unit: "bpm", hasDetail: true },
  { key: "hrv", label: "HRV", unit: "ms", hasDetail: true },
  { key: "stress", label: "Stress", unit: "", hasDetail: true },
  { key: "spo2", label: "SpO2", unit: "%", hasDetail: true },
  { key: "temperature", label: "Temperature", unit: "\u00b0", hasDetail: true },
];

const SLEEP_STAGE_ORDER = ["deep", "light", "rem", "awake"];
const SLEEP_STAGE_LABELS = { deep: "Deep", light: "Light", rem: "REM", awake: "Awake" };

function formatDuration(totalSeconds) {
  const totalMin = Math.round(totalSeconds / 60);
  const hr = Math.floor(totalMin / 60);
  const min = totalMin % 60;
  return hr > 0 ? `${hr}<span class="unit">hr</span> ${min}<span class="unit">min</span>` : `${min}<span class="unit">min</span>`;
}

function renderSleepCard(sleep) {
  if (!sleep) {
    return `
      <div class="sleep-summary-card">
        <p class="metric-card-empty">No completed sleep session in the last 7 days.</p>
      </div>
    `;
  }

  const stages = sleep.stages_min || {};
  const totalStageMin = SLEEP_STAGE_ORDER.reduce((sum, s) => sum + (stages[s] || 0), 0);

  const bar = totalStageMin > 0
    ? SLEEP_STAGE_ORDER.filter(s => stages[s] > 0).map(s => {
        const pct = (stages[s] / totalStageMin) * 100;
        return `<div class="sleep-stage-seg ${s}" style="width:${pct}%"></div>`;
      }).join("")
    : "";

  const legend = SLEEP_STAGE_ORDER.filter(s => stages[s] !== undefined).map(s => `
    <span class="sleep-stage-legend-item">
      <span class="sleep-stage-dot sleep-stage-seg ${s}"></span>
      ${SLEEP_STAGE_LABELS[s]} ${stages[s]}m
    </span>
  `).join("");

  return `
    <div class="sleep-summary-card">
      <div class="sleep-summary-top">
        <span class="sleep-summary-duration">${formatDuration(sleep.duration_s)}</span>
        <span class="sleep-summary-date">${escapeHtml(sleep.sleep_date)}</span>
      </div>
      ${bar ? `<div class="sleep-stage-bar">${bar}</div><div class="sleep-stage-legend">${legend}</div>` : ""}
    </div>
  `;
}

function renderMetricCard(field, byDevice) {
  const devices = Object.keys(byDevice);
  const tappableAttrs = field.hasDetail ? ` data-detail-field="${field.key}" role="button" tabindex="0"` : "";
  const cardClass = field.hasDetail ? "metric-card metric-card-tappable" : "metric-card";

  if (devices.length === 0) {
    return `
      <div class="${cardClass}"${tappableAttrs}>
        <div class="metric-card-label">${field.label}</div>
        <p class="metric-card-empty">No data yet today</p>
      </div>
    `;
  }

  const rows = devices.map(device => {
    const stats = byDevice[device];
    const subParts = [];
    if (stats.avg !== undefined) subParts.push(`avg ${stats.avg}`);
    if (stats.min !== undefined && stats.max !== undefined) subParts.push(`${stats.min}\u2013${stats.max}`);
    const sub = subParts.length ? `<div class="metric-sub">${subParts.join(" \u00b7 ")}</div>` : "";
    return `
      <div class="metric-device-row">
        <div class="metric-device-name">${escapeHtml(device)}</div>
        <div class="metric-value">${stats.last}<span class="unit">${field.unit}</span></div>
        ${sub}
      </div>
    `;
  }).join("");

  return `
    <div class="${cardClass}"${tappableAttrs}>
      <div class="metric-card-label">${field.label}</div>
      ${rows}
    </div>
  `;
}

function renderStepsCard(steps) {
  const devices = Object.keys(steps);
  if (devices.length === 0) {
    return `
      <div class="steps-card">
        <div class="metric-card-label">Steps</div>
        <p class="metric-card-empty">No data yet today</p>
      </div>
    `;
  }
  const rows = devices.map(device => `
    <div class="metric-device-row">
      <div class="metric-device-name">${escapeHtml(device)}</div>
      <div class="metric-value">${steps[device].toLocaleString()}</div>
    </div>
  `).join("");
  return `
    <div class="steps-card">
      <div class="metric-card-label">Steps</div>
      ${rows}
    </div>
  `;
}

export async function loadToday(anchorDate = todayISO()) {
  const container = document.getElementById("today-content");
  try {
    const data = await api(`/today?date=${anchorDate}`);

    const sleepHtml = renderSleepCard(data.sleep);
    const stepsHtml = renderStepsCard(data.steps || {});
    const metricsHtml = METRIC_FIELDS.map(f => renderMetricCard(f, (data.vitals || {})[f.key] || {})).join("");

    container.innerHTML = `
      ${renderDateNav("day", anchorDate)}
      <p class="today-section-label">Last night's sleep</p>
      ${sleepHtml}
      <p class="today-section-label">Today</p>
      ${stepsHtml}
      <div class="metric-grid">${metricsHtml}</div>
    `;

    // Wire up whichever cards have a detail view. Delegated on the
    // container rather than one listener per card, since this whole
    // block gets re-rendered (innerHTML replaced) every time loadToday()
    // runs - a per-card listener would need explicit cleanup to avoid
    // piling up duplicates across refreshes.
    container.querySelectorAll("[data-detail-field]").forEach(el => {
      // Opens the detail view on the SAME day currently being viewed
      // here, not always today - tapping "Heart Rate" while looking at
      // three days ago should show that day's heart rate, not jump
      // back to today's.
      const open = () => openMetricDetail(el.dataset.detailField, anchorDate);
      el.addEventListener("click", open);
      el.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    });

    wireTodayDateNav(anchorDate);
  } catch (e) {
    container.innerHTML = `<p class="status">Error loading today's data: ${escapeHtml(e.message)}</p>`;
  }
}

// A trimmed-down version of the detail-view's wireDetailControls -
// prev/next and the date-picker input only, since Today has no D/W/M/Y
// period switcher to also wire up (it's always a single day, just with
// a movable anchor date).
function wireTodayDateNav(anchorDate) {
  const prevBtn = document.querySelector('.date-nav-btn[data-nav="prev"]');
  const nextBtn = document.querySelector('.date-nav-btn[data-nav="next"]');
  if (prevBtn) {
    prevBtn.addEventListener("click", () => loadToday(shiftISODate(anchorDate, -1)));
  }
  if (nextBtn && !nextBtn.disabled) {
    nextBtn.addEventListener("click", () => loadToday(shiftISODate(anchorDate, 1)));
  }

  const dateInput = document.querySelector(".date-nav-input");
  const dateLabel = document.querySelector(".date-nav-label");
  if (dateInput) {
    dateInput.addEventListener("change", () => {
      if (dateInput.value) loadToday(dateInput.value);
    });
  }
  if (dateLabel && dateInput) {
    // Same showPicker() fix as the detail views - current Chrome only
    // opens a date input's native picker when the calendar-icon
    // affordance itself is clicked, not "anywhere in the input" the
    // way it used to, and this input is invisible (opacity: 0,
    // covering the label).
    dateLabel.addEventListener("click", (e) => {
      if (typeof dateInput.showPicker === "function") {
        e.preventDefault();
        try {
          dateInput.showPicker();
        } catch (err) {
          // Rare - the person can still use the fallback click-
          // passthrough path.
        }
      }
    });
  }
}
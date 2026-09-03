// --- tab switching ---
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

function escapeHtml(str) {
  // Any place user-typed or externally-sourced text (a regex keyword
  // rule pattern, a calendar name, a tag, a username, an ICS event
  // title from someone else's calendar invite) gets inserted into a
  // template literal that's later assigned to .innerHTML MUST go
  // through this first. Without it, the browser's HTML parser reads
  // characters like < > & as markup rather than literal text - a
  // regex pattern like <\d{3}> gets parsed as an (invalid) HTML tag
  // and silently vanishes from what's rendered, which is exactly the
  // "characters disappear" bug this fixes. Not primarily a security
  // fix (this is a single-user, self-hosted app) - it's a display-
  // correctness fix, since the disappearing content was never
  // executable, just mis-parsed as markup instead of text.
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  // /auth/login's own 401 means "wrong credentials", not "your session
  // expired" - you're never in a session yet at that point, so treating
  // it the same way masks the real reason (e.g. "invalid username or
  // password") behind a misleading message. Let it fall through to the
  // generic error handling below instead.
  if (resp.status === 401 && path !== "/auth/login") {
    // Session expired or was revoked mid-use - drop back to the login
    // screen rather than leaving the UI in a broken half-authenticated
    // state. showLogin is defined later in this file but hoisted since
    // it's a function declaration.
    showLogin();
    throw new Error("session expired - please log in again");
  }
  if (!resp.ok) {
    throw new Error(data.detail || `Request failed (${resp.status})`);
  }
  return data;
}

// --- Today tab ---

const METRIC_FIELDS = [
  { key: "heart_rate", label: "Heart Rate", unit: "bpm", hasDetail: true },
  { key: "hrv", label: "HRV", unit: "ms", hasDetail: true },
  { key: "stress", label: "Stress", unit: "" },
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

async function loadToday() {
  const container = document.getElementById("today-content");
  try {
    const data = await api("/today");

    const sleepHtml = renderSleepCard(data.sleep);
    const stepsHtml = renderStepsCard(data.steps || {});
    const metricsHtml = METRIC_FIELDS.map(f => renderMetricCard(f, (data.vitals || {})[f.key] || {})).join("");

    container.innerHTML = `
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
      const open = () => openMetricDetail(el.dataset.detailField);
      el.addEventListener("click", open);
      el.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    });
  } catch (e) {
    container.innerHTML = `<p class="status">Error loading today's data: ${escapeHtml(e.message)}</p>`;
  }
}

// --- Metric detail views (opened from a Today card, not a tab) ---

let activeCharts = [];

function openDetailScreen(title) {
  document.getElementById("detail-title").textContent = title;
  document.getElementById("detail-screen").style.display = "block";
}

function closeDetailScreen() {
  document.getElementById("detail-screen").style.display = "none";
  activeCharts.forEach(c => c.destroy());
  activeCharts = [];
}

document.getElementById("detail-back-btn").addEventListener("click", closeDetailScreen);

// Each view can plot more than one field (e.g. Heart Rate's page also
// shows Resting Heart Rate below it) - each entry in `charts` becomes
// its own card.
const DETAIL_VIEWS = {
  heart_rate: {
    title: "Heart Rate",
    charts: [
      { field: "heart_rate", label: "Heart Rate" },
      { field: "resting_heart_rate", label: "Resting Heart Rate", showBaseline: true },
    ],
  },
  hrv: {
    title: "HRV",
    charts: [
      {
        field: "hrv",
        label: "HRV",
        showBaseline: true,
        baseline: {
          lowLabel: "Lower",
          highLabel: "Higher",
          unit: "ms",
          // 1.5 standard deviations is a reasonable-but-not-clinically-
          // validated line for "worth noting" - there's no settled
          // consensus on exactly how much day-to-day HRV drift signals
          // stress specifically, so this is a starting point, easy to
          // tune later, not a claim of medical precision.
          driftThreshold: 1.5,
          lowClue: "Lower than your usual \u2013 can reflect stress, poor sleep, illness, or fatigue",
          highClue: "Higher than your usual \u2013 often a sign of good recovery",
          normalClue: "Within your normal range",
        },
      },
    ],
  },
  spo2: {
    title: "SpO2",
    charts: [
      {
        field: "spo2",
        label: "SpO2",
        // SpO2 is only sampled when the wearer is already still (see
        // RANGE_PERIODS' own comment on this) - a continuous line for
        // the day view would draw misleading straight segments across
        // long gaps during activity. Hourly bars (matching Zepp's own
        // SpO2 day view) leave a quiet hour as a simple gap instead.
        dayViewStyle: "bars",
        // SpO2 never meaningfully varies below the 90s in a healthy
        // reading - auto-scaling the y-axis down to 0 would compress
        // the whole visible range into a sliver at the top of the
        // chart. 75% floors it well below anything except a genuinely
        // serious reading, while leaving real day-to-day variation
        // clearly visible.
        yMin: 75,
        showBaseline: true,
        baseline: {
          lowLabel: "Lower",
          highLabel: "Higher",
          unit: "%",
          // Same starting-point reasoning as HRV's threshold - not a
          // clinically validated line, just a reasonable default.
          // SpO2's healthy range is naturally much tighter than HRV's
          // though, so a given z-score here reflects a smaller
          // absolute % swing - worth keeping in mind if this ever
          // needs retuning independently of HRV's threshold.
          driftThreshold: 1.5,
          lowClue: "Lower than your usual \u2013 worth keeping an eye on; can reflect poor sleep, altitude, or a respiratory issue",
          highClue: "Higher than your recent average",
          normalClue: "Within your normal range",
        },
      },
    ],
  },
  temperature: {
    title: "Temperature",
    // No Year here, unlike every other view - deliberately, matching
    // Zepp's own temperature page. Skin temperature is a relative
    // marker that swings with environment, meals, and exercise (its
    // own in-app description, per the person's research) - the whole
    // "differential from recent baseline" concept below is inherently
    // a short-window comparison, and a year of daily deltas against a
    // rolling 7-day baseline would just be noise, not signal.
    periods: ["day", "week", "month"],
    charts: [
      { field: "temperature", label: "Temperature" },
    ],
    // A second, PARALLEL section (not just another entry in `charts`)
    // - Day shows a single point-in-time comparison (reusing the same
    // baseline bar HRV/SpO2 already use), but Week/Month show a whole
    // TREND of past deltas, a genuinely different thing the regular
    // per-chart pipeline (fetch raw values, render a chart) doesn't
    // represent - see renderDetailPeriod's own comment on this.
    differential: {
      field: "temperature",
      label: "Change from Baseline",
      unit: "\u00b0",
      baseline: {
        lowLabel: "Cooler",
        highLabel: "Warmer",
        unit: "\u00b0",
        // Unlike HRV/SpO2's threshold (a statistical z-score with no
        // settled convention), this one is a DIRECT match to Zepp's
        // own confirmed "optimal" band - not a guess.
        driftThreshold: 0.5,
        lowClue: "Cooler than your recent baseline",
        highClue: "Warmer than your recent baseline \u2013 can precede illness, poor recovery, or reflect a warm sleep environment",
        normalClue: "Within your optimal range",
      },
      // Confirmed thresholds (+-0.5 / +-1.0 / +-1.5) come from the
      // person's own reading of the Zepp app. The band LABELS are
      // NOT confirmed beyond "optimal" for the first tier - Zepp's own
      // wording for the middle band wasn't legible when this was
      // written. "Notable"/"Significant" are reasonable placeholders,
      // not verified matches - worth revisiting if the real wording
      // ever gets confirmed.
      bands: [
        { threshold: 0.5, label: "Optimal", color: "#6ecf97" },
        { threshold: 1.0, label: "Notable", color: "#f0c674" },
        { threshold: 1.5, label: "Significant", color: "#e88a8a" },
      ],
    },
  },
};

// Distinct colors per device dataset on the chart - cycles if there
// are ever more devices than colors defined here, rather than erroring.
const DEVICE_CHART_COLORS = ["#e88a8a", "#6ea8fe", "#4fd8b8", "#f0c674"];

const DETAIL_PERIODS = [
  { key: "day", label: "D" },
  { key: "week", label: "W" },
  { key: "month", label: "M" },
  { key: "year", label: "Y" },
];

// Days to shift the anchor date by one prev/next step, per period -
// matches each period's own window size (RANGE_PERIODS on the backend),
// so "previous" moves a whole week/month/year at a time, not just a day.
const PERIOD_SHIFT_DAYS = { day: 1, week: 7, month: 30, year: 365 };

// Local-timezone-safe YYYY-MM-DD helpers. Deliberately NOT using
// Date.toISOString() for this - it always converts to UTC first, which
// silently shifts the date near local midnight (e.g. 11pm local on
// Sep 3 in a timezone behind UTC becomes "Sep 4" after the UTC
// conversion). Date's getFullYear()/getMonth()/getDate() are local-
// timezone-aware, so building the string from those avoids that.
function dateToISO(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function isoToDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d); // local midnight, not UTC
}

function todayISO() {
  return dateToISO(new Date());
}

function shiftISODate(iso, days) {
  const d = isoToDate(iso);
  d.setDate(d.getDate() + days);
  return dateToISO(d);
}

function renderPeriodButtons(activePeriod, availablePeriods) {
  const periods = availablePeriods
    ? DETAIL_PERIODS.filter(p => availablePeriods.includes(p.key))
    : DETAIL_PERIODS;
  const buttons = periods.map(p => `
    <button class="period-btn${p.key === activePeriod ? " active" : ""}" data-period="${p.key}">${p.label}</button>
  `).join("");
  return `<div class="period-switcher">${buttons}</div>`;
}

function renderDateNav(period, anchorDate) {
  const isToday = anchorDate === todayISO();
  let label;
  if (period === "day") {
    label = isToday ? "Today" : isoToDate(anchorDate).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  } else {
    const startISO = shiftISODate(anchorDate, -(PERIOD_SHIFT_DAYS[period] - 1));
    const startLabel = isoToDate(startISO).toLocaleDateString([], { month: "short", day: "numeric" });
    const endLabel = isoToDate(anchorDate).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
    label = `${startLabel} \u2013 ${endLabel}`;
  }
  // Navigating past today doesn't make sense (no future data) - once
  // the anchor date IS today, "next" is disabled rather than silently
  // returning an empty period.
  const nextDisabled = anchorDate >= todayISO();
  return `
    <div class="date-nav">
      <button class="date-nav-btn" data-nav="prev" aria-label="Previous">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
      </button>
      <label class="date-nav-label">
        <span>${escapeHtml(label)}</span>
        <input type="date" class="date-nav-input" data-current-anchor="${anchorDate}" value="${anchorDate}" max="${todayISO()}">
      </label>
      <button class="date-nav-btn" data-nav="next" aria-label="Next"${nextDisabled ? " disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
      </button>
    </div>
  `;
}

async function openMetricDetail(viewKey) {
  const view = DETAIL_VIEWS[viewKey];
  if (!view) return; // no detail view wired up for this field yet

  openDetailScreen(view.title);
  await renderDetailPeriod(view, "day", todayISO());
}

function computeStatsFromSeries(series, period) {
  // Stats computed client-side from whatever data is already fetched
  // for the current chart/period, rather than depending on a separate
  // pre-fetched prop (Today's vitals summary, which doesn't cover
  // resting_heart_rate or any non-day period anyway) - one code path
  // that works the same way regardless of which field or period is
  // being viewed.
  const stats = {};
  for (const [device, points] of Object.entries(series)) {
    if (!points.length) continue;
    // Detect the actual shape rather than trusting `period === "day"` -
    // a chart with dayViewStyle:"bars" (SpO2) fetches range-bar-shaped
    // data ({t, min, max, median}) for its day view too, same as
    // week/month, not the raw-point shape ({t, v}) plain "day" used to
    // always mean.
    const isRawPoints = "v" in points[0];
    if (isRawPoints) {
      const values = points.map(p => p.v);
      stats[device] = {
        last: points[points.length - 1].v,
        min: Math.min(...values),
        max: Math.max(...values),
      };
    } else {
      stats[device] = {
        min: Math.min(...points.map(p => p.min)),
        max: Math.max(...points.map(p => p.max)),
      };
    }
  }
  return stats;
}

function renderStatsCard(stats) {
  const devices = Object.keys(stats);
  if (devices.length === 0) return "";
  const rows = devices.map(device => {
    const s = stats[device];
    const valueText = s.last !== undefined ? s.last : `${s.min}\u2013${s.max}`;
    const subText = s.last !== undefined ? `min ${s.min} \u00b7 max ${s.max}` : "";
    return `
      <div class="detail-stat-row">
        <span class="metric-device-name">${escapeHtml(device)}</span>
        <span class="detail-stat-value">${valueText}</span>
        <span class="metric-sub">${subText}</span>
      </div>
    `;
  }).join("");
  return `<div class="detail-stats-card">${rows}</div>`;
}

const BASELINE_DAYS = 7;
const BASELINE_Z_CAP = 2;

function renderDriftClue(z, threshold, lowClue, highClue, normalClue) {
  // Only rendered when the caller opted in with a threshold - Resting
  // Heart Rate doesn't set one, so its bar stays exactly as it was
  // (no clue text, no behavior change) while HRV gets this extra line.
  let text, cls;
  if (Math.abs(z) < threshold) {
    text = normalClue;
    cls = "drift-normal";
  } else if (z > 0) {
    text = highClue;
    cls = "drift-high";
  } else {
    text = lowClue;
    cls = "drift-low";
  }
  if (!text) return "";
  return `<p class="baseline-clue ${cls}">${escapeHtml(text)}</p>`;
}

function renderBaselineBar(comparisonByDevice, baselineDays, config = {}) {
  // Defaults match Resting Heart Rate's original hardcoded behavior
  // exactly - generalized so HRV (Lower/Higher, ms, plus a drift clue)
  // can reuse the same rendering and z-score-capping logic rather than
  // a near-duplicate function.
  const {
    lowLabel = "Slower",
    highLabel = "Faster",
    unit = "bpm",
    driftThreshold = null,
    lowClue = null,
    highClue = null,
    normalClue = null,
  } = config;

  const devices = Object.keys(comparisonByDevice);
  if (devices.length === 0) {
    return `
      <div class="baseline-card">
        <p class="metric-card-empty">Not enough history yet to compare (need at least 2 days)</p>
      </div>
    `;
  }
  const rows = devices.map(device => {
    const c = comparisonByDevice[device];
    // Clamp to +-BASELINE_Z_CAP standard deviations before mapping to
    // the bar's width, so a rare big swing pins to the end of the bar
    // rather than going off-scale - the person's own suggested approach.
    const clampedZ = Math.max(-BASELINE_Z_CAP, Math.min(BASELINE_Z_CAP, c.z));
    const pct = 50 + (clampedZ / BASELINE_Z_CAP) * 50;
    const deltaText = c.delta > 0 ? `+${c.delta}` : `${c.delta}`;
    const clueHtml = driftThreshold !== null ? renderDriftClue(c.z, driftThreshold, lowClue, highClue, normalClue) : "";
    return `
      <div class="baseline-row">
        <div class="metric-device-name">${escapeHtml(device)}</div>
        <div class="baseline-track">
          <div class="baseline-center-tick"></div>
          <div class="baseline-marker" style="left: ${pct}%"></div>
        </div>
        <div class="baseline-labels">
          <span>${escapeHtml(lowLabel)}</span>
          <span class="baseline-delta">${deltaText} ${escapeHtml(unit)} vs ${baselineDays}-day avg (${c.baseline_mean})</span>
          <span>${escapeHtml(highLabel)}</span>
        </div>
        ${clueHtml}
      </div>
    `;
  }).join("");
  return `<div class="baseline-card">${rows}</div>`;
}

async function renderDetailPeriod(view, period, anchorDate) {
  const content = document.getElementById("detail-content");
  content.innerHTML = renderPeriodButtons(period, view.periods) + renderDateNav(period, anchorDate) + `<p class="muted">Loading...</p>`;
  wireDetailControls(view, period, anchorDate);

  activeCharts.forEach(c => c.destroy());
  activeCharts = [];

  // Every chart's series fetches in parallel. On the day view, charts
  // with a baseline comparison (currently just Resting Heart Rate)
  // also fetch their baseline comparison alongside - a bar comparing
  // "today" against a trailing average only makes sense for a single
  // day's value, not a week/month/year range, so it's day-only. On
  // week/month, also fetch the 7-day rolling mean overlay - not
  // supported (or requested) for year, since it doesn't map onto
  // monthly bars.
  //
  // A view's optional `differential` section (temperature so far) is
  // fetched separately from the regular charts array, since it's a
  // genuinely different thing per period: on Day it's the SAME single
  // today-vs-baseline comparison /vitals/baseline already provides
  // (reused, not refetched under a different name); on Week/Month it's
  // a whole TREND of past deltas from a new, dedicated endpoint
  // (/vitals/differential) - not something the existing per-chart
  // fetch/render pipeline (keyed by field, one fetch shape per field)
  // could represent without either fetch colliding with the raw-value
  // chart sharing the same field, or forcing every other chart through
  // a shape it doesn't need.
  let seriesByField;
  let baselineByField = {};
  let rollingMeanByField = {};
  let differentialBaseline = null;
  let differentialSeries = null;
  try {
    seriesByField = Object.fromEntries(await Promise.all(
      view.charts.map(async c => [c.field, await fetchDetailSeries(c, period, anchorDate)])
    ));
    if (period === "day") {
      const baselineCharts = view.charts.filter(c => c.showBaseline);
      baselineByField = Object.fromEntries(await Promise.all(
        baselineCharts.map(async c => [c.field, await api(`/vitals/baseline/${c.field}?days=${BASELINE_DAYS}&date=${anchorDate}`)])
      ));
      if (view.differential) {
        differentialBaseline = await api(`/vitals/baseline/${view.differential.field}?days=${BASELINE_DAYS}&date=${anchorDate}`);
      }
    } else if (period === "week" || period === "month") {
      rollingMeanByField = Object.fromEntries(await Promise.all(
        view.charts.map(async c => [c.field, await api(`/vitals/rolling-mean/${c.field}?period=${period}&end_date=${anchorDate}`)])
      ));
      if (view.differential) {
        differentialSeries = await api(`/vitals/differential/${view.differential.field}?period=${period}&end_date=${anchorDate}`);
      }
    }
  } catch (e) {
    content.innerHTML = renderPeriodButtons(period, view.periods) + renderDateNav(period, anchorDate) + `<p class="status">Error loading chart data: ${escapeHtml(e.message)}</p>`;
    wireDetailControls(view, period, anchorDate);
    return;
  }

  const cardsHtml = view.charts.map((c, i) => {
    const series = seriesByField[c.field];
    const stats = computeStatsFromSeries(series, period);
    const baselineHtml = (period === "day" && c.showBaseline)
      ? renderBaselineBar(baselineByField[c.field] || {}, BASELINE_DAYS, c.baseline || {})
      : "";
    return `
      <p class="today-section-label">${c.label}</p>
      <div class="detail-chart-card">
        <canvas id="detail-chart-${i}"></canvas>
      </div>
      ${renderStatsCard(stats)}
      ${baselineHtml}
    `;
  }).join("");

  const differentialHtml = view.differential
    ? `
      <p class="today-section-label">${view.differential.label}</p>
      ${period === "day"
        ? renderBaselineBar(differentialBaseline || {}, BASELINE_DAYS, view.differential.baseline || {})
        : `<div class="detail-chart-card"><canvas id="detail-diff-chart"></canvas></div>${renderBandLegend(view.differential.bands)}`
      }
    `
    : "";

  content.innerHTML = renderPeriodButtons(period, view.periods) + renderDateNav(period, anchorDate) + cardsHtml + differentialHtml;
  wireDetailControls(view, period, anchorDate);

  view.charts.forEach((c, i) => {
    const series = seriesByField[c.field];
    const devices = Object.keys(series);
    const canvas = document.getElementById(`detail-chart-${i}`);
    if (devices.length === 0) {
      canvas.replaceWith(Object.assign(document.createElement("p"), {
        className: "metric-card-empty", textContent: "No data for this period",
      }));
      return;
    }
    // Which chart function to use follows the ACTUAL shape of what was
    // fetched, not the period string - a chart with dayViewStyle:"bars"
    // fetches range-bar-shaped data ({t, min, max, median}) for its day
    // view too (via fetchDetailSeries), same shape week/month already
    // use, so it needs buildRangeBarChart even though period === "day".
    const isRawPoints = "v" in series[devices[0]][0];
    const chart = isRawPoints
      ? buildLineChart(canvas, series, devices)
      : buildRangeBarChart(canvas, series, devices, period, rollingMeanByField[c.field] || {}, c.yMin);
    activeCharts.push(chart);
  });

  if (view.differential && period !== "day") {
    const diffCanvas = document.getElementById("detail-diff-chart");
    const devices = differentialSeries ? Object.keys(differentialSeries) : [];
    if (devices.length === 0) {
      diffCanvas.replaceWith(Object.assign(document.createElement("p"), {
        className: "metric-card-empty", textContent: "Not enough history yet for a trend",
      }));
    } else {
      activeCharts.push(buildDifferentialChart(diffCanvas, differentialSeries, devices, view.differential));
    }
  }
}

function wireDetailControls(view, activePeriod, anchorDate) {
  // Period switch keeps the SAME anchor date - switching from Week to
  // Month while looking at a past date should stay in that same area
  // of history, not snap back to today.
  document.querySelectorAll(".period-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const period = btn.dataset.period;
      if (period === activePeriod) return;
      renderDetailPeriod(view, period, anchorDate);
    });
  });

  const prevBtn = document.querySelector('.date-nav-btn[data-nav="prev"]');
  const nextBtn = document.querySelector('.date-nav-btn[data-nav="next"]');
  const shift = PERIOD_SHIFT_DAYS[activePeriod];
  if (prevBtn) {
    prevBtn.addEventListener("click", () => {
      renderDetailPeriod(view, activePeriod, shiftISODate(anchorDate, -shift));
    });
  }
  if (nextBtn && !nextBtn.disabled) {
    nextBtn.addEventListener("click", () => {
      // Clamp at today - stepping forward from a date within `shift`
      // days of today should land exactly on today, not overshoot
      // into a meaningless future date.
      const next = shiftISODate(anchorDate, shift);
      renderDetailPeriod(view, activePeriod, next > todayISO() ? todayISO() : next);
    });
  }

  const dateInput = document.querySelector(".date-nav-input");
  const dateLabel = document.querySelector(".date-nav-label");
  if (dateInput) {
    dateInput.addEventListener("change", () => {
      if (dateInput.value) renderDetailPeriod(view, activePeriod, dateInput.value);
    });
  }
  if (dateLabel && dateInput) {
    // Current Chrome only opens a date input's native picker when the
    // small calendar-icon affordance itself is clicked, not "anywhere
    // in the input" the way it used to - since this input is invisible
    // (opacity: 0, covering the label), tapping the visible label text
    // would hit the non-icon area and silently do nothing. showPicker()
    // is the purpose-built fix: it opens the native picker regardless
    // of where the trigger was clicked. Falls back to the invisible
    // input's own default click-passthrough behavior (still present,
    // still functional to whatever extent a given browser supports it)
    // in browsers where showPicker() isn't available.
    dateLabel.addEventListener("click", (e) => {
      if (typeof dateInput.showPicker === "function") {
        e.preventDefault();
        try {
          dateInput.showPicker();
        } catch (err) {
          // Rare (e.g. not called as a direct result of the user
          // gesture in some edge case) - nothing more to do here, the
          // person can still use the fallback click-passthrough path.
        }
      }
    });
  }
}

async function fetchDetailSeries(chart, period, anchorDate) {
  // Day view fetches raw per-point data (a continuous line) UNLESS the
  // chart specifically opts into hourly bars for its day view
  // (dayViewStyle: "bars" - SpO2, where readings are sparse enough
  // that a connected line would draw misleading straight segments
  // across the gaps). Everything else (week/month/year, and any
  // chart's default day view) already goes through /vitals/range.
  if (period === "day" && chart.dayViewStyle !== "bars") {
    return api(`/today/series/${chart.field}?date=${anchorDate}`);
  }
  return api(`/vitals/range/${chart.field}?period=${period}&end_date=${anchorDate}`);
}

function buildLineChart(canvas, series, devices) {
  const datasets = devices.map((device, i) => ({
    label: device,
    // Epoch milliseconds, not the raw ISO string - lets Chart.js's
    // 'linear' x-axis handle each device's points on their own actual
    // timestamps (devices don't sample at identical instants) without
    // needing a separate date-adapter library at all (Chart.js's
    // 'time' scale requires one, e.g. chartjs-adapter-date-fns - that
    // adds a dependency with known script-load-order fragility for no
    // real benefit here, since a formatted tick callback on a plain
    // numeric axis gives the same HH:MM labels with one less moving part).
    data: series[device].map(p => ({ x: new Date(p.t).getTime(), y: p.v })),
    borderColor: DEVICE_CHART_COLORS[i % DEVICE_CHART_COLORS.length],
    backgroundColor: "transparent",
    borderWidth: 2,
    // A line needs at least two points to draw anything - a
    // continuous series like heart_rate has plenty, so hiding point
    // markers (pointRadius: 0) keeps that chart clean. But a sparser
    // series (resting_heart_rate is often just one reading a day)
    // can genuinely have only a single point, where there's no line
    // to connect AND no marker - the chart renders completely empty
    // even though the data is there. Show a visible dot specifically
    // for that single-point case, stay clean otherwise.
    pointRadius: series[device].length <= 1 ? 4 : 0,
    tension: 0.25,
  }));

  return new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          type: "linear",
          ticks: {
            color: "#8a8d99",
            callback: (val) => new Date(val).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
          },
          grid: { color: "#2a2d38" },
        },
        y: {
          ticks: { color: "#8a8d99" },
          grid: { color: "#2a2d38" },
        },
      },
      plugins: {
        legend: { display: devices.length > 1, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleTimeString() : "",
          },
        },
      },
    },
  });
}

// Draws a short horizontal dash at each bar's median value, using the
// bar element's OWN computed x-position and width (read after Chart.js
// lays out the bars) rather than a second 'line'-type dataset. A
// 'line' dataset was tried first and had two real problems: (1) by
// default Chart.js draws the FIRST dataset in the array topmost (per
// Chart.js's own docs), so a median dataset added after the bars
// rendered underneath them; (2) a line-type point on a shared category
// axis plots at the CATEGORY's center, not at the position of any one
// grouped bar - so with two devices' bars side by side, the median dot
// landed between them instead of over either bar. Reading the bar
// element's real geometry after afterDatasetsDraw sidesteps both:
// drawing happens after every dataset (always on top), and the x/width
// come directly from wherever Chart.js actually placed that specific
// bar, so multi-device grouping is handled correctly for free.
const medianMarkerPlugin = {
  id: "medianMarkers",
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    const yScale = chart.scales.y;
    chart.data.datasets.forEach((dataset, datasetIndex) => {
      if (!dataset.median) return;
      const meta = chart.getDatasetMeta(datasetIndex);
      if (meta.hidden) return;
      dataset.median.forEach((medianValue, i) => {
        if (medianValue === null || medianValue === undefined) return;
        const barElement = meta.data[i];
        if (!barElement) return;
        const yPixel = yScale.getPixelForValue(medianValue);
        const halfWidth = barElement.width / 2;
        const xCenter = barElement.x;
        ctx.save();
        ctx.strokeStyle = "#e8e9ed";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(xCenter - halfWidth, yPixel);
        ctx.lineTo(xCenter + halfWidth, yPixel);
        ctx.stroke();
        ctx.restore();
      });
    });
  },
};

function buildRangeBarChart(canvas, series, devices, period, rollingMean = {}, yMin) {
  // Floating bars: Chart.js draws a [min, max] pair as a bar spanning
  // that range, rather than a bar from zero - exactly the "vertical
  // range bar per period" pattern from the Zepp research (see
  // wearable-events/UI_DESIGN_NOTES.md's Weekly zoom-level notes).
  // period === "day" only ever reaches this chart (rather than
  // buildLineChart) when a chart opts into hourly bars for its day
  // view (dayViewStyle: "bars"), so this branch is unambiguous.
  const labelFormat = period === "year"
    ? (iso) => new Date(iso).toLocaleDateString([], { month: "short", year: "2-digit" })
    : period === "day"
    ? (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric" })
    : (iso) => new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });

  // Bars are grouped by period start across devices - build one shared
  // label axis from whichever device has the most periods, then look
  // up each device's [min, max] per label (or null if that device has
  // no data for that specific period, so bars don't misalign).
  const allPeriods = [...new Set(devices.flatMap(d => series[d].map(p => p.t)))].sort();
  const labels = allPeriods.map(labelFormat);

  const barDatasets = devices.map((device, i) => {
    const byPeriod = Object.fromEntries(series[device].map(p => [p.t, [p.min, p.max]]));
    const rawData = allPeriods.map(t => byPeriod[t] || null);
    // A day with only a single reading (or a genuinely flat value,
    // e.g. resting_heart_rate is often exactly one reading/day) has
    // min === max - a zero-height floating bar, which Chart.js simply
    // doesn't draw anything for, the same "nothing to draw" problem
    // the single-point line chart had. Pad the DRAWN range slightly
    // so something is always visible, but keep the tooltip showing
    // the real, unpadded values (see the `raw` array + tooltip
    // callback below) rather than silently showing a fabricated wider
    // range as if it were real data.
    const paddedData = rawData.map(pair => {
      if (!pair) return null;
      const [min, max] = pair;
      return min === max ? [min - 0.5, max + 0.5] : pair;
    });
    const byPeriodMedian = Object.fromEntries(series[device].map(p => [p.t, p.median]));
    return {
      label: device,
      data: paddedData,
      raw: rawData,
      median: allPeriods.map(t => byPeriodMedian[t] ?? null),
      backgroundColor: DEVICE_CHART_COLORS[i % DEVICE_CHART_COLORS.length],
      borderRadius: 4,
    };
  });

  // 7-day rolling mean: a genuine connecting line overlaid across the
  // whole chart - week/month only (daily-bucketed), where "7 day"
  // aligns naturally with the bars; not requested/rendered for year
  // (monthly-bucketed - a 7-day mean doesn't map onto a month bar).
  // order: -1 (below the bars' default of 0) so this draws LAST, i.e.
  // on top - Chart.js's own docs describe order as a weight where
  // lower values draw later/on top.
  const rollingDatasets = (period === "week" || period === "month")
    ? devices.filter(d => rollingMean[d] && rollingMean[d].length).map((device) => {
        const byDay = Object.fromEntries(rollingMean[device].map(p => [p.t, p.value]));
        const i = devices.indexOf(device);
        return {
          type: "line",
          label: `${device} 7-day avg`,
          isOverlay: true,
          order: -1,
          data: allPeriods.map(t => (t in byDay ? byDay[t] : null)),
          showLine: true,
          borderColor: DEVICE_CHART_COLORS[i % DEVICE_CHART_COLORS.length],
          borderWidth: 2,
          borderDash: [4, 3],
          pointRadius: 0,
          backgroundColor: "transparent",
          spanGaps: true,
        };
      })
    : [];

  return new Chart(canvas, {
    type: "bar",
    data: { labels, datasets: [...barDatasets, ...rollingDatasets] },
    plugins: [medianMarkerPlugin],
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true },
          grid: { display: false },
        },
        y: {
          ticks: { color: "#8a8d99" },
          grid: { color: "#2a2d38" },
          // A field like SpO2 naturally lives in a narrow high range
          // (mid-90s to 100%) - auto-scaling to include 0 (or even a
          // wide default range) wastes most of the chart's height and
          // makes real, meaningful drops hard to see. Only set when a
          // chart's config actually specifies one (yMin) - other
          // fields keep Chart.js's normal auto-scaling untouched.
          min: yMin,
        },
      },
      plugins: {
        legend: {
          display: devices.length > 1,
          labels: {
            color: "#e8e9ed",
            // Only the bar datasets get their own legend entry - the
            // rolling-mean line is a visual annotation on the same
            // device's bar, not a separate series worth cluttering the
            // legend with (median isn't even a dataset anymore, so it
            // never reaches the legend at all).
            filter: (item, data) => !data.datasets[item.datasetIndex].isOverlay,
          },
        },
        tooltip: {
          callbacks: {
            label: (item) => {
              if (item.dataset.raw) {
                const real = item.dataset.raw[item.dataIndex];
                return real ? `${item.dataset.label}: ${real[0]}\u2013${real[1]}` : "";
              }
              // The rolling-mean overlay dataset doesn't carry a `raw`
              // array (that's specific to the padded-bar workaround
              // above) - fall back to the plotted value directly,
              // which for this dataset IS the real value.
              const v = item.parsed.y;
              return v === null || v === undefined ? "" : `${item.dataset.label}: ${Math.round(v * 10) / 10}`;
            },
          },
        },
      },
    },
  });
}

// Which band a delta falls into, by absolute magnitude - bands are
// given as an ordered list of {threshold, color, label}, checked from
// smallest threshold up; a delta beyond every threshold uses the LAST
// band's color (the most severe one), rather than falling through
// uncolored.
function bandForDelta(delta, bands) {
  const absDelta = Math.abs(delta);
  for (const band of bands) {
    if (absDelta <= band.threshold) return band;
  }
  return bands[bands.length - 1];
}

// A small persistent color key beneath the differential trend chart -
// without this, the only way to learn what a bar's color means is to
// tap it and read the tooltip one bar at a time, which defeats the
// point of a chart meant for a quick "how am I doing" glance.
function renderBandLegend(bands) {
  // toFixed(1) rather than the raw number - JS drops trailing zeros
  // (1.0 stringifies as "1"), which reads as visually inconsistent
  // sitting next to "0.5" in the same legend.
  const fmt = (v) => v.toFixed(1);
  const items = bands.map((band, i) => {
    const prevThreshold = i === 0 ? null : bands[i - 1].threshold;
    const rangeText = i === 0
      ? `within \u00b1${fmt(band.threshold)}\u00b0`
      : i === bands.length - 1
      ? `beyond \u00b1${fmt(prevThreshold)}\u00b0`
      : `\u00b1${fmt(prevThreshold)}\u2013${fmt(band.threshold)}\u00b0`;
    return `
      <div class="band-legend-item">
        <span class="band-swatch" style="background: ${band.color}"></span>
        <span>${escapeHtml(band.label)} (${rangeText})</span>
      </div>
    `;
  }).join("");
  return `<div class="band-legend">${items}</div>`;
}

function buildDifferentialChart(canvas, series, devices, config) {
  // Deliberately colored by SEVERITY BAND, not by device the way every
  // other chart in this app colors its bars - the whole point of this
  // chart is "how far off is this reading", so the color needs to
  // carry that meaning directly rather than just distinguishing which
  // device a bar belongs to. With more than one device, bars still
  // group side by side per day (so two devices' readings for the same
  // night don't overlap), each independently colored by its own delta.
  const allPeriods = [...new Set(devices.flatMap(d => series[d].map(p => p.t)))].sort();
  const labels = allPeriods.map(iso => isoToDate(iso).toLocaleDateString([], { month: "short", day: "numeric" }));

  const datasets = devices.map(device => {
    const byPeriod = Object.fromEntries(series[device].map(p => [p.t, p]));
    const points = allPeriods.map(t => byPeriod[t] || null);
    return {
      label: device,
      data: points.map(p => (p ? p.delta : null)),
      raw: points,
      backgroundColor: points.map(p => (p ? bandForDelta(p.delta, config.bands).color : "transparent")),
      borderRadius: 4,
    };
  });

  return new Chart(canvas, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true },
          grid: { display: false },
        },
        y: {
          ticks: { color: "#8a8d99", callback: (v) => `${v > 0 ? "+" : ""}${v}${config.unit}` },
          grid: { color: "#2a2d38" },
        },
      },
      plugins: {
        legend: { display: devices.length > 1, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const p = item.dataset.raw[item.dataIndex];
              if (!p) return "";
              const band = bandForDelta(p.delta, config.bands);
              const deltaText = p.delta > 0 ? `+${p.delta}` : `${p.delta}`;
              return `${item.dataset.label}: ${deltaText}${config.unit} (${band.label})`;
            },
          },
        },
      },
    },
  });
}

// --- Tags tab ---
// --- Duration-tag entry ---
// Simple, stateless: tap the button, pick or type a duration, log
// immediately with that duration_min. No running timer, nothing that
// can be lost by closing the tab mid-activity.
const DURATION_QUICKPICKS = [5, 10, 15, 30, 60, 90];

async function loadTagButtons() {
  const container = document.getElementById("tag-buttons");
  try {
    const defs = await api("/tag_definitions");
    if (!defs.length) {
      container.innerHTML = '<p class="muted">No tag buttons configured yet.</p>';
      return;
    }
    container.innerHTML = "";
    defs.forEach(def => {
      const btn = document.createElement("button");
      btn.className = "tag-btn";
      btn.textContent = def.label;
      if (def.is_duration) {
        btn.addEventListener("click", () => openDurationPicker(def.tag, def.label));
      } else {
        btn.addEventListener("click", () => logTag(def.tag, btn));
      }
      container.appendChild(btn);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

async function logTag(tag, btnEl) {
  const status = document.getElementById("tags-status");
  try {
    await api("/events", { method: "POST", body: JSON.stringify({ tags: [tag] }) });
    if (btnEl) {
      btnEl.classList.add("flash");
      setTimeout(() => btnEl.classList.remove("flash"), 300);
    }
    status.textContent = `Logged "${tag}"`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
}

function closeDurationPicker() {
  const picker = document.getElementById("duration-picker");
  picker.style.display = "none";
  picker.innerHTML = "";
}

function openDurationPicker(tag, label) {
  const picker = document.getElementById("duration-picker");
  picker.style.display = "block";
  picker.innerHTML = `
    <p class="section-label duration-picker-title">${label} - how long?</p>
    <div class="duration-quickpicks">
      ${DURATION_QUICKPICKS.map(m => `<button class="small-btn duration-quickpick" data-min="${m}">${m}m</button>`).join("")}
    </div>
    <div class="duration-custom-row">
      <input type="number" min="1" id="duration-custom-input" placeholder="Custom minutes">
      <button class="small-btn" id="duration-confirm-btn">Log</button>
      <button class="small-btn" id="duration-cancel-btn">Cancel</button>
    </div>
  `;

  picker.querySelectorAll(".duration-quickpick").forEach(qb => {
    qb.addEventListener("click", () => logDurationTag(tag, label, parseInt(qb.dataset.min, 10)));
  });

  const customInput = document.getElementById("duration-custom-input");
  document.getElementById("duration-confirm-btn").addEventListener("click", () => {
    const minutes = parseInt(customInput.value, 10);
    if (!minutes || minutes <= 0) {
      customInput.focus();
      return;
    }
    logDurationTag(tag, label, minutes);
  });
  customInput.addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("duration-confirm-btn").click();
  });

  document.getElementById("duration-cancel-btn").addEventListener("click", closeDurationPicker);

  customInput.focus();
}

async function logDurationTag(tag, label, minutes) {
  const status = document.getElementById("tags-status");
  status.textContent = `Logging "${label}"...`;
  try {
    await api("/events", { method: "POST", body: JSON.stringify({ tags: [tag], duration_min: minutes }) });
    status.textContent = `Logged "${label}" for ${minutes}min`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    closeDurationPicker();
  }
}

document.getElementById("custom-tag-submit").addEventListener("click", () => {
  const input = document.getElementById("custom-tag-input");
  const tag = input.value.trim();
  if (!tag) return;
  logTag(tag, null);
  input.value = "";
});

// --- Sleep tab ---
// All known qualifier chips - kept as one list so both fresh submission
// and editing can send every qualifier explicitly as true/false.
// InfluxDB only overwrites fields actually included in a write, so
// omitting a previously-true qualifier would silently leave it set
// instead of clearing it - explicit false is required to actually
// un-set one.
const KNOWN_SLEEP_QUALIFIERS = ["groggy", "woke_up_often", "vivid_dreams", "racing_thoughts"];

function buildQualifiersPayload(selectedSet) {
  const qualifiers = {};
  KNOWN_SLEEP_QUALIFIERS.forEach(q => { qualifiers[q] = selectedSet.has(q); });
  return qualifiers;
}

function humanizeQualifierLabel(q) {
  const words = q.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

let selectedScore = null;
const selectedQualifiers = new Set();

document.querySelectorAll(".sleep-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".sleep-btn").forEach(b => b.classList.remove("selected"));
    btn.classList.add("selected");
    selectedScore = parseInt(btn.dataset.score, 10);
    document.getElementById("sleep-submit").disabled = false;
  });
});

function renderSleepQualifierChips() {
  // Rendered from KNOWN_SLEEP_QUALIFIERS rather than static HTML, so
  // adding/removing a qualifier there is genuinely sufficient - no
  // matching index.html edit needed. The edit-form chips (further down)
  // already worked this way; this brings the initial-submission chips
  // in line with that, closing the gap that made a JS-only addition
  // silently not show up here.
  const container = document.getElementById("sleep-qualifier-chips");
  container.innerHTML = "";
  KNOWN_SLEEP_QUALIFIERS.forEach(q => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.dataset.qualifier = q;
    chip.textContent = humanizeQualifierLabel(q);
    chip.addEventListener("click", () => {
      if (selectedQualifiers.has(q)) {
        selectedQualifiers.delete(q);
        chip.classList.remove("selected");
      } else {
        selectedQualifiers.add(q);
        chip.classList.add("selected");
      }
    });
    container.appendChild(chip);
  });
}
renderSleepQualifierChips();

document.getElementById("sleep-submit").addEventListener("click", async () => {
  const status = document.getElementById("sleep-status");
  if (selectedScore === null) return;

  const qualifiers = buildQualifiersPayload(selectedQualifiers);

  status.textContent = "Submitting...";
  try {
    const result = await api("/sleep", {
      method: "POST",
      body: JSON.stringify({ score: selectedScore, qualifiers }),
    });
    status.textContent = `Logged for ${result.sleep_date}`;
    loadSleepHistory();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Sleep history ---
const sleepEditDrafts = new Map(); // entry_id -> { score, qualifiers: Set }

function localTimeString(isoTimestamp) {
  const d = new Date(isoTimestamp);
  const pad = n => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderSleepEntryReadonly(entry) {
  const qualifierChips = KNOWN_SLEEP_QUALIFIERS
    .filter(q => entry.qualifiers && entry.qualifiers[q])
    .map(q => `<span class="chip-small">${humanizeQualifierLabel(q)}</span>`)
    .join("");
  // HH:MM alongside the date - multiple entries can now share a date
  // (e.g. one session starting just after local midnight, another
  // just before the next one; naps too), so the time is what actually
  // distinguishes them at a glance.
  const timeLabel = entry.start_time ? ` ${localTimeString(entry.start_time)}` : "";
  return `
    <div class="sleep-entry-main">
      <span class="sleep-entry-date">${escapeHtml(entry.sleep_date)}${timeLabel}</span>
      <span class="sleep-entry-score">${"●".repeat(entry.score)}${"○".repeat(5 - entry.score)}</span>
      <button class="small-btn sleep-edit-btn">Edit</button>
    </div>
    ${qualifierChips ? `<div class="timeline-tags">${qualifierChips}</div>` : ""}
  `;
}

function renderSleepEntryEditForm(entry) {
  const draft = sleepEditDrafts.get(entry.entry_id);
  const scoreButtons = [1, 2, 3, 4, 5].map(n => `
    <button class="sleep-btn ${draft.score === n ? "selected" : ""}" data-score="${n}">${n}</button>
  `).join("");
  const chips = KNOWN_SLEEP_QUALIFIERS.map(q => `
    <button class="chip ${draft.qualifiers.has(q) ? "selected" : ""}" data-qualifier="${q}">${humanizeQualifierLabel(q)}</button>
  `).join("");

  return `
    <div class="sleep-edit-form" data-entry-id="${escapeHtml(entry.entry_id)}">
      <div class="sleep-scale sleep-edit-scale">${scoreButtons}</div>
      <div class="qualifier-chips">${chips}</div>
      <div class="timeline-edit-actions">
        <button class="small-btn sleep-save-btn">Save</button>
        <button class="small-btn sleep-cancel-btn">Cancel</button>
        <button class="small-btn danger sleep-delete-btn">Delete</button>
      </div>
      <p class="sleep-edit-status status"></p>
    </div>
  `;
}

function rerenderSleepEntry(row, entry) {
  if (sleepEditDrafts.has(entry.entry_id)) {
    row.innerHTML = renderSleepEntryEditForm(entry);
    wireSleepEditForm(row, entry);
  } else {
    row.innerHTML = renderSleepEntryReadonly(entry);
    row.querySelector(".sleep-edit-btn").addEventListener("click", () => {
      sleepEditDrafts.set(entry.entry_id, {
        score: entry.score,
        qualifiers: new Set(KNOWN_SLEEP_QUALIFIERS.filter(q => entry.qualifiers && entry.qualifiers[q])),
      });
      rerenderSleepEntry(row, entry);
    });
  }
}

function wireSleepEditForm(row, entry) {
  const form = row.querySelector(".sleep-edit-form");
  const status = form.querySelector(".sleep-edit-status");
  const draft = sleepEditDrafts.get(entry.entry_id);

  form.querySelectorAll(".sleep-edit-scale .sleep-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      draft.score = parseInt(btn.dataset.score, 10);
      rerenderSleepEntry(row, entry);
    });
  });

  form.querySelectorAll(".chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const q = chip.dataset.qualifier;
      if (draft.qualifiers.has(q)) draft.qualifiers.delete(q);
      else draft.qualifiers.add(q);
      rerenderSleepEntry(row, entry);
    });
  });

  form.querySelector(".sleep-cancel-btn").addEventListener("click", () => {
    sleepEditDrafts.delete(entry.entry_id);
    rerenderSleepEntry(row, entry);
  });

  form.querySelector(".sleep-save-btn").addEventListener("click", async () => {
    status.textContent = "Saving...";
    try {
      await api(`/sleep/${entry.entry_id}`, {
        method: "PATCH",
        body: JSON.stringify({ score: draft.score, qualifiers: buildQualifiersPayload(draft.qualifiers) }),
      });
      sleepEditDrafts.delete(entry.entry_id);
      loadSleepHistory();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  form.querySelector(".sleep-delete-btn").addEventListener("click", async () => {
    const timeLabel = entry.start_time ? ` ${localTimeString(entry.start_time)}` : "";
    if (!confirm(`Delete the sleep entry for ${entry.sleep_date}${timeLabel}? This can't be undone.`)) return;
    status.textContent = "Deleting...";
    try {
      await api(`/sleep/${entry.entry_id}`, { method: "DELETE" });
      sleepEditDrafts.delete(entry.entry_id);
      loadSleepHistory();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });
}

async function loadSleepHistory() {
  const container = document.getElementById("sleep-history");
  container.innerHTML = '<p class="muted">Loading...</p>';
  try {
    const entries = await api("/sleep");
    if (!entries.length) {
      container.innerHTML = '<p class="muted">No sleep entries yet.</p>';
      return;
    }
    container.innerHTML = "";
    entries.forEach(entry => {
      const row = document.createElement("div");
      row.className = "list-row sleep-entry-row";
      rerenderSleepEntry(row, entry);
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

// --- Calendars tab ---
async function loadCalendars() {
  const container = document.getElementById("calendar-list");
  try {
    const cals = await api("/calendars");
    if (!cals.length) {
      container.innerHTML = '<p class="muted">No calendars added yet.</p>';
      return;
    }
    container.innerHTML = "";
    cals.forEach(cal => {
      const row = document.createElement("div");
      row.className = "calendar-row";
      const lastSynced = cal.last_synced ? `Last synced: ${cal.last_synced}` : "Never synced";
      const errorLine = cal.last_error ? `<div class="cal-error">${cal.last_error}</div>` : "";
      row.innerHTML = `
        <div class="cal-name">${escapeHtml(cal.name)} ${cal.enabled ? "" : "(disabled)"}</div>
        <div class="cal-meta">${lastSynced} · default: ${cal.default_tag}</div>
        ${errorLine}
      `;
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("cal-add-submit").addEventListener("click", async () => {
  const status = document.getElementById("cal-status");
  const name = document.getElementById("cal-name").value.trim();
  const ics_url = document.getElementById("cal-url").value.trim();
  const default_tag = document.getElementById("cal-default-tag").value.trim();

  if (!name || !ics_url || !default_tag) {
    status.textContent = "All fields required";
    return;
  }

  try {
    await api("/calendars", { method: "POST", body: JSON.stringify({ name, ics_url, default_tag }) });
    status.textContent = "Added";
    document.getElementById("cal-name").value = "";
    document.getElementById("cal-url").value = "";
    document.getElementById("cal-default-tag").value = "";
    loadCalendars();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Manage tab: keyword rules (staged draft, no hot-apply) ---
let committedRules = [];      // last-known-saved rules from the server, each may be marked _markedForDeletion
let pendingNewRules = [];     // rules added to the draft but not yet saved

function draftIsDirty() {
  return pendingNewRules.length > 0 || committedRules.some(r => r._markedForDeletion);
}

function renderRuleList() {
  const container = document.getElementById("rule-list");
  const rows = [];

  committedRules.forEach(rule => {
    const fieldNote = rule.is_regex ? `regex on ${rule.match_field}` : `contains, on ${rule.match_field}`;
    const exclusiveNote = rule.exclusive === undefined ? " · exclusivity unknown (old rule row?)" : (!rule.exclusive ? " · stacks (non-exclusive)" : "");
    const marked = rule._markedForDeletion;
    const row = document.createElement("div");
    row.className = "list-row" + (marked ? " marked-deleted" : "");
    row.innerHTML = `
      <div>
        <div class="row-title">"${escapeHtml(rule.keyword)}" → <strong>${escapeHtml(rule.tag)}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote}${exclusiveNote}${marked ? " · marked for deletion" : ""}</div>
      </div>
      <button class="small-btn ${marked ? "" : "danger"}">${marked ? "Undo" : "Delete"}</button>
    `;
    row.querySelector(".small-btn").addEventListener("click", () => {
      rule._markedForDeletion = !rule._markedForDeletion;
      renderRuleList();
    });
    rows.push(row);
  });

  pendingNewRules.forEach((rule, idx) => {
    const fieldNote = rule.is_regex ? `regex on ${rule.match_field}` : `contains, on ${rule.match_field}`;
    const exclusiveNote = rule.exclusive === undefined ? " · exclusivity unknown (old rule row?)" : (!rule.exclusive ? " · stacks (non-exclusive)" : "");
    const row = document.createElement("div");
    row.className = "list-row pending-new";
    row.innerHTML = `
      <div>
        <div class="row-title">"${escapeHtml(rule.keyword)}" → <strong>${escapeHtml(rule.tag)}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote}${exclusiveNote} · pending</div>
      </div>
      <button class="small-btn danger">Remove</button>
    `;
    row.querySelector(".small-btn").addEventListener("click", () => {
      pendingNewRules.splice(idx, 1);
      renderRuleList();
    });
    rows.push(row);
  });

  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<p class="muted">No rules yet.</p>';
  } else {
    rows.forEach(r => container.appendChild(r));
  }

  document.getElementById("rule-save-batch").disabled = !draftIsDirty();
}

async function loadKeywordRules() {
  try {
    committedRules = await api("/keyword_rules");
    committedRules.forEach(r => { r._markedForDeletion = false; });
    pendingNewRules = [];
    renderRuleList();
  } catch (e) {
    document.getElementById("rule-list").innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("rule-add-draft").addEventListener("click", () => {
  const keyword = document.getElementById("rule-keyword").value.trim();
  const tag = document.getElementById("rule-tag").value.trim();
  const category = document.getElementById("rule-category").value;
  const match_field = document.getElementById("rule-match-field").value;
  const is_regex = document.getElementById("rule-is-regex").checked;
  const priority = parseInt(document.getElementById("rule-priority").value, 10) || 0;
  const exclusive = document.getElementById("rule-exclusive").checked;
  const status = document.getElementById("rule-status");

  if (!keyword || !tag) {
    status.textContent = "Keyword and tag are required";
    return;
  }

  pendingNewRules.push({ keyword, tag, category, match_field, is_regex, priority, exclusive, enabled: true });
  document.getElementById("rule-keyword").value = "";
  document.getElementById("rule-tag").value = "";
  document.getElementById("rule-priority").value = "0";
  document.getElementById("rule-is-regex").checked = false;
  // Deliberately NOT resetting rule-exclusive here - it used to silently
  // flip back to checked after every add, which could leave a rule
  // exclusive when the person believed they'd already unchecked it for
  // this session. Leaving it as the person last set it is more
  // predictable when adding several related rules in a row.
  status.textContent = "Added to draft - not saved yet";
  renderRuleList();
});

document.getElementById("rule-save-batch").addEventListener("click", async () => {
  const status = document.getElementById("rule-status");
  const deleted_ids = committedRules.filter(r => r._markedForDeletion).map(r => r.id);

  status.textContent = "Saving...";
  try {
    const result = await api("/keyword_rules/save_batch", {
      method: "POST",
      body: JSON.stringify({ added: pendingNewRules, deleted_ids }),
    });

    status.textContent = "Rules saved";
    await loadKeywordRules(); // refresh committed state, clears draft

    if (result.affected_events > 0) {
      const sample = result.sample_titles.join(", ");
      const confirmed = confirm(
        `This rule change would reclassify ${result.affected_events} previously-synced event(s), ` +
        `e.g.: ${sample}${result.affected_events > result.sample_titles.length ? ", ..." : ""}.\n\n` +
        `Reprocess them now? This runs in the background - you can keep using the app while it works.`
      );
      if (confirmed) {
        startReprocess();
      }
    }
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Reprocess: background trigger + polling banner ---
let reprocessPollHandle = null;

async function startReprocess() {
  const banner = document.getElementById("reprocess-banner");
  banner.style.display = "block";
  banner.className = "reprocess-banner running";
  banner.textContent = "Starting reprocess...";

  try {
    await api("/reprocess", { method: "POST" });
  } catch (e) {
    banner.className = "reprocess-banner error";
    banner.textContent = `Error starting reprocess: ${e.message}`;
    return;
  }

  if (reprocessPollHandle) clearInterval(reprocessPollHandle);
  reprocessPollHandle = setInterval(pollReprocessStatus, 1500);
  pollReprocessStatus();
}

async function pollReprocessStatus() {
  const banner = document.getElementById("reprocess-banner");
  try {
    const s = await api("/reprocess/status");

    if (s.status === "running") {
      banner.style.display = "block";
      banner.className = "reprocess-banner running";
      banner.textContent = `Reprocessing... ${s.processed}/${s.total} checked, ${s.changed} changed`;
    } else if (s.status === "done") {
      banner.className = "reprocess-banner done";
      banner.textContent = `Reprocess complete - ${s.changed}/${s.total} event(s) updated`;
      clearInterval(reprocessPollHandle);
      reprocessPollHandle = null;
      setTimeout(() => { banner.style.display = "none"; }, 6000);
    } else if (s.status === "error") {
      banner.className = "reprocess-banner error";
      banner.textContent = `Reprocess failed: ${s.error}`;
      clearInterval(reprocessPollHandle);
      reprocessPollHandle = null;
    }
  } catch (e) {
    // Transient poll failure - don't kill the banner, just try again next tick
  }
}

// On load, if a reprocess was already running from a previous session
// (e.g. page refreshed mid-run), pick up polling rather than losing track of it.
async function checkReprocessOnLoad() {
  try {
    const s = await api("/reprocess/status");
    if (s.status === "running") {
      reprocessPollHandle = setInterval(pollReprocessStatus, 1500);
      pollReprocessStatus();
    }
  } catch (e) {
    // ignore
  }
}

// --- Manage tab: tag button definitions ---
async function loadTagDefManage() {
  const container = document.getElementById("tagdef-list");
  try {
    const defs = await api("/tag_definitions");
    if (!defs.length) {
      container.innerHTML = '<p class="muted">No tag buttons yet.</p>';
      return;
    }
    container.innerHTML = "";
    defs.forEach(def => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `
        <div>
          <div class="row-title">${escapeHtml(def.label)} <span class="muted">(${escapeHtml(def.tag)})</span></div>
          <div class="row-meta">${def.category}${def.is_duration ? " · duration" : ""} · order ${def.sort_order}</div>
        </div>
        <button class="small-btn danger" data-id="${def.id}">Delete</button>
      `;
      row.querySelector(".small-btn").addEventListener("click", async () => {
        await api(`/tag_definitions/${def.id}`, { method: "DELETE" });
        loadTagDefManage();
        loadTagButtons(); // keep the Tags tab in sync
      });
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("tagdef-add-submit").addEventListener("click", async () => {
  const status = document.getElementById("tagdef-status");
  const tag = document.getElementById("tagdef-tag").value.trim();
  const label = document.getElementById("tagdef-label").value.trim();
  const category = document.getElementById("tagdef-category").value;
  const is_duration = document.getElementById("tagdef-is-duration").checked;
  const sort_order = parseInt(document.getElementById("tagdef-sort-order").value, 10) || 0;

  if (!tag || !label) {
    status.textContent = "Tag and label are required";
    return;
  }

  try {
    await api("/tag_definitions", {
      method: "POST",
      body: JSON.stringify({ tag, label, category, is_duration, sort_order }),
    });
    status.textContent = "Tag button added";
    document.getElementById("tagdef-tag").value = "";
    document.getElementById("tagdef-label").value = "";
    document.getElementById("tagdef-sort-order").value = "0";
    document.getElementById("tagdef-is-duration").checked = false;
    loadTagDefManage();
    loadTagButtons(); // keep the Tags tab in sync
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Timeline tab ---
function formatDateInput(d) {
  return d.toISOString().slice(0, 10); // YYYY-MM-DD
}

function defaultTimelineRange() {
  const now = new Date();
  const start = new Date(now);
  start.setDate(start.getDate() - 7);
  const end = new Date(now);
  end.setDate(end.getDate() + 1);
  return { start: formatDateInput(start), end: formatDateInput(end) };
}

function localDateString(isoTimestamp) {
  // Local calendar date, not UTC's - a timestamp shortly after local
  // midnight (any timezone ahead of UTC) can otherwise group under
  // the previous day's heading, since UTC's date for that instant is
  // still yesterday even though it's already tomorrow locally. Uses
  // the browser's own local timezone automatically (no TZ config
  // needed here, unlike the backend - the browser already knows).
  const d = new Date(isoTimestamp);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function groupByDay(entries) {
  const groups = {};
  entries.forEach(entry => {
    const day = localDateString(entry.timestamp);
    if (!groups[day]) groups[day] = [];
    groups[day].push(entry);
  });
  return groups;
}

function renderTagChips(tags) {
  if (!tags || !tags.length) return "";
  return `<div class="timeline-tags">${tags.map(t => `<span class="chip-small">${escapeHtml(t)}</span>`).join("")}</div>`;
}

// --- Timeline edit state ---
// event_id -> Set of tags currently being edited in that entry's open editor.
// Entries not in this map are shown read-only (the default).
const timelineEditDrafts = new Map();

function isoToDatetimeLocalValue(iso) {
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function datetimeLocalValueToIso(value) {
  const [datePart, timePart] = value.split("T");
  const [y, m, d] = datePart.split("-").map(Number);
  const [hh, mm] = timePart.split(":").map(Number);
  return new Date(y, m - 1, d, hh, mm).toISOString();
}

function renderTimelineEditForm(entry) {
  const draft = timelineEditDrafts.get(entry.event_id);
  const tags = Array.from(draft);
  const chipsHtml = tags.map(t => `
    <span class="chip-small editable-chip">
      ${t} <button class="chip-remove" data-remove-tag="${t}">&times;</button>
    </span>
  `).join("");

  const timeFieldHtml = entry.kind === "manual"
    ? `<label class="timeline-edit-label">Time
         <input type="datetime-local" class="timeline-edit-datetime" value="${isoToDatetimeLocalValue(entry.timestamp)}">
       </label>
       <label class="timeline-edit-label">Duration (minutes)
         <input type="number" min="0" class="timeline-edit-duration" value="${entry.duration_min ?? ""}" placeholder="none">
       </label>`
    : `<p class="muted small-note">Time and title come from the calendar feed and can't be edited here - only tags.</p>`;

  const deleteBtnHtml = entry.kind === "manual"
    ? `<button class="small-btn danger timeline-delete-btn">Delete event</button>`
    : "";

  const resetBtnHtml = (entry.kind === "calendar" && entry.manually_tagged)
    ? `<button class="small-btn timeline-reset-btn">Reset to auto</button>`
    : "";

  return `
    <div class="timeline-edit-form" data-event-id="${entry.event_id}" data-kind="${entry.kind}">
      <div class="timeline-edit-tags">${chipsHtml || '<span class="muted">No tags - add one below</span>'}</div>
      <div class="timeline-edit-add-tag">
        <input type="text" class="timeline-new-tag-input" placeholder="Add tag...">
        <button class="small-btn timeline-add-tag-btn">Add</button>
      </div>
      ${timeFieldHtml}
      <div class="timeline-edit-actions">
        <button class="small-btn timeline-save-btn">Save</button>
        <button class="small-btn timeline-cancel-btn">Cancel</button>
        ${deleteBtnHtml}
        ${resetBtnHtml}
      </div>
      <p class="timeline-edit-status status"></p>
    </div>
  `;
}

function wireTimelineEditForm(row, entry) {
  const form = row.querySelector(".timeline-edit-form");
  const status = form.querySelector(".timeline-edit-status");

  form.querySelectorAll("[data-remove-tag]").forEach(btn => {
    btn.addEventListener("click", () => {
      timelineEditDrafts.get(entry.event_id).delete(btn.dataset.removeTag);
      rerenderTimelineEntry(row, entry);
    });
  });

  form.querySelector(".timeline-add-tag-btn").addEventListener("click", () => {
    const input = form.querySelector(".timeline-new-tag-input");
    const tag = input.value.trim();
    if (!tag) return;
    timelineEditDrafts.get(entry.event_id).add(tag);
    rerenderTimelineEntry(row, entry);
  });

  form.querySelector(".timeline-cancel-btn").addEventListener("click", () => {
    timelineEditDrafts.delete(entry.event_id);
    rerenderTimelineEntry(row, entry);
  });

  form.querySelector(".timeline-save-btn").addEventListener("click", async () => {
    const draftTags = Array.from(timelineEditDrafts.get(entry.event_id));
    if (!draftTags.length) {
      status.textContent = "At least one tag is required";
      return;
    }
    status.textContent = "Saving...";
    try {
      if (entry.kind === "manual") {
        const timeInput = form.querySelector(".timeline-edit-datetime");
        const durationInput = form.querySelector(".timeline-edit-duration");
        const body = { tags: draftTags };
        const newIso = datetimeLocalValueToIso(timeInput.value);
        if (newIso !== entry.timestamp) body.timestamp = newIso;
        const newDuration = durationInput.value === "" ? null : parseInt(durationInput.value, 10);
        if (newDuration !== null && newDuration !== entry.duration_min) body.duration_min = newDuration;
        await api(`/events/${entry.event_id}`, { method: "PATCH", body: JSON.stringify(body) });
      } else {
        await api(`/calendar_events/${entry.event_id}/tags`, {
          method: "PATCH",
          body: JSON.stringify({ tags: draftTags }),
        });
      }
      timelineEditDrafts.delete(entry.event_id);
      loadTimeline(); // full reload - simplest way to reflect the save, including any day-grouping change from a time edit
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  const deleteBtn = form.querySelector(".timeline-delete-btn");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (!confirm("Delete this logged event? This can't be undone.")) return;
      status.textContent = "Deleting...";
      try {
        await api(`/events/${entry.event_id}`, { method: "DELETE" });
        timelineEditDrafts.delete(entry.event_id);
        loadTimeline();
      } catch (e) {
        status.textContent = `Error: ${e.message}`;
      }
    });
  }

  const resetBtn = form.querySelector(".timeline-reset-btn");
  if (resetBtn) {
    resetBtn.addEventListener("click", async () => {
      if (!confirm("Reset this event's tags to automatic keyword-rule classification? Your manual edit will be discarded.")) return;
      status.textContent = "Resetting...";
      try {
        await api(`/calendar_events/${entry.event_id}/reset_tags`, { method: "POST" });
        timelineEditDrafts.delete(entry.event_id);
        loadTimeline();
      } catch (e) {
        status.textContent = `Error: ${e.message}`;
      }
    });
  }
}

function renderTimelineEntryReadonly(entry) {
  const time = entry.timestamp.slice(11, 16);
  const durationNote = entry.duration_min ? ` · ${entry.duration_min}min` : "";
  const overrideBadge = entry.manually_tagged ? ' <span class="chip-small">manually tagged</span>' : "";

  if (entry.kind === "calendar") {
    return `
      <div class="timeline-entry-main">
        <span class="timeline-time">${time}</span>
        <span class="timeline-kind-badge cal">Calendar</span>
        <span class="timeline-title">${escapeHtml(entry.title) || "(untitled event)"}</span>
        <span class="muted timeline-cal-name">${escapeHtml(entry.calendar) || ""}${durationNote}</span>
        <button class="small-btn timeline-edit-btn">Edit tags</button>
      </div>
      ${renderTagChips(entry.tags)}${overrideBadge}
    `;
  }
  return `
    <div class="timeline-entry-main">
      <span class="timeline-time">${time}</span>
      <span class="timeline-kind-badge manual">Logged</span>
      <span class="muted">${durationNote}</span>
      <button class="small-btn timeline-edit-btn">Edit</button>
    </div>
    ${renderTagChips(entry.tags)}
  `;
}

function rerenderTimelineEntry(row, entry) {
  if (timelineEditDrafts.has(entry.event_id)) {
    row.innerHTML = renderTimelineEditForm(entry);
    wireTimelineEditForm(row, entry);
  } else {
    row.innerHTML = renderTimelineEntryReadonly(entry);
    row.querySelector(".timeline-edit-btn").addEventListener("click", () => {
      timelineEditDrafts.set(entry.event_id, new Set(entry.tags));
      rerenderTimelineEntry(row, entry);
    });
  }
}

async function loadTimeline() {
  const container = document.getElementById("timeline-list");
  const start = document.getElementById("timeline-start").value;
  const end = document.getElementById("timeline-end").value;

  container.innerHTML = '<p class="muted">Loading...</p>';
  try {
    const params = new URLSearchParams();
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    const entries = await api(`/timeline?${params.toString()}`);

    if (!entries.length) {
      container.innerHTML = '<p class="muted">Nothing in this range yet.</p>';
      return;
    }

    const groups = groupByDay(entries);
    const days = Object.keys(groups).sort().reverse(); // most recent day first

    container.innerHTML = "";
    days.forEach(day => {
      const heading = document.createElement("div");
      heading.className = "timeline-day-heading";
      heading.textContent = day;
      container.appendChild(heading);

      groups[day].forEach(entry => {
        const row = document.createElement("div");
        row.className = `timeline-entry timeline-entry-${entry.kind}`;
        rerenderTimelineEntry(row, entry);
        container.appendChild(row);
      });
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

function initTimelineControls() {
  const defaults = defaultTimelineRange();
  document.getElementById("timeline-start").value = defaults.start;
  document.getElementById("timeline-end").value = defaults.end;
  document.getElementById("timeline-refresh").addEventListener("click", loadTimeline);
}

// --- init ---
async function initApp() {
  try {
    const me = await api("/auth/me");
    showApp(me);
  } catch (e) {
    showLogin();
  }
}

function showLogin() {
  document.getElementById("login-screen").style.display = "flex";
  document.getElementById("app-shell").style.display = "none";
}

function showApp(me) {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app-shell").style.display = "block";
  document.getElementById("current-username").textContent = me.username;

  loadToday();
  loadTagButtons();
  loadCalendars();
  loadKeywordRules();
  loadTagDefManage();
  loadUserList();
  loadClaimPicker();
  checkReprocessOnLoad();
  initTimelineControls();
  loadTimeline();
  loadSleepHistory();
}

document.getElementById("login-submit").addEventListener("click", async () => {
  const status = document.getElementById("login-status");
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;

  if (!username || !password) {
    status.textContent = "Username and password required";
    return;
  }

  status.textContent = "Logging in...";
  try {
    const me = await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    document.getElementById("login-password").value = "";
    showApp(me);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// Allow pressing Enter in either login field to submit
["login-username", "login-password"].forEach(id => {
  document.getElementById(id).addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("login-submit").click();
  });
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch (e) {
    // even if the request fails, drop back to the login screen -
    // an invalid/expired session should look the same either way
  }
  showLogin();
});

// --- Household members ---
async function loadUserList() {
  const container = document.getElementById("user-list");
  try {
    const users = await api("/users");
    if (!users.length) {
      container.innerHTML = '<p class="muted">No accounts yet.</p>';
      return;
    }
    container.innerHTML = "";
    users.forEach(u => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `<div class="row-title">${escapeHtml(u.username)}</div>`;
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

const MANUAL_OPTION_VALUE = "__manual__";

async function loadClaimPicker() {
  const hint = document.getElementById("claim-hint");
  const select = document.getElementById("new-user-username-select");
  const manualInput = document.getElementById("new-user-username-manual");

  try {
    const unclaimed = await api("/unclaimed_ring_users");

    if (unclaimed.length === 0) {
      // No ring data to claim yet - manual entry is the only path, with
      // a clear warning rather than silently accepting a mismatch.
      hint.textContent = "No unclaimed ring data found yet - you can still add this person, " +
        "but double-check the username matches their ring parser's GADGETBRIDGE_USER once it's set up.";
      hint.className = "muted small-note warning-note";
      select.style.display = "none";
      manualInput.style.display = "block";
      manualInput.placeholder = "Username (must match their GADGETBRIDGE_USER)";
      return;
    }

    hint.textContent = "Pick from ring data that's already synced, to avoid typos that would break correlation.";
    hint.className = "muted small-note";

    select.innerHTML = "";
    unclaimed.forEach(username => {
      const opt = document.createElement("option");
      opt.value = username;
      opt.textContent = username;
      select.appendChild(opt);
    });
    const manualOpt = document.createElement("option");
    manualOpt.value = MANUAL_OPTION_VALUE;
    manualOpt.textContent = "— enter manually instead —";
    select.appendChild(manualOpt);

    select.style.display = "block";
    manualInput.style.display = "none";
    manualInput.value = "";

    select.onchange = () => {
      if (select.value === MANUAL_OPTION_VALUE) {
        manualInput.style.display = "block";
        manualInput.placeholder = "Username (must match their GADGETBRIDGE_USER)";
      } else {
        manualInput.style.display = "none";
      }
    };
  } catch (e) {
    // Influx unreachable or some other failure - don't block account
    // creation over it, just fall back to manual entry.
    hint.textContent = "Couldn't check ring data - you can still add this person manually.";
    hint.className = "muted small-note warning-note";
    select.style.display = "none";
    manualInput.style.display = "block";
  }
}

document.getElementById("new-user-submit").addEventListener("click", async () => {
  const status = document.getElementById("user-status");
  const select = document.getElementById("new-user-username-select");
  const manualInput = document.getElementById("new-user-username-manual");
  const password = document.getElementById("new-user-password").value;

  let username;
  if (select.style.display !== "none" && select.value !== MANUAL_OPTION_VALUE) {
    username = select.value;
  } else {
    username = manualInput.value.trim();
  }

  if (!username || !password) {
    status.textContent = "Username and password required";
    return;
  }

  try {
    const result = await api("/users", { method: "POST", body: JSON.stringify({ username, password }) });
    status.textContent = result.linked_to_ring_data
      ? `Added ${username} - linked to existing ring data`
      : `Added ${username} - no matching ring data found yet, double-check the username later`;
    manualInput.value = "";
    document.getElementById("new-user-password").value = "";
    loadUserList();
    loadClaimPicker(); // refresh so the just-claimed username drops off the list
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

initApp();
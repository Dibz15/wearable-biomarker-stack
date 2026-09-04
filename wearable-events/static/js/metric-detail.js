// --- Metric detail views (opened from a Today card, not a tab) ---
import { escapeHtml, api, formatNum, dateToISO, isoToDate, todayISO, shiftISODate } from "./core.js";
import { buildLineChart, buildRangeBarChart, buildDifferentialChart, renderBandLegend } from "./metric-charts.js";

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
      { field: "temperature", label: "Temperature", decimals: 1 },
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
      decimals: 1,
      baseline: {
        lowLabel: "Cooler",
        highLabel: "Warmer",
        unit: "\u00b0",
        decimals: 1,
        // Fixed absolute scale, not the statistical z-score every
        // other field's comparison bar uses - matches how the W/M
        // differential trend chart already works, and how Zepp itself
        // actually displays temperature (confirmed fixed thresholds,
        // not a statistical measure). scaleMax=1.5 means the bar's
        // full width represents +-1.5 degrees; driftThreshold=0.5 is
        // now correctly compared against the RAW DEGREE delta, not a
        // z-score (an earlier version of this compared it against z
        // instead, which would have miscalibrated the clue - a z-score
        // commonly exceeds 0.5 even for an unremarkable night).
        scaleType: "fixed",
        scaleMax: 1.5,
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

export async function openMetricDetail(viewKey) {
  const view = DETAIL_VIEWS[viewKey];
  if (!view) return; // no detail view wired up for this field yet

  openDetailScreen(view.title);
  await renderDetailPeriod(view, "day", todayISO());
}


function computeStatsFromSeries(series, period, decimals) {
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
        last: formatNum(points[points.length - 1].v, decimals),
        min: formatNum(Math.min(...values), decimals),
        max: formatNum(Math.max(...values), decimals),
      };
    } else {
      stats[device] = {
        min: formatNum(Math.min(...points.map(p => p.min)), decimals),
        max: formatNum(Math.max(...points.map(p => p.max)), decimals),
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
    // "zscore" (default): position and drift-clue driven by the
    // statistical z-score, same as HRV/SpO2/Resting HR - there's no
    // fixed, field-meaningful unit to scale the bar to for those.
    // "fixed": position and drift-clue driven by the RAW delta
    // instead, scaled to +-scaleMax - for a field like temperature
    // where Zepp itself uses fixed absolute thresholds (confirmed
    // +-0.5/1.0/1.5 degrees), not a statistical measure. Mixing the
    // two up front (a threshold chosen in degrees, compared against a
    // z-score) would silently miscalibrate the clue - a z-score
    // commonly exceeds 0.5 in magnitude even for an unremarkable
    // night, so a "0.5" threshold meant for degrees would fire on the
    // z-score almost constantly.
    scaleType = "zscore",
    scaleMax = BASELINE_Z_CAP,
    decimals,
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
    // Clamp before mapping to the bar's width, so a rare big swing
    // pins to the end of the bar rather than going off-scale - the
    // person's own suggested approach.
    const driftMetric = scaleType === "fixed" ? c.delta : c.z;
    const cap = scaleType === "fixed" ? scaleMax : BASELINE_Z_CAP;
    const clamped = Math.max(-cap, Math.min(cap, driftMetric));
    const pct = 50 + (clamped / cap) * 50;
    const deltaText = c.delta > 0 ? `+${formatNum(c.delta, decimals)}` : `${formatNum(c.delta, decimals)}`;
    const clueHtml = driftThreshold !== null ? renderDriftClue(driftMetric, driftThreshold, lowClue, highClue, normalClue) : "";
    return `
      <div class="baseline-row">
        <div class="metric-device-name">${escapeHtml(device)}</div>
        <div class="baseline-track">
          <div class="baseline-center-tick"></div>
          <div class="baseline-marker" style="left: ${pct}%"></div>
        </div>
        <div class="baseline-labels">
          <span>${escapeHtml(lowLabel)}</span>
          <span class="baseline-delta">${deltaText} ${escapeHtml(unit)} vs ${baselineDays}-day avg (${formatNum(c.baseline_mean, decimals)})</span>
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
    const stats = computeStatsFromSeries(series, period, c.decimals);
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
      ? buildLineChart(canvas, series, devices, c.decimals)
      : buildRangeBarChart(canvas, series, devices, period, rollingMeanByField[c.field] || {}, c.yMin, c.decimals);
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
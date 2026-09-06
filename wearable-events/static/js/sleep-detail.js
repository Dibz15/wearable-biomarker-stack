// --- Sleep sub-detail pages (Sleep Duration, and later Sleep Heart
// Rate / Sleep Respiratory Rate / Sleep Regularity) - each linked from
// the main Sleep tab, opened in the shared detail-screen overlay
// (same one metric-detail.js's DETAIL_VIEWS and activity.js use).
import { escapeHtml, api, todayISO, shiftISODate } from "./core.js";
import { openDetailScreen, registerActiveChart, clearActiveCharts, renderDateNav, renderBaselineBar, renderPeriodButtons, wireDetailControls } from "./metric-detail.js";
import { buildTrendBarChart, buildVitalsHypnogramSVG, buildRangeBarChart, buildTimeScatterChart, buildSleepStageStackedChart } from "./metric-charts.js";

function formatHoursMinutes(hours) {
  const totalMin = Math.round(hours * 60);
  const hr = Math.floor(totalMin / 60);
  const min = totalMin % 60;
  return hr > 0 ? `${hr}<span class="unit">hr</span> ${min}<span class="unit">min</span>` : `${min}<span class="unit">min</span>`;
}

// Horizontal 0-to-goal range bar with a marker for the night's actual
// value - matches UI_DESIGN_NOTES.md's "Page: Sleep Duration" spec,
// including the value being allowed to sit visibly past the goal line
// when duration exceeds it (a genuinely good night shouldn't be
// clipped at the goal). The bar's own total width represents 125% of
// goal (a fixed, bounded scale, not infinite overflow) - goal itself
// always sits at the 80% mark, leaving real room to the right for an
// over-goal night's fill to visibly extend past it.
function renderDurationRangeBar(hours, goalHours) {
  const scaleMax = goalHours * 1.25;
  const goalPct = (goalHours / scaleMax) * 100; // always 80
  const valuePct = Math.min(100, (hours / scaleMax) * 100);
  const overGoal = hours > goalHours;
  return `
    <div class="duration-range-bar">
      <div class="duration-range-fill${overGoal ? " over-goal" : ""}" style="width:${valuePct}%"></div>
      <div class="duration-range-goal-line" style="left:${goalPct}%"></div>
    </div>
    <div class="duration-range-labels">
      <span>0</span>
      <span>Goal ${formatHoursMinutes(goalHours)}</span>
    </div>
  `;
}

function renderDurationLabel(rec) {
  if (rec.meets_recommendation) return `<span class="duration-tier duration-tier-good">Recommended</span>`;
  if (rec.below) return `<span class="duration-tier duration-tier-attention">Below Recommended</span>`;
  return `<span class="duration-tier duration-tier-attention">Above Recommended</span>`;
}

// Only day/week/month offered here for now, deliberately - "year"
// needs its own monthly-aggregation pass (get_sleep_stage_trend
// returns one row per NIGHT regardless of period, which is exactly
// right for week (7 bars) and month (28-31 bars, matching
// UI_DESIGN_NOTES.md's own "Time Asleep (advanced)" spec precisely)
// but would mean up to 365 raw bars for a year - not usable, and not
// what that same spec calls for either ("Y collapses to one entry per
// month"). Left out rather than shipped in a form that doesn't match
// its own documented target.
const SLEEP_DURATION_PERIODS = ["day", "week", "month"];

export async function openSleepDurationDetail(anchorDate = todayISO()) {
  openDetailScreen("Sleep Duration");
  await renderSleepDurationPeriod("day", anchorDate);
}

async function renderSleepDurationPeriod(period, anchorDate) {
  if (period === "day") {
    await renderSleepDurationDay(anchorDate);
  } else {
    await renderSleepDurationRollup(period, anchorDate);
  }
}

async function renderSleepDurationDay(anchorDate) {
  const content = document.getElementById("detail-content");
  content.innerHTML = `${renderPeriodButtons("day", SLEEP_DURATION_PERIODS)}${renderDateNav("day", anchorDate)}<p class="muted">Loading...</p>`;
  wireDetailControls(renderSleepDurationPeriod, "day", anchorDate);

  clearActiveCharts();

  try {
    // "Last 7 days" always ends at the night currently being viewed,
    // keeping it consistent with date-nav rather than always pinned
    // to the real today.
    const weekEnd = anchorDate;
    const [overview, trend] = await Promise.all([
      api(`/sleep/overview?date=${anchorDate}`),
      api(`/sleep/timing-trend?period=week&end_date=${weekEnd}`),
    ]);

    if (overview === null) {
      content.innerHTML = `
        ${renderPeriodButtons("day", SLEEP_DURATION_PERIODS)}
        ${renderDateNav("day", anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep session recorded for this night.</p>
        </div>
      `;
      wireDetailControls(renderSleepDurationPeriod, "day", anchorDate);
      return;
    }

    const rec = overview.duration_recommendation;
    const goalHours = overview.duration_goal_s / 3600;

    content.innerHTML = `
      ${renderPeriodButtons("day", SLEEP_DURATION_PERIODS)}
      ${renderDateNav("day", anchorDate)}
      <div class="sleep-summary-card">
        <div class="sleep-summary-top">
          <span class="sleep-summary-duration">${formatHoursMinutes(rec.hours)}</span>
          ${renderDurationLabel(rec)}
        </div>
        ${renderDurationRangeBar(rec.hours, goalHours)}
        <p class="sleep-duration-source">
          Guideline: ${rec.range_min_hours}\u2013${rec.range_max_hours}hr (${escapeHtml(rec.source)}), ${escapeHtml(rec.age_bracket.replace("_", " "))} bracket.
        </p>
      </div>

      <p class="today-section-label">Last 7 Days</p>
      <div class="detail-chart-card">
        <canvas id="sleep-duration-trend-chart"></canvas>
      </div>
    `;
    wireDetailControls(renderSleepDurationPeriod, "day", anchorDate);

    if (trend.length > 0) {
      // Group by each night's own reporting device, not assumed to
      // match the currently-viewed night's device - realistically a
      // single-device app, but a week's worth of nights could in
      // principle span a device change.
      const devices = [...new Set(trend.map(t => t.device))];
      const series = {};
      devices.forEach(d => { series[d] = []; });
      trend.forEach(t => {
        series[t.device].push({ t: t.date, value: Math.round((t.duration_s / 3600) * 10) / 10 });
      });
      const chart = buildTrendBarChart(
        document.getElementById("sleep-duration-trend-chart"), series, devices,
        { yAxisTitle: "hours", decimals: 1, unit: "h", meanLabel: "Week average" }
      );
      if (chart) registerActiveChart(chart);
    } else {
      document.getElementById("sleep-duration-trend-chart").replaceWith(Object.assign(document.createElement("p"), {
        className: "metric-card-empty", textContent: "No sleep data for the last 7 days",
      }));
    }
  } catch (e) {
    content.innerHTML = `${renderPeriodButtons("day", SLEEP_DURATION_PERIODS)}${renderDateNav("day", anchorDate)}<p class="status">Error loading sleep duration data: ${escapeHtml(e.message)}</p>`;
    wireDetailControls(renderSleepDurationPeriod, "day", anchorDate);
  }
}

async function renderSleepDurationRollup(period, anchorDate) {
  const content = document.getElementById("detail-content");
  content.innerHTML = `${renderPeriodButtons(period, SLEEP_DURATION_PERIODS)}${renderDateNav(period, anchorDate)}<p class="muted">Loading...</p>`;
  wireDetailControls(renderSleepDurationPeriod, period, anchorDate);

  clearActiveCharts();

  try {
    const [timingTrend, stageTrend] = await Promise.all([
      api(`/sleep/timing-trend?period=${period}&end_date=${anchorDate}`),
      api(`/sleep/stage-trend?period=${period}&end_date=${anchorDate}`),
    ]);

    if (timingTrend.length === 0) {
      content.innerHTML = `
        ${renderPeriodButtons(period, SLEEP_DURATION_PERIODS)}
        ${renderDateNav(period, anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep sessions recorded for this period.</p>
        </div>
      `;
      wireDetailControls(renderSleepDurationPeriod, period, anchorDate);
      return;
    }

    // A straight mean across however many nights actually have a
    // recorded session - nights with no session are already omitted
    // by get_sleep_timing_trend() itself, not zero-filled, so this
    // isn't skewed toward 0 by missing nights.
    const avgHours = timingTrend.reduce((sum, t) => sum + t.duration_s, 0) / timingTrend.length / 3600;

    content.innerHTML = `
      ${renderPeriodButtons(period, SLEEP_DURATION_PERIODS)}
      ${renderDateNav(period, anchorDate)}
      <div class="sleep-summary-card">
        <div class="sleep-summary-top">
          <span class="sleep-summary-duration">${formatHoursMinutes(avgHours)}</span>
          <span class="sleep-summary-date">Average \u00b7 ${timingTrend.length} night${timingTrend.length === 1 ? "" : "s"}</span>
        </div>
      </div>

      <p class="today-section-label">Sleep Composition</p>
      <div class="detail-chart-card">
        <canvas id="sleep-stage-stacked-chart"></canvas>
      </div>
    `;
    wireDetailControls(renderSleepDurationPeriod, period, anchorDate);

    if (stageTrend.length > 0) {
      const chart = buildSleepStageStackedChart(
        document.getElementById("sleep-stage-stacked-chart"), stageTrend, { labelFormat: "day" }
      );
      if (chart) registerActiveChart(chart);
    } else {
      document.getElementById("sleep-stage-stacked-chart").replaceWith(Object.assign(document.createElement("p"), {
        className: "metric-card-empty", textContent: "No sleep stage data for this period",
      }));
    }
  } catch (e) {
    content.innerHTML = `${renderPeriodButtons(period, SLEEP_DURATION_PERIODS)}${renderDateNav(period, anchorDate)}<p class="status">Error loading sleep composition data: ${escapeHtml(e.message)}</p>`;
    wireDetailControls(renderSleepDurationPeriod, period, anchorDate);
  }
}

// Trimmed-down date-nav shared by every sleep sub-detail page
// (prev/next/date-picker only, no period switcher - each of these
// pages is a single "day" view, not a D/W/M/Y one, per
// UI_DESIGN_NOTES.md's own spec for all of them). Takes the render
// function to call on navigation, same callback pattern
// wireDetailControls (metric-detail.js) already established, rather
// than each page hardcoding its own copy of this wiring.
//
// Scoped to #detail-content specifically - the same class of bug
// fixed earlier this session for Today's own date-nav applies here
// too, now that multiple containers with identical .date-nav-btn
// markup can coexist in the DOM.
function wireSubDetailDateNav(anchorDate, renderFn) {
  const container = document.getElementById("detail-content");
  const prevBtn = container.querySelector('.date-nav-btn[data-nav="prev"]');
  const nextBtn = container.querySelector('.date-nav-btn[data-nav="next"]');
  if (prevBtn) {
    prevBtn.addEventListener("click", () => renderFn(shiftISODate(anchorDate, -1)));
  }
  if (nextBtn && !nextBtn.disabled) {
    nextBtn.addEventListener("click", () => renderFn(shiftISODate(anchorDate, 1)));
  }

  const dateInput = container.querySelector(".date-nav-input");
  const dateLabel = container.querySelector(".date-nav-label");
  if (dateInput) {
    dateInput.addEventListener("change", () => {
      if (dateInput.value) renderFn(dateInput.value);
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

// --- Sleep Heart Rate / Sleep Respiratory Rate ---
// Structurally identical pages (UI_DESIGN_NOTES.md confirms Sleep
// Respiratory Rate is "near-identical layout to Sleep Heart Rate"),
// one field-parameterized implementation rather than two near-copies.
const VITALS_PAGE_CONFIG = {
  heart_rate: { title: "Sleep Heart Rate", unit: "bpm", lowLabel: "Slower", highLabel: "Faster", decimals: 0 },
  sleep_respiratory_rate: { title: "Sleep Respiratory Rate", unit: "brpm", lowLabel: "Lower", highLabel: "Higher", decimals: 0 },
};

export async function openSleepHeartRateDetail(anchorDate = todayISO()) {
  openDetailScreen("Sleep Heart Rate");
  await renderSleepVitalsDay("heart_rate", anchorDate);
}

export async function openSleepRespiratoryRateDetail(anchorDate = todayISO()) {
  openDetailScreen("Sleep Respiratory Rate");
  await renderSleepVitalsDay("sleep_respiratory_rate", anchorDate);
}

async function renderSleepVitalsDay(field, anchorDate) {
  const cfg = VITALS_PAGE_CONFIG[field];
  const renderFn = (d) => renderSleepVitalsDay(field, d);
  const content = document.getElementById("detail-content");
  content.innerHTML = `${renderDateNav("day", anchorDate)}<p class="muted">Loading...</p>`;
  wireSubDetailDateNav(anchorDate, renderFn);

  clearActiveCharts();

  try {
    // "Last 7 days" always ends at the night currently being viewed,
    // same reasoning as Sleep Duration's own trend window.
    const weekEnd = anchorDate;
    const fetches = [
      api(`/sleep/overview?date=${anchorDate}`),
      api(`/sleep/hypnogram?date=${anchorDate}`),
      api(`/sleep/vitals-series/${field}?date=${anchorDate}`),
      api(`/sleep/vitals-baseline/${field}?date=${anchorDate}`),
      api(`/sleep/vitals-trend/${field}?period=week&end_date=${weekEnd}`),
    ];
    // Resting HR is device-computed (not something this app aggregates
    // itself - see get_today_vitals()'s own comment on
    // HUAMI_HEART_RATE_RESTING_SAMPLE), and only meaningful for heart
    // rate specifically, not respiratory rate - fetched via the same
    // /today endpoint the Today tab itself uses (now including
    // resting_heart_rate's own "last" reading), rather than a new
    // endpoint just for this one value.
    if (field === "heart_rate") fetches.push(api(`/today?date=${anchorDate}`));
    const [overview, hypnogram, vitalsSeries, baseline, trend, todaySummary] = await Promise.all(fetches);

    if (overview === null) {
      content.innerHTML = `
        ${renderDateNav("day", anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep session recorded for this night.</p>
        </div>
      `;
      wireSubDetailDateNav(anchorDate, renderFn);
      return;
    }

    const avgValue = field === "heart_rate" ? overview.avg_heart_rate : overview.avg_respiratory_rate;
    // vitalsSeries is {"<device>": [{t,v},...]} with at most one
    // device by construction (get_sleep_vitals_series is already
    // scoped to the primary device server-side) - flatten to that
    // one device's own point list, or an empty list if it's missing
    // entirely (e.g. no readings of this specific field that night).
    const vitalsPoints = Object.values(vitalsSeries)[0] || [];

    // Same device as the rest of this page's own data (overview.device),
    // not just "whichever device happened first" in the /today response.
    const restingHr = (field === "heart_rate" && todaySummary && todaySummary.vitals && todaySummary.vitals.resting_heart_rate)
      ? todaySummary.vitals.resting_heart_rate[overview.device]?.last
      : undefined;

    content.innerHTML = `
      ${renderDateNav("day", anchorDate)}
      <p class="today-section-label">${escapeHtml(cfg.title)}</p>
      <div class="sleep-summary-card">
        <div class="sleep-summary-top">
          <span class="sleep-summary-duration">${avgValue !== null && avgValue !== undefined ? avgValue : "\u2013"}<span class="unit"> ${escapeHtml(cfg.unit)}</span></span>
          <span class="sleep-summary-date">${escapeHtml(anchorDate)}</span>
        </div>
      </div>

      <div class="sleep-hypnogram-card">
        ${vitalsPoints.length > 0 && hypnogram.length > 0
          ? buildVitalsHypnogramSVG(vitalsPoints, hypnogram, { width: 800, height: 180 })
          : `<p class="metric-card-empty">No data for this night</p>`}
      </div>

      ${restingHr !== undefined ? `
        <div class="sleep-summary-card">
          <div class="sleep-summary-top">
            <span class="sleep-summary-duration">${restingHr}<span class="unit"> ${escapeHtml(cfg.unit)}</span></span>
            <span class="sleep-summary-date">Resting HR</span>
          </div>
        </div>
      ` : ""}

      <p class="today-section-label">Last 7 Days</p>
      <div class="detail-chart-card">
        <canvas id="sleep-vitals-trend-chart"></canvas>
      </div>

      ${renderBaselineBar(baseline, 7, { lowLabel: cfg.lowLabel, highLabel: cfg.highLabel, unit: cfg.unit, decimals: cfg.decimals })}
    `;
    wireSubDetailDateNav(anchorDate, renderFn);

    if (trend.length > 0) {
      // Same per-night-own-device grouping as Sleep Duration's trend -
      // but this chart shows each night's real MIN-MAX RANGE (with its
      // own median tick, via buildRangeBarChart's existing per-bar
      // marker) rather than a single value-per-night bar, since sleep
      // HR/respiratory rate genuinely varies within a night and that
      // range is the more useful comparison than just the nightly mean.
      const devices = [...new Set(trend.map(t => t.device))];
      const series = {};
      devices.forEach(d => { series[d] = []; });
      trend.forEach(t => { series[t.device].push({ t: t.date, min: t.min, max: t.max, median: t.median }); });

      // A tight y-axis (min-to-max-plus-margin) rather than Chart.js's
      // own default scaling for a floating-bar dataset, which trends
      // toward including 0 - same fix already applied to Sleep
      // Regularity's own range-bar chart, applied proactively here
      // too rather than waiting for the same symptom to get reported
      // again for a different chart.
      const allValues = trend.flatMap(t => [t.min, t.max]);
      const span = Math.max(...allValues) - Math.min(...allValues);
      const margin = Math.max(span * 0.15, 2);
      const yMin = Math.min(...allValues) - margin;
      const yMax = Math.max(...allValues) + margin;

      // The flat "week average" line uses each night's own MEAN (not
      // median) - matching the same "average of nightly averages"
      // semantics as this page's own big number and Sleep Duration's
      // "Week average" line, not a separate, differently-defined
      // statistic.
      const meanValues = trend.map(t => t.mean).filter(v => v !== null && v !== undefined);
      const weekMean = meanValues.length ? meanValues.reduce((a, b) => a + b, 0) / meanValues.length : null;

      const chart = buildRangeBarChart(
        document.getElementById("sleep-vitals-trend-chart"), series, devices, "week",
        {}, yMin, cfg.decimals, undefined,
        { yMax, meanLine: weekMean !== null ? { value: weekMean, label: "Week average" } : undefined }
      );
      if (chart) registerActiveChart(chart);
    } else {
      document.getElementById("sleep-vitals-trend-chart").replaceWith(Object.assign(document.createElement("p"), {
        className: "metric-card-empty", textContent: "No data for the last 7 days",
      }));
    }
  } catch (e) {
    content.innerHTML = `${renderDateNav("day", anchorDate)}<p class="status">Error loading ${escapeHtml(cfg.title.toLowerCase())} data: ${escapeHtml(e.message)}</p>`;
    wireSubDetailDateNav(anchorDate, renderFn);
  }
}

// --- Sleep Regularity ---
// Structurally different from the other 3 sub-detail pages: no single
// night's own "big number" (regularity is inherently a multi-night
// consistency question), and Zepp's own 0-100% regularity score isn't
// reproducible (its scoring formula was never published - see
// UI_DESIGN_NOTES.md's own note on this page). Built instead as three
// visualizations of the same underlying per-night timing data that
// don't need that formula to be useful: a bedtime-to-waketime range
// per night, and separate consistency scatters for each end of that
// range. All three come from the SAME /sleep/timing-trend fetch
// already built for Sleep Duration - no new backend endpoint needed.

// Bedtimes are naturally PM, wake times AM - plotting raw 24h time-of-
// day would put a night's own bedtime and wake time far apart on the
// axis instead of adjacent. Anchoring the day at noon (rather than
// midnight) keeps a normal night contiguous: times before noon are
// treated as continuing past 24 (7:00 AM -> 31), so an 11 PM bedtime
// (23) and a 7 AM wake time (31) sit 8 hours apart on the axis, matching
// the real elapsed time between them. Assumes a conventional bedtime-
// after-noon / wake-before-noon pattern - a night that doesn't fit that
// (e.g. an extreme shift-work schedule) would plot oddly, but that's a
// reasonable simplification for what this chart is for.
function noonAnchoredHour(iso) {
  const d = new Date(iso);
  let h = d.getHours() + d.getMinutes() / 60;
  if (h < 12) h += 24;
  return h;
}

function formatClockTime(hour) {
  const h = ((hour % 24) + 24) % 24;
  const period = h < 12 ? "AM" : "PM";
  const h12raw = Math.floor(h) % 12;
  const h12 = h12raw === 0 ? 12 : h12raw;
  const min = Math.round((h % 1) * 60);
  return `${h12}:${String(min).padStart(2, "0")} ${period}`;
}

// The Sleep Regularity Index (SRI) - a real, peer-reviewed, widely-
// validated metric (Phillips et al. 2017), not an attempt to
// reproduce Zepp's own unpublished 0-100% "regularity" score (see
// UI_DESIGN_NOTES.md's own note that that formula was never
// published). Deliberately no "good/bad" tier label here - the
// literature classifies regular-vs-irregular sleepers by COHORT-
// SPECIFIC quintiles (top/bottom fifth of whatever study population),
// not a fixed universal cutoff, so inventing one here would be
// claiming more precision than the source actually supports. The raw
// score plus its own citation is the honest amount of interpretation
// to offer.
function renderSleepRegularityIndex(sri) {
  if (sri === null) {
    return `
      <div class="sleep-summary-card">
        <p class="metric-card-empty">Not enough consecutive nights of data yet to compute a Sleep Regularity Index (needs at least 2 consecutive night-to-night comparisons).</p>
      </div>
    `;
  }
  return `
    <div class="sleep-summary-card">
      <div class="sleep-summary-top">
        <span class="sleep-summary-duration">${sri.sri}</span>
        <span class="sleep-summary-date">Sleep Regularity Index</span>
      </div>
      <p class="sleep-duration-source">
        The percentage probability of being asleep (or awake) at the same clock time on any two nights \u2013 100 means an identical sleep/wake schedule every night, 0 a statistically random pattern, and negative values a consistently reversed one. Based on ${sri.pairs_used} night-to-night comparison${sri.pairs_used === 1 ? "" : "s"}. Source: ${escapeHtml(sri.source)}.
      </p>
    </div>
  `;
}

export async function openSleepRegularityDetail(anchorDate = todayISO()) {
  openDetailScreen("Sleep Regularity");
  await renderSleepRegularityDay(anchorDate);
}

async function renderSleepRegularityDay(anchorDate) {
  const content = document.getElementById("detail-content");
  content.innerHTML = `${renderDateNav("day", anchorDate)}<p class="muted">Loading...</p>`;
  wireSubDetailDateNav(anchorDate, renderSleepRegularityDay);

  clearActiveCharts();

  try {
    const [trend, sri] = await Promise.all([
      api(`/sleep/timing-trend?period=week&end_date=${anchorDate}`),
      api(`/sleep/regularity-index?end_date=${anchorDate}`),
    ]);

    if (!trend.length) {
      content.innerHTML = `
        ${renderDateNav("day", anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep sessions recorded in the last 7 days.</p>
        </div>
      `;
      wireSubDetailDateNav(anchorDate, renderSleepRegularityDay);
      return;
    }

    const bedtimes = trend.map(t => noonAnchoredHour(t.start_time));
    const waketimes = trend.map(t => noonAnchoredHour(t.end_time));
    const avgBedtime = bedtimes.reduce((a, b) => a + b, 0) / bedtimes.length;
    const avgWaketime = waketimes.reduce((a, b) => a + b, 0) / waketimes.length;

    content.innerHTML = `
      ${renderDateNav("day", anchorDate)}
      ${renderSleepRegularityIndex(sri)}
      <div class="sleep-summary-card">
        <div class="sleep-stats-row">
          <div class="sleep-stat-item">
            <span class="sleep-stat-value">${formatClockTime(avgBedtime)}</span>
            <span class="sleep-stat-label">Avg Bedtime</span>
          </div>
          <div class="sleep-stat-item">
            <span class="sleep-stat-value">${formatClockTime(avgWaketime)}</span>
            <span class="sleep-stat-label">Avg Wake Time</span>
          </div>
        </div>
      </div>

      <p class="today-section-label">Last 7 Days</p>
      <div class="detail-chart-card">
        <canvas id="sleep-regularity-window-chart"></canvas>
      </div>

      <p class="today-section-label">Went to Bed</p>
      <div class="detail-chart-card">
        <canvas id="sleep-regularity-bedtime-chart"></canvas>
      </div>

      <p class="today-section-label">Get Up</p>
      <div class="detail-chart-card">
        <canvas id="sleep-regularity-waketime-chart"></canvas>
      </div>
    `;
    wireSubDetailDateNav(anchorDate, renderSleepRegularityDay);

    const devices = [...new Set(trend.map(t => t.device))];

    // Shared margin helper - a tight [min,max] axis (rather than
    // Chart.js's own default auto-scaling, which for a floating-bar
    // dataset tends toward including 0 and produced a near-24-hour-wide
    // axis here) with a little breathing room on each side so a point
    // sitting exactly at the real min/max isn't drawn flush against the
    // chart's own edge.
    function marginRange(values, marginHours) {
      const min = Math.min(...values);
      const max = Math.max(...values);
      return [min - marginHours, max + marginHours];
    }

    const midpoints = trend.map((t, i) => (bedtimes[i] + waketimes[i]) / 2);
    const meanMidpoint = midpoints.reduce((a, b) => a + b, 0) / midpoints.length;
    const [windowMin, windowMax] = marginRange([...bedtimes, ...waketimes], 0.5);

    const windowSeries = {};
    devices.forEach(d => { windowSeries[d] = []; });
    trend.forEach(t => {
      windowSeries[t.device].push({
        t: t.date,
        min: noonAnchoredHour(t.start_time),
        max: noonAnchoredHour(t.end_time),
        // No `median` here (unlike other buildRangeBarChart callers) -
        // this chart shows ONE flat week-mean line (extra.meanLine
        // below) instead of a per-night median tick, so there's
        // nothing for the shared median-marker plugin to draw per bar.
      });
    });
    const windowChart = buildRangeBarChart(
      document.getElementById("sleep-regularity-window-chart"), windowSeries, devices, "week",
      {}, windowMin, undefined, formatClockTime,
      { yMax: windowMax, meanLine: { value: meanMidpoint, label: "Week average" } }
    );
    if (windowChart) registerActiveChart(windowChart);

    const [bedtimeMin, bedtimeMax] = marginRange(bedtimes, 0.5);
    const bedtimeSeries = {};
    devices.forEach(d => { bedtimeSeries[d] = []; });
    trend.forEach(t => { bedtimeSeries[t.device].push({ t: t.date, value: noonAnchoredHour(t.start_time) }); });
    const bedtimeChart = buildTimeScatterChart(
      document.getElementById("sleep-regularity-bedtime-chart"), bedtimeSeries, devices,
      { yTickCallback: formatClockTime, connectLine: true, yMin: bedtimeMin, yMax: bedtimeMax }
    );
    if (bedtimeChart) registerActiveChart(bedtimeChart);

    const [waketimeMin, waketimeMax] = marginRange(waketimes, 0.5);
    const waketimeSeries = {};
    devices.forEach(d => { waketimeSeries[d] = []; });
    trend.forEach(t => { waketimeSeries[t.device].push({ t: t.date, value: noonAnchoredHour(t.end_time) }); });
    const waketimeChart = buildTimeScatterChart(
      document.getElementById("sleep-regularity-waketime-chart"), waketimeSeries, devices,
      { yTickCallback: formatClockTime, connectLine: true, yMin: waketimeMin, yMax: waketimeMax }
    );
    if (waketimeChart) registerActiveChart(waketimeChart);
  } catch (e) {
    content.innerHTML = `${renderDateNav("day", anchorDate)}<p class="status">Error loading sleep regularity data: ${escapeHtml(e.message)}</p>`;
    wireSubDetailDateNav(anchorDate, renderSleepRegularityDay);
  }
}
// --- Sleep Reports (linked from the main Sleep tab) ---
// Zepp's own version of this same idea (UI_DESIGN_NOTES.md's "Time
// Asleep (advanced)" page notes) bundles everything richer than a
// single night's own view into one W/M rollup page - composition,
// regularity, per-stage duration trends, vitals trends, and the
// journal rollup - rather than spreading it across the Sleep Duration
// page's own period views (which now stays focused on duration alone,
// per direct request, matching Zepp's own separation between its
// simpler "Sleep Duration" page and this richer one).
import { escapeHtml, api, todayISO } from "./core.js";
import { openDetailScreen, registerActiveChart, clearActiveCharts, renderDateNav, renderPeriodButtons, wireDetailControls } from "./metric-detail.js";
import { buildSleepStageStackedChart, buildBedtimeWaketimeChart, buildTrendBarChart, buildTimeScatterChart, HYPNOGRAM_STAGE_COLORS } from "./metric-charts.js";
import { noonAnchoredHour, formatClockTime } from "./sleep-detail.js";

// Only week/month, deliberately - no day (a single night has nothing
// to roll up) and no year (get_sleep_stage_trend/get_sleep_timing_trend
// both return one row per NIGHT regardless of period, which would mean
// up to 365 raw bars for a year on several of these charts - the same
// reasoning already applied to the Sleep Duration page's own period
// selector).
const SLEEP_REPORTS_PERIODS = ["week", "month"];

// Every chart on this page (Deep/REM/Awake, Heart Rate, Respiratory
// Rate) previously landed on the exact same color, all for the same
// reason as the earlier Workout Detail page bug: each is built with a
// single, fake "device" label (a metric name, not a real device -
// this app's own sleep data is confirmed single-device-per-night by
// construction, see _primary_device_session's own docstring), and
// device-index-based coloring always picks index 0 when there's only
// one entry. Deep/REM/Awake reuse the EXACT same colors as the Sleep
// Composition chart directly above them (HYPNOGRAM_STAGE_COLORS) - a
// deliberate choice, not just "any 3 distinct colors": the same
// "Deep" purple in both charts reinforces that they're the same
// metric, not an unrelated new palette. Heart Rate/Respiratory Rate
// get their own colors, matching the red=heart/blue=breath
// associations already used for Workout Detail's own charts.
const REPORT_COLORS = {
  deep: HYPNOGRAM_STAGE_COLORS.deep,
  rem: HYPNOGRAM_STAGE_COLORS.rem,
  awake: HYPNOGRAM_STAGE_COLORS.awake,
  heartRate: "#ff6b6b",
  respiratoryRate: "#6ea8fe",
};

export async function openSleepReportsDetail(anchorDate = todayISO()) {
  openDetailScreen("Sleep Reports");
  await renderSleepReportsPeriod("week", anchorDate);
}

function sectionCard(title, canvasId) {
  return `
    <p class="today-section-label">${escapeHtml(title)}</p>
    <div class="detail-chart-card">
      <canvas id="${canvasId}"></canvas>
    </div>
  `;
}

// Shared margin helper - same reasoning as the Sleep Regularity page's
// own copy (a tight [min,max] axis with a little breathing room,
// rather than Chart.js's own default auto-scaling toward 0).
// Margin is 10% of the actual bedtime/waketime range, not a flat
// fixed amount - a real reported gap, the old fixed 0.5h margin read
// as too tight relative to a typical multi-hour bedtime/waketime
// spread. Floored at 0.5h so a genuinely tight period (bedtime barely
// varying) still gets a visibly real margin rather than one that
// shrinks toward nothing. Same reasoning/implementation as the
// identical helper in sleep-detail.js's own Sleep Regularity page.
function marginRange(values, minMarginHours = 0.5) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const margin = Math.max((max - min) * 0.1, minMarginHours);
  return [min - margin, max + margin];
}

// Deep/REM/Awake each get the exact same "bar chart + mean line"
// shape as the existing sleep-duration trend chart (buildTrendBarChart)
// - just plotting a different field per night. stageTrend already has
// everything needed (one fetch already covers Sleep Composition above
// AND all three of these), no extra API calls per chart.
function buildStageMinutesSeries(stageTrend, stageKey) {
  return { "Minutes": stageTrend.map(entry => ({ t: entry.date, value: entry.stages_min[stageKey] ?? 0 })) };
}

// --- Bedtime Journal / Wake-up Mood / Sleep Qualifiers rollup ---
// Moved here from sleep-detail.js (where it briefly lived alongside
// the Sleep Duration page's own week/month view, before that page was
// simplified back to just duration) - this is where it belongs
// alongside the rest of the richer weekly/monthly breakdowns.
function humanizeTagLabel(key) {
  const words = key.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Mood scores are numbers (1-5), not tag keys - rendered as the same
// filled/empty circle rating already used for a single night's own
// journal entry elsewhere (sleep-overview.js's renderJournalReadOnly),
// so a rollup row for "mostly 4s" reads the same way a single night's
// own score does, not as a bare unlabeled number.
function moodScoreLabel(score) {
  return "\u25cf".repeat(score) + "\u25cb".repeat(5 - score);
}

function renderJournalRollupRow(label, pct, nights) {
  return `
    <div class="workout-zone-row">
      <div class="workout-zone-header">
        <span class="workout-zone-name">${escapeHtml(label)}</span>
        <span class="metric-sub">${pct}%</span>
      </div>
      <div class="workout-zone-bar-track">
        <div class="workout-zone-bar-fill" style="width: ${pct}%"></div>
      </div>
      <div class="workout-zone-footer">
        <span class="metric-sub">${nights} night${nights === 1 ? "" : "s"}</span>
      </div>
    </div>
  `;
}

function renderJournalRollupSection(title, rows, noRecordNights, noRecordPct, labelFn) {
  if (rows.length === 0 && noRecordNights === 0) return "";
  const rowsHtml = rows.map(r => renderJournalRollupRow(labelFn(r.key), r.pct, r.nights)).join("")
    + (noRecordNights > 0 ? renderJournalRollupRow("No record", noRecordPct, noRecordNights) : "");
  return `
    <p class="today-section-label">${escapeHtml(title)}</p>
    <div class="sleep-summary-card">${rowsHtml}</div>
  `;
}

function renderJournalRollup(rollup) {
  if (!rollup) return "";
  return `
    ${renderJournalRollupSection("Bedtime Journal", rollup.pre_sleep_factors, rollup.no_record_nights, rollup.no_record_pct, humanizeTagLabel)}
    ${renderJournalRollupSection("Wake-up Mood", rollup.mood_scores, rollup.no_record_nights, rollup.no_record_pct, moodScoreLabel)}
    ${renderJournalRollupSection("Sleep Qualifiers", rollup.qualifiers, rollup.no_record_nights, rollup.no_record_pct, humanizeTagLabel)}
  `;
}

async function renderSleepReportsPeriod(period, anchorDate) {
  const content = document.getElementById("detail-content");
  content.innerHTML = `${renderPeriodButtons(period, SLEEP_REPORTS_PERIODS)}${renderDateNav(period, anchorDate)}<p class="muted">Loading...</p>`;
  wireDetailControls(renderSleepReportsPeriod, period, anchorDate);

  clearActiveCharts();

  try {
    const [timingTrend, stageTrend, hrTrend, respTrend, journalRollup] = await Promise.all([
      api(`/sleep/timing-trend?period=${period}&end_date=${anchorDate}`),
      api(`/sleep/stage-trend?period=${period}&end_date=${anchorDate}`),
      api(`/sleep/vitals-trend/heart_rate?period=${period}&end_date=${anchorDate}`),
      api(`/sleep/vitals-trend/sleep_respiratory_rate?period=${period}&end_date=${anchorDate}`),
      api(`/sleep/journal-rollup?period=${period}&end_date=${anchorDate}`),
    ]);

    if (timingTrend.length === 0 && stageTrend.length === 0) {
      content.innerHTML = `
        ${renderPeriodButtons(period, SLEEP_REPORTS_PERIODS)}
        ${renderDateNav(period, anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep sessions recorded for this period.</p>
        </div>
      `;
      wireDetailControls(renderSleepReportsPeriod, period, anchorDate);
      return;
    }

    // No point markers at all on the month view - a direct request,
    // since 28-31 dots on one chart reads as clutter. Week (7 points)
    // keeps them.
    const pointRadius = period === "week" ? undefined : 0;

    content.innerHTML = `
      ${renderPeriodButtons(period, SLEEP_REPORTS_PERIODS)}
      ${renderDateNav(period, anchorDate)}
      ${stageTrend.length > 0 ? sectionCard("Sleep Composition", "sleep-reports-composition-chart") : ""}
      ${timingTrend.length > 0 ? sectionCard("Sleep Regularity", "sleep-reports-regularity-chart") : ""}
      ${stageTrend.length > 0 ? sectionCard("Deep Sleep", "sleep-reports-deep-chart") : ""}
      ${stageTrend.length > 0 ? sectionCard("REM Sleep", "sleep-reports-rem-chart") : ""}
      ${stageTrend.length > 0 ? sectionCard("Awake Time", "sleep-reports-awake-chart") : ""}
      ${hrTrend.length > 0 ? sectionCard("Sleep Heart Rate", "sleep-reports-hr-chart") : ""}
      ${respTrend.length > 0 ? sectionCard("Sleep Respiratory Rate", "sleep-reports-resp-chart") : ""}
      ${renderJournalRollup(journalRollup)}
    `;
    wireDetailControls(renderSleepReportsPeriod, period, anchorDate);

    if (stageTrend.length > 0) {
      const compositionChart = buildSleepStageStackedChart(
        document.getElementById("sleep-reports-composition-chart"), stageTrend, { labelFormat: "day" }
      );
      if (compositionChart) registerActiveChart(compositionChart);

      const deepChart = buildTrendBarChart(
        document.getElementById("sleep-reports-deep-chart"), buildStageMinutesSeries(stageTrend, "deep"), ["Minutes"],
        { yAxisTitle: "minutes", decimals: 0, unit: "m", meanLabel: "Average", colorOverride: REPORT_COLORS.deep }
      );
      if (deepChart) registerActiveChart(deepChart);

      const remChart = buildTrendBarChart(
        document.getElementById("sleep-reports-rem-chart"), buildStageMinutesSeries(stageTrend, "rem"), ["Minutes"],
        { yAxisTitle: "minutes", decimals: 0, unit: "m", meanLabel: "Average", colorOverride: REPORT_COLORS.rem }
      );
      if (remChart) registerActiveChart(remChart);

      const awakeChart = buildTrendBarChart(
        document.getElementById("sleep-reports-awake-chart"), buildStageMinutesSeries(stageTrend, "awake"), ["Minutes"],
        { yAxisTitle: "minutes", decimals: 0, unit: "m", meanLabel: "Average", colorOverride: REPORT_COLORS.awake }
      );
      if (awakeChart) registerActiveChart(awakeChart);
    }

    if (timingTrend.length > 0) {
      const bedtimes = timingTrend.map(t => noonAnchoredHour(t.start_time));
      const waketimes = timingTrend.map(t => noonAnchoredHour(t.end_time));
      const [yMin, yMax] = marginRange([...bedtimes, ...waketimes]);
      const regularityChart = buildBedtimeWaketimeChart(
        document.getElementById("sleep-reports-regularity-chart"), timingTrend,
        {
          bedtimeValue: t => noonAnchoredHour(t.start_time),
          waketimeValue: t => noonAnchoredHour(t.end_time),
          yTickCallback: formatClockTime,
          yMin, yMax,
          showMeanLines: true,
          pointRadius,
        }
      );
      if (regularityChart) registerActiveChart(regularityChart);
    }

    if (hrTrend.length > 0) {
      const hrSeries = { "Heart Rate": hrTrend.map(t => ({ t: t.date, value: t.mean })) };
      const hrChart = buildTimeScatterChart(
        document.getElementById("sleep-reports-hr-chart"), hrSeries, ["Heart Rate"],
        { yTickCallback: (v) => `${Math.round(v)} bpm`, connectLine: true, meanLabel: "Average", pointRadius, colorOverride: REPORT_COLORS.heartRate }
      );
      if (hrChart) registerActiveChart(hrChart);
    }

    if (respTrend.length > 0) {
      const respSeries = { "Respiratory Rate": respTrend.map(t => ({ t: t.date, value: t.mean })) };
      const respChart = buildTimeScatterChart(
        document.getElementById("sleep-reports-resp-chart"), respSeries, ["Respiratory Rate"],
        { yTickCallback: (v) => `${Math.round(v)} brpm`, connectLine: true, meanLabel: "Average", pointRadius, colorOverride: REPORT_COLORS.respiratoryRate }
      );
      if (respChart) registerActiveChart(respChart);
    }
  } catch (e) {
    content.innerHTML = `${renderPeriodButtons(period, SLEEP_REPORTS_PERIODS)}${renderDateNav(period, anchorDate)}<p class="status">Error loading sleep reports data: ${escapeHtml(e.message)}</p>`;
    wireDetailControls(renderSleepReportsPeriod, period, anchorDate);
  }
}
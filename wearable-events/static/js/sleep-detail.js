// --- Sleep sub-detail pages (Sleep Duration, and later Sleep Heart
// Rate / Sleep Respiratory Rate / Sleep Regularity) - each linked from
// the main Sleep tab, opened in the shared detail-screen overlay
// (same one metric-detail.js's DETAIL_VIEWS and activity.js use).
import { escapeHtml, api, todayISO, shiftISODate } from "./core.js";
import { openDetailScreen, registerActiveChart, clearActiveCharts, renderDateNav } from "./metric-detail.js";
import { buildRangeBarChart } from "./metric-charts.js";

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

export async function openSleepDurationDetail(anchorDate = todayISO()) {
  openDetailScreen("Sleep Duration");
  await renderSleepDurationDay(anchorDate);
}

async function renderSleepDurationDay(anchorDate) {
  const content = document.getElementById("detail-content");
  content.innerHTML = `${renderDateNav("day", anchorDate)}<p class="muted">Loading...</p>`;
  wireDurationDateNav(anchorDate);

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
        ${renderDateNav("day", anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep session recorded for this night.</p>
        </div>
      `;
      wireDurationDateNav(anchorDate);
      return;
    }

    const rec = overview.duration_recommendation;
    const goalHours = overview.duration_goal_s / 3600;

    content.innerHTML = `
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
    wireDurationDateNav(anchorDate);

    if (trend.length > 0) {
      // Group by each night's own reporting device, not assumed to
      // match the currently-viewed night's device - realistically a
      // single-device app, but a week's worth of nights could in
      // principle span a device change.
      const devices = [...new Set(trend.map(t => t.device))];
      const series = {};
      devices.forEach(d => { series[d] = []; });
      trend.forEach(t => {
        const hours = t.duration_s / 3600;
        series[t.device].push({ t: t.date, min: hours, max: hours, median: hours });
      });
      const chart = buildRangeBarChart(
        document.getElementById("sleep-duration-trend-chart"), series, devices, "week", {}, 0, 1
      );
      if (chart) registerActiveChart(chart);
    } else {
      document.getElementById("sleep-duration-trend-chart").replaceWith(Object.assign(document.createElement("p"), {
        className: "metric-card-empty", textContent: "No sleep data for the last 7 days",
      }));
    }
  } catch (e) {
    content.innerHTML = `${renderDateNav("day", anchorDate)}<p class="status">Error loading sleep duration data: ${escapeHtml(e.message)}</p>`;
    wireDurationDateNav(anchorDate);
  }
}

// Trimmed-down date-nav, same pattern as Today's/Sleep overview's own
// (prev/next/date-picker only, no period switcher - this page is a
// single "day" view, not a D/W/M/Y one, per UI_DESIGN_NOTES.md).
// Scoped to #detail-content specifically - the same class of bug
// fixed earlier this session for Today's own date-nav applies here
// too, now that multiple containers with identical .date-nav-btn
// markup can coexist in the DOM.
function wireDurationDateNav(anchorDate) {
  const container = document.getElementById("detail-content");
  const prevBtn = container.querySelector('.date-nav-btn[data-nav="prev"]');
  const nextBtn = container.querySelector('.date-nav-btn[data-nav="next"]');
  if (prevBtn) {
    prevBtn.addEventListener("click", () => renderSleepDurationDay(shiftISODate(anchorDate, -1)));
  }
  if (nextBtn && !nextBtn.disabled) {
    nextBtn.addEventListener("click", () => renderSleepDurationDay(shiftISODate(anchorDate, 1)));
  }

  const dateInput = container.querySelector(".date-nav-input");
  const dateLabel = container.querySelector(".date-nav-label");
  if (dateInput) {
    dateInput.addEventListener("change", () => {
      if (dateInput.value) renderSleepDurationDay(dateInput.value);
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
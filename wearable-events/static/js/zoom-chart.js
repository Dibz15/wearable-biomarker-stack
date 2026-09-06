import { escapeHtml, formatNum } from "./core.js";

// Shared fullscreen zoomed-chart view - opened from an "expand" button
// on any time-series chart (the sleep hypnogram, workout detail's own
// per-sample charts). One reusable component, not a page-specific one,
// per the person's own explicit request: a minimap strip with the
// current zoom window highlighted, a scrubber to pan that window, and
// checkboxes to toggle which series overlay the main chart - MULTIPLE
// at once (not a single-select tab group), e.g. HR + respiratory rate
// together on the sleep hypnogram zoom, or HR + elevation + cadence +
// stride + speed together on a workout's zoom.

const DEFAULT_WINDOW_MINUTES = 60;

// Module-level, not per-call state - only one zoom view can be open at
// a time (it's a fullscreen overlay), so there's nothing gained by
// threading this through every helper's own arguments instead.
let mainChart = null;
let activeState = null;

/**
 * Open the zoom view.
 *
 * @param {object} config
 * @param {string} config.title - shown in the header.
 * @param {Array<{key: string, label: string, color: string, unit?: string,
 *   decimals?: number, defaultOn?: boolean, stepped?: boolean,
 *   valueLabels?: Record<number,string>,
 *   bands?: Array<{startMs: number, endMs: number, color: string, depth: number}>,
 *   points: Array<{t: string|number|Date, v: number}>}>} config.series
 *   - one entry per toggleable series. `points` need not be pre-sorted
 *   or pre-filtered to any particular range - the full available range
 *   across every series' own points becomes the pannable range.
 *   `stepped` draws a stepped (not smoothed) line on the main chart.
 *   `valueLabels` maps encoded numeric values to display names (e.g.
 *   sleep stage depth 0-3 to Deep/Light/REM/Awake) for both the axis
 *   ticks and the readout. `bands`, if provided on the FIRST series
 *   only, replaces the overview strip's default sparkline with real
 *   colored segments positioned by depth (0=bottom, 1=top) - used for
 *   the sleep hypnogram so the compressed overview actually resembles
 *   the real full-size hypnogram instead of a generic line.
 * @param {number} [config.windowMinutes] - initial zoom window width,
 *   clamped to the full available range if the range is narrower.
 * @param {() => void} [config.onBack] - called after the view closes.
 */
export function openZoomChart({ title, series, windowMinutes = DEFAULT_WINDOW_MINUTES, onBack = null }) {
  const screen = document.getElementById("zoom-screen");
  const content = document.getElementById("zoom-content");
  const titleEl = document.getElementById("zoom-title");
  const backBtn = document.getElementById("zoom-back-btn");

  titleEl.textContent = title;

  const { fullStart, fullEnd } = computeFullRange(series);

  const close = () => {
    screen.style.display = "none";
    if (mainChart) {
      mainChart.destroy();
      mainChart = null;
    }
    activeState = null;
    if (onBack) onBack();
  };
  backBtn.onclick = close;

  if (fullStart === null || fullStart === fullEnd) {
    content.innerHTML = `<p class="metric-card-empty">Not enough data to zoom into.</p>`;
    screen.style.display = "block";
    return;
  }

  const windowMs = Math.min(windowMinutes * 60 * 1000, fullEnd - fullStart);
  const enabledKeys = new Set(series.filter(s => s.defaultOn !== false).map(s => s.key));

  const state = {
    series, fullStart, fullEnd, windowMs, enabledKeys,
    windowStart: fullStart,
    selectedT: null,
  };
  activeState = state;

  content.innerHTML = `
    <div class="zoom-overview-wrap"><svg id="zoom-overview-svg" viewBox="0 0 300 60" preserveAspectRatio="none"></svg></div>
    <div id="zoom-readout" class="zoom-readout"></div>
    <div class="zoom-main-chart-wrap"><canvas id="zoom-main-chart"></canvas></div>
    <input type="range" id="zoom-scrubber" class="zoom-scrubber" min="0" max="1000" value="0" aria-label="Pan through time">
    <div id="zoom-series-toggles" class="zoom-series-toggles">
      ${series.map(s => `
        <label class="zoom-series-toggle">
          <input type="checkbox" data-series-key="${escapeHtml(s.key)}" ${enabledKeys.has(s.key) ? "checked" : ""}>
          <span class="zoom-series-toggle-swatch" style="background:${s.color}"></span>
          ${escapeHtml(s.label)}
        </label>
      `).join("")}
    </div>
  `;

  renderOverview(state);
  renderMainChart(state);
  renderReadout(state);
  wireScrubber(state);
  wireToggles(state);

  screen.style.display = "block";
}

// --- internals ---

function pointTimeMs(p) {
  return p.t instanceof Date ? p.t.getTime() : new Date(p.t).getTime();
}

function computeFullRange(series) {
  let fullStart = null, fullEnd = null;
  for (const s of series) {
    for (const p of s.points) {
      const t = pointTimeMs(p);
      if (fullStart === null || t < fullStart) fullStart = t;
      if (fullEnd === null || t > fullEnd) fullEnd = t;
    }
  }
  return { fullStart, fullEnd };
}

function findNearestPoint(points, targetT) {
  if (!points.length) return null;
  let best = points[0];
  let bestDiff = Math.abs(pointTimeMs(points[0]) - targetT);
  for (const p of points) {
    const diff = Math.abs(pointTimeMs(p) - targetT);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = p;
    }
  }
  return best;
}

function renderOverview(state) {
  const svg = document.getElementById("zoom-overview-svg");
  if (!svg) return;
  const { fullStart, fullEnd, windowStart, windowMs, series } = state;
  const span = fullEnd - fullStart;

  // Sparkline (or banded strip, see below) of the FIRST series only -
  // the overview is for "where am I in the whole range" context, not
  // precise reading, so it deliberately stays visually constant
  // regardless of which series are currently toggled on the main
  // chart below.
  const primary = series[0];
  let content = "";

  if (primary && primary.bands && primary.bands.length > 0) {
    // Banded style - real per-segment colors positioned at a vertical
    // depth (0=bottom, 1=top), e.g. the sleep hypnogram's own colored
    // stage bars (awake near top, deep near bottom, exactly like the
    // real full-size hypnogram) - a real reported problem with the
    // plain single-color sparkline this replaces: it read as a
    // meaningless zig-zag that didn't visually resemble sleep staging
    // at all. bands is optional and series-supplied - this stays
    // generic (any series could provide it), not hardcoded to sleep
    // specifically.
    const barHeight = 10;
    const usableHeight = 60 - barHeight;
    content = primary.bands
      .map(b => {
        const x = ((b.startMs - fullStart) / span) * 300;
        const w = Math.max(((b.endMs - b.startMs) / span) * 300, 1);
        const y = (1 - b.depth) * usableHeight;
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${barHeight}" rx="2" fill="${b.color}"/>`;
      })
      .join("");
  } else if (primary && primary.points.length > 1) {
    const values = primary.points.map(p => p.v);
    const minV = Math.min(...values);
    const maxV = Math.max(...values);
    const rangeV = maxV - minV || 1;
    const pathD = primary.points
      .map((p, i) => {
        const x = ((pointTimeMs(p) - fullStart) / span) * 300;
        const y = 55 - ((p.v - minV) / rangeV) * 50;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    content = `<path d="${pathD}" fill="none" stroke="${primary.color}" stroke-width="1.5" opacity="0.6"/>`;
  }

  const winX = ((windowStart - fullStart) / span) * 300;
  const winW = Math.max((windowMs / span) * 300, 2);

  svg.innerHTML = `
    ${content}
    <rect class="zoom-window-rect" x="${winX.toFixed(1)}" y="0" width="${winW.toFixed(1)}" height="60"/>
  `;
}

function computeFullSeriesRange(s) {
  if (!s.points.length) return { min: 0, max: 1 };
  const values = s.points.map(p => p.v);
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (s.valueLabels) {
    // A categorical/encoded series (e.g. sleep stage depth) - use the
    // exact label range with a small fixed pad so the top/bottom step
    // doesn't touch the chart edge, not a percentage-of-range grace
    // (a percentage of a 0-3 range would barely pad it at all).
    const labelKeys = Object.keys(s.valueLabels).map(Number);
    return { min: Math.min(...labelKeys) - 0.5, max: Math.max(...labelKeys) + 0.5 };
  }
  const range = max - min || 1;
  const grace = range * 0.1;
  return { min: min - grace, max: max + grace };
}

// Points strictly inside [windowStart, windowEnd], PLUS one point just
// outside each edge (the last one before windowStart, the first one
// after windowEnd) if they exist. A real reported problem with a
// strict in-window-only filter: a segment that STARTS before the
// window (e.g. a sleep stage that began 10 minutes before the visible
// window) had its own start point excluded, so a stepped line had no
// value at all at the left edge until the NEXT segment's start
// scrolled into view - whole stretches visibly vanished while
// scrubbing even though the underlying stage never actually stopped.
// Including one padding point past each edge lets the line (stepped
// or smooth) draw continuously across the full visible width,
// clipped by the chart's own x min/max rather than by this filter.
// Single pass, points assumed chronologically ordered (true for every
// real caller so far).
function windowedPointsWithPadding(points, windowStart, windowEnd) {
  let beforeIdx = -1;
  let afterIdx = -1;
  const within = [];
  for (let i = 0; i < points.length; i++) {
    const t = pointTimeMs(points[i]);
    if (t < windowStart) {
      beforeIdx = i; // keeps advancing to the LATEST point still before the window
    } else if (t > windowEnd) {
      if (afterIdx === -1) afterIdx = i; // only the FIRST point past the window
    } else {
      within.push(points[i]);
    }
  }
  const result = [];
  if (beforeIdx !== -1) result.push(points[beforeIdx]);
  result.push(...within);
  if (afterIdx !== -1) result.push(points[afterIdx]);
  return result;
}

// Explicit, evenly-spaced tick positions across [min, max] - NOT left
// to Chart.js's own "linear" scale default, which picks "nice round
// NUMBERS" (e.g. step sizes like 1e5, 5e5, 1e6 ms) with no awareness
// that these values are actually timestamps. A real reported bug this
// caused: depending on exactly where the current window's min/max
// happened to fall, sometimes ZERO of those round-number positions
// landed inside the visible range, sometimes just one - so as the
// window panned, the tick count flickered between 0 and 1 rather than
// staying stable, and the one tick that DID show jumped around
// unpredictably rather than sliding smoothly with the window. Evenly
// dividing the CURRENT window itself into `count` fixed fractions
// (0%, 1/3, 2/3, 100% for count=4) guarantees a stable count and
// smooth, proportional movement regardless of the absolute timestamp
// values involved.
function computeEvenTicks(min, max, count) {
  const ticks = [];
  const step = (max - min) / (count - 1);
  for (let i = 0; i < count; i++) {
    ticks.push({ value: min + step * i });
  }
  return ticks;
}

function renderMainChart(state) {
  const canvas = document.getElementById("zoom-main-chart");
  if (!canvas) return;
  if (mainChart) {
    mainChart.destroy();
    mainChart = null;
  }

  const windowEnd = state.windowStart + state.windowMs;
  const enabledSeries = state.series.filter(s => state.enabledKeys.has(s.key));
  if (enabledSeries.length === 0) return;

  const datasets = enabledSeries.map(s => {
    const windowed = windowedPointsWithPadding(s.points, state.windowStart, windowEnd);
    return {
      label: s.label,
      seriesKey: s.key,
      data: windowed.map(p => ({ x: pointTimeMs(p), y: p.v })),
      borderColor: s.color,
      backgroundColor: s.color,
      yAxisID: `y-${s.key}`,
      pointRadius: windowed.length <= 1 ? 4 : 0,
      borderWidth: 2,
      tension: s.stepped ? 0 : 0.2,
      stepped: s.stepped || false,
    };
  });

  const scales = {
    x: {
      type: "linear",
      min: state.windowStart,
      max: windowEnd,
      // Overrides Chart.js's own auto-tick algorithm entirely (see
      // computeEvenTicks's own comment for why the default was unstable
      // here) with exactly 4 evenly-spaced positions across the current
      // window - stable count, smooth movement while panning.
      afterBuildTicks: (axis) => {
        axis.ticks = computeEvenTicks(state.windowStart, windowEnd, 4);
      },
      ticks: {
        color: "#8a8d99",
        callback: (val) => new Date(val).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      },
      grid: { color: "#2a2d38" },
    },
  };
  // Only the FIRST enabled series gets a visible/labeled y-axis - with
  // several series of different units overlaid (bpm, meters, spm...) a
  // shared or fully-labeled multi-axis reads as clutter, and the exact
  // numbers are already available per series in the readout below.
  // Every series still gets its OWN scale internally (yAxisID), just
  // hidden ones for anything past the first, so each line is still
  // shown proportionally within its own real range rather than
  // fighting for the same axis. A series with valueLabels (e.g. the
  // sleep hypnogram, whose points are a numeric stage-depth encoding,
  // not a real physical quantity) gets those as its own axis tick
  // labels instead of raw numbers, when it's the visible one.
  //
  // min/max are computed from the series' FULL data (computeFullSeriesRange),
  // not just whatever's currently windowed - a real reported problem
  // with an earlier version, where scrubbing panned into a locally-flat
  // stretch and the auto-scaled axis kept rescaling to fit just that
  // stretch, making the line visually jump even though nothing about
  // the underlying data actually changed. A fixed range computed once
  // up front keeps the same line looking the same regardless of which
  // window is currently in view.
  enabledSeries.forEach((s, i) => {
    const range = computeFullSeriesRange(s);
    scales[`y-${s.key}`] = {
      position: i === 0 ? "left" : "right",
      display: i === 0,
      min: range.min,
      max: range.max,
      grid: { display: i === 0, color: "#2a2d38" },
      ticks: {
        color: "#8a8d99",
        ...(s.valueLabels ? { callback: (v) => s.valueLabels[Math.round(v)] ?? "", stepSize: 1 } : {}),
      },
    };
  });

  mainChart = new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      scales,
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
      },
      onClick: (_evt, elements) => {
        if (!elements.length) return;
        const el = elements[0];
        const point = mainChart.data.datasets[el.datasetIndex].data[el.index];
        state.selectedT = point.x;
        renderReadout(state);
      },
    },
  });
}

function renderReadout(state) {
  const readout = document.getElementById("zoom-readout");
  if (!readout) return;

  if (state.selectedT === null) {
    readout.innerHTML = `<div class="zoom-readout-time">Tap the chart to inspect a point</div>`;
    return;
  }

  const t = state.selectedT;
  const timeStr = new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const enabledSeries = state.series.filter(s => state.enabledKeys.has(s.key));

  const values = enabledSeries
    .map(s => {
      const nearest = findNearestPoint(s.points, t);
      return nearest ? { series: s, point: nearest } : null;
    })
    .filter(Boolean);

  readout.innerHTML = `
    <div class="zoom-readout-time">${escapeHtml(timeStr)}</div>
    <div class="zoom-readout-values">
      ${values
        .map(({ series, point }) => {
          const displayValue = series.valueLabels
            ? (series.valueLabels[Math.round(point.v)] ?? "")
            : formatNum(point.v, series.decimals ?? 0);
          return `
        <span class="zoom-readout-value">
          <span class="zoom-readout-swatch" style="background:${series.color}"></span>
          ${escapeHtml(String(displayValue))}
          <span class="zoom-readout-unit">${escapeHtml(series.valueLabels ? "" : series.unit || "")}</span>
        </span>
      `;
        })
        .join("")}
    </div>
  `;
}

function wireScrubber(state) {
  const scrubber = document.getElementById("zoom-scrubber");
  if (!scrubber) return;
  const pannable = state.fullEnd - state.fullStart - state.windowMs;

  if (pannable <= 0) {
    scrubber.disabled = true;
    scrubber.value = 0;
    return;
  }

  scrubber.max = 1000;
  scrubber.value = String(Math.round(((state.windowStart - state.fullStart) / pannable) * 1000));
  scrubber.oninput = () => {
    const frac = Number(scrubber.value) / 1000;
    state.windowStart = state.fullStart + frac * pannable;
    renderOverview(state);
    renderMainChart(state);
  };
}

function wireToggles(state) {
  const container = document.getElementById("zoom-series-toggles");
  if (!container) return;
  container.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.onchange = () => {
      const key = cb.dataset.seriesKey;
      if (cb.checked) state.enabledKeys.add(key);
      else state.enabledKeys.delete(key);
      renderMainChart(state);
      renderReadout(state);
    };
  });
}

// Exposed for tests only - not part of the public API surface other
// modules should call.
export const _internal = { computeFullRange, findNearestPoint, pointTimeMs };
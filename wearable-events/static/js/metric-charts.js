import { isoToDate, formatNum, escapeHtml } from "./core.js";

// Distinct colors per device dataset on the chart - cycles if there
// are ever more devices than colors defined here, rather than erroring.
const DEVICE_CHART_COLORS = ["#e88a8a", "#6ea8fe", "#4fd8b8", "#f0c674"];

export function buildLineChart(canvas, series, devices, decimals, yAxisLabel = null, minZero = false, colorOverride = null) {
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
    // Cycling by DEVICE index makes sense when a chart is comparing
    // several devices' own readings of the SAME metric (this app's
    // main use for buildLineChart) - but is the wrong axis entirely
    // for a page like Workout Detail, where every chart has exactly
    // one device and instead wants each DIFFERENT METRIC (HR,
    // elevation, speed...) to have its own distinct color - a real
    // reported gap, since every single-device chart was silently
    // landing on DEVICE_CHART_COLORS[0] every time. colorOverride
    // lets a caller specify that directly instead of relying on
    // device-index cycling, without changing the multi-device case.
    borderColor: colorOverride || DEVICE_CHART_COLORS[i % DEVICE_CHART_COLORS.length],
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
          // "grace" pads the auto-computed min/max by this fraction
          // of the data's own range, so a line doesn't touch the
          // very top/bottom edge of the chart - a real usability gap
          // reported directly (every chart's y-axis was tightly
          // bound to the exact data min/max with zero breathing
          // room). Chart.js's own built-in option for exactly this,
          // not a hand-rolled min/max computation.
          grace: "10%",
          // grace pads BOTH sides of the range - fine for something
          // like heart rate or elevation, where values near the
          // bottom of the chart are still meaningfully "not zero",
          // but wrong for a genuinely non-negative physical quantity
          // (speed, cadence, stride) sampled near its own floor - a
          // real reported bug, since data starting near 0 (e.g. speed
          // at the start of a walk) got graced into a NEGATIVE axis
          // minimum, which doesn't mean anything for these charts.
          // An explicit min here overrides grace's own lower bound
          // (Chart.js's own documented behavior: explicit min/max
          // take precedence over any auto-computed value) while
          // leaving the top of the range still graced normally.
          min: minZero ? 0 : undefined,
          ticks: {
            color: "#8a8d99",
            callback: (v) => formatNum(v, decimals),
          },
          grid: { color: "#2a2d38" },
          // Optional (existing callers don't pass this and get the
          // same unlabeled axis as before) - a short unit label (e.g.
          // "bpm", "mph") next to the axis itself, since a bare column
          // of numbers doesn't say what they're numbers OF.
          title: yAxisLabel ? { display: true, text: yAxisLabel, color: "#8a8d99" } : { display: false },
        },
      },
      plugins: {
        legend: { display: devices.length > 1, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleTimeString() : "",
            label: (item) => `${item.dataset.label}: ${formatNum(item.parsed.y, decimals)}`,
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

// Shared "dashed mean line across the whole chart" plugin factory -
// used by both buildTrendBarChart (a plain single value per bucket)
// and buildRangeBarChart's optional meanLine option (a range-bar chart
// wanting ONE flat line instead of the existing per-bar median tick).
// Drawn via afterDatasetsDraw (always on top, not order-weight
// dependent) directly across chartArea.left-to-right (the chart's real
// pixel bounds, not tied to the category axis's own per-point
// positions) - see buildHypnogramSVG's own comment for why a
// dataset-based line mixed onto a category axis doesn't reach the
// real edges or draw on top reliably; this sidesteps both issues the
// same way.
function buildMeanLinePlugin(id, meanValue, label, formatFn) {
  if (meanValue === null || meanValue === undefined) return null;
  return {
    id,
    afterDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      const y = scales.y.getPixelForValue(meanValue);

      ctx.save();
      ctx.strokeStyle = "#8a8d99";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 5]);
      ctx.beginPath();
      ctx.moveTo(chartArea.left, y);
      ctx.lineTo(chartArea.right, y);
      ctx.stroke();
      ctx.restore();

      // Flips below the line instead of above when the line sits too
      // close to the chart's own top edge for an above-line label to fit.
      const text = `${label || "Average"}: ${formatFn(meanValue)}`;
      const labelBelow = y - chartArea.top < 14;
      ctx.save();
      ctx.fillStyle = "#8a8d99";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = labelBelow ? "top" : "bottom";
      ctx.fillText(text, chartArea.right, labelBelow ? y + 4 : y - 4);
      ctx.restore();
    },
  };
}

export function buildRangeBarChart(canvas, series, devices, period, rollingMean = {}, yMin, decimals, yTickCallback, extra = {}) {
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
    plugins: [
      medianMarkerPlugin,
      // Optional single flat mean line for the whole chart - an
      // alternative to the per-bar median tick above, for a chart like
      // Sleep Regularity's sleep-window bar where "the week's average"
      // is more useful than a per-night median mark. Callers that want
      // this simply don't pass a `median` value per point at all (the
      // median plugin already no-ops on a missing/null median), so the
      // two never compete for the same bar.
      buildMeanLinePlugin("rangeBarMeanLine", extra.meanLine?.value, extra.meanLine?.label, yTickCallback || ((v) => formatNum(v, decimals))),
    ].filter(Boolean),
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true },
          grid: { display: false },
        },
        y: {
          ticks: {
            color: "#8a8d99",
            // Optional trailing override - every existing caller
            // omits this and keeps the original formatNum(v,decimals)
            // numeric formatting exactly as before. Added for Sleep
            // Regularity's own use of this function: a bedtime/wake
            // floating bar needs its Y-axis labeled as clock times
            // ("11:00 PM"), not plain numbers - the underlying floating-
            // bar mechanism (a genuine [min,max] range per period) is
            // exactly right for that case, unlike Duration's earlier,
            // now-fixed misuse of this same function for a single value.
            callback: yTickCallback || ((v) => formatNum(v, decimals)),
          },
          grid: { color: "#2a2d38" },
          // A field like SpO2 naturally lives in a narrow high range
          // (mid-90s to 100%) - auto-scaling to include 0 (or even a
          // wide default range) wastes most of the chart's height and
          // makes real, meaningful drops hard to see. Only set when a
          // chart's config actually specifies one (yMin) - other
          // fields keep Chart.js's normal auto-scaling untouched.
          // extra.yMax is the same idea for the top of the range -
          // Chart.js's own auto-scaling for a floating-bar dataset
          // defaults toward including 0, which for something like
          // Sleep Regularity's noon-anchored clock-time axis produces
          // a much wider range than the data actually spans.
          min: yMin,
          max: extra.yMax,
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
                if (!real) return "";
                const fmt = yTickCallback || ((v) => formatNum(v, decimals));
                return `${item.dataset.label}: ${fmt(real[0])}\u2013${fmt(real[1])}`;
              }
              // The rolling-mean overlay dataset doesn't carry a `raw`
              // array (that's specific to the padded-bar workaround
              // above) - fall back to the plotted value directly,
              // which for this dataset IS the real value. Defaults to
              // 1 decimal specifically HERE (decimals ?? 1, not just
              // decimals) to preserve this tooltip's original behavior
              // for every field that doesn't pass a decimals config -
              // it always rounded to 1 decimal before this parameter
              // existed, and removing that for non-temperature fields
              // would be an unrelated regression, not the requested fix.
              const v = item.parsed.y;
              return v === null || v === undefined ? "" : `${item.dataset.label}: ${formatNum(v, decimals ?? 1)}`;
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
export function renderBandLegend(bands) {
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

// A separate function from renderBandLegend above, not a shared one
// with a mode flag - that one is genuinely specific to temperature's
// SYMMETRIC delta-from-baseline bands (hardcodes the +- symbol and a
// degree unit baked into its own range formatting). Stress's tiers are
// a plain ASCENDING range starting at 0 (0-39/40-59/60-79/80-100, no
// +-, no fixed unit), a different enough shape that forcing one
// function to handle both would need more branching than just writing
// the second one directly.
export function renderTierLegend(bands, unit = "") {
  const items = bands.map((band, i) => {
    const rangeStart = i === 0 ? 0 : bands[i - 1].max + 1;
    const rangeText = `${rangeStart}\u2013${band.max}${unit}`;
    return `
      <div class="band-legend-item">
        <span class="band-swatch" style="background: ${band.color}"></span>
        <span>${escapeHtml(band.label)} (${rangeText})</span>
      </div>
    `;
  }).join("");
  return `<div class="band-legend">${items}</div>`;
}

export function buildDifferentialChart(canvas, series, devices, config) {
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

  // The y-axis needs a fixed enough reference frame that the band
  // coloring stays meaningful across different weeks - if left fully
  // auto-scaled, a week where every delta happens to sit near 0 would
  // shrink the axis down to fit that narrow range, and a tiny,
  // unremarkable blip would look just as visually dramatic as a real
  // swing looks on some OTHER week's differently-scaled axis. Always
  // show at least the full range out to the outermost band's
  // threshold (so "how close is this to Significant" stays visually
  // legible even on a calm week), but extend further with a little
  // headroom if an actual delta genuinely exceeds that, so a real
  // outlier doesn't get clipped at the edge instead of shown.
  const outermostThreshold = config.bands[config.bands.length - 1].threshold;
  const maxAbsDelta = Math.max(0, ...devices.flatMap(d => series[d].map(p => Math.abs(p.delta))));
  const axisBound = Math.max(outermostThreshold, maxAbsDelta * 1.1);

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
          min: -axisBound,
          max: axisBound,
          ticks: { color: "#8a8d99", callback: (v) => `${v > 0 ? "+" : ""}${formatNum(v, config.decimals)}${config.unit}` },
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
              const deltaVal = formatNum(p.delta, config.decimals);
              const deltaText = deltaVal > 0 ? `+${deltaVal}` : `${deltaVal}`;
              return `${item.dataset.label}: ${deltaText}${config.unit} (${band.label})`;
            },
          },
        },
      },
    },
  });
}

// Which band a raw VALUE falls into, by ascending threshold - unlike
// bandForDelta (which bands by absolute magnitude of a delta from a
// baseline), this bands a plain reading against fixed absolute
// thresholds starting from 0 (Zepp's own stress tiers: 0-39/40-59/
// 60-79/80-100), so it compares the value directly, not its distance
// from anything.
function bandForValue(value, bands) {
  for (const band of bands) {
    if (value <= band.max) return band;
  }
  return bands[bands.length - 1];
}

// Per-reading bars (not hourly aggregates like buildRangeBarChart, nor
// a continuous line like buildLineChart) - each bar is one actual
// reading, colored by which fixed tier its own value falls into. Built
// for Stress's day view specifically: readings arrive roughly every 5
// minutes (automatic) plus occasional manual ones, and Zepp's own
// chart colors each individual reading by tier rather than aggregating
// or connecting them with a line - collapsing that down to hourly
// min/max bars (like SpO2's chart) would lose the exact thing this
// chart exists to show.
//
// Deliberately a CATEGORY x-axis (bars evenly spaced by reading INDEX,
// like buildRangeBarChart), not a true time-linear one - consistent
// with the rest of this app's bar charts, and simpler/more robust than
// getting Chart.js's linear-axis bar-width handling exactly right for
// a mostly-but-not-perfectly-regular 5-minute cadence. This does mean
// a large gap (e.g. overnight) doesn't visually take up more x-axis
// space than a run of closely-spaced readings - a reasonable trade,
// not a bug, given how this app's other bar charts already work.
export function buildTieredBarChart(canvas, series, devices, config) {
  const allTimes = [...new Set(devices.flatMap(d => series[d].map(p => p.t)))].sort();
  const labels = allTimes.map(iso => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));

  const datasets = devices.map(device => {
    const byTime = Object.fromEntries(series[device].map(p => [p.t, p.v]));
    const values = allTimes.map(t => (t in byTime ? byTime[t] : null));
    return {
      label: device,
      data: values,
      backgroundColor: values.map(v => (v === null ? "transparent" : bandForValue(v, config.bands).color)),
      borderRadius: 2,
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
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true, autoSkipPadding: 16 },
          grid: { display: false },
        },
        y: {
          min: 0,
          // No implicit default - every caller states its own scale
          // explicitly (Stress: 100, its own fixed 0-100 tier scale;
          // Activity's raw intensity: 255, its confirmed real range).
          // config.yMax === null means "no fixed ceiling, auto-scale
          // to the data" (Activity's Steps chart, which has no fixed
          // upper bound the way a percentage-based metric does).
          max: config.yMax === null ? undefined : config.yMax,
          ticks: { color: "#8a8d99" },
          grid: { color: "#2a2d38" },
        },
      },
      plugins: {
        legend: { display: devices.length > 1, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const v = item.parsed.y;
              if (v === null || v === undefined) return "";
              const band = bandForValue(v, config.bands);
              return `${item.dataset.label}: ${formatNum(v, config.decimals)}${config.unit} (${band.label})`;
            },
          },
        },
      },
    },
  });
}

// Time-in-tier breakdown for ONE device's day - Zepp's own "Daily
// Stress" pie chart. Takes that device's raw readings directly (not a
// pre-computed breakdown) and does the tier-counting itself, matching
// how every other chart function here takes raw series and does its
// own necessary computation rather than expecting the caller to shape
// it first. A pie inherently can't combine multiple devices into one
// chart, so this builds exactly one device's pie - the caller loops
// over devices and calls this once per device if there's more than one.
//
// This is genuinely "% of READINGS in each tier", not "% of TIME" -
// readings arrive roughly every 5 minutes when automatic, so the two
// are close but not identical (an unusually large gap would be counted
// as zero time in this metric, even though the wearer's ACTUAL stress
// state presumably didn't just vanish during it). Simpler and more
// honest than pretending to reconstruct true elapsed time from
// irregular samples when Zepp's own exact method isn't confirmed.
export function buildTierPieChart(canvas, points, config) {
  const counts = config.bands.map(() => 0);
  for (const p of points) {
    const band = bandForValue(p.v, config.bands);
    counts[config.bands.indexOf(band)]++;
  }
  const total = points.length || 1;

  return new Chart(canvas, {
    type: "pie",
    data: {
      labels: config.bands.map(b => b.label),
      datasets: [{
        data: counts,
        backgroundColor: config.bands.map(b => b.color),
        borderColor: "#1a1c24",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      animation: false,
      plugins: {
        legend: { display: false }, // the shared band legend beneath already explains the colors
        tooltip: {
          callbacks: {
            label: (item) => {
              const pct = Math.round((item.parsed / total) * 100);
              return `${item.label}: ${pct}%`;
            },
          },
        },
      },
    },
  });
}

// A pie chart from PRE-COMPUTED category totals (not raw points to
// bin, unlike buildTierPieChart above) - {label, value, color} per
// slice. Used for Gradient Distribution (Uphill/Flat/Downhill time),
// and reusable for any future "show a few known category durations as
// a pie" need without forcing that data through buildTierPieChart's
// own "bin raw samples by value" shape, which doesn't fit data that's
// already aggregated.
export function buildCategoryPieChart(canvas, categories) {
  const total = categories.reduce((sum, c) => sum + c.value, 0) || 1;
  return new Chart(canvas, {
    type: "pie",
    data: {
      labels: categories.map(c => c.label),
      datasets: [{
        data: categories.map(c => c.value),
        backgroundColor: categories.map(c => c.color),
        borderColor: "#1a1c24",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      animation: false,
      plugins: {
        legend: { display: false }, // rendered as an HTML legend alongside, same as buildTierPieChart's own callers
        tooltip: {
          callbacks: {
            label: (item) => {
              const pct = Math.round((item.parsed / total) * 100);
              return `${item.label}: ${pct}%`;
            },
          },
        },
      },
    },
  });
}

// x-axis label for one bucket, per the granularity the caller is
// plotting - shared by buildStackedMinutesChart and
// buildActivityTimeChart, the two Activity-page charts that plot
// per-bucket data over either a day (hourly buckets), a week/month
// (daily buckets), or a year (monthly buckets).
function formatBucketLabel(iso, format) {
  const d = new Date(iso);
  if (format === "hour") return d.toLocaleTimeString([], { hour: "numeric" });
  if (format === "month") return d.toLocaleDateString([], { month: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// Sitting vs. active minutes, stacked per bucket - the Activity page's
// day-view hourly comparison chart (bucket = hour) and its Week/Month/
// Year "sitting vs standing" chart (bucket = day or month), sharing
// this one function since both are the exact same shape
// (get_hourly_activity_breakdown / get_activity_time_range_series both
// return {sitting_minutes, active_minutes} per bucket) - only the
// label format differs, passed in via config.labelFormat rather than
// needing two near-identical functions.
//
// Each device gets its OWN stack (Chart.js's `stack` property keyed
// by device name), so multiple devices render as separate stacked
// bar pairs side by side per bucket rather than merging their minutes
// together - correct behavior even though a single device is the
// realistic case for this app right now.
export function buildStackedMinutesChart(canvas, series, devices, config) {
  const allTimes = [...new Set(devices.flatMap(d => series[d].map(p => p.t)))].sort();
  const labels = allTimes.map(iso => formatBucketLabel(iso, config.labelFormat));

  const sittingColor = config.sittingColor || "#6ea8fe";
  const activeColor = config.activeColor || "#f0c674";

  const datasets = [];
  devices.forEach(device => {
    const byTime = Object.fromEntries(series[device].map(p => [p.t, p]));
    datasets.push({
      label: devices.length > 1 ? `${device} \u2013 Sitting` : "Sitting",
      data: allTimes.map(t => (t in byTime ? byTime[t].sitting_minutes : null)),
      backgroundColor: sittingColor,
      stack: device,
      borderRadius: 2,
    });
    datasets.push({
      label: devices.length > 1 ? `${device} \u2013 Active` : "Active",
      data: allTimes.map(t => (t in byTime ? byTime[t].active_minutes : null)),
      backgroundColor: activeColor,
      stack: device,
      borderRadius: 2,
    });
  });

  return new Chart(canvas, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          stacked: true,
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true, autoSkipPadding: 16 },
          grid: { display: false },
        },
        y: {
          stacked: true,
          min: 0,
          ticks: { color: "#8a8d99" },
          grid: { color: "#2a2d38" },
          title: { display: true, text: "minutes", color: "#8a8d99" },
        },
      },
      plugins: {
        legend: { display: true, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const v = item.parsed.y;
              if (v === null || v === undefined) return "";
              return `${item.dataset.label}: ${v} min`;
            },
          },
        },
      },
    },
  });
}

// Total active minutes per bucket - the Activity page's Week/Month/
// Year "total activity time" chart, plotted alongside steps. Same
// per-bucket data as buildStackedMinutesChart (in fact the identical
// API response - this just plots active_minutes alone, unstacked,
// since "how much total activity time" doesn't need the sitting
// breakdown a device-name/day view already provides elsewhere).
export function buildActivityTimeChart(canvas, series, devices, config) {
  const allTimes = [...new Set(devices.flatMap(d => series[d].map(p => p.t)))].sort();
  const labels = allTimes.map(iso => formatBucketLabel(iso, config.labelFormat));

  const datasets = devices.map((device, i) => {
    const byTime = Object.fromEntries(series[device].map(p => [p.t, p]));
    return {
      label: device,
      data: allTimes.map(t => (t in byTime ? byTime[t].active_minutes : null)),
      backgroundColor: DEVICE_CHART_COLORS[i % DEVICE_CHART_COLORS.length],
      borderRadius: 2,
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
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true, autoSkipPadding: 16 },
          grid: { display: false },
        },
        y: {
          min: 0,
          ticks: { color: "#8a8d99" },
          grid: { color: "#2a2d38" },
          title: { display: true, text: "active minutes", color: "#8a8d99" },
        },
      },
      plugins: {
        legend: { display: devices.length > 1, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const v = item.parsed.y;
              if (v === null || v === undefined) return "";
              return `${item.dataset.label}: ${v} min`;
            },
          },
        },
      },
    },
  });
}

// Clinical hypnogram convention: depth increases downward (Awake at
// top, Deep at bottom), REM placed between Light and Awake since it's
// physiologically a "lighter" state despite being distinct from light
// sleep. Same colors as the existing .sleep-stage-seg CSS (Today's
// proportion bar, and the legend below this chart) - reused rather
// than a separate palette invented just for this chart, so the same
// stage reads as the same color everywhere in the app.
const HYPNOGRAM_STAGE_ORDER_TOP_TO_BOTTOM = ["awake", "rem", "light", "deep"];
const HYPNOGRAM_STAGE_COLORS = { deep: "#7c6ce8", light: "#6ea8fe", rem: "#4fd8b8", awake: "#e88a8a" };
const HYPNOGRAM_STAGE_LABELS = { deep: "Deep", light: "Light", rem: "REM", awake: "Awake" };

// Per-night sleep-stage composition across a week/month/year - the
// Sleep Duration page's own week/month/year rollup view (see
// UI_DESIGN_NOTES.md's "Time Asleep (advanced)" page notes - Zepp's
// own version of this same chart). Takes get_sleep_stage_trend()'s own
// real shape directly ([{date, stages_min: {light,deep,rem,awake}}]) -
// already one row per night with a single, already-resolved primary
// device (see that function's own docstring), so unlike
// buildStackedMinutesChart above this needs no device-keyed series
// grouping at all. Same stage order/colors/labels as the hypnogram
// elsewhere in this app (HYPNOGRAM_STAGE_COLORS/LABELS), stacked
// bottom-to-top as Deep/Light/REM/Awake - reading a stacked bar
// bottom-up as "deepest sleep first" mirrors the hypnogram's own
// top-to-bottom depth convention, not an arbitrary new order.
export function buildSleepStageStackedChart(canvas, stageTrend, config) {
  const labels = stageTrend.map(entry => formatBucketLabel(entry.date, config.labelFormat));
  const stageKeys = ["deep", "light", "rem", "awake"];

  const datasets = stageKeys.map(stage => ({
    label: HYPNOGRAM_STAGE_LABELS[stage],
    data: stageTrend.map(entry => entry.stages_min[stage] ?? 0),
    backgroundColor: HYPNOGRAM_STAGE_COLORS[stage],
    stack: "stages",
    borderRadius: 2,
  }));

  return new Chart(canvas, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          stacked: true,
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true, autoSkipPadding: 16 },
          grid: { display: false },
        },
        y: {
          stacked: true,
          min: 0,
          ticks: { color: "#8a8d99" },
          grid: { color: "#2a2d38" },
          title: { display: true, text: "minutes", color: "#8a8d99" },
        },
      },
      plugins: {
        legend: { display: true, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const v = item.parsed.y;
              if (!v) return "";
              const h = Math.floor(v / 60);
              const m = Math.round(v % 60);
              return `${item.dataset.label}: ${h > 0 ? `${h}h ` : ""}${m}m`;
            },
          },
        },
      },
    },
  });
}

// Builds a rounded-rect SVG path with SELECTIVE per-corner rounding -
// a plain <rect rx> only supports uniform rounding on all 4 corners,
// which is the wrong tool here: a bar whose edge connects to a
// transition stem needs that specific corner SQUARE (see
// buildHypnogramSVG's own comment on why), while its other corners
// stay rounded. `corners` is {tl, tr, bl, br}, each true (rounded,
// using radius r) or false (square, radius 0). Standard clockwise
// rounded-rect path construction, starting just past the top-left
// corner.
export function roundedRectPath(x, y, w, h, r, corners) {
  const rtl = corners.tl ? r : 0;
  const rtr = corners.tr ? r : 0;
  const rbr = corners.br ? r : 0;
  const rbl = corners.bl ? r : 0;
  const f = (n) => n.toFixed(2);
  return [
    `M ${f(x + rtl)} ${f(y)}`,
    `L ${f(x + w - rtr)} ${f(y)}`,
    rtr ? `A ${f(rtr)} ${f(rtr)} 0 0 1 ${f(x + w)} ${f(y + rtr)}` : `L ${f(x + w)} ${f(y)}`,
    `L ${f(x + w)} ${f(y + h - rbr)}`,
    rbr ? `A ${f(rbr)} ${f(rbr)} 0 0 1 ${f(x + w - rbr)} ${f(y + h)}` : `L ${f(x + w)} ${f(y + h)}`,
    `L ${f(x + rbl)} ${f(y + h)}`,
    rbl ? `A ${f(rbl)} ${f(rbl)} 0 0 1 ${f(x)} ${f(y + h - rbl)}` : `L ${f(x)} ${f(y + h)}`,
    `L ${f(x)} ${f(y + rtl)}`,
    rtl ? `A ${f(rtl)} ${f(rtl)} 0 0 1 ${f(x + rtl)} ${f(y)}` : `L ${f(x)} ${f(y)}`,
    "Z",
  ].join(" ");
}

// A true hypnogram - sleep stage over time, one filled rounded bar per
// segment, laid out on a hand-computed SVG grid rather than a Chart.js
// scale. Deliberately NOT built on Chart.js, after two rounds of
// genuinely subtle Chart.js linear-scale-as-categorical-axis bugs (a
// tick-placement mismatch, then a chart-area sizing collapse specific
// to this testing environment) that this environment couldn't reliably
// catch before shipping - a charting library's internal scale-fitting
// machinery is the wrong tool for "4 fixed rows, exact proportional
// time width per segment", which is simple enough to compute directly.
// Every coordinate below is plain arithmetic on the input data, fully
// verifiable with string/math assertions alone, no rendering engine
// required to trust the result is correct.
//
// `segments` is the ordered {stage, start, duration_min} list
// /sleep/hypnogram returns (see get_sleep_hypnogram_for_night() on the
// backend). `options.width`/`options.height` size the SVG's viewBox
// (defaults chosen to match this app's existing chart-card sizing).
// Returns an SVG markup string, or "" for no data - the caller sets
// this as innerHTML directly, no canvas/chart-instance involved.
export function buildHypnogramSVG(segments, options = {}) {
  if (!segments.length) return "";

  const width = options.width || 800;
  const height = options.height || 200;
  const labelWidth = 46; // reserved left margin for stage row labels
  const bottomAxisHeight = 20; // reserved bottom margin for time labels
  const chartWidth = width - labelWidth;
  const chartHeight = height - bottomAxisHeight;
  const bandHeight = chartHeight / HYPNOGRAM_STAGE_ORDER_TOP_TO_BOTTOM.length;

  // The bar itself is thinner than its own band (leaves visible
  // breathing room above/below within each row) - this, plus rounded
  // ends and the thin connector stems below, is what gives the
  // "swoopy" look (Oura/Whoop-style) rather than solid stacked blocks.
  const barFraction = 0.55;
  const barThickness = bandHeight * barFraction;

  // A small, fixed corner radius - NOT scaled to the bar's own
  // thickness (an earlier version did this and produced corners the
  // person found too large/heavy). Still capped by half the bar's own
  // thickness/width so a very short or brief segment's rounding can
  // never exceed its own rect and create a rendering artifact.
  const cornerRadius = 4;

  const sessionStartMs = new Date(segments[0].start).getTime();
  const last = segments[segments.length - 1];
  const sessionEndMs = new Date(last.start).getTime() + last.duration_min * 60000;
  const totalMs = Math.max(sessionEndMs - sessionStartMs, 1); // guard against a degenerate zero-length session

  // Per-segment geometry computed once, shared by both the bars and
  // the connector stems below - x/width from real elapsed time and
  // duration, barTop/barBottom centered within the segment's own
  // stage band.
  const geometry = segments.map(seg => {
    const startMs = new Date(seg.start).getTime();
    const x = labelWidth + ((startMs - sessionStartMs) / totalMs) * chartWidth;
    const w = (seg.duration_min * 60000 / totalMs) * chartWidth;
    const bandIndex = HYPNOGRAM_STAGE_ORDER_TOP_TO_BOTTOM.indexOf(seg.stage);
    const bandCenter = bandIndex >= 0 ? bandIndex * bandHeight + bandHeight / 2 : bandHeight / 2;
    return {
      stage: seg.stage,
      x, w,
      bandIndex: bandIndex >= 0 ? bandIndex : 0,
      barTop: bandCenter - barThickness / 2,
      barBottom: bandCenter + barThickness / 2,
      color: HYPNOGRAM_STAGE_COLORS[seg.stage] || "#8a8d99",
    };
  });

  // Which corner of a bar a transition-to-a-DIFFERENT-band neighbor
  // occupies, so that corner can be left square - a stem drawn to a
  // bar's nominal edge only visually touches it if the bar's own fill
  // actually extends all the way to that edge at that exact X, which
  // a rounded corner (curving away before reaching the edge) breaks.
  // Making the connecting corner square is what makes the stem
  // actually look connected, not just numerically adjacent.
  const rects = geometry.map((g, i) => {
    const prev = geometry[i - 1];
    const next = geometry[i + 1];
    const corners = { tl: true, tr: true, bl: true, br: true };
    if (prev && prev.bandIndex !== g.bandIndex) {
      // prev sits above (smaller bandIndex) -> stem lands on g's TOP-LEFT;
      // prev sits below -> stem lands on g's BOTTOM-LEFT.
      if (prev.bandIndex < g.bandIndex) corners.tl = false;
      else corners.bl = false;
    }
    if (next && next.bandIndex !== g.bandIndex) {
      if (next.bandIndex < g.bandIndex) corners.tr = false;
      else corners.br = false;
    }
    const r = Math.max(0, Math.min(cornerRadius, barThickness / 2, g.w / 2));
    const d = roundedRectPath(g.x, g.barTop, g.w, barThickness, r, corners);
    return `<path d="${d}" fill="${g.color}" />`;
  }).join("");

  // Thin vertical stems bridging each transition - drawn UNDERNEATH
  // the bars (before them in the markup) so each bar's own square
  // connecting corner sits flush against the stem with no gap.
  const connectors = [];
  for (let i = 0; i < geometry.length - 1; i++) {
    const a = geometry[i];
    const b = geometry[i + 1];
    if (a.bandIndex === b.bandIndex) continue; // same row, no vertical connector needed
    const x = b.x; // == a.x + a.w, the exact boundary between the two segments
    const [y1, y2] = a.bandIndex < b.bandIndex
      ? [a.barBottom, b.barTop]   // b is a LOWER row (deeper stage) than a
      : [a.barTop, b.barBottom];  // b is a HIGHER row (lighter stage) than a
    connectors.push(`<line x1="${x.toFixed(2)}" y1="${y1.toFixed(2)}" x2="${x.toFixed(2)}" y2="${y2.toFixed(2)}" stroke="#4a4d5a" stroke-width="1" />`);
  }

  const gridLines = HYPNOGRAM_STAGE_ORDER_TOP_TO_BOTTOM.map((_, i) => {
    const y = i * bandHeight;
    return `<line x1="${labelWidth}" y1="${y.toFixed(2)}" x2="${width}" y2="${y.toFixed(2)}" stroke="#2a2d38" stroke-width="1" />`;
  }).join("");
  const bottomLine = `<line x1="${labelWidth}" y1="${chartHeight.toFixed(2)}" x2="${width}" y2="${chartHeight.toFixed(2)}" stroke="#2a2d38" stroke-width="1" />`;

  const yLabels = HYPNOGRAM_STAGE_ORDER_TOP_TO_BOTTOM.map((stage, i) => {
    const y = i * bandHeight + bandHeight / 2;
    return `<text x="${(labelWidth - 8).toFixed(2)}" y="${y.toFixed(2)}" text-anchor="end" dominant-baseline="middle" fill="#8a8d99" font-size="11">${HYPNOGRAM_STAGE_LABELS[stage]}</text>`;
  }).join("");

  // Time labels at ~4 roughly-even points across the night, not one
  // per segment (which could be dozens) - matches the tick density
  // the earlier Chart.js x-axis aimed for via autoSkip, just computed
  // directly instead of relying on a library's own skip heuristic.
  const labelCount = 4;
  const xLabels = Array.from({ length: labelCount + 1 }, (_, i) => {
    const frac = i / labelCount;
    const x = labelWidth + frac * chartWidth;
    const t = new Date(sessionStartMs + frac * totalMs);
    const text = t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const anchor = i === 0 ? "start" : i === labelCount ? "end" : "middle";
    return `<text x="${x.toFixed(2)}" y="${(chartHeight + 14).toFixed(2)}" text-anchor="${anchor}" fill="#8a8d99" font-size="11">${text}</text>`;
  }).join("");

  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" xmlns="http://www.w3.org/2000/svg" font-family="inherit">
    ${gridLines}
    ${bottomLine}
    ${connectors.join("")}
    ${rects}
    ${yLabels}
    ${xLabels}
  </svg>`;
}

// A simple bar-from-zero trend chart with a dashed mean-line overlaid
// across the WHOLE chart (one constant value, not per-bar) - for
// trends like "Last 7 days duration" where each day has exactly ONE
// value (not a min/max range), and the useful comparison is "this
// day vs the week's overall average", not "this day's own range".
// Deliberately NOT buildRangeBarChart, which draws a floating
// [min,max] bar per period - reused for this once by mistake,
// producing a bar that starts/ends at nearly the same height (its
// own min==max padding logic drawing a ~1-unit-tall floating bar
// centered on the value) rather than a real 0-to-value bar. `series`
// is {"<device>": [{t, value}, ...]}, one point per bucket.
export function buildTrendBarChart(canvas, series, devices, config = {}) {
  const allPeriods = [...new Set(devices.flatMap(d => series[d].map(p => p.t)))].sort();
  const labels = allPeriods.map(t => new Date(t).toLocaleDateString([], { month: "short", day: "numeric" }));

  const barDatasets = devices.map((device, i) => {
    const byPeriod = Object.fromEntries(series[device].map(p => [p.t, p.value]));
    return {
      type: "bar",
      label: device,
      data: allPeriods.map(t => (t in byPeriod ? byPeriod[t] : null)),
      backgroundColor: DEVICE_CHART_COLORS[i % DEVICE_CHART_COLORS.length],
      borderRadius: 4,
    };
  });

  // The dashed mean line - one constant value across the whole chart,
  // computed over all real (non-null) values across all devices, not
  // per-device or per-bucket. null (not drawn at all) if there's no
  // data to average.
  //
  // Deliberately NOT a "line"-type dataset plotted on the same
  // category x-axis as the bars - two real problems with that
  // approach (both reported directly, confirmed real): a category
  // axis positions each point at its OWN category's center, so the
  // line only spans between the first and last bar's centers, never
  // reaching the chart's actual left/right edges (the axis reserves
  // half a category's padding on each side, by design, so bars don't
  // touch the edges); and Chart.js's default per-dataset draw order
  // put the line dataset visually BEHIND the bars rather than over
  // them. A canvas plugin drawn in afterDatasetsDraw sidesteps both
  // at once: it draws after every dataset (always on top, not
  // dependent on dataset order/an `order` weight trick), directly
  // from chartArea.left to chartArea.right (the chart's real pixel
  // bounds, not tied to the category axis's own point positions at
  // all).
  const allValues = devices.flatMap(d => series[d].map(p => p.value)).filter(v => v !== null && v !== undefined);
  const mean = allValues.length ? allValues.reduce((a, b) => a + b, 0) / allValues.length : null;
  const meanFormat = (v) => `${config.decimals !== undefined ? v.toFixed(config.decimals) : v}${config.unit || ""}`;
  const meanLinePlugin = buildMeanLinePlugin("trendMeanLine", mean, config.meanLabel, meanFormat);

  return new Chart(canvas, {
    type: "bar",
    data: { labels, datasets: barDatasets },
    plugins: [meanLinePlugin].filter(Boolean),
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true, autoSkipPadding: 16 },
          grid: { display: false },
        },
        y: {
          min: 0,
          ticks: { color: "#8a8d99" },
          grid: { color: "#2a2d38" },
          title: config.yAxisTitle ? { display: true, text: config.yAxisTitle, color: "#8a8d99" } : undefined,
        },
      },
      plugins: {
        legend: { display: devices.length > 1, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const v = item.parsed.y;
              if (v === null || v === undefined) return "";
              const formatted = config.decimals !== undefined ? v.toFixed(config.decimals) : v;
              return `${item.dataset.label}: ${formatted}${config.unit || ""}`;
            },
          },
        },
      },
    },
  });
}

// A continuous vitals line (heart_rate or sleep_respiratory_rate)
// over one full night, with the sleep-stage hypnogram as translucent
// FULL-HEIGHT BACKGROUND BANDS behind it - matches UI_DESIGN_NOTES.md's
// "sleep-stage lanes as the chart's background" pattern for the Sleep
// Heart Rate / Sleep Respiratory Rate detail pages: one glance shows
// both what the signal was doing and what stage was active at once.
//
// Hand-computed SVG, same reasoning as buildHypnogramSVG: this is a
// genuinely simple layout (time-proportional X, a value range Y, a
// polyline, some background rects) that doesn't need a charting
// library's scale-fitting machinery, and this project has already
// hit real, hard-to-diagnose Chart.js bugs (documented on
// buildHypnogramSVG itself) building something in this same family.
//
// `vitalsPoints` is an ORDERED {t, v} list for ONE device (already
// filtered/flattened by the caller - see get_sleep_vitals_series() on
// the backend). `hypnogramSegments` is the same ordered
// {stage, start, duration_min} list buildHypnogramSVG takes, used
// here only to anchor the shared time range and draw the background
// bands - not for its own 4-row layout.
export function buildVitalsHypnogramSVG(vitalsPoints, hypnogramSegments, options = {}) {
  if (!vitalsPoints.length || !hypnogramSegments.length) return "";

  const width = options.width || 800;
  const height = options.height || 180;
  const labelWidth = 34; // reserved left margin for Y-axis min/max labels
  const bottomAxisHeight = 20; // reserved bottom margin for time labels
  const chartWidth = width - labelWidth;
  const chartHeight = height - bottomAxisHeight;

  // Anchored to the SESSION's own bounds (from the hypnogram segments),
  // not just the vitals points' own range - keeps this chart's time
  // axis consistent with the background bands even if vitals readings
  // don't quite cover the session's full start/end.
  const sessionStartMs = new Date(hypnogramSegments[0].start).getTime();
  const lastSeg = hypnogramSegments[hypnogramSegments.length - 1];
  const sessionEndMs = new Date(lastSeg.start).getTime() + lastSeg.duration_min * 60000;
  const totalMs = Math.max(sessionEndMs - sessionStartMs, 1);

  const xForMs = (ms) => labelWidth + ((ms - sessionStartMs) / totalMs) * chartWidth;
  const xFor = (iso) => xForMs(new Date(iso).getTime());

  // Y range from the real data, padded so the line doesn't touch the
  // chart's own top/bottom edges. A flat/near-flat series (min≈max)
  // gets a minimum padded span so it doesn't collapse to a degenerate
  // zero-height scale.
  const values = vitalsPoints.map(p => p.v);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const span = Math.max(rawMax - rawMin, 2);
  const pad = span * 0.15;
  const yMin = rawMin - pad;
  const yMax = rawMax + pad;
  const yFor = (v) => chartHeight - ((v - yMin) / (yMax - yMin)) * chartHeight;

  const bands = hypnogramSegments.map(seg => {
    const x = xFor(seg.start);
    const segEndMs = new Date(seg.start).getTime() + seg.duration_min * 60000;
    const w = xForMs(segEndMs) - x;
    const color = HYPNOGRAM_STAGE_COLORS[seg.stage] || "#8a8d99";
    return `<rect x="${x.toFixed(2)}" y="0" width="${w.toFixed(2)}" height="${chartHeight.toFixed(2)}" fill="${color}" opacity="0.16" />`;
  }).join("");

  const points = vitalsPoints.map(p => `${xFor(p.t).toFixed(2)},${yFor(p.v).toFixed(2)}`).join(" ");

  const yLabels = [
    `<text x="${(labelWidth - 6).toFixed(2)}" y="8" text-anchor="end" fill="#8a8d99" font-size="10">${Math.round(rawMax)}</text>`,
    `<text x="${(labelWidth - 6).toFixed(2)}" y="${(chartHeight - 2).toFixed(2)}" text-anchor="end" fill="#8a8d99" font-size="10">${Math.round(rawMin)}</text>`,
  ].join("");

  const labelCount = 4;
  const xLabels = Array.from({ length: labelCount + 1 }, (_, i) => {
    const frac = i / labelCount;
    const x = labelWidth + frac * chartWidth;
    const t = new Date(sessionStartMs + frac * totalMs);
    const text = t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const anchor = i === 0 ? "start" : i === labelCount ? "end" : "middle";
    return `<text x="${x.toFixed(2)}" y="${(chartHeight + 14).toFixed(2)}" text-anchor="${anchor}" fill="#8a8d99" font-size="11">${text}</text>`;
  }).join("");

  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" xmlns="http://www.w3.org/2000/svg" font-family="inherit">
    ${bands}
    <polyline points="${points}" fill="none" stroke="#e8e9ed" stroke-width="1.75" />
    ${yLabels}
    ${xLabels}
  </svg>`;
}

// One point per period, no connecting line - for things like Sleep
// Regularity's "Went to bed" / "Get up" scatter charts (bedtime or
// wake time per night). A "line" chart type with showLine:false and
// real point markers, same base approach buildLineChart already uses
// for sparse single-reading-per-day series, just without ever
// connecting the dots even when there IS a full run of consecutive
// days (a scatter is about consistency/spread, not a trend line).
// `config.yTickCallback` formats the Y-axis (and tooltip) values -
// same optional-override shape buildRangeBarChart's own extension
// uses, for a consistent way to plot "time of day" style values
// across both chart types.
export function buildTimeScatterChart(canvas, series, devices, config = {}) {
  const allPeriods = [...new Set(devices.flatMap(d => series[d].map(p => p.t)))].sort();
  const labels = allPeriods.map(t => new Date(t).toLocaleDateString([], { month: "short", day: "numeric" }));

  const datasets = devices.map((device, i) => {
    const byPeriod = Object.fromEntries(series[device].map(p => [p.t, p.value]));
    const color = DEVICE_CHART_COLORS[i % DEVICE_CHART_COLORS.length];
    return {
      type: "line",
      label: device,
      data: allPeriods.map(t => (t in byPeriod ? byPeriod[t] : null)),
      // Off by default (a genuine scatter, matching this function's own
      // name/purpose) - Sleep Regularity's own "Went to bed"/"Get up"
      // charts opt into connected lines explicitly (config.connectLine),
      // since seeing night-to-night drift as a connected path reads more
      // like the "consistency over time" story those charts are for,
      // without changing the default for any other future caller that
      // just wants points.
      showLine: !!config.connectLine,
      borderColor: color,
      borderWidth: 1.5,
      spanGaps: true,
      pointRadius: 5,
      pointBackgroundColor: color,
      pointBorderColor: color,
    };
  });

  const fmt = config.yTickCallback || ((v) => v);

  // Optional flat mean line across the whole chart, same
  // buildMeanLinePlugin factory buildRangeBarChart/buildTrendBarChart
  // already use - only built when config.meanLabel is provided, so
  // every existing caller (Sleep Regularity's own scatters, which
  // don't ask for this) is unaffected.
  let meanPlugin = null;
  if (config.meanLabel) {
    const allValues = devices.flatMap(d => series[d].map(p => p.value)).filter(v => v !== null && v !== undefined);
    const mean = allValues.length ? allValues.reduce((a, b) => a + b, 0) / allValues.length : null;
    meanPlugin = buildMeanLinePlugin("scatterMeanLine", mean, config.meanLabel, fmt);
  }

  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    plugins: [meanPlugin].filter(Boolean),
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          ticks: { color: "#8a8d99", maxRotation: 0, autoSkip: true },
          grid: { display: false },
        },
        y: {
          ticks: { color: "#8a8d99", callback: fmt },
          grid: { color: "#2a2d38" },
          min: config.yMin,
          max: config.yMax,
        },
      },
      plugins: {
        legend: { display: devices.length > 1, labels: { color: "#e8e9ed" } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const v = item.parsed.y;
              return v === null || v === undefined ? "" : `${item.dataset.label}: ${fmt(v)}`;
            },
          },
        },
      },
    },
  });
}
import { isoToDate, formatNum } from "./core.js";

// Distinct colors per device dataset on the chart - cycles if there
// are ever more devices than colors defined here, rather than erroring.
const DEVICE_CHART_COLORS = ["#e88a8a", "#6ea8fe", "#4fd8b8", "#f0c674"];

export function buildLineChart(canvas, series, devices, decimals) {
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
          ticks: {
            color: "#8a8d99",
            callback: (v) => formatNum(v, decimals),
          },
          grid: { color: "#2a2d38" },
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

export function buildRangeBarChart(canvas, series, devices, period, rollingMean = {}, yMin, decimals) {
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
          ticks: {
            color: "#8a8d99",
            callback: (v) => formatNum(v, decimals),
          },
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
                return real ? `${item.dataset.label}: ${formatNum(real[0], decimals)}\u2013${formatNum(real[1], decimals)}` : "";
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
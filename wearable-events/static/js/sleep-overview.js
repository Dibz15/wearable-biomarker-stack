// --- Sleep tab: per-night objective overview + subjective journal ---
// Both objective data (device-derived: duration, hypnogram, quality
// metrics, stats row) and the subjective "how did you sleep" journal
// for the SAME night now live in this one file, matching Zepp's own
// top-to-bottom order (objective data first, subjective input below
// it - see UI_DESIGN_NOTES.md's "Sleep tab" entry). The journal used
// to be a separate module (sleep.js, now retired) with its own flat
// "Recent nights" list across every logged entry at once - that
// predated each night having its own dedicated, date-nav-driven page
// and has been replaced: each night's page now shows/edits only its
// own entry (see the journal section further down).
import { escapeHtml, api, todayISO, shiftISODate } from "./core.js";
import { renderDateNav } from "./metric-detail.js";
import { buildHypnogramSVG, HYPNOGRAM_STAGE_COLORS, HYPNOGRAM_STAGE_LABELS } from "./metric-charts.js";
import { openSleepDurationDetail, openSleepHeartRateDetail, openSleepRespiratoryRateDetail, openSleepRegularityDetail } from "./sleep-detail.js";
import { openSleepReportsDetail } from "./sleep-reports.js";
import { openZoomChart } from "./zoom-chart.js";

const SLEEP_STAGE_ORDER = ["deep", "light", "rem", "awake"];
const SLEEP_STAGE_LABELS = { deep: "Deep", light: "Light", rem: "REM", awake: "Awake" };

// --- Subjective sleep journal, scoped to ONE specific night ---
// Replaces the old flat "Recent nights" list (sleep.js, now retired) -
// that predates each night having its own dedicated page and showed
// every logged entry across many nights at once. Each night's own
// page now shows/edits only ITS OWN entry: the submit form when
// nothing's been logged yet, or a read-only summary with an Edit
// option once something has.
const KNOWN_SLEEP_QUALIFIERS = ["groggy", "woke_up_often", "vivid_dreams", "racing_thoughts"];
// A SEPARATE category from the qualifiers above - things that
// happened BEFORE sleep, not how the sleep itself felt. Kept as its
// own known-key list (matching write_sleep_point's own "factor_"
// prefix convention on the backend) so the two chip groups render and
// submit distinctly rather than being lumped into one.
const KNOWN_PRE_SLEEP_FACTORS = [
  "read", "alcohol", "late_water", "late_eating", "late_screen_time",
  "games", "anxiety", "illness", "late_work", "late_shower",
];

function humanizeChipLabel(key) {
  const words = key.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function buildBoolPayload(knownKeys, selectedSet) {
  // Every known key sent explicitly as true/false, not just the ones
  // that are true - InfluxDB only overwrites fields actually included
  // in a write, so omitting a previously-true one would silently leave
  // it set instead of clearing it (same reasoning already documented
  // for qualifiers - applies identically to pre-sleep factors).
  const payload = {};
  knownKeys.forEach(k => { payload[k] = selectedSet.has(k); });
  return payload;
}

function renderChipButtons(knownKeys, selectedSet) {
  return knownKeys.map(k => `
    <button class="chip ${selectedSet.has(k) ? "selected" : ""}" data-chip="${k}">${humanizeChipLabel(k)}</button>
  `).join("");
}

// In-progress submit/edit draft for the CURRENTLY-VIEWED night only -
// explicitly reset in loadSleepOverview() whenever a different night
// loads, since this is inherently per-night state that shouldn't
// survive navigating away.
let journalDraft = null; // { score: number|null, qualifiers: Set, factors: Set } | null
let journalEditingExisting = false; // true while editing an ALREADY-LOGGED entry

function renderJournalReadOnly(entry) {
  const qualifierChips = KNOWN_SLEEP_QUALIFIERS
    .filter(q => entry.qualifiers && entry.qualifiers[q])
    .map(q => `<span class="chip-small">${humanizeChipLabel(q)}</span>`)
    .join("");
  const factorChips = KNOWN_PRE_SLEEP_FACTORS
    .filter(f => entry.pre_sleep_factors && entry.pre_sleep_factors[f])
    .map(f => `<span class="chip-small">${humanizeChipLabel(f)}</span>`)
    .join("");
  return `
    <div class="sleep-journal-card">
      <div class="sleep-journal-header">
        <span class="sleep-entry-score">${"\u25cf".repeat(entry.score)}${"\u25cb".repeat(5 - entry.score)}</span>
        <button class="small-btn" id="journal-edit-btn">Edit</button>
      </div>
      ${qualifierChips ? `<div class="timeline-tags">${qualifierChips}</div>` : ""}
      ${factorChips ? `
        <p class="sleep-journal-subheading">Pre-Sleep Factors</p>
        <div class="timeline-tags">${factorChips}</div>
      ` : ""}
    </div>
  `;
}

function renderJournalForm(isEditingExisting) {
  const scoreButtons = [1, 2, 3, 4, 5].map(n => `
    <button class="sleep-btn ${journalDraft.score === n ? "selected" : ""}" data-score="${n}">${n}</button>
  `).join("");
  return `
    <div class="sleep-journal-card">
      <p class="section-label">How did you sleep?</p>
      <div class="sleep-scale">${scoreButtons}</div>
      <p class="sleep-journal-subheading">Sleep Quality</p>
      <div class="qualifier-chips" id="journal-qualifier-chips">${renderChipButtons(KNOWN_SLEEP_QUALIFIERS, journalDraft.qualifiers)}</div>
      <p class="sleep-journal-subheading">Pre-Sleep Factors</p>
      <div class="qualifier-chips" id="journal-factor-chips">${renderChipButtons(KNOWN_PRE_SLEEP_FACTORS, journalDraft.factors)}</div>
      <div class="timeline-edit-actions">
        <button class="small-btn" id="journal-save-btn" ${journalDraft.score === null ? "disabled" : ""}>${isEditingExisting ? "Save" : "Submit"}</button>
        ${isEditingExisting ? `
          <button class="small-btn" id="journal-cancel-btn">Cancel</button>
          <button class="small-btn danger" id="journal-delete-btn">Delete</button>
        ` : ""}
      </div>
      <p class="status" id="journal-status"></p>
    </div>
  `;
}

// entry is null when nothing's been logged for this night yet - the
// ONLY case where the submit form shows unconditionally. Once an
// entry exists, it shows read-only by default; the form only
// reappears if the person explicitly taps Edit (journalEditingExisting).
function renderJournalSection(entry) {
  if (entry && !journalEditingExisting) return renderJournalReadOnly(entry);
  if (!journalDraft) {
    journalDraft = entry
      ? {
          score: entry.score,
          qualifiers: new Set(KNOWN_SLEEP_QUALIFIERS.filter(q => entry.qualifiers && entry.qualifiers[q])),
          factors: new Set(KNOWN_PRE_SLEEP_FACTORS.filter(f => entry.pre_sleep_factors && entry.pre_sleep_factors[f])),
        }
      : { score: null, qualifiers: new Set(), factors: new Set() };
  }
  return renderJournalForm(!!entry);
}

function rerenderJournal(anchorDate, entry) {
  const container = document.getElementById("sleep-journal-section");
  container.innerHTML = renderJournalSection(entry);
  wireJournalSection(anchorDate, entry);
}

async function reloadJournalSection(anchorDate) {
  // Re-fetches just the journal entry (not the whole page's overview/
  // hypnogram data, which didn't change) after a save/delete, since
  // the entry's own existence/content may now be different.
  const container = document.getElementById("sleep-journal-section");
  try {
    const entry = await api(`/sleep/entry?date=${anchorDate}`);
    container.innerHTML = renderJournalSection(entry);
    wireJournalSection(anchorDate, entry);
  } catch (e) {
    container.innerHTML = `<p class="status">Error loading sleep journal: ${escapeHtml(e.message)}</p>`;
  }
}

function wireJournalSection(anchorDate, entry) {
  const container = document.getElementById("sleep-journal-section");

  if (entry && !journalEditingExisting) {
    container.querySelector("#journal-edit-btn").addEventListener("click", () => {
      journalEditingExisting = true;
      journalDraft = null; // force a fresh draft rebuilt from the existing entry
      rerenderJournal(anchorDate, entry);
    });
    return;
  }

  container.querySelectorAll(".sleep-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      journalDraft.score = parseInt(btn.dataset.score, 10);
      rerenderJournal(anchorDate, entry);
    });
  });
  container.querySelectorAll(".chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const key = chip.dataset.chip;
      const set = KNOWN_PRE_SLEEP_FACTORS.includes(key) ? journalDraft.factors : journalDraft.qualifiers;
      if (set.has(key)) set.delete(key); else set.add(key);
      rerenderJournal(anchorDate, entry);
    });
  });

  const status = container.querySelector("#journal-status");
  container.querySelector("#journal-save-btn").addEventListener("click", async () => {
    if (journalDraft.score === null) return;
    status.textContent = entry ? "Saving..." : "Submitting...";
    const payload = {
      score: journalDraft.score,
      qualifiers: buildBoolPayload(KNOWN_SLEEP_QUALIFIERS, journalDraft.qualifiers),
      pre_sleep_factors: buildBoolPayload(KNOWN_PRE_SLEEP_FACTORS, journalDraft.factors),
    };
    try {
      if (entry) {
        await api(`/sleep/${entry.entry_id}`, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        await api(`/sleep?date=${anchorDate}`, { method: "POST", body: JSON.stringify(payload) });
      }
      journalDraft = null;
      journalEditingExisting = false;
      await reloadJournalSection(anchorDate);
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  const cancelBtn = container.querySelector("#journal-cancel-btn");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      journalDraft = null;
      journalEditingExisting = false;
      rerenderJournal(anchorDate, entry);
    });
  }

  const deleteBtn = container.querySelector("#journal-delete-btn");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (!confirm("Delete this sleep journal entry? This can't be undone.")) return;
      status.textContent = "Deleting...";
      try {
        await api(`/sleep/${entry.entry_id}`, { method: "DELETE" });
        journalDraft = null;
        journalEditingExisting = false;
        await reloadJournalSection(anchorDate);
      } catch (e) {
        status.textContent = `Error: ${e.message}`;
      }
    });
  }
}

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
      <div class="sleep-stat-item metric-card-tappable" data-detail-field="sleep-duration" role="button" tabindex="0">
        <span class="sleep-stat-value">${wakeHours}h ${wakeMin}m</span>
        <span class="sleep-stat-label">Duration</span>
      </div>
      <div class="sleep-stat-item">
        <span class="sleep-stat-value">${overview.wake_events}</span>
        <span class="sleep-stat-label">Wake Events</span>
      </div>
      <div class="sleep-stat-item metric-card-tappable" data-detail-field="sleep-heart-rate" role="button" tabindex="0">
        <span class="sleep-stat-value">${overview.avg_heart_rate !== null ? overview.avg_heart_rate : "\u2013"}</span>
        <span class="sleep-stat-label">Avg HR</span>
      </div>
      <div class="sleep-stat-item metric-card-tappable" data-detail-field="sleep-respiratory-rate" role="button" tabindex="0">
        <span class="sleep-stat-value">${overview.avg_respiratory_rate !== null ? overview.avg_respiratory_rate : "\u2013"}</span>
        <span class="sleep-stat-label">Avg BRPM</span>
      </div>
    </div>
  `;
}

// Naps, shown as their own distinct section - matching Zepp's own
// real treatment (confirmed directly from their app's screenshots
// this session: naps are never folded into the main sleep
// composition, always their own separate category). Reuses the
// existing .activity-session-* row shape from the Activity page's own
// session list (a name + time-range + a right-aligned stat - the same
// shape fits a nap row just as well as an activity session), rather
// than inventing new markup for what's structurally the same kind of
// "list of time-bounded events" display. Renders nothing at all (not
// an empty-state message) when there are no naps that day - most days
// have none, and a permanent "No naps" card on every single day would
// be far more noise than signal.
function renderNapsSection(naps) {
  if (!naps || naps.length === 0) return "";
  const rows = naps.map(n => {
    const start = new Date(n.start_time).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const end = new Date(n.end_time).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const durationMin = Math.round(n.duration_s / 60);
    return `
      <div class="activity-session-row">
        <div class="activity-session-main">
          <span class="activity-session-label">Nap</span>
          <span class="metric-sub">${escapeHtml(start)} \u2013 ${escapeHtml(end)}</span>
        </div>
        <div class="activity-session-side">
          <span class="metric-sub">${durationMin} min</span>
          <span class="metric-device-name">${escapeHtml(n.device || "")}</span>
        </div>
      </div>
    `;
  }).join("");
  return `
    <p class="today-section-label">Naps</p>
    <div class="activity-session-list">${rows}</div>
  `;
}

// Numeric "depth" encoding for the sleep stage stepped-line series in
// the hypnogram zoom view - matches HYPNOGRAM_STAGE_ORDER_TOP_TO_BOTTOM's
// own visual convention (awake highest/top, deep lowest/bottom) used by
// buildHypnogramSVG itself, just as numbers instead of vertical
// position, so the zoomed stepped line reads the same way the SVG
// hypnogram already does (deep sleep dips low, awake spikes high).
const STAGE_DEPTH = { deep: 0, light: 1, rem: 2, awake: 3 };
const STAGE_DEPTH_LABELS = {
  0: HYPNOGRAM_STAGE_LABELS.deep,
  1: HYPNOGRAM_STAGE_LABELS.light,
  2: HYPNOGRAM_STAGE_LABELS.rem,
  3: HYPNOGRAM_STAGE_LABELS.awake,
};
// Same HR/respiratory colors used on the Sleep Reports page's own
// REPORT_COLORS, so a series looks the same wherever it appears.
const ZOOM_HR_COLOR = "#ff6b6b";
const ZOOM_RESP_COLOR = "#6ea8fe";

// Converts the hypnogram's own {stage, start, duration_min} segments
// (buildHypnogramSVG's input shape) into a stepped-line zoom series -
// the person's own explicit spec: on the hypnogram's zoom, HR and
// respiratory rate are OPTIONAL overlays "on top" of the sleep stage
// itself, so the stage line is the always-meaningful default-on
// series here, not one of the optional toggles.
function hypnogramToZoomSeries(segments) {
  if (!segments || segments.length === 0) return null;
  const points = segments.map(seg => ({ t: seg.start, v: STAGE_DEPTH[seg.stage] ?? 1 }));
  // Extend one more point to the LAST segment's own end, so the
  // stepped line runs all the way to the real wake time instead of
  // stopping short at the final segment's start.
  const last = segments[segments.length - 1];
  const lastEndMs = new Date(last.start).getTime() + last.duration_min * 60000;
  points.push({ t: new Date(lastEndMs).toISOString(), v: STAGE_DEPTH[last.stage] ?? 1 });

  return {
    key: "hypnogram",
    label: "Sleep Stage",
    color: HYPNOGRAM_STAGE_COLORS.deep,
    defaultOn: true,
    stepped: true,
    valueLabels: STAGE_DEPTH_LABELS,
    points,
  };
}

export async function loadSleepOverview(anchorDate = todayISO()) {
  const container = document.getElementById("sleep-overview");
  container.innerHTML = `${renderDateNav("day", anchorDate)}<p class="muted">Loading...</p>`;
  wireSleepOverviewDateNav(anchorDate);

  // A fresh night's own journal state, not whatever was left over from
  // whichever night was being viewed before this navigation.
  journalDraft = null;
  journalEditingExisting = false;

  try {
    const [overview, hypnogram, journalEntry, regularityIndex, naps, hrSeries, respSeries] = await Promise.all([
      api(`/sleep/overview?date=${anchorDate}`),
      api(`/sleep/hypnogram?date=${anchorDate}`),
      api(`/sleep/entry?date=${anchorDate}`),
      api(`/sleep/regularity-index?end_date=${anchorDate}`),
      api(`/sleep/naps?date=${anchorDate}`),
      api(`/sleep/vitals-series/heart_rate?date=${anchorDate}`),
      api(`/sleep/vitals-series/sleep_respiratory_rate?date=${anchorDate}`),
    ]);

    if (overview === null) {
      container.innerHTML = `
        ${renderDateNav("day", anchorDate)}
        <div class="sleep-summary-card">
          <p class="metric-card-empty">No sleep session recorded for this night.</p>
        </div>
        ${renderNapsSection(naps)}
      `;
      wireSleepOverviewDateNav(anchorDate);
      return;
    }

    const hasStageData = hypnogram.length > 0;

    container.innerHTML = `
      ${renderDateNav("day", anchorDate)}
      <div class="sleep-summary-card">
        <div class="sleep-summary-top">
          <span class="sleep-summary-duration metric-card-tappable" data-detail-field="sleep-duration" role="button" tabindex="0">${formatDuration(overview.duration_s)}</span>
          <span class="sleep-summary-date">${escapeHtml(anchorDate)}</span>
        </div>
        ${hasStageData
          ? `<div class="sleep-hypnogram-card">${buildHypnogramSVG(hypnogram, { width: 800, height: 190 })}</div><div class="sleep-stage-legend">${renderHypnogramLegend(hypnogram)}</div><button id="hypnogram-zoom-trigger" class="small-btn" style="margin-top:0.5rem;">Zoom \u2197</button>`
          : `<p class="metric-card-empty">No stage data for this night</p>`}
      </div>
      ${renderSleepStatsRow(overview)}
      ${renderSleepQuality(overview.sleep_quality)}
      ${renderNapsSection(naps)}
      <div class="sleep-summary-card metric-card-tappable" data-detail-field="sleep-regularity" role="button" tabindex="0">
        <div class="sleep-summary-top">
          <span class="metric-card-label">Sleep Regularity</span>
          <span class="sleep-stat-value">${regularityIndex !== null ? regularityIndex.sri : "\u2013"}</span>
        </div>
      </div>
      <div class="sleep-summary-card metric-card-tappable" data-detail-field="sleep-reports" role="button" tabindex="0">
        <span class="metric-card-label">Sleep Reports</span>
      </div>
      <div id="sleep-journal-section">${renderJournalSection(journalEntry)}</div>
    `;
    wireSleepOverviewDateNav(anchorDate);
    // Each sub-detail page has one or more tappable elements linking
    // to it (Duration has two - the header number and the stats-row
    // item; Heart Rate/Respiratory Rate/Regularity/Reports have one
    // each, so far) - one map from data-detail-field value to its
    // opener, then wire every matching element generically, rather
    // than hardcoding one query per field.
    const detailOpeners = {
      "sleep-duration": () => openSleepDurationDetail(anchorDate),
      "sleep-heart-rate": () => openSleepHeartRateDetail(anchorDate),
      "sleep-respiratory-rate": () => openSleepRespiratoryRateDetail(anchorDate),
      "sleep-regularity": () => openSleepRegularityDetail(anchorDate),
      "sleep-reports": () => openSleepReportsDetail(anchorDate),
    };
    container.querySelectorAll("[data-detail-field]").forEach(el => {
      const open = detailOpeners[el.dataset.detailField];
      if (!open) return;
      el.addEventListener("click", open);
      el.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    });

    // Hypnogram zoom trigger - opens the shared zoom view with the
    // sleep stage itself as the always-on stepped line, and HR /
    // respiratory rate as OPTIONAL overlays "on top" (the person's own
    // explicit spec) - both default off, since the stage line alone is
    // what most taps into this want to see first.
    const zoomTrigger = document.getElementById("hypnogram-zoom-trigger");
    if (zoomTrigger) {
      zoomTrigger.addEventListener("click", () => {
        const stageSeries = hypnogramToZoomSeries(hypnogram);
        const hrPoints = Object.values(hrSeries || {})[0] || [];
        const respPoints = Object.values(respSeries || {})[0] || [];
        const series = [stageSeries].filter(Boolean);
        if (hrPoints.length > 0) {
          series.push({ key: "hr", label: "Heart Rate", color: ZOOM_HR_COLOR, unit: "bpm", decimals: 0, defaultOn: false, points: hrPoints });
        }
        if (respPoints.length > 0) {
          series.push({ key: "resp", label: "Respiratory Rate", color: ZOOM_RESP_COLOR, unit: "brpm", decimals: 1, defaultOn: false, points: respPoints });
        }
        openZoomChart({ title: "Sleep Stage", series, windowMinutes: 120 });
      });
    }

    wireJournalSection(anchorDate, journalEntry);
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
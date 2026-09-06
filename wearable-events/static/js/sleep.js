// --- Sleep tab ---
import { escapeHtml, api } from "./core.js";

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

export async function loadSleepHistory() {
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
// --- Timeline tab ---
import { escapeHtml, api } from "./core.js";

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

export async function loadTimeline() {
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

export function initTimelineControls() {
  const defaults = defaultTimelineRange();
  document.getElementById("timeline-start").value = defaults.start;
  document.getElementById("timeline-end").value = defaults.end;
  document.getElementById("timeline-refresh").addEventListener("click", loadTimeline);
}
// --- Calendars tab ---
import { escapeHtml, api } from "./core.js";

export async function loadCalendars() {
  const container = document.getElementById("calendar-list");
  try {
    const cals = await api("/calendars");
    if (!cals.length) {
      container.innerHTML = '<p class="muted">No calendars added yet.</p>';
      return;
    }
    container.innerHTML = "";
    cals.forEach(cal => {
      const row = document.createElement("div");
      row.className = "calendar-row";
      const lastSynced = cal.last_synced ? `Last synced: ${cal.last_synced}` : "Never synced";
      const errorLine = cal.last_error ? `<div class="cal-error">${cal.last_error}</div>` : "";
      row.innerHTML = `
        <div class="cal-name">${escapeHtml(cal.name)} ${cal.enabled ? "" : "(disabled)"}</div>
        <div class="cal-meta">${lastSynced} · default: ${cal.default_tag}</div>
        ${errorLine}
      `;
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("cal-add-submit").addEventListener("click", async () => {
  const status = document.getElementById("cal-status");
  const name = document.getElementById("cal-name").value.trim();
  const ics_url = document.getElementById("cal-url").value.trim();
  const default_tag = document.getElementById("cal-default-tag").value.trim();

  if (!name || !ics_url || !default_tag) {
    status.textContent = "All fields required";
    return;
  }

  try {
    await api("/calendars", { method: "POST", body: JSON.stringify({ name, ics_url, default_tag }) });
    status.textContent = "Added";
    document.getElementById("cal-name").value = "";
    document.getElementById("cal-url").value = "";
    document.getElementById("cal-default-tag").value = "";
    loadCalendars();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});
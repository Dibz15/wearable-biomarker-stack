// --- Manage tab: keyword rules (staged draft, no hot-apply) ---
import { escapeHtml, api } from "./core.js";
import { loadTagButtons } from "./tags.js";

let committedRules = [];      // last-known-saved rules from the server, each may be marked _markedForDeletion
let pendingNewRules = [];     // rules added to the draft but not yet saved

function draftIsDirty() {
  return pendingNewRules.length > 0 || committedRules.some(r => r._markedForDeletion);
}

function renderRuleList() {
  const container = document.getElementById("rule-list");
  const rows = [];

  committedRules.forEach(rule => {
    const fieldNote = rule.is_regex ? `regex on ${rule.match_field}` : `contains, on ${rule.match_field}`;
    const exclusiveNote = rule.exclusive === undefined ? " · exclusivity unknown (old rule row?)" : (!rule.exclusive ? " · stacks (non-exclusive)" : "");
    const marked = rule._markedForDeletion;
    const row = document.createElement("div");
    row.className = "list-row" + (marked ? " marked-deleted" : "");
    row.innerHTML = `
      <div>
        <div class="row-title">"${escapeHtml(rule.keyword)}" → <strong>${escapeHtml(rule.tag)}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote}${exclusiveNote}${marked ? " · marked for deletion" : ""}</div>
      </div>
      <button class="small-btn ${marked ? "" : "danger"}">${marked ? "Undo" : "Delete"}</button>
    `;
    row.querySelector(".small-btn").addEventListener("click", () => {
      rule._markedForDeletion = !rule._markedForDeletion;
      renderRuleList();
    });
    rows.push(row);
  });

  pendingNewRules.forEach((rule, idx) => {
    const fieldNote = rule.is_regex ? `regex on ${rule.match_field}` : `contains, on ${rule.match_field}`;
    const exclusiveNote = rule.exclusive === undefined ? " · exclusivity unknown (old rule row?)" : (!rule.exclusive ? " · stacks (non-exclusive)" : "");
    const row = document.createElement("div");
    row.className = "list-row pending-new";
    row.innerHTML = `
      <div>
        <div class="row-title">"${escapeHtml(rule.keyword)}" → <strong>${escapeHtml(rule.tag)}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote}${exclusiveNote} · pending</div>
      </div>
      <button class="small-btn danger">Remove</button>
    `;
    row.querySelector(".small-btn").addEventListener("click", () => {
      pendingNewRules.splice(idx, 1);
      renderRuleList();
    });
    rows.push(row);
  });

  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<p class="muted">No rules yet.</p>';
  } else {
    rows.forEach(r => container.appendChild(r));
  }

  document.getElementById("rule-save-batch").disabled = !draftIsDirty();
}

export async function loadKeywordRules() {
  try {
    committedRules = await api("/keyword_rules");
    committedRules.forEach(r => { r._markedForDeletion = false; });
    pendingNewRules = [];
    renderRuleList();
  } catch (e) {
    document.getElementById("rule-list").innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("rule-add-draft").addEventListener("click", () => {
  const keyword = document.getElementById("rule-keyword").value.trim();
  const tag = document.getElementById("rule-tag").value.trim();
  const category = document.getElementById("rule-category").value;
  const match_field = document.getElementById("rule-match-field").value;
  const is_regex = document.getElementById("rule-is-regex").checked;
  const priority = parseInt(document.getElementById("rule-priority").value, 10) || 0;
  const exclusive = document.getElementById("rule-exclusive").checked;
  const status = document.getElementById("rule-status");

  if (!keyword || !tag) {
    status.textContent = "Keyword and tag are required";
    return;
  }

  pendingNewRules.push({ keyword, tag, category, match_field, is_regex, priority, exclusive, enabled: true });
  document.getElementById("rule-keyword").value = "";
  document.getElementById("rule-tag").value = "";
  document.getElementById("rule-priority").value = "0";
  document.getElementById("rule-is-regex").checked = false;
  // Deliberately NOT resetting rule-exclusive here - it used to silently
  // flip back to checked after every add, which could leave a rule
  // exclusive when the person believed they'd already unchecked it for
  // this session. Leaving it as the person last set it is more
  // predictable when adding several related rules in a row.
  status.textContent = "Added to draft - not saved yet";
  renderRuleList();
});

document.getElementById("rule-save-batch").addEventListener("click", async () => {
  const status = document.getElementById("rule-status");
  const deleted_ids = committedRules.filter(r => r._markedForDeletion).map(r => r.id);

  status.textContent = "Saving...";
  try {
    const result = await api("/keyword_rules/save_batch", {
      method: "POST",
      body: JSON.stringify({ added: pendingNewRules, deleted_ids }),
    });

    status.textContent = "Rules saved";
    await loadKeywordRules(); // refresh committed state, clears draft

    if (result.affected_events > 0) {
      const sample = result.sample_titles.join(", ");
      const confirmed = confirm(
        `This rule change would reclassify ${result.affected_events} previously-synced event(s), ` +
        `e.g.: ${sample}${result.affected_events > result.sample_titles.length ? ", ..." : ""}.\n\n` +
        `Reprocess them now? This runs in the background - you can keep using the app while it works.`
      );
      if (confirmed) {
        startReprocess();
      }
    }
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Reprocess: background trigger + polling banner ---
let reprocessPollHandle = null;

async function startReprocess() {
  const banner = document.getElementById("reprocess-banner");
  banner.style.display = "block";
  banner.className = "reprocess-banner running";
  banner.textContent = "Starting reprocess...";

  try {
    await api("/reprocess", { method: "POST" });
  } catch (e) {
    banner.className = "reprocess-banner error";
    banner.textContent = `Error starting reprocess: ${e.message}`;
    return;
  }

  if (reprocessPollHandle) clearInterval(reprocessPollHandle);
  reprocessPollHandle = setInterval(pollReprocessStatus, 1500);
  pollReprocessStatus();
}

async function pollReprocessStatus() {
  const banner = document.getElementById("reprocess-banner");
  try {
    const s = await api("/reprocess/status");

    if (s.status === "running") {
      banner.style.display = "block";
      banner.className = "reprocess-banner running";
      banner.textContent = `Reprocessing... ${s.processed}/${s.total} checked, ${s.changed} changed`;
    } else if (s.status === "done") {
      banner.className = "reprocess-banner done";
      banner.textContent = `Reprocess complete - ${s.changed}/${s.total} event(s) updated`;
      clearInterval(reprocessPollHandle);
      reprocessPollHandle = null;
      setTimeout(() => { banner.style.display = "none"; }, 6000);
    } else if (s.status === "error") {
      banner.className = "reprocess-banner error";
      banner.textContent = `Reprocess failed: ${s.error}`;
      clearInterval(reprocessPollHandle);
      reprocessPollHandle = null;
    }
  } catch (e) {
    // Transient poll failure - don't kill the banner, just try again next tick
  }
}

// On load, if a reprocess was already running from a previous session
// (e.g. page refreshed mid-run), pick up polling rather than losing track of it.
export async function checkReprocessOnLoad() {
  try {
    const s = await api("/reprocess/status");
    if (s.status === "running") {
      reprocessPollHandle = setInterval(pollReprocessStatus, 1500);
      pollReprocessStatus();
    }
  } catch (e) {
    // ignore
  }
}

// --- Manage tab: tag button definitions ---
export async function loadTagDefManage() {
  const container = document.getElementById("tagdef-list");
  try {
    const defs = await api("/tag_definitions");
    if (!defs.length) {
      container.innerHTML = '<p class="muted">No tag buttons yet.</p>';
      return;
    }
    container.innerHTML = "";
    defs.forEach(def => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `
        <div>
          <div class="row-title">${escapeHtml(def.label)} <span class="muted">(${escapeHtml(def.tag)})</span></div>
          <div class="row-meta">${def.category}${def.is_duration ? " · duration" : ""} · order ${def.sort_order}</div>
        </div>
        <button class="small-btn danger" data-id="${def.id}">Delete</button>
      `;
      row.querySelector(".small-btn").addEventListener("click", async () => {
        await api(`/tag_definitions/${def.id}`, { method: "DELETE" });
        loadTagDefManage();
        loadTagButtons(); // keep the Tags tab in sync
      });
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("tagdef-add-submit").addEventListener("click", async () => {
  const status = document.getElementById("tagdef-status");
  const tag = document.getElementById("tagdef-tag").value.trim();
  const label = document.getElementById("tagdef-label").value.trim();
  const category = document.getElementById("tagdef-category").value;
  const is_duration = document.getElementById("tagdef-is-duration").checked;
  const sort_order = parseInt(document.getElementById("tagdef-sort-order").value, 10) || 0;

  if (!tag || !label) {
    status.textContent = "Tag and label are required";
    return;
  }

  try {
    await api("/tag_definitions", {
      method: "POST",
      body: JSON.stringify({ tag, label, category, is_duration, sort_order }),
    });
    status.textContent = "Tag button added";
    document.getElementById("tagdef-tag").value = "";
    document.getElementById("tagdef-label").value = "";
    document.getElementById("tagdef-sort-order").value = "0";
    document.getElementById("tagdef-is-duration").checked = false;
    loadTagDefManage();
    loadTagButtons(); // keep the Tags tab in sync
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});
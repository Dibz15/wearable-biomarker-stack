// --- init ---
import { escapeHtml, api } from "./core.js";
import { loadToday } from "./today.js";
import { loadTagButtons } from "./tags.js";
import { loadCalendars } from "./calendars.js";
import { loadKeywordRules, checkReprocessOnLoad, loadTagDefManage } from "./manage.js";
import { initTimelineControls, loadTimeline } from "./timeline.js";
import { loadSleepHistory } from "./sleep.js";
import { loadSleepOverview } from "./sleep-overview.js";

// api() (in core.js) dispatches this instead of calling showLogin()
// directly, to avoid a circular import between core.js and this file -
// see core.js's own comment on that 401 handler for the full reasoning.
window.addEventListener("session-expired", () => showLogin());

async function initApp() {
  try {
    const me = await api("/auth/me");
    showApp(me);
  } catch (e) {
    showLogin();
  }
}

function showLogin() {
  document.getElementById("login-screen").style.display = "flex";
  document.getElementById("app-shell").style.display = "none";
}

function showApp(me) {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app-shell").style.display = "block";
  document.getElementById("current-username").textContent = me.username;

  loadToday();
  loadTagButtons();
  loadCalendars();
  loadKeywordRules();
  loadTagDefManage();
  loadUserList();
  loadClaimPicker();
  checkReprocessOnLoad();
  initTimelineControls();
  loadTimeline();
  loadSleepHistory();
  loadSleepOverview();
}

document.getElementById("login-submit").addEventListener("click", async () => {
  const status = document.getElementById("login-status");
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;

  if (!username || !password) {
    status.textContent = "Username and password required";
    return;
  }

  status.textContent = "Logging in...";
  try {
    const me = await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    document.getElementById("login-password").value = "";
    showApp(me);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// Allow pressing Enter in either login field to submit
["login-username", "login-password"].forEach(id => {
  document.getElementById(id).addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("login-submit").click();
  });
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch (e) {
    // even if the request fails, drop back to the login screen -
    // an invalid/expired session should look the same either way
  }
  showLogin();
});

// --- Household members ---
async function loadUserList() {
  const container = document.getElementById("user-list");
  try {
    const users = await api("/users");
    if (!users.length) {
      container.innerHTML = '<p class="muted">No accounts yet.</p>';
      return;
    }
    container.innerHTML = "";
    users.forEach(u => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `<div class="row-title">${escapeHtml(u.username)}</div>`;
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

const MANUAL_OPTION_VALUE = "__manual__";

async function loadClaimPicker() {
  const hint = document.getElementById("claim-hint");
  const select = document.getElementById("new-user-username-select");
  const manualInput = document.getElementById("new-user-username-manual");

  try {
    const unclaimed = await api("/unclaimed_ring_users");

    if (unclaimed.length === 0) {
      // No ring data to claim yet - manual entry is the only path, with
      // a clear warning rather than silently accepting a mismatch.
      hint.textContent = "No unclaimed ring data found yet - you can still add this person, " +
        "but double-check the username matches their ring parser's GADGETBRIDGE_USER once it's set up.";
      hint.className = "muted small-note warning-note";
      select.style.display = "none";
      manualInput.style.display = "block";
      manualInput.placeholder = "Username (must match their GADGETBRIDGE_USER)";
      return;
    }

    hint.textContent = "Pick from ring data that's already synced, to avoid typos that would break correlation.";
    hint.className = "muted small-note";

    select.innerHTML = "";
    unclaimed.forEach(username => {
      const opt = document.createElement("option");
      opt.value = username;
      opt.textContent = username;
      select.appendChild(opt);
    });
    const manualOpt = document.createElement("option");
    manualOpt.value = MANUAL_OPTION_VALUE;
    manualOpt.textContent = "— enter manually instead —";
    select.appendChild(manualOpt);

    select.style.display = "block";
    manualInput.style.display = "none";
    manualInput.value = "";

    select.onchange = () => {
      if (select.value === MANUAL_OPTION_VALUE) {
        manualInput.style.display = "block";
        manualInput.placeholder = "Username (must match their GADGETBRIDGE_USER)";
      } else {
        manualInput.style.display = "none";
      }
    };
  } catch (e) {
    // Influx unreachable or some other failure - don't block account
    // creation over it, just fall back to manual entry.
    hint.textContent = "Couldn't check ring data - you can still add this person manually.";
    hint.className = "muted small-note warning-note";
    select.style.display = "none";
    manualInput.style.display = "block";
  }
}

document.getElementById("new-user-submit").addEventListener("click", async () => {
  const status = document.getElementById("user-status");
  const select = document.getElementById("new-user-username-select");
  const manualInput = document.getElementById("new-user-username-manual");
  const password = document.getElementById("new-user-password").value;

  let username;
  if (select.style.display !== "none" && select.value !== MANUAL_OPTION_VALUE) {
    username = select.value;
  } else {
    username = manualInput.value.trim();
  }

  if (!username || !password) {
    status.textContent = "Username and password required";
    return;
  }

  try {
    const result = await api("/users", { method: "POST", body: JSON.stringify({ username, password }) });
    status.textContent = result.linked_to_ring_data
      ? `Added ${username} - linked to existing ring data`
      : `Added ${username} - no matching ring data found yet, double-check the username later`;
    manualInput.value = "";
    document.getElementById("new-user-password").value = "";
    loadUserList();
    loadClaimPicker(); // refresh so the just-claimed username drops off the list
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

initApp();
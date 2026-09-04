// --- tab switching ---
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

export function escapeHtml(str) {
  // Any place user-typed or externally-sourced text (a regex keyword
  // rule pattern, a calendar name, a tag, a username, an ICS event
  // title from someone else's calendar invite) gets inserted into a
  // template literal that's later assigned to .innerHTML MUST go
  // through this first. Without it, the browser's HTML parser reads
  // characters like < > & as markup rather than literal text - a
  // regex pattern like <\d{3}> gets parsed as an (invalid) HTML tag
  // and silently vanishes from what's rendered, which is exactly the
  // "characters disappear" bug this fixes. Not primarily a security
  // fix (this is a single-user, self-hosted app) - it's a display-
  // correctness fix, since the disappearing content was never
  // executable, just mis-parsed as markup instead of text.
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  // /auth/login's own 401 means "wrong credentials", not "your session
  // expired" - you're never in a session yet at that point, so treating
  // it the same way masks the real reason (e.g. "invalid username or
  // password") behind a misleading message. Let it fall through to the
  // generic error handling below instead.
  if (resp.status === 401 && path !== "/auth/login") {
    // Session expired or was revoked mid-use - drop back to the login
    // screen rather than leaving the UI in a broken half-authenticated
    // state. This used to call showLogin() directly, relying on
    // function hoisting within a single classic script (showLogin was
    // defined much later in that same file). Now that this lives in
    // its own module, calling showLogin() directly would require
    // importing it from app.js - which itself imports api() from
    // here, a circular dependency. Dispatching an event instead keeps
    // this module from needing to know anything about UI navigation
    // at all; app.js listens for it and shows the login screen.
    window.dispatchEvent(new CustomEvent("session-expired"));
    throw new Error("session expired - please log in again");
  }
  if (!resp.ok) {
    throw new Error(data.detail || `Request failed (${resp.status})`);
  }
  return data;
}

// A single place to round-and-format a display number - `decimals`
// left undefined means "don't force a precision", preserving whatever
// a field already showed before this existed (heart_rate/HRV/etc.
// don't need this; temperature does, per person's explicit request).
export function formatNum(v, decimals) {
  if (v === null || v === undefined) return v;
  return decimals === undefined ? v : Number(v.toFixed(decimals));
}

// Local-timezone-safe YYYY-MM-DD helpers. Deliberately NOT using
// Date.toISOString() for this - it always converts to UTC first, which
// silently shifts the date near local midnight (e.g. 11pm local on
// Sep 3 in a timezone behind UTC becomes "Sep 4" after the UTC
// conversion). Date's getFullYear()/getMonth()/getDate() are local-
// timezone-aware, so building the string from those avoids that.
export function dateToISO(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function isoToDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d); // local midnight, not UTC
}

export function todayISO() {
  return dateToISO(new Date());
}

export function shiftISODate(iso, days) {
  const d = isoToDate(iso);
  d.setDate(d.getDate() + days);
  return dateToISO(d);
}
// --- tab switching ---
import { navigate } from "./router.js";

// Pure DOM toggle, no navigation of its own - the router calls this
// directly when a /app/{tab} route dispatches, and the click handler
// below now goes through navigate() instead of calling this directly,
// so a tab click and a browser back/forward landing on the same tab
// both end up here through the same one path.
export function showTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
  const panel = document.getElementById(`tab-${name}`);
  if (btn) btn.classList.add("active");
  if (panel) panel.classList.add("active");
}

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => navigate(`/app/${btn.dataset.tab}`));
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

// --- GET response cache ---
//
// Paging back and forth through periods (or day-by-day through a
// detail view) re-requests the exact same URLs constantly, and every
// one of those was a full round trip. This keeps recent GET responses
// so a revisit is instant.
//
// TTL'd rather than permanent, even though past days look immutable:
// Gadgetbridge exports on its own schedule, so a night's data can be
// backfilled into InfluxDB hours after the fact. An unbounded cache
// keyed on "the requested date is in the past" would pin the empty
// or partial version of that night until a full page reload. Sixty
// seconds is long enough to cover the back-and-forth navigation this
// exists for and short enough that a sync lands on its own.
const CACHE_TTL_MS = 60 * 1000;
const MAX_CACHE_ENTRIES = 120;
const _apiCache = new Map(); // path -> { at, promise }

export function clearApiCache() {
  _apiCache.clear();
}

export async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  // Auth endpoints are deliberately never cached - /auth/me is how the
  // app detects that a session has been revoked, and serving a stale
  // "you're still logged in" from cache would defeat that.
  const cacheable = method === "GET" && !path.startsWith("/auth/");

  if (cacheable) {
    const hit = _apiCache.get(path);
    if (hit && Date.now() - hit.at < CACHE_TTL_MS) {
      // Returns the stored PROMISE, not a stored value - so several
      // callers firing the same request in one tick (the detail views
      // batch four to six at a time) share one round trip instead of
      // racing to start identical ones.
      return hit.promise;
    }
    _apiCache.delete(path);
  } else {
    // Any write can invalidate anything - a new tag, a sleep-journal
    // edit and a reprocess all change what unrelated GETs return, and
    // working out exactly which ones is more machinery than this is
    // worth. Dropping everything costs at most one refetch.
    clearApiCache();
  }

  const promise = _apiFetch(path, options);

  if (cacheable) {
    if (_apiCache.size >= MAX_CACHE_ENTRIES) {
      // Map preserves insertion order, so the first key is the oldest.
      _apiCache.delete(_apiCache.keys().next().value);
    }
    _apiCache.set(path, { at: Date.now(), promise });
    // A failed request must not be left in the cache, or the error
    // gets replayed for the next full minute.
    promise.catch(() => _apiCache.delete(path));
  }

  return promise;
}

async function _apiFetch(path, options = {}) {
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
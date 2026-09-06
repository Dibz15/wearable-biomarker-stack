// --- Client-side routing (History API) ---
//
// Every real "page" this app has lives under /app/* - deliberately a
// DIFFERENT namespace than the API's own paths (GET /sleep, GET
// /activity/sessions, etc.). A bare path like /sleep or /activity
// would collide with a real API endpoint of the same name - a full
// browser navigation (refresh, bookmark, shared link) there would hit
// the API and get raw JSON back instead of this app's own shell. See
// app/main.py's own spa_fallback route for the matching backend half
// of this - it serves index.html for any /app/* path regardless of
// what's after it, so a refresh deep in the app doesn't 404.
//
// Route handlers are registered by each page module via registerRoute()
// rather than listed here, so this file doesn't need to import (and
// therefore doesn't create an import-order dependency on) every page
// module that exists - each module registers its own route(s) as a
// side effect of being imported once, in app.js.

const routes = [];

// Called at the START of every dispatch, before the matched route's
// own handler runs - for UI state that needs clearing on ANY
// navigation, not just navigation to a specific kind of route.
// Currently just the zoom-chart overlay (registered by zoom-chart.js
// itself): it's a sub-overlay opened from WITHIN an already-open
// detail screen, and its own back button (not a route of its own -
// see zoom-chart.js's own comment on why it deliberately has no URL)
// is the only in-app way to close it, since it covers the whole
// screen while open. The one path that bypasses that button entirely
// is the browser's OWN back/forward - pressing it while zoom is open
// navigates the detail screen underneath without ever going through
// zoom's own close logic, which would otherwise leave it stuck open
// on top of whatever the router just navigated the page underneath to.
// A plain function reference here (not an import) keeps this file
// from needing to know zoom-chart.js exists at all.
const beforeDispatchHooks = [];

export function registerBeforeDispatch(fn) {
  beforeDispatchHooks.push(fn);
}

// Each pushState/replaceState carries a monotonically increasing
// "depth" - used by goBack() to tell "the user actually navigated
// here from elsewhere in this app (real history.back() is safe and
// correct)" apart from "this is where a fresh load/refresh/bookmark
// landed directly (history.back() would leave the app entirely, to
// whatever the browser had open before)". Module-level rather than
// read back from history.state on every call, since state itself
// only needs to carry the app's own depth, not derive it each time.
let depthCounter = 0;

export function registerRoute(pattern, handler) {
  routes.push({ pattern, handler });
}

// Navigates to a genuinely NEW page - adds a history entry, so the
// back button steps back through it. Use for anything that opens a
// different screen (tapping a workout row, a Today card, a tab).
export function navigate(path) {
  depthCounter += 1;
  history.pushState({ depth: depthCounter }, "", path);
  dispatch();
}

// Updates the URL for the SAME logical page with new params (prev/
// next day, switching W/M/Y period) - never adds a history entry, so
// clicking through several days doesn't require pressing back once
// per day to actually leave the page. Does NOT re-dispatch (the
// caller is already in the middle of rendering the new state itself -
// re-dispatching here would re-render redundantly, and could recurse
// back into the same render function that's calling this).
export function replaceUrl(path) {
  const depth = history.state?.depth ?? depthCounter;
  history.replaceState({ depth }, "", path);
}

// The shared detail-screen/zoom-screen back buttons call this instead
// of a raw history.back() - real browser back when there's app-
// internal history to actually go back to, otherwise (a fresh load or
// bookmark landed directly on this page, so there's nothing of this
// app's own before it in history) falls forward to a sensible parent
// route instead, so the back button never navigates the tab away from
// the app entirely.
export function goBack(fallbackPath) {
  if ((history.state?.depth ?? 0) > 0) {
    history.back();
  } else {
    navigate(fallbackPath);
  }
}

function dispatch() {
  beforeDispatchHooks.forEach(fn => fn());
  const url = new URL(location.href);
  for (const route of routes) {
    const match = url.pathname.match(route.pattern);
    if (match) {
      route.handler(url.searchParams, match);
      return true;
    }
  }
  return false;
}

// Called once, after every page module has registered its own
// route(s) (see app.js). Establishes an explicit depth-0 state for
// wherever the app actually landed (a fresh load/refresh should never
// leave history.state as null - every goBack() call site relies on
// being able to read a real depth from it), then renders whatever the
// current URL already says - a refresh or a shared link should land
// on that same page directly, not always reset to Today.
//
// Idempotent - showApp() (app.js) calls this every time it runs, which
// happens more than once per page load if the person logs out and
// back in without a full browser refresh. Guarding against a second
// popstate listener piling up avoids every subsequent back/forward
// press dispatching twice (harmless in practice, since every render
// function just overwrites its own content wholesale, but wasteful
// and not the intended behavior).
let initialized = false;

export function initRouter() {
  if (initialized) return;
  initialized = true;
  window.addEventListener("popstate", () => dispatch());
  history.replaceState({ depth: 0 }, "", location.pathname + location.search);
  const matched = dispatch();
  if (!matched) {
    // Nothing recognized (e.g. a bare "/", or an unrecognized path) -
    // default to Today, replacing so this doesn't leave a dead entry
    // behind that "back" would return to.
    replaceUrl("/app/today");
    dispatch();
  }
}
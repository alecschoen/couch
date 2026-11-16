// Wires the launcher together: couchd connection, flood, focus, router, toasts, and the
// remote's keys. A page calls this once with its screens.
//
//   import { createApp } from "./js/app.js";
//   const app = createApp();
//   app.router.register("home", homeScreen);
//   app.start("home");
//
// Keys meant for an app in front of the launcher (`app` isn't "launcher") are ignored, except
// Home, which also takes the launcher back to its first screen.
//
// Keys, in order, until something takes them:
//   0. the idle clock (idle.js): a key that wakes it does nothing else
//   1. the innermost focus scope (an open sheet: Back closes it)
//   2. the top screen's onKey()
//   3. arrows move focus, OK clicks, Back goes back, Home goes to the first screen

import { Couch } from "./couch.js";
import { Toasts } from "./components.js";
import { FocusEngine } from "./focus.js";
import { IdleClock } from "./idle.js";
import { useKeyboardAsRemote } from "./keys.js";
import { Router } from "./router.js";
import { settings } from "./settings.js";
import { Theme } from "./theme.js";

const ARROWS = new Set(["up", "down", "left", "right"]);

export function createApp({ flood = "#flood", screens = "#screens", layer = "#layer", toasts = "#toasts" } = {}) {
  const $ = (sel) => (typeof sel === "string" ? document.querySelector(sel) : sel);
  const app = new EventTarget();

  app.couch = new Couch();
  settings.bind(app.couch); // settings live in couchd; see settings.js
  app.theme = new Theme($(flood));
  app.focus = new FocusEngine(document.body);
  app.layer = $(layer);
  app.toasts = new Toasts($(toasts));
  app.router = new Router({ container: $(screens), focus: app.focus, theme: app.theme, app });
  app.idle = new IdleClock(app); // over everything once couchd says idle; see idle.js

  // detail is the key message's data, e.g. { key: "select", hold: true } for a held OK.
  // True if a scope or the screen took the key (the app's "key" event says so as `taken`).
  app.handleKey = (key, detail = { key }) => {
    if (app.focus.scopeKey(key, detail)) return true;
    if (app.router.handleKey(key, detail)) return true;
    if (ARROWS.has(key)) app.focus.move(key);
    else if (key === "select") app.focus.activate();
    else if (key === "back") app.router.back();
    else if (key === "home") app.router.home();
    return false;
  };

  app.couch.addEventListener("key", (e) => {
    const { key, app: target } = e.detail;
    if (target && target !== "launcher" && key !== "home") return;
    // A press that wakes the idle clock does only that.
    const taken = app.idle.wake(e.detail) || app.handleKey(key, e.detail);
    app.dispatchEvent(new CustomEvent("key", { detail: { ...e.detail, taken } }));
  });
  app.couch.addEventListener("toast", (e) => app.toasts.show(e.detail.text, e.detail.level));
  app.couch.addEventListener("state", (e) => {
    const mode = e.detail.state.system?.mode;
    if (mode) document.documentElement.dataset.mode = mode;
  });

  app.start = (first) => {
    useKeyboardAsRemote(app.couch);
    app.couch.connect();
    app.router.go(first);
  };

  return app;
}

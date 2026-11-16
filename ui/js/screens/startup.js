// Startup: the Couch lockup, a progress bar and "Turning on the TV", while couchd starts its
// services and wakes the TV (live state `startup`). screens/system.js puts it up and takes it
// down.
//
// The boot splash (#4) hands over to this screen, so its first frame must look like the
// splash plus the bar: the night ground, and the stacked lockup at exactly the splash's
// position and size (ui/brand/splash.svg: 340 × 255 px at 790, 412). It appears without the
// usual slide-in, and nothing on it waits for couchd.

import { LOCKUP_STACKED } from "../brand.js";
import { h } from "../components.js";

const STEPS = {
  tv: "Turning on the TV",
  services: "Getting ready",
};
const SLOW = "Taking longer than usual"; // shown when couchd hasn't answered for SLOW_MS
const SLOW_MS = 30_000;

export function startupLabel(startup) {
  return STEPS[startup?.step] ?? STEPS.tv;
}

export function startupScreen(_params, app) {
  const { couch } = app;
  const lockup = h("div", { class: "startup__lockup" });
  lockup.innerHTML = LOCKUP_STACKED;
  const label = h("span", { class: "startup__label" }, startupLabel(null));
  const el = h(
    "section",
    { class: "startup", "aria-live": "polite" },
    lockup,
    h("div", { class: "startup__progress progress is-busy is-thin", role: "progressbar", "aria-label": "Starting" }, h("div", { class: "progress__bar" })),
    label,
  );

  let slow = false;
  const render = () => {
    label.textContent = couch.connected ? startupLabel(couch.state.startup) : slow ? SLOW : STEPS.tv;
  };
  const timer = setTimeout(() => {
    slow = true;
    render();
  }, SLOW_MS);
  couch.addEventListener("state", render);
  couch.addEventListener("connection", render);

  return {
    el,
    palette: "system",
    still: true,
    onKey: () => true, // nothing to do yet; couchd moves on by itself
    destroy() {
      clearTimeout(timer);
      couch.removeEventListener("state", render);
      couch.removeEventListener("connection", render);
    },
  };
}

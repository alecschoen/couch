// The screens couchd decides: Startup, Opening and the error screens. They go up as router
// takeovers, above whatever screen the user was on, and come down when couchd's state says
// so. Call systemScreens(app) before app.start(), so Startup is the very first frame.
//
//   systemScreens(app);
//   app.start("home");
//
// Which one, in order:
//   1. Startup, until couchd's first snapshot, and while `startup.done` is false
//   2. Opening, while `apps.opening` is set
//   3. an error screen, while `apps.error` is set
//
// When couchd restarts under a running launcher, Startup waits RESTART_GRACE_MS before it goes
// up: a restart with the TV already on is done within a second, and Home shouldn't flash.
//
// couchd times all three out itself. In case it doesn't (or a message is lost), each also has
// a limit here, after which the screen under it shows again.

import { errorScreen } from "./error.js";
import { openingScreen } from "./opening.js";
import { startupScreen } from "./startup.js";

const LIMITS_MS = {
  startup: 45_000, // couchd gives up waiting at 30 s
  opening: 40_000, // a tab gets 10 s to load; Kodi 10 s to start
  error: 150_000, // couchd clears errors after 2 minutes
};
const RESTART_GRACE_MS = 1500;

// What couchd's state calls for: { name, params, key } or null. `key` changes whenever the
// screen should be rebuilt.
export function wanted(state, { heard }) {
  if (!heard || state.startup?.done === false) return { name: "startup", params: {}, key: "startup" };
  const apps = state.apps ?? {};
  if (apps.opening) return { name: "opening", params: { app: apps.opening }, key: `opening:${apps.opening}` };
  if (apps.error) return { name: "error", params: { error: apps.error }, key: `error:${JSON.stringify(apps.error)}` };
  return null;
}

export function systemScreens(app) {
  const { couch, router, toasts } = app;
  router.register("startup", startupScreen).register("opening", openingScreen).register("error", errorScreen);

  let heard = false; // had a full snapshot from couchd
  let graceUntil = 0; // after a reconnect, Startup holds off until then
  let graceTimer = null;
  let shown = null; // key of the takeover up now
  let timer = null;
  const expired = new Set(); // keys whose limit ran out while couchd still wanted them

  const update = () => {
    let raw = wanted(couch.state, { heard });
    const wait = graceUntil - Date.now();
    if (raw?.name === "startup" && shown !== "startup" && wait > 0) {
      clearTimeout(graceTimer);
      graceTimer = setTimeout(update, wait);
      raw = null;
    }
    for (const key of expired) if (key !== raw?.key) expired.delete(key);
    const want = raw && !expired.has(raw.key) ? raw : null;
    const key = want?.key ?? null;
    if (key === shown) return;
    clearTimeout(timer);
    shown = key;
    if (!want) {
      router.release();
      return;
    }
    router.takeover(want.name, want.params);
    timer = setTimeout(() => {
      console.warn(`couch: ${want.name} screen timed out`);
      expired.add(key);
      update();
    }, LIMITS_MS[want.name]);
  };

  couch.addEventListener("state", (e) => {
    if (e.detail.patch === null) {
      if (heard) graceUntil = Date.now() + RESTART_GRACE_MS;
      heard = true;
    }
    update();
  });

  // The error screen says what went wrong, so a failed launch needn't say it again in a toast.
  // A command's reply can beat the state patch that sets the error, hence the short wait.
  const show = toasts.show.bind(toasts);
  toasts.show = (text, level) => {
    if (level !== "error") return show(text, level);
    setTimeout(() => couch.state.apps?.error || show(text, level), 250);
    return null;
  };

  update();
}

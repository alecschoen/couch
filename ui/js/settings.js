// User settings, kept by couchd (docs/protocol.md, "Settings"), and the hooks that apply them.
//
//   import { settings } from "./settings.js";
//   settings.get("display.evening");               // "sunset" | "always" | "off"
//   await settings.set("display.evening", "sunset");  // saved by couchd; throws if refused
//   settings.addEventListener("change", (e) => e.detail.key);
//
// createApp() calls settings.bind(app.couch). From then on values come from couchd's live
// state section `settings`, and every change, from this screen or anywhere else, arrives as
// a "change" event. A copy is kept in localStorage only so the first paint after a reload
// has the right colours before the socket is up.
//
// "evening" and "motion" still work as short names for display.evening and display.motion.
// `settings.sun` is couchd's live state `evening` (sunset, sunrise, is it dark); a "change"
// event with key "sun" says it moved.

const CACHE_KEY = "couch.settings";
const SUN_KEY = "couch.sun";

export const DEFAULTS = {
  "display.evening": "sunset",
  "display.motion": "auto",
  "display.dim_after": 300,
  "audio.volume_limit": 100,
  "audio.tv_remote_volume": true,
  "audio.ui_sounds": "on",
  "bluetooth.auto_reconnect": true,
  "network.wifi": false,
  "tv.off_when_idle": false,
  "tv.standby_on_poweroff": false,
  "updates.overnight": true,
};

const ALIASES = { evening: "display.evening", motion: "display.motion" };
const full = (key) => ALIASES[key] ?? key;

export const CHOICES = {
  "audio.ui_sounds": [
    ["on", "On"],
    ["tv", "TV only"],
    ["off", "Off"],
  ],
  "display.evening": [
    ["sunset", "After sunset"],
    ["always", "Always"],
    ["off", "Off"],
  ],
  "display.motion": [
    ["auto", "Auto"],
    ["reduced", "On"],
    ["full", "Off"],
  ],
  "display.dim_after": [
    [60, "1 minute"],
    [120, "2 minutes"],
    [300, "5 minutes"],
    [600, "10 minutes"],
    [900, "15 minutes"],
    [1800, "30 minutes"],
    [3600, "1 hour"],
    [0, "Never"],
  ],
};
CHOICES.evening = CHOICES["display.evening"];
CHOICES.motion = CHOICES["display.motion"];

class Settings extends EventTarget {
  constructor() {
    super();
    this._values = { ...DEFAULTS, ...pick(load(CACHE_KEY)) };
    this._couch = null;
    // couchd's live state `evening` (couchd/evening.py): is the sun down, today's times.
    this.sun = load(SUN_KEY);
  }

  get(key) {
    return this._values[full(key)];
  }

  // Sends the change to couchd. Shows at once; if couchd refuses, goes back and throws.
  async set(key, value) {
    key = full(key);
    const before = this._values[key];
    this._apply({ [key]: value });
    if (!this._couch) return;
    try {
      await this._couch.command("settings.set", { key, value });
    } catch (e) {
      this._apply({ [key]: before });
      throw e;
    }
  }

  bind(couch) {
    this._couch = couch;
    const take = (state) => {
      if (state?.settings) this._apply(state.settings);
      if (state?.evening) this._applySun(couch.state.evening);
    };
    couch.addEventListener("state", (e) => take(e.detail.patch ?? e.detail.state));
    take(couch.state);
  }

  _applySun(sun) {
    if (JSON.stringify(sun) === JSON.stringify(this.sun)) return;
    this.sun = { ...sun };
    save(SUN_KEY, this.sun);
    this.dispatchEvent(new CustomEvent("change", { detail: { key: "sun", value: this.sun } }));
  }

  _apply(values) {
    const changed = Object.entries(values).filter(([k, v]) => this._values[k] !== v);
    if (!changed.length) return;
    for (const [k, v] of changed) this._values[k] = v;
    save(CACHE_KEY, this._values);
    for (const [key, value] of changed) {
      this.dispatchEvent(new CustomEvent("change", { detail: { key, value } }));
    }
  }
}

function pick(cached) {
  return Object.fromEntries(Object.entries(cached ?? {}).filter(([k]) => k in DEFAULTS));
}

function load(key) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? {};
  } catch {
    return {};
  }
}

function save(key, values) {
  try {
    localStorage.setItem(key, JSON.stringify(values));
  } catch {
    // Only the cache; couchd has the real copy.
  }
}

export const settings = new Settings();

// ---- Reduced motion: sets <html data-motion="reduced"> for css/tokens.css and motion.js.

const systemReduced = matchMedia("(prefers-reduced-motion: reduce)");

export function applyMotion() {
  const mode = settings.get("display.motion");
  const isReduced = mode === "reduced" || (mode === "auto" && systemReduced.matches);
  document.documentElement.dataset.motion = isReduced ? "reduced" : "full";
}

settings.addEventListener("change", (e) => e.detail.key === "display.motion" && applyMotion());
systemReduced.addEventListener("change", applyMotion);
applyMotion();

// ---- Evening colours. "After sunset" follows couchd's `evening.dark` (sunset and sunrise
// where the Pi's timezone is, couchd/evening.py); fixed hours until couchd has said.

export const EVENING_STARTS = 18; // 18:00
export const EVENING_ENDS = 7; // 07:00

export function isEvening(mode = settings.get("display.evening"), now = new Date(), sun = settings.sun) {
  if (mode === "always") return true;
  if (mode === "off") return false;
  if (typeof sun?.dark === "boolean") return sun.dark;
  const hour = now.getHours();
  return hour >= EVENING_STARTS || hour < EVENING_ENDS;
}

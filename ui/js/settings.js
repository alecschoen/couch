// UI settings, and the hooks that apply them.
//
//   import { settings } from "./settings.js";
//   settings.get("evening");                       // "sunset" | "always" | "off"
//   settings.set("evening", "sunset");
//   settings.addEventListener("change", (e) => e.detail.key);
//
// couchd doesn't store settings yet, so for now they live in this browser profile
// (localStorage). When it does, swap load() and save() for a state section and a command;
// nothing else needs to change.

const STORAGE_KEY = "couch.settings";

export const DEFAULTS = {
  evening: "always", // Evening colors: "sunset" (after sunset) | "always" | "off"
  motion: "auto", // Reduce motion: "auto" (follow the system) | "reduced" | "full"
};

export const CHOICES = {
  evening: [
    ["sunset", "After sunset"],
    ["always", "Always"],
    ["off", "Off"],
  ],
  motion: [
    ["auto", "Auto"],
    ["reduced", "On"],
    ["full", "Off"],
  ],
};

class Settings extends EventTarget {
  constructor() {
    super();
    this._values = { ...DEFAULTS, ...load() };
  }

  get(key) {
    return this._values[key];
  }

  set(key, value) {
    if (this._values[key] === value) return;
    this._values[key] = value;
    save(this._values);
    this.dispatchEvent(new CustomEvent("change", { detail: { key, value } }));
  }
}

function load() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) ?? {};
  } catch {
    return {};
  }
}

function save(values) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(values));
  } catch {
    // Not persisted; the setting still applies until the page reloads.
  }
}

export const settings = new Settings();

// ---- Reduced motion: sets <html data-motion="reduced"> for css/tokens.css and motion.js.

const systemReduced = matchMedia("(prefers-reduced-motion: reduce)");

export function applyMotion() {
  const mode = settings.get("motion");
  const isReduced = mode === "reduced" || (mode === "auto" && systemReduced.matches);
  document.documentElement.dataset.motion = isReduced ? "reduced" : "full";
}

settings.addEventListener("change", (e) => e.detail.key === "motion" && applyMotion());
systemReduced.addEventListener("change", applyMotion);
applyMotion();

// ---- Evening colours. "After sunset" uses fixed hours until couchd knows where it is.

export const EVENING_STARTS = 18; // 18:00
export const EVENING_ENDS = 7; // 07:00

export function isEvening(mode = settings.get("evening"), now = new Date()) {
  if (mode === "always") return true;
  if (mode === "off") return false;
  const hour = now.getHours();
  return hour >= EVENING_STARTS || hour < EVENING_ENDS;
}

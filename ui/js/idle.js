// The idle clock (#20, artboard "Screen · Idle clock"): a dark clock and date over the whole
// launcher once couchd says it's idle (live state `idle.idle`, couchd/idle.py). Whatever
// screen was up stays underneath, untouched, and is there again on waking.
//
//   app.idle = new IdleClock(app);      // createApp() does this
//   app.idle.wake(keyDetail)            // true: the key woke the clock (or is its repeat), drop it
//
// Any button wakes it, and that press does nothing else: couchd sends the `key` message
// before `idle` turns false, so the clock is still up when the key gets here. A held arrow's
// repeats are dropped too, until a fresh press. The quick menu (Blue) and anything else on
// the app's "key" event see `taken: true`; code that listens to other messages for the same
// press can ask `app.idle.justWoke()`.
//
// Against burn-in the clock drifts between a few spots every DRIFT_MS. In dev mode, "i" on
// the keyboard brings the clock up at once (or run couchd with COUCH_IDLE_AFTER=10).

import { animate } from "./motion.js";
import { clock, h } from "./components.js";
import { settings } from "./settings.js";

const DRIFT_MS = 3 * 60_000;
const JUST_WOKE_MS = 600;
// Where the clock's top-left corner sits, in turn: the artboard's spots ×1.5 for 1080p.
const SPOTS = [
  [144, 144],
  [780, 270],
  [270, 450],
  [900, 495],
  [495, 180],
];

export class IdleClock {
  constructor(app) {
    this.app = app;
    this.visible = false;
    this._wokeAt = -Infinity;
    this._dropRepeats = false;
    this._stopClock = null;
    this._drift = null;
    this._spot = 0;

    this.time = h("span", { class: "t-clock-idle idle__time" });
    this.date = h("span", { class: "idle__date" });
    this.box = h("div", { class: "idle__clock" }, this.time, this.date);
    this.sound = h("span", {});
    this.el = h(
      "div",
      { class: "idle", "data-palette": "idle", hidden: true, "aria-hidden": "true" },
      this.box,
      h("div", { class: "idle__footer" }, h("span", {}, "Press any button"), this.sound),
    );
    document.body.append(this.el);

    app.couch.addEventListener("state", () => this._sync());
    settings.addEventListener("change", (e) => e.detail.key === "tv.off_when_idle" && this._footer());
    window.addEventListener("keydown", (e) => {
      if (e.key === "i" && document.documentElement.dataset.mode === "dev" && !this.visible) {
        app.couch.command("idle.now").catch((err) => app.toasts.show(err.message, "error"));
      }
    });
  }

  // A key from couchd. True if it's for the clock (it woke it), not for the screen.
  wake(detail = {}) {
    if (this.visible) {
      this.hide();
      this._dropRepeats = true;
      return true;
    }
    if (this._dropRepeats && detail.repeat) return true;
    this._dropRepeats = false;
    return false;
  }

  justWoke() {
    return performance.now() - this._wokeAt < JUST_WOKE_MS;
  }

  show() {
    if (this.visible) return;
    this.visible = true;
    this._footer();
    this._spot = Math.floor(Date.now() / DRIFT_MS) % SPOTS.length;
    this._place();
    this._stopClock = clock(({ time, date }) => {
      this.time.textContent = time;
      this.date.textContent = date;
    });
    this._drift = setInterval(() => {
      this._spot = (this._spot + 1) % SPOTS.length;
      this._place();
    }, DRIFT_MS);
    this.el.hidden = false;
    animate(this.el, [{ opacity: 0 }, { opacity: 1 }], { dur: "flood", ease: "in-out" });
  }

  hide() {
    if (!this.visible) return;
    this.visible = false;
    this._wokeAt = performance.now();
    this._stopClock?.();
    clearInterval(this._drift);
    this.el.hidden = true;
  }

  _sync() {
    const state = this.app.couch.state;
    const onLauncher = (state.apps?.foreground ?? "launcher") === "launcher";
    if (state.idle?.idle && onLauncher) this.show();
    else this.hide();
    if (this.visible) this._footer();
  }

  _place() {
    const [x, y] = SPOTS[this._spot];
    this.box.style.transform = `translate(${x}px, ${y}px)`;
  }

  _footer() {
    const audio = this.app.couch.state.audio ?? {};
    const output = audio.outputs?.find((o) => o.id === audio.current)?.name;
    const parts = [];
    if (output) parts.push(`Sound: ${output}`);
    const off = this.app.couch.state.idle?.tv_off_after;
    if (settings.get("tv.off_when_idle") && off) parts.push(`the TV turns off after ${hours(off)}`);
    this.sound.textContent = parts.join(" · ");
  }
}

function hours(seconds) {
  const n = Math.round(seconds / 3600);
  if (n >= 1) return n === 1 ? "an hour" : `${n} hours`;
  const m = Math.max(1, Math.round(seconds / 60));
  return m === 1 ? "a minute" : `${m} minutes`;
}

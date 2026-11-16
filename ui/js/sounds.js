// UI sounds (#49): short, quiet sounds that confirm what the remote did on the launcher.
//
//   import { UiSounds } from "./sounds.js";
//   new UiSounds(app);          // createApp() does this; it listens from then on
//   sounds.play("select");      // or play one directly
//
// Which sound, for a key the launcher handled (app.js's "key" event):
//   move    an arrow moved focus (or a screen used it: a stepper, a slider)
//   tick    the same, for a held arrow's repeats: lighter, and at most one per REPEAT_GAP_MS
//   edge    an arrow with nowhere to go
//   select  OK
//   back    Back or Home that went somewhere (Back on Home makes no sound, as it does nothing)
// and, without a key:
//   toast   a message pops up
//   error   an error screen goes up
//
// Only the launcher makes these, and only while it's in front: not over YouTube (which has no
// sounds) or Plex (Kodi has its own, which couchd/kodi_sounds.py switches with the same
// setting), and not for the key that wakes the idle clock. The setting `audio.ui_sounds` says
// where: "on" (any output, the default), "tv" (only while the sound is on the TV speakers: the
// jack and Bluetooth can lag behind) or "off".
//
// The sounds are made here with Web Audio, not loaded from files: a sine or triangle with a
// short glide and a soft decay, through a low-pass filter so nothing is sharp at TV volume.
// Each is under 150 ms. They play through Chromium's normal output, so they follow the sound
// output and stay under the volume limit like everything else. couch-browser starts Chromium
// with --autoplay-policy=no-user-gesture-required, so no click is needed first.

import { settings } from "./settings.js";

const MASTER = 0.32; // overall level; each sound's own gain is relative to this
const REPEAT_GAP_MS = 70;
const ARROWS = new Set(["up", "down", "left", "right"]);

// [type, start Hz, end Hz, duration s, gain, delay s]: one or two notes per sound.
export const SOUNDS = {
  move: [["sine", 740, 660, 0.045, 0.5]],
  tick: [["sine", 700, 660, 0.03, 0.28]],
  edge: [["sine", 190, 150, 0.07, 0.55]],
  select: [
    ["triangle", 523, 523, 0.06, 0.45],
    ["triangle", 784, 784, 0.09, 0.4, 0.05],
  ],
  back: [
    ["triangle", 659, 659, 0.06, 0.4],
    ["triangle", 440, 440, 0.09, 0.38, 0.05],
  ],
  toast: [
    ["sine", 659, 659, 0.08, 0.35],
    ["sine", 880, 880, 0.12, 0.3, 0.06],
  ],
  error: [
    ["triangle", 330, 330, 0.08, 0.45],
    ["triangle", 262, 262, 0.12, 0.45, 0.07],
  ],
};

export class UiSounds {
  constructor(app) {
    this.app = app;
    this._ctx = null;
    this._out = null;
    this._lastRepeat = 0;

    app.addEventListener("key", (e) => this._onKey(e.detail));
    app.router.addEventListener("change", (e) => {
      if (e.detail.screen?.name === "error") this.play("error");
    });
    const show = app.toasts.show.bind(app.toasts);
    app.toasts.show = (text, level = "info", iconName = null) => {
      this.play(level === "error" ? "error" : "toast");
      return show(text, level, iconName);
    };
  }

  // Would a sound play now? The setting, and the launcher being in front.
  get enabled() {
    const mode = settings.get("audio.ui_sounds");
    if (mode === "off") return false;
    const state = this.app.couch.state ?? {};
    const front = state.apps?.foreground;
    if (front && front !== "launcher") return false;
    if (mode === "tv" && state.audio?.current !== "tv") return false;
    return true;
  }

  // Which sound a handled key makes, or null. detail: app.js's "key" event.
  static forKey({ key, repeat, app, taken, woke, moved }) {
    if (woke || (app && app !== "launcher")) return null;
    if (ARROWS.has(key)) {
      if (!moved && !taken) return repeat ? null : "edge";
      return repeat ? "tick" : "move";
    }
    if (key === "select") return "select";
    if (key === "back" || key === "home") return moved || taken ? "back" : null;
    return null;
  }

  play(name) {
    const notes = SOUNDS[name];
    if (!notes || !this.enabled) return;
    const ctx = this._context();
    if (!ctx) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const t0 = ctx.currentTime + 0.005;
    for (const [type, from, to, dur, gain, delay = 0] of notes) {
      const start = t0 + delay;
      const osc = ctx.createOscillator();
      osc.type = type;
      osc.frequency.setValueAtTime(from, start);
      if (to !== from) osc.frequency.exponentialRampToValueAtTime(to, start + dur);
      const env = ctx.createGain();
      // A 4 ms attack (no click), then an exponential decay to silence.
      env.gain.setValueAtTime(0.0001, start);
      env.gain.exponentialRampToValueAtTime(gain, start + 0.004);
      env.gain.exponentialRampToValueAtTime(0.0001, start + dur);
      osc.connect(env).connect(this._out);
      osc.start(start);
      osc.stop(start + dur + 0.02);
    }
  }

  _onKey(detail) {
    const name = UiSounds.forKey(detail);
    if (name === "tick") {
      const now = performance.now();
      if (now - this._lastRepeat < REPEAT_GAP_MS) return;
      this._lastRepeat = now;
    }
    if (name) this.play(name);
  }

  _context() {
    if (this._ctx) return this._ctx;
    const Ctx = window.AudioContext ?? window.webkitAudioContext;
    if (!Ctx) return null;
    const ctx = new Ctx({ latencyHint: "interactive" });
    const filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.value = 2400;
    const master = ctx.createGain();
    master.gain.value = MASTER;
    filter.connect(master).connect(ctx.destination);
    this._ctx = ctx;
    this._out = filter;
    return ctx;
  }
}

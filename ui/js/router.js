// Screens and the back stack.
//
//   router.register("bluetooth", (params, app) => ({
//     el,                         // the screen's root element (class="screen")
//     palette: "bt",              // flood colour while it's on top
//     onKey(key) {},              // optional: return true to take a key before focus does
//     onShow() {}, onHide() {},   // optional: on top / covered or closed
//     still: true,                // optional: appear without the slide-in (Startup)
//     destroy() {},               // optional: closed for good (stop timers, listeners)
//   }));
//   router.go("bluetooth");       // push
//   router.back();                // pop; false at the bottom of the stack
//   router.home();                // pop to the first screen
//
//   router.takeover("opening", { app: "plex" });  // a screen above the stack, not in it
//   router.release();                              // take it down: the stack shows again
//
// Covered screens stay in the page, hidden and inert, so going back is instant and focus
// lands where it was.
//
// A takeover is for the screens couchd's state decides (Startup, Opening, errors: see
// screens/system.js). It covers everything, takes the keys, and floods its own colour.
// While it's up, go(), back() and home() still change the stack underneath, out of sight.

import { animate } from "./motion.js";

export class Router extends EventTarget {
  constructor({ container, focus, theme, app }) {
    super();
    this.container = container;
    this.focus = focus;
    this.theme = theme;
    this.app = app;
    this._factories = new Map();
    this._stack = [];
    this._takeover = null;
  }

  register(name, factory) {
    this._factories.set(name, factory);
    return this;
  }

  // The screen on top: the takeover if one is up, else the top of the stack.
  get current() {
    return this._takeover ?? this.top;
  }

  get top() {
    return this._stack.at(-1) ?? null;
  }

  get takenOver() {
    return this._takeover;
  }

  get depth() {
    return this._stack.length;
  }

  go(name, params = {}) {
    if (this.top?.name === name && !Object.keys(params).length) return; // already there
    const screen = this._make(name, params);
    const covered = this.top;
    if (covered && !this._takeover) this._cover(covered);

    this._stack.push(screen);
    if (this._takeover) {
      screen.el.hidden = true;
      screen.el.inert = true;
      this.container.insertBefore(screen.el, this._takeover.el);
    } else {
      this.container.append(screen.el);
      this._show(screen, { entering: true });
    }
  }

  back() {
    if (this._stack.length < 2) return false;
    this._close(this._stack.pop());
    if (!this._takeover) this._show(this.top, { entering: false });
    return true;
  }

  home() {
    if (this._stack.length < 2) return false;
    const top = this._stack.pop();
    while (this._stack.length > 1) this._destroy(this._stack.pop(), { animate: false });
    this._close(top);
    if (!this._takeover) this._show(this.top, { entering: false });
    return true;
  }

  // Put a screen up above the stack, replacing any takeover already up.
  takeover(name, params = {}) {
    const screen = this._make(name, params);
    const previous = this._takeover;
    this._takeover = screen;
    if (previous) this._close(previous);
    else if (this.top) this._cover(this.top);
    this.container.append(screen.el);
    this._show(screen, { entering: !previous });
  }

  // Take the takeover down; the top of the stack comes back as it was.
  release() {
    const screen = this._takeover;
    if (!screen) return;
    this._takeover = null;
    this._close(screen);
    if (this.top) this._show(this.top, { entering: false });
  }

  _make(name, params) {
    const factory = this._factories.get(name);
    if (!factory) throw new Error(`No screen called ${name}`);
    const screen = factory(params, this.app);
    screen.name = name;
    screen.el.classList.add("screen");
    screen.el.dataset.screen = name;
    return screen;
  }

  // The top screen's own key handling. True if it took the key.
  handleKey(key) {
    return this.current?.onKey?.(key) === true;
  }

  _show(screen, { entering }) {
    const { el } = screen;
    el.hidden = false;
    el.inert = false;
    this.theme.set(typeof screen.palette === "function" ? screen.palette() : screen.palette);
    if (!screen.still) {
      animate(el, [{ opacity: 0, transform: `translateY(${entering ? 24 : -24}px)` }, { opacity: 1, transform: "none" }], {
        dur: "screen",
      });
    }
    this.focus.enter(el);
    screen.onShow?.();
    this.dispatchEvent(new CustomEvent("change", { detail: { screen } }));
  }

  _cover(screen) {
    this.focus.remember(screen.el);
    screen.el.inert = true;
    screen.el.hidden = true;
    screen.onHide?.();
  }

  _close(screen) {
    screen.el.inert = true;
    screen.onHide?.();
    this._destroy(screen, { animate: true });
  }

  _destroy(screen, { animate: withAnimation }) {
    screen.destroy?.();
    const fade = withAnimation
      ? animate(screen.el, [{ opacity: 1 }, { opacity: 0 }], { dur: "screen", fill: "forwards" })
      : null;
    if (fade) fade.finished.then(() => screen.el.remove(), () => screen.el.remove());
    else screen.el.remove();
  }
}

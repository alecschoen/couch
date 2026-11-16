// Screens and the back stack.
//
//   router.register("bluetooth", (params, app) => ({
//     el,                         // the screen's root element (class="screen")
//     palette: "bt",              // flood colour while it's on top
//     onKey(key, data) {},        // optional: return true to take a key before focus does
//     onShow() {}, onHide() {},   // optional: on top / covered or closed
//     destroy() {},               // optional: closed for good (stop timers, listeners)
//   }));
//   router.go("bluetooth");       // push
//   router.back();                // pop; false at the bottom of the stack
//   router.home();                // pop to the first screen
//
// Covered screens stay in the page, hidden and inert, so going back is instant and focus
// lands where it was.

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
  }

  register(name, factory) {
    this._factories.set(name, factory);
    return this;
  }

  get current() {
    return this._stack.at(-1) ?? null;
  }

  get depth() {
    return this._stack.length;
  }

  go(name, params = {}) {
    const factory = this._factories.get(name);
    if (!factory) throw new Error(`No screen called ${name}`);
    if (this.current?.name === name && !Object.keys(params).length) return; // already there
    const screen = factory(params, this.app);
    screen.name = name;
    screen.el.classList.add("screen");
    screen.el.dataset.screen = name;

    const covered = this.current;
    if (covered) this._cover(covered);

    this._stack.push(screen);
    this.container.append(screen.el);
    this._show(screen, { entering: true });
  }

  back() {
    if (this._stack.length < 2) return false;
    this._close(this._stack.pop());
    this._show(this.current, { entering: false });
    return true;
  }

  home() {
    if (this._stack.length < 2) return false;
    const top = this._stack.pop();
    while (this._stack.length > 1) this._destroy(this._stack.pop(), { animate: false });
    this._close(top);
    this._show(this.current, { entering: false });
    return true;
  }

  // The top screen's own key handling. True if it took the key.
  handleKey(key, data = { key }) {
    return this.current?.onKey?.(key, data) === true;
  }

  _show(screen, { entering }) {
    const { el } = screen;
    el.hidden = false;
    el.inert = false;
    this.theme.set(typeof screen.palette === "function" ? screen.palette() : screen.palette);
    animate(el, [{ opacity: 0, transform: `translateY(${entering ? 24 : -24}px)` }, { opacity: 1, transform: "none" }], {
      dur: "screen",
    });
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

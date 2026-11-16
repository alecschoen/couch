// Spatial focus: the arrow keys move focus to the nearest focusable element in that direction.
//
//   const focus = new FocusEngine(document.body);
//   focus.move("right");        // true if focus moved
//   focus.activate();           // OK: clicks the focused element
//   focus.enter(screenEl);      // focus a screen's first element (see "Where focus starts")
//
// What's focusable: enabled <button>s, and anything with tabindex="-1" or data-focusable,
// inside the current scope. Hidden, disabled and inert elements are skipped, so screens that
// aren't on top (the router makes them inert) never take focus.
//
// The engine keeps track of focus itself and marks the focused element with .is-focused,
// which components.css draws as a solid ink fill. It also moves DOM focus along, but doesn't
// rely on it: a window without OS focus gets no :focus and no focus events. Listen for the
// "couch:focus" event (it bubbles) to react to focus arriving on an element.
//
// Where focus starts, in a container: the element it had last (if remembered), else
// [data-focus-default], else the first focusable.
//
// Markup hooks:
//   data-focus-group     Coming back into a group returns to the element it had last,
//                        not the geometrically nearest one (a row of cards, a side list).
//   data-focus-enter-start  On a group: the first time in, start where enter() would
//                        (a settings panel next to its tabs), not at the nearest element.
//   data-focus-key="…"   A stable id. When a re-render replaces the focused element, focus
//                        moves to the new element with the same key.
//   data-scroll          A scroll container: focus scrolls it so the focused element shows.
//
// Scopes: a sheet or dialog calls pushScope(el, { onKey }) to keep focus inside it, and
// popScope() to hand focus back to where it was.

import { animate } from "./motion.js";

const FOCUSABLE = "button:not([disabled]), [tabindex='-1'], [data-focusable]";
const DIRECTIONS = new Set(["up", "down", "left", "right"]);
const SCROLL_MARGIN = 96;

export class FocusEngine extends EventTarget {
  constructor(root = document.body) {
    super();
    this.root = root;
    this._scopes = [];
    this._groups = new WeakMap(); // group element → last focused element in it
    this._remembered = new WeakMap(); // container → last focused element in it
    this._current = null;
    this._last = null; // { el, key, rect, scope } of the last focus, to recover from re-renders
    // A mouse click in dev mode moves DOM focus; follow it.
    root.addEventListener("focusin", (e) => {
      if (e.target !== this._current && this.candidates().includes(e.target)) this.focus(e.target);
    });
    // Re-renders can take the focused element out of the page. Put focus back right away.
    new MutationObserver(() => this.ensure()).observe(root, { childList: true, subtree: true });
  }

  get scope() {
    return this._scopes.at(-1)?.el ?? this.root;
  }

  get current() {
    const el = this._current;
    return el && el.isConnected && this.scope.contains(el) && isFocusable(el) ? el : null;
  }

  // ---- Moving

  move(direction) {
    if (!DIRECTIONS.has(direction)) return false;
    const from = this.current;
    if (!from) return this.enter(this.scope);

    const target = this._best(from, direction);
    if (!target) {
      this._bump(from, direction);
      return false;
    }
    this.focus(target);
    return true;
  }

  activate() {
    const el = this.current;
    if (!el) return false;
    el.click();
    return true;
  }

  focus(el) {
    if (!el) return false;
    const previous = this._current;
    if (previous && previous !== el) previous.classList.remove("is-focused");
    this._current = el;
    el.classList.add("is-focused");
    el.focus({ preventScroll: true });
    this._scrollIntoView(el);
    if (previous !== el) this._noteFocus(el);
    return true;
  }

  // Focus the element that should have focus in a container (see "Where focus starts").
  enter(container = this.scope) {
    return this.focus(this.initial(container));
  }

  initial(container) {
    const candidates = this.candidates(container);
    const remembered = this._remembered.get(container);
    if (remembered && candidates.includes(remembered)) return remembered;
    const preferred = container.querySelector("[data-focus-default]");
    if (preferred && candidates.includes(preferred)) return preferred;
    return candidates[0] ?? null;
  }

  // Remember the focused element of a container (a screen being covered), for enter() later.
  remember(container) {
    const el = this.current ?? (this._last?.el?.isConnected ? this._last.el : null);
    if (el && container.contains(el)) this._remembered.set(container, el);
  }

  candidates(container = this.scope) {
    return [...container.querySelectorAll(FOCUSABLE)].filter(isFocusable);
  }

  // ---- Scopes

  pushScope(el, { onKey = null } = {}) {
    this._scopes.push({ el, onKey, returnTo: this.current });
    this.enter(el);
  }

  popScope(el) {
    const i = this._scopes.findLastIndex((s) => s.el === el);
    if (i < 0) return;
    const [scope] = this._scopes.splice(i, 1);
    if (scope.returnTo?.isConnected && isFocusable(scope.returnTo)) this.focus(scope.returnTo);
    else this.enter(this.scope);
  }

  // The innermost scope's key handler, if it has one. Returns true if it took the key.
  scopeKey(key, detail = { key }) {
    const scope = this._scopes.at(-1);
    return scope?.onKey ? scope.onKey(key, detail) === true : false;
  }

  // ---- Recovering after a re-render

  ensure() {
    if (this.current) return;
    const last = this._last;
    if (!last || last.scope !== this.scope) return;
    const candidates = this.candidates();
    if (!candidates.length) return;
    const same = last.key && candidates.find((c) => c.dataset.focusKey === last.key);
    this.focus(same ?? nearest(candidates, last.rect));
  }

  // ---- Internals

  _noteFocus(el) {
    for (let g = el.closest("[data-focus-group]"); g; g = g.parentElement.closest("[data-focus-group]")) {
      this._groups.set(g, el);
    }
    this._last = { el, key: el.dataset.focusKey ?? null, rect: layoutRect(el), scope: this.scope };
    el.dispatchEvent(new CustomEvent("couch:focus", { bubbles: true }));
    this.dispatchEvent(new CustomEvent("change", { detail: { el } }));
  }

  _best(from, direction) {
    const origin = layoutRect(from);
    let best = null;
    let bestScore = Infinity;
    for (const el of this.candidates()) {
      if (el === from || el.contains(from) || from.contains(el)) continue;
      const score = scoreCandidate(origin, layoutRect(el), direction);
      if (score < bestScore) {
        bestScore = score;
        best = el;
      }
    }
    if (!best) return null;

    // Entering a group from outside it: go back to where the group's focus was, or to its
    // start if it asks for that.
    // Groups nest (a segmented control in a settings panel): the outermost one entered wins.
    const entered = [];
    for (let g = best.closest("[data-focus-group]"); g && !g.contains(from); g = g.parentElement.closest("[data-focus-group]")) {
      entered.unshift(g);
    }
    for (const group of entered) {
      const last = this._groups.get(group);
      if (last && last.isConnected && isFocusable(last) && this.scope.contains(last)) return last;
      if (group.hasAttribute("data-focus-enter-start")) return this.initial(group) ?? best;
    }
    return best;
  }

  _scrollIntoView(el) {
    const box = el.closest("[data-scroll]");
    if (!box) return;
    const r = el.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    let top = box.scrollTop;
    if (r.top < b.top + SCROLL_MARGIN) top -= b.top + SCROLL_MARGIN - r.top;
    else if (r.bottom > b.bottom - SCROLL_MARGIN) top += r.bottom - (b.bottom - SCROLL_MARGIN);
    if (top !== box.scrollTop) {
      box.scrollTo({ top, behavior: document.documentElement.dataset.motion === "reduced" ? "instant" : "smooth" });
    }
  }

  // Nothing that way: a small nudge, so the press doesn't feel lost.
  _bump(el, direction) {
    if (el.getAnimations().length) return;
    const d = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] }[direction];
    animate(el, [{ transform: "none" }, { transform: `translate(${d[0] * 9}px, ${d[1] * 9}px)` }, { transform: "none" }], {
      dur: "quick",
      ease: "out",
    });
  }
}

// Where an element sits in the layout, ignoring transforms. Cards slide and screens fade in
// with transforms; geometry from mid-animation positions would make quick presses skip.
function layoutRect(el) {
  let left = 0;
  let top = 0;
  for (let node = el; node; node = node.offsetParent) {
    left += node.offsetLeft;
    top += node.offsetTop;
  }
  for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
    left -= node.scrollLeft;
    top -= node.scrollTop;
  }
  const width = el.offsetWidth;
  const height = el.offsetHeight;
  return { left, top, width, height, right: left + width, bottom: top + height };
}

function isFocusable(el) {
  if (el.closest("[inert], [hidden]")) return false;
  if (!el.getClientRects().length) return false;
  return getComputedStyle(el).visibility !== "hidden";
}

// Lower is better. Only elements past the origin in that direction count. Ones that overlap
// the origin across the direction of travel (same row for left/right, same column for
// up/down) win over ones that don't; then the nearest, then the best lined up.
function scoreCandidate(o, c, direction) {
  const horizontal = direction === "left" || direction === "right";
  const sign = direction === "right" || direction === "down" ? 1 : -1;

  const oStart = horizontal ? o.left : o.top;
  const oEnd = horizontal ? o.right : o.bottom;
  const cStart = horizontal ? c.left : c.top;
  const cEnd = horizontal ? c.right : c.bottom;
  const cCenter = (cStart + cEnd) / 2;

  // Must lie ahead: its centre past our far edge (so a wide row doesn't go "right" into a
  // control inside its own span), and its near edge not behind our near edge.
  if ((cCenter - (sign > 0 ? oEnd : oStart)) * sign <= 0) return Infinity;
  if (sign > 0 ? cStart < oStart : cEnd > oEnd) return Infinity;

  const gap = Math.max(0, sign > 0 ? cStart - oEnd : oStart - cEnd);

  const oCrossStart = horizontal ? o.top : o.left;
  const oCrossEnd = horizontal ? o.bottom : o.right;
  const cCrossStart = horizontal ? c.top : c.left;
  const cCrossEnd = horizontal ? c.bottom : c.right;
  const overlap = Math.min(oCrossEnd, cCrossEnd) - Math.max(oCrossStart, cCrossStart);
  const crossGap = Math.max(0, -overlap);
  const crossOffset = Math.abs((cCrossStart + cCrossEnd) / 2 - (oCrossStart + oCrossEnd) / 2);

  if (overlap > 0) return gap + crossOffset * 0.1;
  // Not lined up. Left and right still reach the next column over (a side list and its
  // panel), but up and down only within a 45° cone, so Down at the end of a side list
  // doesn't jump across the screen.
  if (!horizontal && crossGap > gap) return Infinity;
  return 100_000 + gap + crossGap * 2 + crossOffset * 0.1;
}

function nearest(candidates, rect) {
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  let best = candidates[0];
  let bestD = Infinity;
  for (const el of candidates) {
    const r = layoutRect(el);
    const d = Math.hypot(r.left + r.width / 2 - cx, r.top + r.height / 2 - cy);
    if (d < bestD) {
      bestD = d;
      best = el;
    }
  }
  return best;
}

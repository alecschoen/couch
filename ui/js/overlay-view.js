// The overlays drawn over every app (#18): the quick menu, toasts and the volume panel, as
// in the design's "Screen · Quick menu and toast". couchd decides what they show; this only
// draws it.
//
// One file, two places. The launcher imports it (ui/js/overlay.js). couchd injects the same
// source into YouTube's page, inside a closed shadow root with tokens.css, components.css
// and overlay.css (couchd/overlay_page.py). So: no imports, nothing but DOM, and nothing
// here may take focus or listen for keys. couchd routes the remote's keys while the quick
// menu is open; the page underneath never sees them.
//
//   const view = new OverlayView(root, { corner, palette: () => "yt" });
//   view.menu({ open: true, title: "YouTube", palette: "yt", focus: "home", items: [...] });
//   view.toast({ text: "Sound now on TV speakers", level: "info", icon: "speaker" });
//   view.volume({ name: "TV speakers", volume: 60, muted: false });

export const TOAST_MS = 2400; // toasts and the volume panel go after this long

const SVG = "http://www.w3.org/2000/svg";
const ICONS = {
  speaker: ["M4 9v6h4l5 4V5L8 9z", "M17 9a4 4 0 0 1 0 6"],
  bluetooth: ["M7 7l10 10-5 4V3l5 4L7 17"],
  check: ["M5 12l5 5 9-10"],
  info: ["M12 11v5M12 8h.01", "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18"],
  update: ["M12 4v11", "M7 10l5 5 5-5", "M5 20h14"],
  muted: ["M4 9v6h4l5 4V5L8 9z", "M17 10l4 4M21 10l-4 4"],
};

// The icon a toast gets when couchd doesn't name one.
export function toastIcon({ text = "", level = "info", icon } = {}) {
  if (icon && ICONS[icon]) return icon;
  if (level !== "info") return "info";
  return /^Sound (now|is) on /.test(text) ? "speaker" : "check";
}

export class OverlayView {
  // root: a full-screen element to draw into. corner: where toasts and the volume panel go
  // (the launcher passes its toast stack); made inside root if not given. palette(): the
  // palette for a menu whose state names none (the launcher's own screen colour).
  constructor(root, { corner = null, palette = () => null } = {}) {
    this.root = root;
    this.doc = root.ownerDocument;
    this.palette = palette;
    this.corner = corner ?? this._el("div", "overlay-corner");
    if (!corner) root.append(this.corner);
    this.sheet = null;
    this._items = null; // the menu's item ids, as drawn
    this._toast = null;
    this._volume = null;
    this._timers = new Map();
  }

  // ---- Quick menu: a bottom sheet in the app's colour. couchd moves the focus.
  menu(state) {
    if (!state || !state.open) {
      this._closeSheet();
      return;
    }
    const ids = (state.items || []).map((it) => it.id).join(" ");
    if (!this.sheet || this.sheet.dataset.closing || this._items !== ids) this._openSheet(state, ids);
    const sheet = this.sheet;
    sheet.dataset.palette = state.palette || this.palette() || "system";
    sheet.querySelector(".qm__title").textContent = state.title || "";
    for (const it of state.items || []) {
      const card = sheet.querySelector(`[data-item="${it.id}"]`);
      card.querySelector(".qm-card__label").textContent = it.label;
      card.querySelector(".qm-card__value").textContent = it.value;
      card.classList.toggle("is-focused", it.id === state.focus);
    }
  }

  _openSheet(state, ids) {
    this._closeSheet({ now: true });
    const sheet = this._el("div", "sheet qm");
    sheet.setAttribute("role", "dialog");
    const head = this._el("div", "qm__head");
    head.append(this._el("span", "qm__title"), this._el("span", "qm__hint", "Back to close"));
    const cards = this._el("div", "qm__cards");
    for (const it of state.items || []) {
      const card = this._el("div", "qm-card");
      card.dataset.item = it.id;
      card.append(this._el("span", "qm-card__label"), this._el("span", "qm-card__value"));
      cards.append(card);
    }
    sheet.append(head, cards);
    this.root.append(sheet);
    this.sheet = sheet;
    this._items = ids;
    sheet.getBoundingClientRect(); // start closed, then slide in
    sheet.classList.add("is-open");
  }

  _closeSheet({ now = false } = {}) {
    const sheet = this.sheet;
    if (!sheet) return;
    this.sheet = null;
    this._items = null;
    if (now) {
      sheet.remove();
      return;
    }
    sheet.dataset.closing = "1";
    sheet.classList.remove("is-open");
    setTimeout(() => sheet.remove(), this._ms("--dur-sheet", 320));
  }

  // ---- Toast: one at a time here, top right. A new one replaces it, and the volume panel.
  toast({ text, level = "info", icon } = {}) {
    if (!text) return;
    this._drop("volume");
    const el = this._el("div", `toast${level === "error" ? " is-error" : ""}`);
    el.setAttribute("role", "status");
    const sound = level === "info" && /^(Sound now on )(.+)$/.exec(text);
    const body = this._el("span", "", sound ? sound[1] : text);
    if (sound) body.append(this._el("b", "toast__em", sound[2]));
    el.append(this._icon(toastIcon({ text, level, icon })), body);
    this._show("toast", el);
  }

  // ---- Volume panel: the output and its volume. Stays while it keeps changing.
  volume({ name = "", volume = 0, muted = false } = {}) {
    this._drop("toast");
    let el = this._volume;
    if (!el || !el.isConnected) {
      el = this._el("div", "volume-panel");
      el.dataset.palette = "audio";
      el.setAttribute("role", "status");
      const top = this._el("div", "volume-panel__top");
      top.append(this._el("span", "volume-panel__name"), this._el("span", "volume-panel__value"));
      const track = this._el("div", "volume-panel__track");
      track.append(this._el("div", "volume-panel__fill"));
      el.append(top, track);
      this._show("volume", el);
    } else {
      this._arm("volume", el);
    }
    const level = Math.max(0, Math.min(100, Math.round(volume)));
    el.classList.toggle("is-muted", !!muted);
    el.querySelector(".volume-panel__name").textContent = name;
    el.querySelector(".volume-panel__value").textContent = muted ? "Muted" : String(level);
    el.querySelector(".volume-panel__fill").style.transform = `scaleX(${muted ? 0 : level / 100})`;
  }

  // A toast elsewhere in the corner (the launcher's own stack) replaces the panel too.
  hideVolume() {
    this._drop("volume");
  }

  // ---- helpers
  _show(kind, el) {
    this._drop(kind);
    this["_" + kind] = el;
    this.corner.prepend(el);
    el.animate?.([{ opacity: 0, transform: "translateY(-18px)" }, { opacity: 1, transform: "none" }], {
      duration: this._ms("--dur-quick", 200),
      easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
    });
    this._arm(kind, el);
  }

  _arm(kind, el) {
    clearTimeout(this._timers.get(kind));
    this._timers.set(kind, setTimeout(() => this._drop(kind, { fade: true }), TOAST_MS));
  }

  _drop(kind, { fade = false } = {}) {
    clearTimeout(this._timers.get(kind));
    const el = this["_" + kind];
    if (!el) return;
    this["_" + kind] = null;
    const out = fade && el.animate?.([{ opacity: 1 }, { opacity: 0 }], { duration: this._ms("--dur-quick", 200), fill: "forwards" });
    if (out) out.finished.then(() => el.remove(), () => el.remove());
    else el.remove();
  }

  _ms(token, fallback) {
    const view = this.doc.defaultView;
    const raw = view ? view.getComputedStyle(this.root).getPropertyValue(token).trim() : "";
    const ms = parseFloat(raw);
    return Number.isFinite(ms) ? ms : fallback;
  }

  _el(tag, cls = "", text = null) {
    const el = this.doc.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  _icon(name) {
    const svg = this.doc.createElementNS(SVG, "svg");
    for (const [k, v] of Object.entries({
      class: "icon",
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "1.8",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    })) svg.setAttribute(k, v);
    for (const d of ICONS[name] || []) {
      const path = this.doc.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }
}

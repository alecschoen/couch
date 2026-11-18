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

// The on-screen keyboard's icons (#77): [viewBox, stroke width, filled, ...paths].
const KB_ICONS = {
  search: ["0 0 24 24", 2.2, false, "M17 10.5a6.5 6.5 0 1 1-13 0a6.5 6.5 0 1 1 13 0", "m15.5 15.5 5 5"],
  email: [
    "0 0 24 24", 2, false,
    "M5.5 5h13A2.5 2.5 0 0 1 21 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5v-9A2.5 2.5 0 0 1 5.5 5z",
    "m3.5 6.5 8.5 6.5 8.5-6.5",
  ],
  password: [
    "0 0 24 24", 2, false,
    "M7 10.5h10a2.5 2.5 0 0 1 2.5 2.5v5a2.5 2.5 0 0 1-2.5 2.5H7A2.5 2.5 0 0 1 4.5 18v-5A2.5 2.5 0 0 1 7 10.5z",
    "M8 10.5V8a4 4 0 0 1 8 0v2.5",
  ],
  eye: ["0 0 24 24", 2, false, "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M15 12a3 3 0 1 1-6 0a3 3 0 1 1 6 0"],
  shift: ["0 0 24 24", 2, false, "M12 3.5 3.5 12H8v7.5h8V12h4.5z"],
  caps: ["0 0 24 24", 2, true, "M12 3.5 3.5 12H8v7.5h8V12h4.5z"],
  backspace: ["0 0 24 24", 2, false, "M9 5h11a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H9l-6.5-7z", "m12 9.5 5 5m0-5-5 5"],
  space: ["0 0 32 20", 2, false, "M3 8v6h26V8"],
  go: ["0 0 24 24", 2.4, false, "M4 12h15m-6-6 6 6-6 6"],
  find: ["0 0 24 24", 2.4, false, "M17 10.5a6.5 6.5 0 1 1-13 0a6.5 6.5 0 1 1 13 0", "m15.5 15.5 5 5"],
};
const KB_ICON_SIZE = { space: [64, 40], backspace: [44, 44], go: [36, 36], find: [36, 36] };
const KB_HINTS = [["red", "Delete"], ["blue", "Shift"], [null, "0–9", " Numbers"], [null, "Back", " to close"]];

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
    this.kb = null; // the on-screen keyboard's parts, while it's up
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

  // ---- On-screen keyboard (#77): a sheet over a scrim, in the app's colour. Only in
  // Chromium apps' pages: couchd says what the field holds, the keys, and which has focus.
  keyboard(state) {
    if (!state || !state.open) {
      this._closeKeyboard();
      return;
    }
    if (!this.kb || this.kb.closing) this._openKeyboard();
    const kb = this.kb;
    kb.sheet.dataset.palette = state.palette || this.palette() || "system";
    kb.sheet.dataset.kind = state.kind;
    kb.kicker.textContent = state.kicker || "";
    kb.title.textContent = state.title || "";
    if (kb.kind !== state.kind) {
      kb.kind = state.kind;
      kb.icon.replaceChildren(...(KB_ICONS[state.kind] ? [this._kbIcon(state.kind, 44)] : []));
    }

    // The field: its text with the caret, or its placeholder.
    const text = state.text || "";
    const caret = Number.isInteger(state.caret) ? state.caret : text.length;
    kb.before.textContent = text.slice(0, caret);
    kb.after.textContent = text.slice(caret);
    kb.placeholder.textContent = text ? "" : state.placeholder || "";
    kb.text.classList.toggle("is-masked", !!state.masked);
    kb.reveal.hidden = !state.reveal;
    kb.revealLabel.textContent = state.reveal || "";
    kb.reveal.classList.toggle("is-focused", state.focus === "reveal");
    kb.text.scrollLeft = Math.max(0, kb.caret.offsetLeft + 60 - kb.text.clientWidth);

    // Suggestions: a row of chips, there for fields that have them (even while empty).
    kb.chips.hidden = !Array.isArray(state.chips);
    const chips = state.chips || [];
    if (kb.chipList !== chips.join("\n")) {
      kb.chipList = chips.join("\n");
      kb.chips.replaceChildren(
        ...chips.map((label) => {
          const chip = this._el("div", "kb-chip");
          chip.append(this._kbIcon("search", 30), this._el("span", "", label));
          return chip;
        }),
      );
    }
    [...kb.chips.children].forEach((chip, i) => chip.classList.toggle("is-focused", state.focus === `chip:${i}`));

    // The keys: drawn again only when they change (another page, Shift).
    const rows = JSON.stringify(state.rows || []);
    if (kb.rows !== rows) {
      kb.rows = rows;
      kb.keyEls = new Map();
      kb.keys.replaceChildren(...(state.rows || []).map((row) => this._kbRow(row, kb.keyEls)));
    }
    for (const [id, el] of kb.keyEls) el.classList.toggle("is-focused", id === state.focus);
  }

  _openKeyboard() {
    this._closeKeyboard({ now: true });
    const scrim = this._el("div", "scrim kb-scrim");
    const sheet = this._el("div", "sheet kb");
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-label", "On-screen keyboard");

    const head = this._el("div", "kb__head");
    const titles = this._el("div", "kb__titles");
    const kicker = this._el("span", "kb__kicker");
    const title = this._el("span", "kb__title");
    titles.append(kicker, title);
    const hints = this._el("div", "kb__hints");
    for (const [dot, strong, rest] of KB_HINTS) {
      const hint = this._el("span", dot ? "kb__hint" : "");
      if (dot) hint.append(this._el("span", `kb__dot is-${dot}`), strong);
      else hint.append(this._el("b", "", strong), rest);
      hints.append(hint);
    }
    head.append(titles, hints);

    const field = this._el("div", "kb__field");
    const box = this._el("div", "kb__box");
    const icon = this._el("span", "kb__icon");
    const text = this._el("span", "kb__text");
    const before = this._el("span", "kb__before");
    const caret = this._el("span", "kb__caret");
    const after = this._el("span", "kb__after");
    const placeholder = this._el("span", "kb__placeholder");
    text.append(before, caret, after, placeholder);
    const reveal = this._el("div", "kb-chip kb__reveal");
    const revealLabel = this._el("span");
    reveal.append(this._kbIcon("eye", 34), revealLabel);
    box.append(icon, text, reveal);
    const chips = this._el("div", "kb__chips");
    field.append(box, chips);

    const keys = this._el("div", "kb__keys");
    sheet.append(head, field, keys);
    const at = this.corner.parentNode === this.root ? this.corner : null; // toasts stay on top
    this.root.insertBefore(scrim, at);
    this.root.insertBefore(sheet, at);
    this.kb = { scrim, sheet, kicker, title, icon, text, before, caret, after, placeholder, reveal,
      revealLabel, chips, keys, kind: null, chipList: null, rows: null, keyEls: new Map(), closing: false };
    sheet.getBoundingClientRect(); // start closed, then slide in
    scrim.classList.add("is-open");
    sheet.classList.add("is-open");
  }

  _closeKeyboard({ now = false } = {}) {
    const kb = this.kb;
    if (!kb) return;
    this.kb = null;
    if (now) {
      kb.scrim.remove();
      kb.sheet.remove();
      return;
    }
    kb.closing = true;
    kb.scrim.classList.remove("is-open");
    kb.sheet.classList.remove("is-open");
    setTimeout(() => {
      kb.scrim.remove();
      kb.sheet.remove();
    }, this._ms("--dur-sheet", 320));
  }

  _kbRow(row, keyEls) {
    const el = this._el("div", "kb__row");
    for (const key of row) {
      if (key.gap) {
        const gap = this._el("span", "kb-gap");
        gap.style.flex = `${key.w} 1 0`;
        el.append(gap);
        continue;
      }
      const cls = ["kb-key", key.fn ? "is-fn" : "", key.go ? "is-go" : ""].filter(Boolean).join(" ");
      const k = this._el("div", cls);
      k.style.flex = `${key.w} 1 0`;
      const icon = key.icon && this._kbIcon(key.icon, ...(KB_ICON_SIZE[key.icon] || [40, 40]));
      if (icon && key.icon !== "go") k.append(icon);
      if (key.label) k.append(this._el("span", "", key.label));
      if (icon && key.icon === "go") k.append(icon); // Next →
      if (key.dot) k.append(this._el("span", `kb-key__dot is-${key.dot}`));
      keyEls.set(key.id, k);
      el.append(k);
    }
    return el;
  }

  _kbIcon(name, width, height = width) {
    const [viewBox, stroke, filled, ...paths] = KB_ICONS[name];
    const svg = this.doc.createElementNS(SVG, "svg");
    for (const [k, v] of Object.entries({
      viewBox,
      width,
      height,
      fill: filled ? "currentColor" : "none",
      stroke: "currentColor",
      "stroke-width": stroke,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    })) svg.setAttribute(k, v);
    for (const d of paths) {
      const path = this.doc.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
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

// Builders for the base components in css/components.css, plus the behaviour some of them
// need (card widths, toasts, sheets, the clock). Screens build their DOM with these.
//
//   import { h, card, cardRow, row, pill, setting } from "./components.js";
//   h("div", { class: "stack gap-3" }, h("span", { class: "t-title" }, "Bluetooth"), list);

import { animate, duration, easing, reduced, settle } from "./motion.js";
import { TOAST_MS, toastIcon } from "./overlay-view.js";

// ---- h(): a tiny element builder. Attributes: class, dataset, style (object), on<Event>
// handlers, and anything else as an attribute. Children: nodes, strings, arrays, null.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs ?? {})) {
    if (value == null || value === false) continue;
    if (name === "class") el.className = value;
    else if (name === "dataset") {
      for (const [k, v] of Object.entries(value)) if (v != null) el.dataset[k] = v;
    }
    else if (name === "style") Object.assign(el.style, value);
    else if (name.startsWith("on") && typeof value === "function") el.addEventListener(name.slice(2).toLowerCase(), value);
    else el.setAttribute(name, value === true ? "" : value);
  }
  el.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return el;
}

// ---- Icons: the stroke icons from the design, drawn in currentColor.
const ICONS = {
  tv: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/>',
  headphones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/>',
  bluetooth: '<path d="M7 7l10 10-5 4V3l5 4L7 17"/>',
  speaker: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M17 9a4 4 0 0 1 0 6"/>',
  power: '<path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  chevron: '<path d="M9 5l7 7-7 7"/>',
  update: '<path d="M12 4v11"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/>',
  muted: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M17 10l4 4M21 10l-4 4"/>',
  usb: '<path d="M12 3v14"/><path d="M8 7l4-4 4 4"/><circle cx="12" cy="19" r="2"/><path d="M12 13l-4-2V8M12 11l4-2V7"/>',
};

export function icon(name, cls = "icon") {
  const span = document.createElement("span");
  span.innerHTML =
    `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] ?? ""}</svg>`;
  return span.firstChild;
}

// ---- Card: the B3 app card. In a cardRow(), the current card is wide.
export function card({ label, key, onSelect, palette, ...attrs }) {
  return h(
    "button",
    { class: "card", dataset: { focusKey: key, palette: palette ?? undefined }, onClick: onSelect, ...attrs },
    h("span", { class: "card__bg", "aria-hidden": "true" }),
    h("span", { class: "card__label" }, label),
  );
}

// A row of cards. The focused card becomes the wide one; the change animates with FLIP
// (transforms only): each card slides from where it was, and its background scales from its
// old width. onCurrent(card, index) runs when the wide card changes.
export function cardRow(cards, { onCurrent } = {}) {
  const row = h("div", { class: "cards", "data-focus-group": "" }, cards);
  const current = cards.find((c) => c.hasAttribute("data-focus-default")) ?? cards[0];
  current?.classList.add("is-wide");
  row.addEventListener("couch:focus", (e) => {
    const target = e.target.closest(".card");
    if (!target || target.parentElement !== row || target.classList.contains("is-wide")) return;
    widen(row, target);
    onCurrent?.(target, [...row.children].indexOf(target));
  });
  return row;
}

function widen(row, target) {
  const cards = [...row.children];
  const before = cards.map((c) => ({
    x: c.getBoundingClientRect().left,
    w: c.querySelector(".card__bg").getBoundingClientRect().width,
  }));
  cards.forEach(settle);
  cards.forEach((c) => c.classList.toggle("is-wide", c === target));
  if (reduced()) return;

  const timing = { duration: duration("card"), easing: easing("out") };
  cards.forEach((c, i) => {
    const after = c.getBoundingClientRect();
    const dx = before[i].x - after.left;
    const sx = before[i].w / after.width;
    if (Math.abs(dx) > 0.5) c.animate([{ transform: `translateX(${dx}px)` }, { transform: "none" }], timing);
    if (Math.abs(sx - 1) > 0.002) {
      c.querySelector(".card__bg").animate([{ transform: `scaleX(${sx})` }, { transform: "none" }], timing);
    }
  });
}

// ---- Pill, segmented control, row, tab, setting, switch

export function pill({ label, key, onSelect, selected = false, primary = false, iconName, ...attrs }) {
  const cls = ["pill", selected && "is-selected", primary && "is-primary"].filter(Boolean).join(" ");
  return h("button", { class: cls, dataset: { focusKey: key }, onClick: onSelect, ...attrs }, iconName && icon(iconName, "icon-sm"), label);
}

// choices: [[value, label], …]
export function segmented({ choices, value, onChange, key = "seg" }) {
  const group = h("div", { class: "segmented", "data-focus-group": "" });
  const draw = (current) => {
    replace(
      group,
      choices.map(([v, label]) =>
        h(
          "button",
          {
            class: `segment${v === current ? " is-selected" : ""}`,
            dataset: { focusKey: `${key}:${v}`, value: v },
            "data-focus-default": v === current || null,
            onClick: () => {
              draw(v);
              onChange?.(v);
            },
          },
          label,
        ),
      ),
    );
  };
  draw(value);
  return group;
}

export function row({ title, sub, end, key, onSelect, selected = false, disabled = false, ...attrs }) {
  return h(
    "button",
    { class: `row${selected ? " is-selected" : ""}`, dataset: { focusKey: key }, onClick: onSelect, disabled, ...attrs },
    h("span", { class: "row__text" }, h("span", { class: "row__title" }, title), sub && h("span", { class: "row__sub" }, sub)),
    end != null && h("span", { class: "row__end" }, end),
  );
}

export function tab({ label, key, selected = false, onSelect, ...attrs }) {
  return h("button", { class: `tab${selected ? " is-selected" : ""}`, dataset: { focusKey: key }, onClick: onSelect, ...attrs }, label);
}

// A settings line. Focusable when it has onSelect; `end` is the value, a switch or a control.
export function setting({ label, hint, value, end, key, onSelect, ...attrs }) {
  const tag = onSelect ? "button" : "div";
  return h(
    tag,
    { class: "setting", dataset: { focusKey: key }, onClick: onSelect, ...attrs },
    h("span", { class: "setting__text" }, h("span", { class: "setting__label" }, label), hint && h("span", { class: "setting__hint" }, hint)),
    value != null && h("span", { class: "setting__value" }, value),
    end,
  );
}

export function toggle({ label, hint, on, onChange, key }) {
  const sw = h("span", { class: `switch${on ? " is-on" : ""}`, "aria-hidden": "true" });
  const line = setting({
    label,
    hint,
    end: sw,
    key,
    role: "switch",
    "aria-checked": String(on),
    onSelect: () => {
      on = !on;
      sw.classList.toggle("is-on", on);
      line.setAttribute("aria-checked", String(on));
      onChange?.(on);
    },
  });
  return line;
}

export function progress(value = 0, { busy = false } = {}) {
  const bar = h("div", { class: "progress__bar" });
  const el = h("div", { class: `progress${busy ? " is-busy" : ""}`, role: "progressbar" }, bar);
  el.set = (v) => bar.style.setProperty("--value", String(Math.max(0, Math.min(1, v))));
  el.set(value);
  return el;
}

// ---- Re-rendering a live list without losing focus or replaying transitions.
// Replaces container's children. Focus comes back by data-focus-key (focus.js ensure(), right
// after the change), and fills don't animate in on the fresh elements.
export function replace(container, ...children) {
  container.classList.add("no-transition");
  container.replaceChildren(...children.flat(Infinity).filter(Boolean));
  requestAnimationFrame(() => requestAnimationFrame(() => container.classList.remove("no-transition")));
}

// ---- Toasts: top right, gone after TOAST_MS. couchd's own toasts come here too, with an
// icon name (or none, and one is picked from the text).
export class Toasts {
  constructor(container, { max = 3, ms = TOAST_MS } = {}) {
    this.container = container;
    this.max = max;
    this.ms = ms;
  }

  show(text, level = "info", iconName = null) {
    // couchd's "Sound now on X" gets the speaker and a bold X, as in the design.
    const sound = level === "info" && /^(Sound now on )(.+)$/.exec(text);
    iconName = toastIcon({ text, level, icon: iconName });
    const body = sound ? h("span", {}, sound[1], h("b", { class: "toast__em" }, sound[2])) : h("span", {}, text);
    const el = h("div", { class: `toast${level === "error" ? " is-error" : ""}`, role: "status" }, icon(iconName), body);
    this.container.prepend(el);
    while (this.container.children.length > this.max) this.container.lastElementChild.remove();
    animate(el, [{ opacity: 0, transform: "translateY(-18px)" }, { opacity: 1, transform: "none" }], { dur: "quick" });
    setTimeout(() => {
      const out = animate(el, [{ opacity: 1 }, { opacity: 0 }], { dur: "quick", fill: "forwards" });
      if (out) out.finished.then(() => el.remove(), () => el.remove());
      else el.remove();
    }, this.ms);
    return el;
  }
}

// ---- Sheet: slides up from the bottom and keeps focus inside until it closes. Back closes it.
//   const close = openSheet(app, { palette: "yt", content: (close) => [title, cardRow(…)] });
export function openSheet(app, { palette, content, onClose }) {
  const scrim = h("div", { class: "scrim" });
  const sheet = h("div", { class: "sheet", dataset: { palette }, role: "dialog" });
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    sheet.inert = true;
    sheet.classList.remove("is-open");
    scrim.classList.remove("is-open");
    app.focus.popScope(sheet);
    const ms = duration("sheet");
    setTimeout(() => {
      scrim.remove();
      sheet.remove();
    }, ms);
    onClose?.();
  };
  sheet.append(...[content(close)].flat(Infinity).filter(Boolean));
  app.layer.append(scrim, sheet);
  // Give the slide-in its start position (closed), then open.
  sheet.getBoundingClientRect();
  sheet.classList.add("is-open");
  scrim.classList.add("is-open");
  app.focus.pushScope(sheet, {
    onKey: (key) => {
      if (key === "back") {
        close();
        return true;
      }
      return false;
    },
  });
  return close;
}

// ---- Clock: calls fn({ time, date }) now and at every minute boundary. Returns stop().
export function clock(fn) {
  let timer;
  const tick = () => {
    const now = new Date();
    fn({
      time: `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`,
      date: now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }),
    });
    timer = setTimeout(tick, 60_000 - (Date.now() % 60_000) + 50);
  };
  tick();
  return () => clearTimeout(timer);
}

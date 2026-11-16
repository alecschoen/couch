// Type: the type scale on the Plex palette (the one with dark ink), and a long list that
// scrolls as focus moves down it.

import { h, setting } from "../components.js";

const SPECIMENS = [
  ["t-hero", "Hero · 210", "YouTube"],
  ["t-clock", "Clock · 168", "21:45"],
  ["t-title", "Title · 96", "Bluetooth"],
  ["t-heading", "Heading · 45", "Where should the sound go?"],
  ["t-row", "Row · 35", "Bluetooth speaker"],
  ["t-card", "Card · 32", "Settings"],
  ["t-lg", "Large · 33", "Make sure the computer running Plex is on."],
  ["t-md", "Medium · 31", "Subscriptions, search and your watch history"],
  ["t-base", "Base · 29", "Headphone jack"],
  ["t-sm", "Small · 27", "Home › Settings"],
  ["t-xs", "Extra small · 24", "Connected · sound plays here"],
];

export function typeScreen(_params, app) {
  const list = h(
    "div",
    { class: "settings", "data-scroll": "", "data-focus-group": "", style: { overflowY: "auto", flex: "1", minHeight: "0", padding: "0 30px", margin: "0 -30px" } },
    SPECIMENS.map(([cls, label, sample], i) =>
      setting({
        key: cls,
        label: h("span", { class: cls, style: { whiteSpace: "nowrap" } }, sample),
        value: label,
        "data-focus-default": i === 0 || null,
        onSelect: () => app.toasts.show(`${label.split(" · ")[0]}: .${cls}`),
      }),
    ),
  );

  const el = h(
    "section",
    {},
    h("span", { class: "breadcrumb" }, "Home › Type"),
    h("div", { class: "spread", style: { marginTop: "27px", marginBottom: "36px" } }, h("span", { class: "t-title" }, "Type"), h("span", { class: "t-sm muted" }, "Space Grotesk for display, Inter for text")),
    list,
  );

  return { el, palette: "plex" };
}

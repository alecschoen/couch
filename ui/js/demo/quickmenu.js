// The quick menu sheet from the design (Screen · Quick menu and toast), wired to couchd.

import { card, cardRow, h, openSheet } from "../components.js";
import { outputName } from "./foundation.js";

let close = null;

export function openQuickMenu(app) {
  if (close) return close();
  const palette = app.theme.palette ?? "settings";

  const nextOutput = async () => {
    const audio = app.couch.state.audio ?? {};
    const usable = (audio.outputs ?? []).filter((o) => o.available);
    if (!usable.length) return;
    const next = usable[(usable.findIndex((o) => o.id === audio.current) + 1) % usable.length];
    try {
      await app.couch.command("audio.set_output", { output: next.id });
      const label = document.querySelector('.sheet [data-focus-key="sound"] .card__label');
      if (label) label.textContent = next.name; // couchd sends the toast
    } catch (e) {
      app.toasts.show(e.message, "error");
    }
  };

  const items = () => [
    { key: "home", label: "Go to", value: "Home", select: () => (close(), app.router.home()) },
    { key: "sound", label: "Sound", value: outputName(app.couch.state), select: nextOutput },
    { key: "console", label: "Open", value: "Console", select: () => (close(), app.router.go("console")) },
  ];

  close = openSheet(app, {
    palette,
    onClose: () => (close = null),
    content: () => [
      h("div", { class: "spread" }, h("span", { class: "t-heading" }, "Quick menu"), h("span", { class: "t-sm muted" }, "Back to close")),
      cardRow(
        items().map((it) => {
          const c = card({ label: it.value, key: it.key, onSelect: it.select });
          c.append(h("span", { class: "t-xs muted", style: { position: "absolute", top: "27px", left: "30px" } }, it.label));
          return c;
        }),
      ),
    ],
  });
  return close;
}

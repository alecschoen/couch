// Components: every base component on one screen, in the Settings layout from the design.
// The Display tab holds the real Evening colors and Reduce motion settings.

import { h, icon, pill, progress, replace, row, segmented, setting, tab, toggle } from "../components.js";
import { CHOICES, settings } from "../settings.js";

export function kitScreen(_params, app) {
  const panel = h("div", { class: "stack", "data-focus-group": "", "data-focus-enter-start": "", "data-scroll": "", style: { overflowY: "auto", minHeight: "0", padding: "0 30px", margin: "0 -30px" } });
  let currentTab = "display";

  const panels = {
    display: () =>
      h(
        "div",
        { class: "settings" },
        setting({ label: "Resolution", value: "1920 × 1080 · 60 Hz" }),
        setting({
          label: "Evening colors",
          hint: "Deeper colours, easier on the eyes in a dark room",
          end: segmented({ key: "evening", choices: CHOICES.evening, value: settings.get("evening"), onChange: (v) => settings.set("evening", v).catch((e) => app.toasts.show(e.message, "error")) }),
        }),
        setting({
          label: "Reduce motion",
          hint: "Auto follows the system setting",
          end: segmented({ key: "motion", choices: CHOICES.motion, value: settings.get("motion"), onChange: (v) => settings.set("motion", v).catch((e) => app.toasts.show(e.message, "error")) }),
        }),
        setting({ key: "dim", label: "Dim to clock after", value: "5 minutes ›", onSelect: () => app.toasts.show("Not a real setting yet") }),
      ),
    lists: () =>
      h(
        "div",
        { class: "list" },
        h("span", { class: "list__label" }, "Rows"),
        row({ key: "r1", title: "Bluetooth speaker", sub: "Connected · sound plays here", end: icon("check"), selected: true }),
        row({ key: "r2", title: "Headphones", sub: "Not connected · OK to connect" }),
        row({ key: "r3", title: "Portable speaker", sub: "Pairing", end: h("span", { class: "spinner" }) }),
        row({ key: "r4", title: "Old soundbar", sub: "Out of range", disabled: true }),
        h("span", { class: "list__label", style: { marginTop: "18px" } }, "Switches"),
        h(
          "div",
          { class: "settings" },
          toggle({ key: "t1", label: "Reconnect Bluetooth at start", on: true }),
          toggle({ key: "t2", label: "TV remote controls volume", hint: "For the headphone jack and Bluetooth. Works on most TVs.", on: false }),
          toggle({ key: "t3", label: "Turn the TV off too", on: true }),
        ),
      ),
    feedback: () => {
      const bar = progress(0.6);
      let level = 0.6;
      const step = (d) => {
        level = Math.max(0, Math.min(1, level + d));
        bar.set(level);
        value.textContent = String(Math.round(level * 100));
      };
      const value = h("span", { class: "t-card num" }, "60");
      return h(
        "div",
        { class: "stack gap-5" },
        h("span", { class: "list__label" }, "Toasts"),
        h(
          "div",
          { class: "pills", "data-focus-group": "" },
          pill({ key: "toast", label: "Show a toast", primary: true, onSelect: () => app.toasts.show("Sound now plays on Bluetooth speaker") }),
          pill({ key: "error", label: "Show an error", onSelect: () => app.toasts.show("Old Speaker didn't accept the pairing", "error") }),
        ),
        h("span", { class: "list__label" }, "Progress"),
        h(
          "div",
          { class: "inline gap-4" },
          icon("speaker"),
          h("div", { class: "grow" }, bar),
          value,
          h(
            "div",
            { class: "pills", "data-focus-group": "" },
            pill({ key: "down", label: "", iconName: "minus", "aria-label": "Less", onSelect: () => step(-0.05) }),
            pill({ key: "up", label: "", iconName: "plus", "aria-label": "More", onSelect: () => step(0.05) }),
          ),
        ),
        h("div", { class: "inline gap-4" }, h("div", { style: { width: "270px" } }, progress(0, { busy: true })), h("span", { class: "t-md muted" }, "Opening")),
        h("span", { class: "list__label" }, "Remote keys"),
        h("div", { class: "inline gap-5" }, h("span", { class: "key key--blue" }, "Blue · quick menu"), h("span", { class: "key key--red" }, "Red · next output")),
      );
    },
  };

  const TABS = [
    ["display", "Display"],
    ["lists", "Lists"],
    ["feedback", "Feedback"],
  ];
  const tabs = h(
    "div",
    { class: "tabs", "data-focus-group": "" },
    TABS.map(([id, label]) =>
      tab({
        key: `tab:${id}`,
        label,
        selected: id === currentTab,
        "data-focus-default": id === currentTab || null,
        onSelect: () => app.focus.enter(panel),
      }),
    ),
  );
  // Moving onto a tab shows it; OK or Right goes into it.
  tabs.addEventListener("couch:focus", (e) => show(e.target.dataset.focusKey.slice(4)));

  function show(id) {
    if (id === currentTab && panel.childElementCount) return;
    currentTab = id;
    for (const t of tabs.children) t.classList.toggle("is-selected", t.dataset.focusKey === `tab:${id}`);
    replace(panel, panels[id]());
    panel.scrollTop = 0;
  }
  show(currentTab);

  const el = h(
    "section",
    {},
    h("span", { class: "breadcrumb" }, "Home › Components"),
    h("span", { class: "t-title", style: { marginTop: "27px" } }, "Components"),
    h(
      "div",
      { style: { display: "grid", gridTemplateColumns: "450px minmax(0, 1fr)", columnGap: "84px", marginTop: "54px", flex: "1", minHeight: "0" } },
      tabs,
      panel,
    ),
  );

  return { el, palette: "bt" };
}

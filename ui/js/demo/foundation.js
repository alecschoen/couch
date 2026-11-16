// The foundation demo's first screen, in the B3 layout: clock, a headline for the current
// card, and a row of cards that flood the screen with their colour. Stands in until Home (#15).

import { card, cardRow, clock, h } from "../components.js";

const CARDS = [
  { id: "console", name: "Console", palette: "settings", blurb: "The TV, sound and Bluetooth, live from couchd" },
  { id: "bluetooth", name: "Bluetooth", palette: "bt", blurb: "Pair and connect speakers and headphones" },
  { id: "kit", name: "Components", palette: "bt", blurb: "Lists, switches, settings, toasts and progress" },
  { id: "type", name: "Type", palette: "plex", blurb: "Space Grotesk and Inter, at TV sizes" },
  { id: "menu", name: "Quick menu", palette: "yt", blurb: "A sheet over the screen. Blue opens it anywhere" },
];

export function outputName(state) {
  const audio = state.audio ?? {};
  return audio.outputs?.find((o) => o.id === audio.current)?.name ?? "…";
}

export function foundationScreen(_params, app) {
  let current = CARDS[0];

  const time = h("span", { class: "t-clock" });
  const date = h("span", { class: "t-md muted" });
  const sound = h("span", { class: "t-sm muted", style: { paddingTop: "18px" } });
  const headline = h("span", { class: "t-title" });
  const blurb = h("span", { class: "t-md muted" });

  const open = (it) => (it.id === "menu" ? app.couch.command("overlay.menu", { open: true }).catch(() => {}) : app.router.go(it.id));
  const cards = CARDS.map((it, i) =>
    card({ label: it.name, key: it.id, onSelect: () => open(it), "data-focus-default": i === 0 || null }),
  );
  const row = cardRow(cards, {
    onCurrent: (_el, i) => {
      current = CARDS[i];
      show();
    },
  });

  const show = () => {
    headline.textContent = current.name;
    blurb.textContent = current.blurb;
    app.theme.set(current.palette);
  };
  const stopClock = clock(({ time: t, date: d }) => {
    time.textContent = t;
    date.textContent = d;
  });
  const onState = (e) => (sound.textContent = `Sound: ${outputName(e.detail.state)}`);
  app.couch.addEventListener("state", onState);
  sound.textContent = `Sound: ${outputName(app.couch.state)}`;
  show();

  const el = h(
    "section",
    {},
    h("div", { class: "spread", style: { alignItems: "flex-start" } }, h("div", { class: "stack gap-1" }, time, date), sound),
    h("div", { class: "stack gap-3 push-down", style: { marginBottom: "54px" } }, headline, blurb),
    row,
  );

  return {
    el,
    palette: () => current.palette,
    destroy() {
      stopClock();
      app.couch.removeEventListener("state", onState);
    },
  };
}

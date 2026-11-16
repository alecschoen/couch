// Home: the B3 colour flood. Clock and date top left, the current sound output top right, a
// headline for the current card, and the card row. The screen floods with the colour of the
// card in focus. On Audio, the blurb gives way to a pill per output; picking one moves the
// sound there (couchd toasts "Sound now on X").
//
// Keys, on top of the usual arrows and OK:
//   OK or Up on Audio    into the output pills, on the current output
//   Down or Back         from the pills, back to the Audio card

import { card, cardRow, clock, h, pill, replace } from "../components.js";

export const CARDS = [
  { id: "youtube", name: "YouTube", palette: "yt", app: "youtube", blurb: "Subscriptions, search and your watch history" },
  { id: "plex", name: "Plex", palette: "plex", app: "plex", blurb: "Your movies and shows" },
  { id: "audio", name: "Audio", palette: "audio", headline: "Play sound through" },
  { id: "bluetooth", name: "Bluetooth", palette: "bt", route: "bluetooth", blurb: bluetoothBlurb },
  { id: "settings", name: "Settings", palette: "settings", route: "settings", blurb: "Display, updates and power" },
];

// ---- What the screen says, from couchd's live state.

export function soundLabel(state) {
  const audio = state.audio;
  if (!audio?.outputs) return "…"; // not heard from couchd yet
  return audio.outputs.find((o) => o.id === audio.current)?.name ?? "Off";
}

export function bluetoothBlurb(state) {
  const connected = (state.bluetooth?.devices ?? []).filter((d) => d.connected).map((d) => d.name);
  const what = connected.length ? `${connected.join(" and ")} connected` : "Nothing connected";
  return `${what} · OK to pair something new`;
}

export function homeScreen(_params, app) {
  const { couch, focus, router, theme, toasts } = app;
  let current = CARDS[0];

  const time = h("span", { class: "t-clock" });
  const date = h("span", { class: "t-md muted" });
  const sound = h("span", { class: "home__sound t-sm muted" });
  const headline = h("span", { class: "t-title" });
  const blurb = h("span", { class: "t-md muted" });
  const outputs = h("div", { class: "pills home__outputs", "data-focus-group": "", hidden: true });

  const fail = (e) => toasts.show(e.message, "error");
  const choose = (it) => {
    if (it.app) couch.command("apps.launch", { app: it.app }).catch(fail);
    else if (it.route) router.go(it.route);
    else if (it.id === "audio") focusOutputs();
  };

  const cards = CARDS.map((it, i) =>
    card({ label: it.name, key: it.id, onSelect: () => choose(it), "data-focus-default": i === 0 || null }),
  );
  const audioCard = cards[CARDS.findIndex((it) => it.id === "audio")];
  const row = cardRow(cards, {
    onCurrent: (_el, i) => {
      current = CARDS[i];
      show();
      theme.set(current.palette);
    },
  });

  // ---- Output pills. Redrawn only when the outputs or the current one change, so a volume
  // patch doesn't replace the pill under focus.
  let drawn = null;
  const drawOutputs = (state) => {
    const audio = state.audio ?? {};
    const list = audio.outputs ?? [];
    const signature = JSON.stringify([list, audio.current]);
    if (signature === drawn) return;
    drawn = signature;
    replace(
      outputs,
      list.map((o) =>
        pill({
          key: `output:${o.id}`,
          label: o.name,
          selected: o.id === audio.current,
          disabled: !o.available,
          onSelect: () => o.id !== couch.state.audio?.current && couch.command("audio.set_output", { output: o.id }).catch(fail),
        }),
      ),
    );
  };

  function focusOutputs() {
    const pills = focus.candidates(outputs);
    focus.focus(pills.find((p) => p.classList.contains("is-selected")) ?? pills[0]);
  }

  // ---- The headline area for the current card.
  function show() {
    const state = couch.state;
    const onAudio = current.id === "audio";
    headline.textContent = current.headline ?? current.name;
    const text = typeof current.blurb === "function" ? current.blurb(state) : current.blurb;
    const hasOutputs = (state.audio?.outputs ?? []).length > 0;
    blurb.textContent = onAudio && !hasOutputs ? "No sound outputs right now" : (text ?? "");
    blurb.hidden = onAudio && hasOutputs;
    outputs.hidden = !blurb.hidden;
  }

  const render = () => {
    sound.textContent = `Sound: ${soundLabel(couch.state)}`;
    drawOutputs(couch.state);
    show();
  };
  const onState = () => render();
  couch.addEventListener("state", onState);
  render();

  const stopClock = clock(({ time: t, date: d }) => {
    time.textContent = t;
    date.textContent = d;
  });

  const el = h(
    "section",
    { class: "home" },
    h("div", { class: "home__top" }, h("div", { class: "home__when" }, time, date), sound),
    h("div", { class: "home__info" }, headline, blurb, outputs),
    row,
  );

  return {
    el,
    palette: () => current.palette,
    onKey(key) {
      const at = focus.current;
      if (at && outputs.contains(at)) {
        if (key === "back" || key === "down") return focus.focus(audioCard);
        return false;
      }
      if (at === audioCard && key === "up" && !outputs.hidden) {
        focusOutputs();
        return true;
      }
      return false;
    },
    destroy() {
      stopClock();
      couch.removeEventListener("state", onState);
    },
  };
}

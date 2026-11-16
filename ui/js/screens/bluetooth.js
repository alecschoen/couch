// The Bluetooth screen (artboard "Screen · Bluetooth"): your devices on the left, devices
// nearby on the right, a tip about pairing mode at the bottom.
//
//   app.router.register("bluetooth", bluetoothScreen);
//   app.router.go("bluetooth");
//
// OK on a device does the obvious next thing:
//   nearby                     pair it (pairing connects an audio device too)
//   paired, not connected      connect it
//   connected, sound elsewhere move the sound to it
//   connected, sound here      Options: Disconnect, Forget
// Holding OK on a paired device (a `select` key with `hold: true`) opens Options too. couchd's "Sound now on X" toast says when
// sound has moved; this screen only toasts what couchd doesn't (a non-audio device, Forget).
//
// The radio scans only while this screen is on top: couchd stops a scan after two minutes,
// so it's asked again every minute, and told to stop when the screen is covered or closed.

import { h, icon, openSheet, pill, replace, row } from "../components.js";

const RESCAN_MS = 60_000;
// After this long with nothing new nearby, say so rather than leave the column empty.
const NOTHING_NEARBY_MS = 6_000;
// How long to keep a device "Connecting" while waiting for its sound to arrive. couchd waits
// 15 s for the output before it gives up.
const SOUND_WAIT_MS = 16_000;

// Is this audio output that device? Same rule as couchd's _output_matches: compare the hex
// digits only, so `bluetooth:AA:BB:…` and `bluez_output.AA_BB_….1` both match.
export function outputMatches(outputId, address) {
  const hex = (s) => String(s).toUpperCase().replace(/[^0-9A-F]/g, "");
  const wanted = hex(address);
  return wanted.length > 0 && hex(outputId).includes(wanted);
}

export function bluetoothScreen(_params, app) {
  const { couch } = app;
  // address → "pair" | "connect" | "sound" (connected, waiting for its sound to move there)
  const pending = new Map();
  const soundTimers = new Map();
  let visible = false;
  let rescan = null;
  let scanSince = 0;
  let nothingTimer = null;
  let options = null; // close() of the open Options sheet

  // ---- Reading state

  const bt = () => couch.state.bluetooth ?? {};
  const audio = () => couch.state.audio ?? {};
  const available = () => couch.state.services?.bluetooth === "running";
  const devices = () => bt().devices ?? [];
  const find = (address) => devices().find((d) => d.address === address);

  const outputFor = (d) =>
    (audio().outputs ?? []).find((o) => o.kind === "bluetooth" && o.available !== false && outputMatches(o.id, d.address));
  const soundHere = (d) => {
    const current = audio().current;
    return current != null && outputMatches(current, d.address);
  };
  const soundName = () => {
    const a = audio();
    return a.outputs?.find((o) => o.id === a.current)?.name ?? "None";
  };

  // ---- Drawing

  const sound = h("span", { class: "breadcrumb" });
  const yours = h("div", { class: "bt-rows", "data-scroll": "" });
  const nearby = h("div", { class: "bt-rows", "data-scroll": "" });
  const looking = h("span", { class: "bt-looking", hidden: true }, h("span", { class: "pulse", "aria-hidden": "true" }), h("span", {}, "Looking for devices"));

  const el = h(
    "section",
    { class: "bt-screen" },
    h("div", { class: "spread" }, h("span", { class: "breadcrumb" }, "Home › Bluetooth"), sound),
    h("h1", { class: "t-title bt-title" }, "Bluetooth"),
    h(
      "div",
      { class: "bt-columns" },
      h("div", { class: "list bt-column", "data-focus-group": "" }, h("span", { class: "list__label" }, "Your devices"), yours),
      h("div", { class: "list bt-column", "data-focus-group": "" }, h("span", { class: "list__label" }, "Nearby", looking), nearby),
    ),
    h(
      "p",
      { class: "bt-tip" },
      icon("info"),
      h("span", {}, "Put your speaker or headphones in pairing mode. That's usually holding its Bluetooth button until a light blinks."),
    ),
  );

  function subFor(d) {
    const p = pending.get(d.address);
    if (p === "pair") return "Pairing";
    if (p === "connect" || p === "sound") return "Connecting";
    if (!d.paired) return "OK to pair";
    if (!d.connected) return "Not connected · OK to connect";
    if (!d.audio) return "Connected";
    return soundHere(d) ? "Connected · sound plays here" : "Connected · OK to play sound here";
  }

  function endFor(d) {
    if (pending.has(d.address)) return h("span", { class: "spinner", "aria-label": "Working" });
    if (d.connected && (soundHere(d) || !d.audio)) {
      const check = icon("check", "icon bt-check");
      check.setAttribute("stroke-width", "2.4");
      return check;
    }
    return null;
  }

  function deviceRow(d, isDefault) {
    return row({
      key: `bt:${d.address}`,
      title: d.name,
      sub: subFor(d),
      end: endFor(d),
      onSelect: () => select(d.address),
      "data-focus-default": isDefault || null,
      "aria-busy": pending.has(d.address) ? "true" : null,
    });
  }

  function note(text) {
    return h("span", { class: "bt-note" }, text);
  }

  function render() {
    sound.textContent = `Sound: ${soundName()}`;
    looking.hidden = !(visible && bt().scanning);

    const all = devices();
    const paired = all.filter((d) => d.paired);
    // Audio devices first: they're what this screen is for. Then by name, as couchd sends them.
    const fresh = all.filter((d) => !d.paired).sort((a, b) => Number(b.audio) - Number(a.audio));
    const preferred = paired.find((d) => d.connected && soundHere(d)) ?? paired.find((d) => d.connected) ?? paired[0];

    if (!available()) {
      replace(yours, note("Bluetooth isn't available right now"));
      replace(nearby);
      return;
    }
    replace(yours, paired.length ? paired.map((d) => deviceRow(d, d === preferred)) : note("Nothing paired yet"));
    const waited = scanSince && Date.now() - scanSince >= NOTHING_NEARBY_MS;
    replace(nearby, fresh.length ? fresh.map((d) => deviceRow(d, !preferred && d === fresh[0])) : waited ? note("Nothing new nearby") : null);
  }

  // ---- Doing

  async function run(name, args) {
    try {
      await couch.command(name, args);
      return true;
    } catch (e) {
      app.toasts.show(e.message, "error");
      return false;
    }
  }

  function waitForSound(address) {
    pending.set(address, "sound");
    clearTimeout(soundTimers.get(address));
    soundTimers.set(
      address,
      setTimeout(() => {
        soundTimers.delete(address);
        if (pending.get(address) === "sound") pending.delete(address);
        render();
      }, SOUND_WAIT_MS),
    );
  }

  // Connected, waiting for sound: done once the sound is there (couchd toasts it).
  function settleSound() {
    for (const [address, what] of pending) {
      if (what !== "sound") continue;
      const d = find(address);
      if (!d || !d.connected || soundHere(d)) {
        pending.delete(address);
        clearTimeout(soundTimers.get(address));
        soundTimers.delete(address);
      }
    }
  }

  async function connectLike(d, what) {
    pending.set(d.address, what);
    render();
    const ok = await run(what === "pair" ? "bluetooth.pair" : "bluetooth.connect", { address: d.address });
    pending.delete(d.address);
    const now = find(d.address);
    if (ok && now?.connected && now.audio && !soundHere(now)) waitForSound(d.address);
    else if (ok && now && !now.audio) app.toasts.show(what === "pair" ? `Paired with ${now.name}` : `${now.name} connected`);
    render();
  }

  async function select(address) {
    const d = find(address);
    if (!d || pending.has(address)) return;
    if (!d.paired) return connectLike(d, "pair");
    if (!d.connected) return connectLike(d, "connect");
    if (d.audio && !soundHere(d)) {
      const output = outputFor(d);
      if (output) {
        await run("audio.set_output", { output: output.id });
        return;
      }
      // Connected but its sound isn't up yet: connecting again brings the audio profile up.
      return connectLike(d, "connect");
    }
    openOptions(d);
  }

  function openOptions(d) {
    if (options || !d?.paired) return;
    const act = (fn) => async () => {
      options?.();
      await fn();
      render();
    };
    options = openSheet(app, {
      palette: "bt",
      onClose: () => (options = null),
      content: () => [
        h("div", { class: "spread" }, h("span", { class: "t-heading" }, d.name), h("span", { class: "t-sm muted" }, "Back to close")),
        h(
          "div",
          { class: "pills", "data-focus-group": "" },
          d.connected &&
            pill({
              key: "disconnect",
              label: "Disconnect",
              "data-focus-default": "",
              onSelect: act(() => run("bluetooth.disconnect", { address: d.address })),
            }),
          pill({
            key: "forget",
            label: "Forget",
            "data-focus-default": d.connected ? null : "",
            onSelect: act(async () => {
              // A connected device gets couchd's "X disconnected" (or "Sound now on …") toast.
              const wasConnected = find(d.address)?.connected;
              if ((await run("bluetooth.forget", { address: d.address })) && !wasConnected) app.toasts.show(`Forgot ${d.name}`);
            }),
          }),
        ),
      ],
    });
  }

  function focusedDevice() {
    const key = app.focus.current?.dataset.focusKey ?? "";
    return key.startsWith("bt:") && el.contains(app.focus.current) ? find(key.slice(3)) : null;
  }

  // ---- Scanning, only while on top

  function startScan() {
    if (!visible || !couch.connected) return;
    if (!scanSince) scanSince = Date.now();
    couch.command("bluetooth.scan", { on: true }).catch(() => {});
    clearInterval(rescan);
    rescan = setInterval(() => couch.command("bluetooth.scan", { on: true }).catch(() => {}), RESCAN_MS);
    clearTimeout(nothingTimer);
    nothingTimer = setTimeout(render, NOTHING_NEARBY_MS);
  }

  function stopScan() {
    clearInterval(rescan);
    clearTimeout(nothingTimer);
    rescan = null;
    scanSince = 0;
    couch.command("bluetooth.scan", { on: false }).catch(() => {});
  }

  const onState = (e) => {
    const patch = e.detail.patch;
    if (patch && !patch.bluetooth && !patch.audio && !patch.services) return;
    settleSound();
    render();
  };
  const onConnection = (e) => {
    if (e.detail.connected) startScan();
  };
  couch.addEventListener("state", onState);
  couch.addEventListener("connection", onConnection);
  render();

  return {
    el,
    palette: "bt",
    onKey(key, detail = {}) {
      // A held OK (sent once, instead of the press) on a paired device opens Options. Held
      // anywhere else, it's an ordinary OK.
      if (key !== "select" || !detail.hold) return false;
      const d = focusedDevice();
      if (!d?.paired) return false;
      openOptions(d);
      return true;
    },
    onShow() {
      visible = true;
      render();
      startScan();
    },
    onHide() {
      visible = false;
      options?.();
      stopScan();
      render();
    },
    destroy() {
      couch.removeEventListener("state", onState);
      couch.removeEventListener("connection", onConnection);
      for (const t of soundTimers.values()) clearTimeout(t);
      if (visible) stopScan();
    },
  };
}

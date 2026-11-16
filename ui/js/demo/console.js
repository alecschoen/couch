// The dev console, rebuilt on the foundation: live state from couchd, and every fake device
// usable with the remote. Stays until the real screens (#15–#19) replace it.

import { h, icon, pill, replace, row } from "../components.js";

const OUTPUT_ICONS = { tv: "tv", jack: "headphones", bluetooth: "bluetooth", usb: "usb" };

export function consoleScreen(_params, app) {
  const { couch } = app;
  const status = h("span", { class: "t-sm muted" });
  const lastKey = h("span", { class: "t-sm muted num" });
  const tv = h("div", { class: "list" });
  const audio = h("div", { class: "list" });
  const bluetooth = h("div", { class: "list", "data-scroll": "", style: { overflowY: "auto", margin: "-6px", padding: "6px" } });

  async function run(name, args) {
    try {
      await couch.command(name, args);
    } catch (e) {
      app.toasts.show(e.message, "error");
    }
  }

  function render() {
    const { tv: t = {}, audio: a = {}, bluetooth: b = {}, system = {} } = couch.state;
    status.textContent = `${couch.connected ? "Connected" : "Reconnecting…"}${system.mode ? ` · ${system.mode} mode` : ""}`;

    replace(
      tv,
      row({
        key: "power",
        title: "Power",
        sub: t.power ?? "unknown",
        end: icon("power"),
        onSelect: () => run(t.power === "on" ? "tv.standby" : "tv.power_on"),
      }),
    );

    const outputs = (a.outputs ?? []).map((o) =>
      row({
        key: `out:${o.id}`,
        title: o.name,
        sub: o.id === a.current ? "Sound plays here" : o.available ? "OK to switch" : "Not available",
        end: o.id === a.current ? icon("check") : icon(OUTPUT_ICONS[o.kind] ?? "speaker"),
        selected: o.id === a.current,
        disabled: !o.available,
        onSelect: () => run("audio.set_output", { output: o.id }),
      }),
    );
    const volume = h(
      "div",
      { class: "pills", "data-focus-group": "" },
      pill({ key: "vol-", label: "", iconName: "minus", "aria-label": "Volume down", onSelect: () => run("audio.set_volume", { volume: Math.max(0, a.volume - 5) }) }),
      pill({
        key: "mute",
        label: a.muted ? "Muted" : `Volume ${a.volume ?? "–"}`,
        selected: a.muted,
        onSelect: () => run("audio.set_muted", { muted: !a.muted }),
      }),
      pill({ key: "vol+", label: "", iconName: "plus", "aria-label": "Volume up", onSelect: () => run("audio.set_volume", { volume: Math.min(100, a.volume + 5) }) }),
    );
    replace(audio, outputs, volume);

    const scan = row({
      key: "scan",
      title: b.scanning ? "Stop searching" : "Search for devices",
      sub: b.scanning ? "Looking for devices" : "Put the device in pairing mode first",
      end: b.scanning ? h("span", { class: "spinner" }) : icon("plus"),
      onSelect: () => run("bluetooth.scan", { on: !b.scanning }),
    });
    const devices = (b.devices ?? []).flatMap((d) => {
      const address = { address: d.address };
      if (!d.paired) {
        return [row({ key: `bt:${d.address}`, title: d.name, sub: d.audio ? "OK to pair" : "OK to pair · not audio", onSelect: () => run("bluetooth.pair", address) })];
      }
      return [
        row({
          key: `bt:${d.address}`,
          title: d.name,
          sub: d.connected ? "Connected · OK to disconnect" : "Not connected · OK to connect",
          end: d.connected ? icon("check") : null,
          selected: d.connected,
          onSelect: () => run(d.connected ? "bluetooth.disconnect" : "bluetooth.connect", address),
        }),
        row({ key: `forget:${d.address}`, title: `Forget ${d.name}`, onSelect: () => run("bluetooth.forget", address) }),
      ];
    });
    replace(bluetooth, scan, devices);
  }

  const column = (label, list) => h("div", { class: "stack gap-3", style: { minHeight: "0" } }, h("span", { class: "list__label" }, label), list);

  const el = h(
    "section",
    {},
    h("div", { class: "spread" }, h("span", { class: "breadcrumb" }, "Home › Console"), h("span", { class: "inline gap-5" }, status, lastKey)),
    h("span", { class: "t-title", style: { marginTop: "27px" } }, "Console"),
    h(
      "div",
      { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1.25fr", columnGap: "54px", marginTop: "54px", flex: "1", minHeight: "0" } },
      h("div", { "data-focus-group": "", class: "stack" }, column("TV", tv)),
      h("div", { "data-focus-group": "", class: "stack" }, column("Sound", audio)),
      h("div", { "data-focus-group": "", class: "stack", style: { minHeight: "0" } }, column("Bluetooth", bluetooth)),
    ),
    h("span", { class: "t-xs faint", style: { marginTop: "36px" } }, "Keyboard: arrows · Enter = OK · Backspace = Back · H = Home · Space = play/pause · F1–F4 = colour keys"),
  );

  const onState = () => render();
  const onConnection = () => render();
  const onKey = (e) => (lastKey.textContent = `Last key: ${e.detail.key}`);
  couch.addEventListener("state", onState);
  couch.addEventListener("connection", onConnection);
  app.addEventListener("key", onKey);
  render();

  return {
    el,
    palette: "settings",
    destroy() {
      couch.removeEventListener("state", onState);
      couch.removeEventListener("connection", onConnection);
      app.removeEventListener("key", onKey);
    },
  };
}

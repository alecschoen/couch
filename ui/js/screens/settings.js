// Settings (#17): categories on the left, the chosen one's settings on the right. Artboard
// "Screen · Settings".
//
//   app.router.register("settings", settingsScreen);
//   app.router.go("settings");               // or go("settings", { category: "sound" })
//
// Moving onto a category shows it; OK or Right goes into it, Back comes out again. Values
// come from couchd (state section `settings`, written with settings.set) and re-render when
// they change anywhere. Rows that pick from a range (Dim to clock after, Volume limit) change
// with Left and Right, or OK to step forward.

import { h, icon, openSheet, pill, replace, row, segmented, setting, tab, toggle } from "../components.js";
import { CHOICES, settings } from "../settings.js";

const CATEGORIES = [
  ["display", "Display"],
  ["sound", "Sound"],
  ["network", "Network"],
  ["remote", "Remote"],
  ["updates", "Updates"],
  ["about", "About"],
  ["power", "Power"],
];

// Live state sections each category shows; a patch to one of them redraws it.
const USES = {
  display: ["settings"],
  sound: ["settings", "audio"],
  network: ["settings", "wifi"],
  remote: ["remote"],
  updates: ["settings", "system"],
  about: ["tv", "services", "system"],
  power: ["settings"],
};

// Until couchd publishes its own (#8: live state `remote.keymap`).
const KEYMAP = [
  { button: "Hold Back", action: "Go home" },
  { button: "Blue button", action: "Quick menu" },
  { button: "Red button", action: "Switch audio output" },
];

const KEY_NAMES = {
  up: "Up", down: "Down", left: "Left", right: "Right", select: "OK", back: "Back", home: "Home",
  play: "Play", pause: "Pause", play_pause: "Play/Pause", stop: "Stop", rewind: "Rewind",
  fast_forward: "Fast forward", red: "Red", green: "Green", yellow: "Yellow", blue: "Blue",
};

const TEST_IDLE_MS = 10_000; // the remote test stops by itself after this long without a key
const CONFIRM_MS = 4000; // Restart and Turn off want a second OK within this long

const VOLUME_LIMIT = { min: 10, max: 100, step: 5 };

export function settingsScreen(params, app) {
  let current = CATEGORIES.some(([id]) => id === params.category) ? params.category : "display";
  let info = null; // system.info, fetched when About opens
  let testing = null; // { timer, lastBack } while the remote test listens
  let lastKey = null; // { name, code } shown in the test box
  let confirming = null; // { action, timer } after a first OK on Restart or Turn off

  const state = () => app.couch.state;

  // ---- Categories

  const tabs = h(
    "div",
    { class: "tabs", "data-focus-group": "" },
    CATEGORIES.map(([id, label]) =>
      tab({
        key: `tab:${id}`,
        label,
        selected: id === current,
        "data-focus-default": id === current || null,
        onSelect: () => app.focus.enter(panel),
      }),
    ),
  );
  tabs.addEventListener("couch:focus", (e) => show(e.target.dataset.focusKey.slice(4)));

  const panel = h("div", {
    class: "settings-panel",
    "data-focus-group": "",
    "data-focus-enter-start": "",
    "data-scroll": "",
  });

  function show(id, { force = false } = {}) {
    if (id === current && panel.childElementCount && !force) return;
    if (id !== current) {
      stopTesting();
      cancelConfirm();
      panel.scrollTop = 0;
    }
    current = id;
    for (const t of tabs.children) t.classList.toggle("is-selected", t.dataset.focusKey === `tab:${id}`);
    if (id === "about" || id === "network") fetchInfo();
    draw();
  }

  function draw() {
    replace(panel, PANELS[current]());
  }

  // ---- Helpers for the panels

  const change = (key, value) =>
    settings.set(key, value).catch((e) => {
      app.toasts.show(e.message, "error");
      draw();
    });

  function stepper({ key, label, value, values, format, wrap = true }) {
    const i = values.indexOf(value);
    const step = (d) => {
      const next = values[Math.max(0, Math.min(values.length - 1, (i < 0 ? 0 : i) + d))];
      if (next !== value) change(key, next);
    };
    const line = setting({
      key,
      label,
      end: h(
        "span",
        { class: "stepper" },
        h("span", { class: `stepper__arrow${i <= 0 ? " is-end" : ""}`, "aria-hidden": "true" }, "‹"),
        h("span", { class: "setting__value num" }, format(value)),
        h("span", { class: `stepper__arrow${i >= values.length - 1 ? " is-end" : ""}`, "aria-hidden": "true" }, "›"),
      ),
      // OK steps forward too (and wraps, where that's harmless).
      onSelect: () => (wrap ? change(key, values[(i + 1) % values.length]) : step(1)),
    });
    line.classList.add("is-stepper");
    line.step = step;
    return line;
  }

  const readout = (label, value, hint) => setting({ label, value, hint });

  // Under Evening colors: when "After sunset" is today (couchd/evening.py).
  function sunHint() {
    const sun = settings.sun ?? {};
    if (typeof sun.dark !== "boolean") return null;
    if (!sun.place) return "After sunset is 18:00 to 07:00 until the timezone is set";
    if (!sun.sunset) return sun.dark ? "The sun doesn't rise today" : "The sun doesn't set today";
    return `Sunset today ${sun.sunset}, sunrise ${sun.sunrise}`;
  }

  // ---- Panels

  const PANELS = {
    display: () =>
      h(
        "div",
        { class: "settings" },
        readout("Resolution", resolution()),
        setting({
          label: "Evening colors",
          hint: sunHint(),
          end: segmented({
            key: "evening",
            choices: CHOICES["display.evening"],
            value: settings.get("display.evening"),
            onChange: (v) => change("display.evening", v),
          }),
        }),
        stepper({
          key: "display.dim_after",
          label: "Dim to clock after",
          value: settings.get("display.dim_after"),
          values: CHOICES["display.dim_after"].map(([v]) => v),
          format: (v) => CHOICES["display.dim_after"].find(([c]) => c === v)?.[1] ?? `${v} s`,
        }),
        toggle({
          key: "tv.off_when_idle",
          label: "Turn the TV off when idle",
          hint: "After 2 hours on Couch with no buttons pressed. Never while something plays.",
          on: settings.get("tv.off_when_idle"),
          onChange: (on) => change("tv.off_when_idle", on),
        }),
      ),

    sound: () => {
      const audio = state().audio ?? {};
      const output = audio.outputs?.find((o) => o.id === audio.current);
      const values = [];
      for (let v = VOLUME_LIMIT.min; v <= VOLUME_LIMIT.max; v += VOLUME_LIMIT.step) values.push(v);
      return h(
        "div",
        { class: "settings" },
        setting({
          key: "output",
          label: "Output",
          value: `${output?.name ?? "None"} ›`,
          onSelect: openOutputs,
        }),
        toggle({
          key: "bluetooth.auto_reconnect",
          label: "Reconnect Bluetooth at start",
          on: settings.get("bluetooth.auto_reconnect"),
          onChange: (on) => change("bluetooth.auto_reconnect", on),
        }),
        toggle({
          key: "audio.tv_remote_volume",
          label: "TV remote controls volume",
          hint: "For the headphone jack and Bluetooth. Works on most TVs.",
          on: settings.get("audio.tv_remote_volume"),
          onChange: (on) => change("audio.tv_remote_volume", on),
        }),
        stepper({
          key: "audio.volume_limit",
          label: "Volume limit",
          value: settings.get("audio.volume_limit"),
          values,
          wrap: false, // from No limit straight to 10 would turn the sound right down
          format: (v) => (v >= VOLUME_LIMIT.max ? "No limit" : String(v)),
        }),
      );
    },

    network: () => {
      const wifi = state().wifi ?? {};
      const rows = [readout("Connection", network())];
      if (wifi.available === false && !settings.get("network.wifi")) {
        rows.push(readout("Wi-Fi", "Not available"));
      } else {
        rows.push(
          toggle({
            key: "network.wifi",
            label: "Wi-Fi",
            hint: wifiHint(wifi),
            on: settings.get("network.wifi"),
            onChange: (on) => change("network.wifi", on),
          }),
        );
      }
      return h("div", { class: "settings" }, rows);
    },

    remote: () => {
      const keymap = Array.isArray(state().remote?.keymap) ? state().remote.keymap : KEYMAP;
      return h(
        "div",
        { class: "settings" },
        keymap.map((k) => readout(k.button, k.action)),
        h(
          "div",
          { class: "remote-test-line" },
          h("span", { class: "setting__label" }, "Test your remote"),
          h(
            "button",
            {
              class: `remote-test${testing ? " is-listening" : ""}`,
              dataset: { focusKey: "remote-test" },
              onClick: () => testing || startTesting(),
            },
            testBoxContent(),
          ),
        ),
      );
    },

    updates: () =>
      h(
        "div",
        { class: "settings" },
        readout("Launcher", `Couch ${state().system?.version ?? ""}`.trim()),
        readout("System", "Not checked yet"),
        toggle({
          key: "updates.overnight",
          label: "Install updates overnight",
          hint: "Only while nothing is playing",
          on: settings.get("updates.overnight"),
          onChange: (on) => change("updates.overnight", on),
        }),
        h(
          "div",
          { class: "pills settings-actions" },
          pill({
            key: "check",
            label: "Check now",
            primary: true,
            onSelect: () => app.toasts.show("Updates aren't set up yet. They come in a later version."),
          }),
        ),
      ),

    about: () =>
      h(
        "div",
        { class: "settings" },
        readout("Name on the TV", info?.name ?? "…"),
        readout("Device", info?.device ?? "…"),
        readout("Network", network()),
        readout("TV connection", tvConnection()),
      ),

    power: () =>
      h(
        "div",
        { class: "stack gap-3 power" },
        powerButton("restart", "Restart", "system.restart", true),
        powerButton("power_off", "Turn off", "system.power_off", false),
        h(
          "div",
          { class: "settings" },
          toggle({
            key: "tv.standby_on_poweroff",
            label: "Turn the TV off too",
            hint: "When Couch turns off. Not on a restart.",
            on: settings.get("tv.standby_on_poweroff"),
            onChange: (on) => change("tv.standby_on_poweroff", on),
          }),
        ),
      ),
  };

  // ---- Display

  let hz = null;
  measureRefresh().then((r) => {
    hz = r;
    if (current === "display") draw();
  });

  function resolution() {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(screen.width * dpr);
    const hgt = Math.round(screen.height * dpr);
    return hz ? `${w} × ${hgt} · ${hz} Hz` : `${w} × ${hgt}`;
  }

  // ---- Sound: pick an output

  function openOutputs() {
    const audio = state().audio ?? {};
    const outputs = audio.outputs ?? [];
    openSheet(app, {
      palette: "settings",
      content: (close) => [
        h("div", { class: "spread" }, h("span", { class: "t-heading" }, "Sound output"), h("span", { class: "t-sm muted" }, "Back to close")),
        h(
          "div",
          { class: "list", "data-focus-group": "" },
          outputs.length
            ? outputs.map((o) =>
                row({
                  key: `out:${o.id}`,
                  title: o.name,
                  sub: o.available ? (o.id === audio.current ? "Sound plays here" : null) : "Not available",
                  selected: o.id === audio.current,
                  disabled: !o.available,
                  end: o.id === audio.current ? icon("check") : null,
                  "data-focus-default": o.id === audio.current || null,
                  onSelect: async () => {
                    close();
                    if (o.id === audio.current) return;
                    try {
                      await app.couch.command("audio.set_output", { output: o.id });
                    } catch (e) {
                      app.toasts.show(e.message, "error");
                    }
                  },
                }),
              )
            : h("span", { class: "t-md muted" }, "No sound outputs right now"),
        ),
      ],
    });
  }

  // ---- Remote: the test box

  function testBoxContent() {
    if (!testing) return h("span", {}, "Press OK, then any button to see what the TV sends");
    if (!lastKey) return h("span", {}, "Listening… press any button. Back twice to stop");
    return [
      h("span", { class: "remote-test__key" }, lastKey.name),
      lastKey.code != null && h("span", { class: "remote-test__code num" }, String(lastKey.code)),
      h("span", { class: "remote-test__hint" }, lastKey.name.startsWith("Back") ? "Back again to stop" : "Back twice to stop"),
    ];
  }

  function startTesting() {
    testing = { timer: null, lastBack: 0 };
    lastKey = null;
    bumpTestTimer();
    draw();
  }

  function stopTesting() {
    if (!testing) return;
    clearTimeout(testing.timer);
    testing = null;
    lastKey = null;
    if (current === "remote") draw();
  }

  function bumpTestTimer() {
    clearTimeout(testing.timer);
    testing.timer = setTimeout(stopTesting, TEST_IDLE_MS);
  }

  function testKey(key, data) {
    const now = Date.now();
    if (key === "back" && now - testing.lastBack < 1500) {
      stopTesting();
      return;
    }
    testing.lastBack = key === "back" ? now : 0;
    const how = data?.hold ? " (held)" : data?.repeat ? " (repeat)" : "";
    lastKey = { name: (KEY_NAMES[key] ?? key) + how, code: data?.code ?? null };
    bumpTestTimer();
    draw();
  }

  // ---- About

  async function fetchInfo() {
    try {
      info = await app.couch.command("system.info");
    } catch {
      info = info ?? { name: "Couch", device: "Unknown", network: undefined };
    }
    if (current === "about" || current === "network") draw();
  }

  function network() {
    if (!info) return "…";
    const n = info.network;
    if (n === undefined) return "Unknown";
    if (n === null) return "Not connected";
    const kind = n.kind === "wifi" ? "Wi-Fi" : "Ethernet";
    return [kind, n.name, n.address].filter(Boolean).join(" · ");
  }

  // ---- Network

  // Under the Wi-Fi toggle: what the radio is doing (live state `wifi`, docs/protocol.md).
  function wifiHint(wifi) {
    if (!settings.get("network.wifi")) return "Off. Couch uses the network cable.";
    if (wifi.available === false) return "Waiting for the Wi-Fi chip";
    if (!wifi.enabled) return "Turning on…";
    if (wifi.connection === "connected") {
      return [wifi.ssid ?? "Connected", signalWords(wifi.signal)].filter(Boolean).join(" · ");
    }
    if (wifi.connection === "connecting") return `Connecting to ${wifi.ssid ?? "a network"}…`;
    return "On. Not connected to a network.";
  }

  function signalWords(signal) {
    if (typeof signal !== "number") return null;
    if (signal >= 70) return "Strong signal";
    if (signal >= 40) return "Good signal";
    return "Weak signal";
  }

  function tvConnection() {
    const s = state();
    const service = s.services?.tv;
    const power = s.tv?.power;
    if (service !== "running" || !power || power === "unknown") return "No HDMI-CEC";
    return power === "on" ? "HDMI-CEC · TV on" : "HDMI-CEC · TV in standby";
  }

  // ---- Power

  function powerButton(action, label, command, primary) {
    const pending = confirming?.action === action;
    const text = pending ? `Press OK again to ${label.toLowerCase()}` : label;
    return h(
      "button",
      {
        class: `power-action${primary ? " is-primary" : ""}${pending ? " is-confirming" : ""}`,
        dataset: { focusKey: `power:${action}` },
        onClick: () => {
          if (!pending) {
            cancelConfirm();
            confirming = { action, timer: setTimeout(cancelConfirm, CONFIRM_MS) };
            draw();
            return;
          }
          cancelConfirm();
          app.couch.command(command).catch((e) => app.toasts.show(e.message, "error"));
        },
      },
      text,
    );
  }

  function cancelConfirm() {
    if (!confirming) return;
    clearTimeout(confirming.timer);
    confirming = null;
    if (current === "power") draw();
  }

  // ---- Keys and live updates

  const inPanel = () => panel.contains(app.focus.current);

  function onKey(key, data) {
    if (testing) {
      if (key === "home") {
        stopTesting();
        return false;
      }
      testKey(key, data);
      return true;
    }
    const focused = app.focus.current;
    if ((key === "left" || key === "right") && focused?.step && inPanel()) {
      focused.step(key === "right" ? 1 : -1);
      return true;
    }
    if (key === "back" && inPanel()) {
      app.focus.focus(tabs.querySelector(`[data-focus-key="tab:${current}"]`));
      return true;
    }
    return false;
  }


  const onState = (e) => {
    const patch = e.detail.patch;
    const touched = patch ? Object.keys(patch) : Object.keys(USES).flatMap((k) => USES[k]);
    if (touched.some((s) => USES[current].includes(s))) draw();
  };
  const onSetting = () => draw();
  app.couch.addEventListener("state", onState);
  settings.addEventListener("change", onSetting);

  // Leaving the screen while a confirmation is up mustn't leave it armed.
  const onFocusChange = () => {
    if (confirming && !app.focus.current?.dataset.focusKey?.startsWith("power:")) cancelConfirm();
  };
  app.focus.addEventListener("change", onFocusChange);

  show(current, { force: true });

  const el = h(
    "section",
    { class: "settings-screen" },
    h("span", { class: "breadcrumb" }, "Home › Settings"),
    h("span", { class: "t-title settings-screen__title" }, "Settings"),
    h("div", { class: "settings-screen__body" }, tabs, panel),
  );

  return {
    el,
    palette: "settings",
    onKey,
    onShow() {
      draw();
    },
    onHide() {
      stopTesting();
      cancelConfirm();
    },
    destroy() {
      stopTesting();
      cancelConfirm();
      app.couch.removeEventListener("state", onState);
      settings.removeEventListener("change", onSetting);
      app.focus.removeEventListener("change", onFocusChange);
    },
  };
}

// The display's refresh rate, from a second of animation frames, rounded to a usual rate.
function measureRefresh(frames = 60) {
  return new Promise((resolve) => {
    const times = [];
    const tick = (t) => {
      times.push(t);
      if (times.length <= frames) requestAnimationFrame(tick);
      else {
        const gaps = times.slice(1).map((v, i) => v - times[i]).sort((a, b) => a - b);
        const median = gaps[Math.floor(gaps.length / 2)];
        const rate = 1000 / median;
        const usual = [24, 25, 30, 50, 60, 75, 90, 100, 120, 144];
        const near = usual.reduce((a, b) => (Math.abs(b - rate) < Math.abs(a - rate) ? b : a));
        resolve(Math.abs(near - rate) < 3 ? near : Math.round(rate));
      }
    };
    requestAnimationFrame(tick);
  });
}

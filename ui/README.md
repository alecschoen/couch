# Launcher UI

The launcher's screens: plain HTML, CSS and JS modules with no build step, served by couchd. Built at 1920×1080 in the evening B3 style. See the design canvas linked from #14.

Nothing loads from the network: the fonts are bundled and every file is local (`tests/test_ui.py` checks this).

## Layout

```
index.html            the page: registers screens and starts the app
css/
  fonts.css           Space Grotesk (display) and Inter (text), bundled woff2, SIL OFL
  tokens.css          palettes, alphas, type sizes, spacing, radii, sizes, durations
  base.css            page basics, the colour flood, screens, type classes, layout helpers
  bluetooth.css       the Bluetooth screen's layout
  home.css            Home's layout
  components.css      cards, pills, segmented controls, rows, tabs, setting lines,
                      switches, progress, toasts, sheet, key hints
  settings.css        the Settings screen's own pieces (steppers, remote test, power)
fonts/                woff2 files and their licences
js/
  couch.js            couchd client: state, commands, toasts, key events; reconnects
  keys.js             keyboard → remote keys, in dev mode
  app.js              createApp(): wires couch, theme, focus, router and toasts together
  router.js           screens and the back stack (go, back, home)
  focus.js            spatial focus engine (arrows move between focusables)
  theme.js            palette flood transitions, evening colours
  motion.js           animate() that respects Reduce motion; durations come from tokens.css
  settings.js         user settings, kept by couchd (docs/protocol.md), and their hooks
  components.js       h(), icons and the component builders
  screens/            the launcher's screens, one module each: home.js (the first screen),
                      bluetooth.js, settings.js
  demo/               the foundation demo: home cards, Components, Type, Console, quick menu
```

## Palettes

Each app has a vivid and a deep tone (`tokens.css`). Set `data-palette="yt|plex|audio|bt|settings|system|idle"` on an element and it gets `--palette-bg`, `--palette-ink` and `--ground`; the router floods the whole screen with the top screen's palette. Evening colors (on by default; *After sunset* = 18:00–07:00) swap every palette to its deep tone.

Outlines and hairlines are `currentColor` at an alpha, so they follow the ink, including on a focused (inverted) element.

## Focus

Focus is a solid ink fill: the engine puts `.is-focused` on the focused element and the CSS inverts it. It doesn't rely on `:focus`, which a window without OS focus never gets. Focusables are enabled `<button>`s and anything with `tabindex="-1"` or `data-focusable`.

Markup hooks (details at the top of `js/focus.js`):

- `data-focus-group`: coming back into the group returns to where its focus was.
- `data-focus-enter-start`: the first time into a group, start at its default, not the nearest.
- `data-focus-default`: where focus starts in a screen or group.
- `data-focus-key="…"`: a stable id, so focus survives a re-render.
- `data-scroll`: a scroll container that follows focus.

Listen for `couch:focus` (bubbles) to react to focus arriving. A sheet calls `focus.pushScope(el, { onKey })` to keep focus inside it.

## Screens

```js
app.router.register("bluetooth", (params, app) => ({
  el,                 // the screen's root element
  palette: "bt",      // or a function, for screens whose colour follows focus
  onKey(key, detail) {}, // optional: return true to take a key before focus does
  onShow() {}, onHide() {}, destroy() {},
}));
app.router.go("bluetooth");
```

Keys go: the open sheet's scope → the top screen's `onKey` → arrows move focus, OK clicks, Back pops, Home goes to the first screen. `detail` is the key message's data. A held OK comes once as `select` with `hold: true` (#8); a screen that wants a long press (Bluetooth: Options on a device) takes it in `onKey`, and otherwise it clicks like a short press. The app then fires its own `key` event with `taken: true` if a scope or screen took the key, so global handlers (Blue for the quick menu) can stand aside.

## Motion

Only `transform` and `opacity` animate (no blur, no big shadows). Durations are CSS custom properties; Reduce motion sets them to zero, so CSS transitions and `motion.animate()` both stop.

## Dev mode

`uv run couchd --dev` opens the UI with fake hardware. Keyboard: arrows, Enter = OK, Backspace = Back, H = Home, Space = play/pause, F1–F4 = colour keys (F4 = blue opens the quick menu).

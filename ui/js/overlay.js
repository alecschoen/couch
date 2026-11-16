// The overlays in the launcher (#18): couchd's quick menu, volume panel and screen requests.
// They're drawn by overlay-view.js, the same code couchd injects into YouTube's page.
//
//   useOverlays(app);   // once, from createApp()
//
// couchd runs them (couchd/overlay.py): it opens the quick menu on Blue, moves its focus,
// and acts on OK. The launcher only draws `state.overlay.menu` when it's the menu's host,
// and the keys meant for the menu come marked `overlay: true`, so screens never see them.
// In dev mode the launcher also draws the menu couchd hosts in Kodi, since the pretend Kodi
// leaves the launcher on screen.
//
// Toasts stay with app.toasts (components.js); couchd's go there too, for TOAST_MS.

import { OverlayView } from "./overlay-view.js";

export function useOverlays(app) {
  const view = new OverlayView(app.layer, {
    corner: app.toasts.container,
    palette: () => app.theme.palette,
  });

  const draw = (state) => {
    const menu = state.overlay?.menu;
    const dev = state.system?.mode === "dev";
    const here = menu && (menu.host === "launcher" || (dev && menu.host === "kodi"));
    view.menu(here ? menu : null);
  };

  app.couch.addEventListener("state", (e) => {
    const patch = e.detail.patch;
    if (!patch || "overlay" in patch || "system" in patch) draw(e.detail.state);
  });
  app.couch.addEventListener("volume_panel", (e) => view.volume(e.detail));
  app.couch.addEventListener("toast", () => view.hideVolume()); // one thing at a time up there
  // The quick menu's Home and Bluetooth: couchd has put the launcher in front.
  app.couch.addEventListener("open_screen", (e) => {
    app.router.home();
    const screen = e.detail?.screen;
    if (screen && screen !== "home") app.router.go(screen);
  });
  app.overlay = view;
  return view;
}

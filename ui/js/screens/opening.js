// Opening: the app's name, big, in its colour, while it loads (live state `apps.opening`).
// For YouTube it covers the tab loading behind the launcher; for Plex it covers the handoff
// from Chromium to Kodi, whose own splash is this same screen (ui/assets/opening-plex.png),
// so the switch reads as one screen.
//
// Keys: Back gives up (couchd stops waiting and stays on the launcher). Home does too,
// through couchd. Nothing else does anything.

import { h } from "../components.js";

export const APP_LOOKS = {
  youtube: { name: "YouTube", palette: "yt" },
  plex: { name: "Plex", palette: "plex" },
};

export function appLook(appId, state) {
  const listed = state?.apps?.list?.find((a) => a.id === appId);
  return APP_LOOKS[appId] ?? { name: listed?.name ?? appId, palette: "system" };
}

export function openingScreen({ app: appId }, app) {
  const look = appLook(appId, app.couch.state);
  const el = h(
    "section",
    { class: "opening", "aria-live": "polite" },
    h("span", { class: "t-hero opening__name" }, look.name),
    h(
      "div",
      { class: "opening__status" },
      h("div", { class: "progress is-busy is-thin opening__progress", role: "progressbar", "aria-label": `Opening ${look.name}` }, h("div", { class: "progress__bar" })),
      h("span", { class: "opening__label" }, "Opening"),
    ),
  );

  return {
    el,
    palette: look.palette,
    onKey(key) {
      if (key === "back") app.couch.command("apps.home").catch(() => {});
      return true;
    },
  };
}

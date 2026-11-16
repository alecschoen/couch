// The error screens: an app couldn't open, or stopped (live state `apps.error`, whose `kind`
// picks the words). Try again launches the app again; Go home (or Back) dismisses it. couchd
// clears the error itself after a couple of minutes, on Home, or on any launch.

import { h, pill } from "../components.js";
import { appLook } from "./opening.js";

const WIFI = "The Wi-Fi isn’t connected. Check that your router is on, then try again.";

// What each kind says, and its colour: grey for the network, else the app's own.
const KINDS = {
  offline: (name, id) => ({
    title: id === "plex" ? `${name} needs the network` : `${name} needs the internet`,
    body: WIFI,
    palette: "settings",
  }),
  server: () => ({
    title: "Can’t reach your Plex server",
    body: "Make sure the computer running Plex is on and connected to the same network.",
  }),
  crashed: (name) => ({
    title: `${name} stopped unexpectedly`,
    body: "It closed on its own. Try again to open it, or go home.",
  }),
  failed: (name) => ({
    title: `${name} didn’t start`,
    body: "Something went wrong while opening it. Give it a moment, then try again.",
  }),
  repeated: (name) => ({
    title: `${name} keeps stopping`,
    body: "It has closed a few times in a row. Give it a minute, then try again.",
  }),
};

export function errorCopy(error, state) {
  const look = appLook(error.app, state);
  const words = KINDS[error.kind]?.(look.name, error.app) ?? { title: error.message, body: "" };
  return { app: look.name, palette: look.palette, ...words };
}

export function errorScreen({ error }, app) {
  const { couch } = app;
  const copy = errorCopy(error, couch.state);
  const goHome = () => couch.command("apps.clear_error").catch(() => {});
  const tryAgain = () => couch.command("apps.launch", { app: error.app }).catch(() => {}); // the screen shows why

  const el = h(
    "section",
    { class: "error", role: "alert" },
    h("span", { class: "error__app" }, copy.app),
    h("h1", { class: "error__title" }, copy.title),
    copy.body ? h("p", { class: "error__body" }, copy.body) : null,
    h(
      "div",
      { class: "error__actions pills", "data-focus-group": "" },
      pill({ key: "retry", label: "Try again", onSelect: tryAgain, "data-focus-default": "" }),
      pill({ key: "home", label: "Go home", onSelect: goHome }),
    ),
  );

  return {
    el,
    palette: copy.palette,
    onKey(key) {
      if (key === "back") {
        goHome();
        return true;
      }
      return key === "home"; // couchd goes home and clears the error; nothing for focus to do
    },
  };
}

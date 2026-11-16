// The keyboard as a TV remote. Keys go to couchd as `remote.press` and come back as `key`
// messages, the same path the real remote takes over CEC. Screens only ever listen for `key`.
// `keyboard: true` tells couchd the key was typed into the launcher, so it stays here even
// if couchd thinks another app is in front (couchd/remote.py).
//
// Key names must be in couchd/keys.py (a test checks this).

export const KEYBOARD = {
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
  Enter: "select",
  Backspace: "back",
  Escape: "back",
  Home: "home",
  h: "home",
  " ": "play_pause",
  MediaPlayPause: "play_pause",
  MediaStop: "stop",
  MediaRewind: "rewind",
  MediaFastForward: "fast_forward",
  "[": "rewind",
  "]": "fast_forward",
  F1: "red",
  F2: "green",
  F3: "yellow",
  F4: "blue",
  0: "0",
  1: "1",
  2: "2",
  3: "3",
  4: "4",
  5: "5",
  6: "6",
  7: "7",
  8: "8",
  9: "9",
};

export function useKeyboardAsRemote(couch) {
  window.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.altKey || event.metaKey) return; // leave browser shortcuts alone
    if (event.target.closest?.("input, textarea, [contenteditable]")) return; // typing
    const key = KEYBOARD[event.key];
    if (!key) return;
    event.preventDefault();
    couch.command("remote.press", { key, keyboard: true }).catch(() => {});
  });
}

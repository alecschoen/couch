// The colour flood: the whole screen takes the colour of the current app or screen.
//
//   const theme = new Theme(document.getElementById("flood"));
//   theme.set("yt");          // floods to YouTube red (deep in the evening)
//
// Palettes are the data-palette names in css/tokens.css. Each flood adds a full-screen layer
// in the new colour and fades it in (opacity only, so the Pi's compositor does the work), then
// drops the layers under it. Text and focus fills follow through <html data-palette>.

import { animate } from "./motion.js";
import { isEvening, settings } from "./settings.js";

const MAX_LAYERS = 3; // quick presses stack floods; older ones go once there are this many

export class Theme {
  constructor(flood, root = document.documentElement) {
    this.flood = flood;
    this.root = root;
    this.palette = null;
    this._applyEvening();
    settings.addEventListener("change", (e) => e.detail.key === "display.evening" && this._eveningChanged());
    // Recheck "after sunset" every minute.
    setInterval(() => this._eveningChanged(), 60_000);
  }

  set(palette) {
    if (!palette || palette === this.palette) return;
    this.palette = palette;
    this.root.dataset.palette = palette;
    this._floodTo(palette);
  }

  _floodTo(palette) {
    const layer = document.createElement("div");
    layer.className = "flood__layer";
    layer.dataset.palette = palette;
    this.flood.append(layer);

    const under = [...this.flood.children].slice(0, -1);
    under.slice(0, Math.max(0, under.length - (MAX_LAYERS - 1))).forEach((l) => l.remove());

    const fade = animate(layer, [{ opacity: 0 }, { opacity: 1 }], { dur: "flood", ease: "out" });
    const done = () => {
      // Only drop what's under this layer, and only if this is still the top one.
      if (this.flood.lastElementChild !== layer) return;
      for (const l of [...this.flood.children]) if (l !== layer) l.remove();
    };
    if (fade) fade.finished.then(done, () => {});
    else done();
  }

  _applyEvening() {
    const on = isEvening();
    const value = on ? "on" : "off";
    if (this.root.dataset.evening === value) return false;
    this.root.dataset.evening = value;
    return true;
  }

  _eveningChanged() {
    // Freeze the colours on screen, switch, then flood to the new tone of the same palette.
    const frozen = [...this.flood.children];
    for (const l of frozen) l.style.backgroundColor = getComputedStyle(l).backgroundColor;
    if (!this._applyEvening()) {
      for (const l of frozen) l.style.backgroundColor = "";
      return;
    }
    if (this.palette) this._floodTo(this.palette);
  }
}

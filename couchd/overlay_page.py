"""The overlay script couchd puts in Chromium apps' pages (YouTube), built from the launcher's
own files so both draw the same overlays (#18).

`SCRIPT` runs in every document of the app's tab (it's part of the tab's PageSetup, so a
reload or a navigation gets it again), and couchd also runs it into a page that was already
open when couchd started. It only defines `window.__couchOverlay`; nothing is drawn, and
nothing in the page is touched, until couchd calls it:

    __couchOverlay.menu({...} | null)   the quick menu, or none
    __couchOverlay.toast({text, level, icon})
    __couchOverlay.volume({name, volume, muted})
    __couchOverlay.keyboard({...} | null)  the on-screen keyboard (#77), or none
    __couchOverlay.fields.state(id)     a text field's value and suggestions (couchd/fields.js)

Everything is drawn in a closed shadow root on a `<couch-overlay>` element under <html>:
YouTube's styles can't reach in and ours can't leak out. The element is `inert`, ignores the
pointer and has no focusable parts, so YouTube keeps focus and its keys. While the quick
menu is open couchd keeps the remote's keys from YouTube itself (couchd/remote.py).

The script also watches the page's text fields (couchd/fields.js): one getting focus is
reported to couchd as `field {...}`, which opens the on-screen keyboard (couchd/overlay.py).

The fonts come with the script (base64, through the FontFace API under names of our own),
since the page can't load anything from couchd. A fresh document says so through the
DevTools binding, `__couchd("overlay")`, so couchd draws an open menu again after a reload.
"""

import base64
import json
import re
from pathlib import Path

from couchd.config import BUNDLED_UI_DIR

HELLO = "overlay"  # what a fresh document sends couchd through window.__couchd
FIELD = "field "  # then JSON: a text field got or lost focus, or changed (couchd/fields.js)
GLOBAL = "__couchOverlay"

UI = BUNDLED_UI_DIR
VIEW = UI / "js" / "overlay-view.js"
FIELDS = Path(__file__).with_name("fields.js")
STYLES = (UI / "css" / "tokens.css", UI / "css" / "components.css", UI / "css" / "overlay.css")
FONTS = {
    "Couch Display": (UI / "fonts" / "space-grotesk-latin.woff2", "300 700"),
    "Couch Text": (UI / "fonts" / "inter-latin.woff2", "100 900"),
}


def shadow_css(css: str) -> str:
    """The launcher's CSS, for a shadow root: `:root` is the shadow host."""
    css = re.sub(r":root(\[[^\]]*\])", r":host(\1)", css)
    return css.replace(":root", ":host")


def view_source() -> str:
    """overlay-view.js as a plain script: its exports become local names."""
    source = VIEW.read_text(encoding="utf-8")
    if re.search(r"^\s*import\b", source, re.M):
        raise ValueError("overlay-view.js must not import anything")
    return re.sub(r"^export\s+", "", source, flags=re.M)


HOST_CSS = """
:host {
  all: initial;
  position: fixed;
  inset: 0;
  z-index: 2147483647;
  pointer-events: none;
  contain: strict;
  --font-display: "Couch Display", "Space Grotesk", system-ui, sans-serif;
  --font-text: "Couch Text", "Inter", system-ui, sans-serif;
}
.overlay {
  position: absolute;
  left: 0;
  top: 0;
  width: 1920px;
  height: 1080px;
  overflow: hidden;
  transform-origin: 0 0;
  font-family: var(--font-text);
  -webkit-font-smoothing: antialiased;
}
"""

TEMPLATE = """// couchd's overlays (#18): ui/js/overlay-view.js, built by couchd/overlay_page.py.
(() => {
  if (window.top !== window || window.%(global)s) return;
  %(view)s
  %(fields)s
  const CSS = %(css)s;
  const FONTS = %(fonts)s;
  let view = null;
  let fontsLoaded = false;

  const loadFonts = () => {
    if (fontsLoaded || typeof FontFace !== "function") return;
    fontsLoaded = true;
    for (const [family, { data, weight }] of Object.entries(FONTS)) {
      try {
        const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
        const face = new FontFace(family, bytes.buffer, { weight, display: "block" });
        document.fonts.add(face);
        face.load().catch(() => {});
      } catch (e) {}
    }
  };

  const ensure = () => {
    if (view && view.host.isConnected) return view;
    const host = document.createElement("couch-overlay");
    host.setAttribute("inert", "");
    host.setAttribute("aria-hidden", "true");
    host.dataset.evening = "on";
    const shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = CSS;
    const root = document.createElement("div");
    root.className = "overlay";
    shadow.append(style, root);
    (document.documentElement || document).append(host);
    loadFonts();
    const fit = () => (root.style.transform = `scale(${innerWidth / 1920}, ${innerHeight / 1080})`);
    fit();
    addEventListener("resize", fit);
    const corner = document.createElement("div");
    corner.className = "overlay-corner";
    corner.dataset.palette = "audio";
    root.append(corner);
    view = new OverlayView(root, { corner });
    view.host = host;
    return view;
  };

  const api = {
    menu(state) {
      const v = ensure();
      if (state) v.host.dataset.evening = state.evening === false ? "off" : "on";
      v.menu(state);
    },
    toast(toast) {
      ensure().toast(toast);
    },
    volume(volume) {
      ensure().volume(volume);
    },
    keyboard(state) {
      if (!state && !view) return;
      const v = ensure();
      if (state) v.host.dataset.evening = state.evening === false ? "off" : "on";
      v.keyboard(state);
    },
    fields: Fields,
  };
  Object.defineProperty(window, "%(global)s", { value: api, configurable: true });

  // A fresh document: tell couchd, so it can draw an open menu again.
  const hello = () => typeof window.__couchd === "function" && window.__couchd("%(hello)s");
  if (document.readyState !== "loading") hello();
  else addEventListener("DOMContentLoaded", hello, { once: true });
})();
"""


def build() -> str:
    # HOST_CSS last: its font names must win over tokens.css's.
    css = "\n".join(shadow_css(p.read_text(encoding="utf-8")) for p in STYLES) + HOST_CSS
    fonts = {
        family: {"data": base64.b64encode(path.read_bytes()).decode(), "weight": weight}
        for family, (path, weight) in FONTS.items()
    }
    return TEMPLATE % {
        "global": GLOBAL,
        "view": view_source().replace("\n", "\n  ").rstrip(),
        "fields": FIELDS.read_text(encoding="utf-8").replace("\n", "\n  ").rstrip(),
        "css": json.dumps(css),
        "fonts": json.dumps(fonts),
        "hello": HELLO,
    }


SCRIPT = build()


def call(method: str, payload: object) -> str:
    """An expression that hands `payload` to the overlay in the page. True if it was there."""
    return (
        f"(() => {{ const o = window.{GLOBAL}; if (!o) return false; "
        f"o.{method}({json.dumps(payload)}); return true; }})()"
    )


def field(method: str, field_id: str) -> str:
    """An expression that calls couchd/fields.js about one field: `state`, `blur` or
    `select`. null if the page has no overlay."""
    return (
        f"(() => {{ const o = window.{GLOBAL}; "
        f"return o && o.fields ? o.fields.{method}({json.dumps(field_id)}) : null; }})()"
    )

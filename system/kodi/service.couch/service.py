"""Couch's Kodi service: open Plex as Kodi starts, quit Kodi when Plex is done.

Kodi on Couch only exists to run Plex (script.plexmod, "PM4K"). When Kodi exits,
couch-kodi.service ends and couchd brings the launcher back, so quitting Kodi *is* going
back to Couch.

How PM4K behaves, from its source (1.3.19):
- While it runs, it sets Window(home).Property(script.plex.started) (default.py) and,
  once its first window is up, script.plex.is_active.
- Back on its home asks "Are you ready to exit Plex?": Exit / Minimize / Cancel.
  Exit closes PM4K and leaves Kodi on its Home: `started` clears.
  Minimize clears `is_active` and shows Kodi's Home with PM4K still loaded.
- Signing out and switching users happen inside it. Only its optional "Restart PM4K" on
  wake (off by default) restarts it: `started` clears, and the new instance takes 2-3 s
  to set it again. Couch takes that as an exit and goes back to the launcher.

So: quit Kodi once `started` has been gone for a moment, or once Kodi's Home has been in
front with PM4K inactive for a moment. Kodi's Home is the Opening screen (skin.couch) until
PM4K's first window is up; then `couch.closing` turns it black, so leaving Plex goes from Plex
to black to the launcher, without the Opening screen flashing up in between.

The decisions live in Watch, which knows nothing about Kodi, so tests can drive it.

It also draws couchd's overlays over Plex (#18): the quick menu and the volume panel.
couchd sends their state with JSON-RPC `JSONRPC.NotifyAll` (sender "couch", message
"overlay"), and Overlay turns it into Window(home) properties `couch.*`. skin.couch's
Custom_1190_CouchOverlay.xml draws them: a dialog that opens and closes on its own visible
condition, so it never has focus and never takes a key from Plex. couchd keeps the remote
from Plex itself while the menu is open. `couch.overlay` says this is running; couchd checks
it before it asks Kodi to show the menu, and otherwise opens it on the launcher.
"""

import json
import time

START_TIMEOUT = 120.0  # the first start renders PM4K's templates, which is slow on a Pi
EXIT_GRACE = 0.5  # `started` clears in PM4K's finally:, its atexit runs after
HOME_GRACE = 1.0
TICK = 0.2

HOME = 10000
PLEX = "script.plexmod"
SENDER = "couch"
OVERLAY = "Other.overlay"  # NotifyAll's message, as Kodi hands it to onNotification
MENU_ITEMS = ("home", "sound", "volume", "bluetooth")
VOLUME_MS = 2400  # unless couchd says otherwise


class Watch:
    """Says when to quit Kodi. `kodi` answers: plex_started(), plex_active(), home_in_front()."""

    def __init__(self, kodi, clock=time.monotonic):
        self.kodi = kodi
        self.clock = clock
        self.began = clock()
        self.seen_started = False
        self.seen_active = False
        self.gone_since = None
        self.home_since = None

    @property
    def closing(self):
        """Home should be black: Plex has been on screen, or looks to be on its way out."""
        return self.seen_active or self.gone_since is not None

    def tick(self):
        """None while Plex is up; otherwise why Kodi should quit."""
        now = self.clock()
        started = self.kodi.plex_started()
        if not self.seen_started:
            if not started:
                return "Plex didn't start" if now - self.began > START_TIMEOUT else None
            self.seen_started = True

        if started:
            self.gone_since = None
        elif self.gone_since is None:
            self.gone_since = now
        elif now - self.gone_since >= EXIT_GRACE:
            return "Plex exited"

        active = self.kodi.plex_active()
        self.seen_active = self.seen_active or active
        # Before PM4K's first window is up, Home is in front and PM4K isn't active yet. And
        # once `started` is gone, that's an exit (or a restart), handled above.
        if started and self.seen_active and not active and self.kodi.home_in_front():
            if self.home_since is None:
                self.home_since = now
            elif now - self.home_since >= HOME_GRACE:
                return "Plex was minimised"
        else:
            self.home_since = None
        return None


class Overlay:
    """couchd's overlay state -> Window(home) properties. `set(key, value)` sets one."""

    def __init__(self, set, clock=time.monotonic):
        self.set = set
        self.clock = clock
        self.volume_until = None

    def up(self, on):
        self.set("couch.overlay", "1" if on else "")
        if not on:
            self.menu(None)
            self.volume(None)

    def handle(self, data):
        if not isinstance(data, dict):
            return
        if "menu" in data:
            self.menu(data["menu"])
        if "volume" in data:
            self.volume(data["volume"], data.get("ms", VOLUME_MS))

    def menu(self, menu):
        if not isinstance(menu, dict) or not menu.get("open"):
            self.set("couch.menu", "")
            return
        self.set("couch.evening", "" if menu.get("evening") is False else "1")
        self.set("couch.menu.title", str(menu.get("title", "")))
        self.set("couch.menu.focus", str(menu.get("focus", "")))
        for item in menu.get("items", ()):
            if isinstance(item, dict) and item.get("id") in MENU_ITEMS:
                self.set(f"couch.menu.{item['id']}.label", str(item.get("label", "")))
                self.set(f"couch.menu.{item['id']}.value", str(item.get("value", "")))
        self.set("couch.menu", "1")

    def volume(self, panel, ms=VOLUME_MS):
        if not isinstance(panel, dict):
            self.volume_until = None
            self.set("couch.volume", "")
            return
        level = max(0, min(100, int(panel.get("volume") or 0)))
        muted = bool(panel.get("muted"))
        self.set("couch.volume.name", str(panel.get("name", "")))
        self.set("couch.volume.value", "Muted" if muted else str(level))
        # The skin draws the bar in steps of 5 (Kodi can't size a control from a property).
        self.set("couch.volume.step", "0" if muted else str(5 * round(level / 5)))
        self.set("couch.volume", "1")
        self.volume_until = self.clock() + ms / 1000

    def tick(self):
        """The volume panel goes once it has been still for a while."""
        if self.volume_until is not None and self.clock() >= self.volume_until:
            self.volume(None)


class Kodi:
    def __init__(self):
        import xbmc
        import xbmcgui

        self.xbmc = xbmc
        self.home = xbmcgui.Window(HOME)
        self.overlay = Overlay(self.home.setProperty)

        class Monitor(xbmc.Monitor):
            def onNotification(monitor, sender, method, data):
                if sender == SENDER and method == OVERLAY:
                    try:
                        self.overlay.handle(json.loads(data))
                    except ValueError:
                        self.log("couldn't read couchd's overlay")

        self.monitor = Monitor()

    def log(self, message):
        self.xbmc.log(f"service.couch: {message}", self.xbmc.LOGINFO)

    def start_plex(self):
        self.xbmc.executebuiltin(f"RunScript({PLEX})")

    def plex_started(self):
        return bool(self.home.getProperty("script.plex.started"))

    def plex_active(self):
        return bool(self.home.getProperty("script.plex.is_active"))

    def home_in_front(self):
        return self.xbmc.getCondVisibility("Window.IsActive(home) + !System.HasActiveModalDialog")

    def closing(self, on):
        self.home.setProperty("couch.closing", "1" if on else "")

    def quit(self):
        self.closing(True)
        self.xbmc.executebuiltin("Quit")


def main():
    kodi = Kodi()
    kodi.log("opening Plex")
    kodi.overlay.up(True)
    kodi.start_plex()
    watch = Watch(kodi)
    closing = False
    while not kodi.monitor.waitForAbort(TICK):
        kodi.overlay.tick()
        reason = watch.tick()
        if watch.closing != closing:
            closing = watch.closing
            kodi.closing(closing)
        if reason:
            kodi.log(f"{reason}; quitting Kodi so Couch comes back")
            kodi.overlay.up(False)
            kodi.quit()
            return
    kodi.overlay.up(False)


if __name__ == "__main__":
    main()

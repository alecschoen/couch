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
- Signing out and switching users happen inside it. Some settings changes restart it:
  `started` blinks off and on again.

So: quit Kodi once `started` has been gone for a moment, or once Kodi's Home has been in
front with PM4K inactive for a moment. Kodi's Home is the Opening screen (skin.couch), and
`couch.closing` tells it to drop "Opening" as soon as PM4K looks to be leaving.

The decisions live in Watch, which knows nothing about Kodi, so tests can drive it.
"""

import time

START_TIMEOUT = 120.0  # the first start renders PM4K's templates, which is slow on a Pi
EXIT_GRACE = 2.0  # PM4K restarting itself clears `started` briefly
HOME_GRACE = 1.0
TICK = 0.2

HOME = 10000
PLEX = "script.plexmod"


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
    def leaving(self):
        """Plex looks to be on its way out (or restarting): Home stops saying "Opening"."""
        return self.gone_since is not None

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


class Kodi:
    def __init__(self):
        import xbmc
        import xbmcgui

        self.xbmc = xbmc
        self.home = xbmcgui.Window(HOME)
        self.monitor = xbmc.Monitor()

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
    kodi.start_plex()
    watch = Watch(kodi)
    closing = False
    while not kodi.monitor.waitForAbort(TICK):
        reason = watch.tick()
        if watch.leaving != closing:
            closing = watch.leaving
            kodi.closing(closing)
        if reason:
            kodi.log(f"{reason}; quitting Kodi so Couch comes back")
            kodi.quit()
            return


if __name__ == "__main__":
    main()

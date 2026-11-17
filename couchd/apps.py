"""Apps and what's on screen: couchd decides.

Three apps today. The launcher (launcher/, native) is what the session runs in cage.
YouTube is a tab in a Chromium that's started for it in the same cage (couch-chromium.service)
and driven over DevTools (couchd/chromium.py): its window goes over the launcher's, and
stopping Chromium shows the launcher again. Plex is Kodi, which needs the whole screen, so
launching it hands the display over (couchd/display.py): the launcher shows its Opening
screen, Kodi takes the screen, and when Kodi exits the session gets it back.

Rules:
- `home` always ends on the launcher, from anywhere, and the remote's Home key does it too.
  So does a Chromium app asking to leave (YouTube's Back from its top level).
  The Pause key (also sent when the TV goes to standby) pauses the app in front: a Chromium
  app over DevTools, Kodi over its JSON-RPC (couchd/kodi_rpc.py).
- Leaving a Chromium app stops Chromium: nothing of it stays running behind the launcher.
- Opening an app shows the launcher's Opening screen (#19) until the app is ready to look
  at: a new YouTube tab loads in the background and comes to the front once it has loaded
  (or after `PAGE_LOAD_WAIT`), and Kodi gets the screen after `opening_delay`. Home or Back
  while it opens gives up on it.
- Before opening, couchd checks the app can work: YouTube needs the internet, Plex its server
  (couchd/network.py). If not, the launcher shows an error screen instead.
- If an app dies (a crashed tab, Kodi failing, Chromium going away under it), we go back
  to the launcher and set `error` for the error screen. Nothing restarts an app on its own;
  after `CRASH_LIMIT` crashes in `CRASH_WINDOW` seconds, launching it is refused for a while.
  An error clears itself after `ERROR_TTL`, on Home, or when anything is launched.
- If the session is gone and nothing brings it back, we restart it, capped the same way, so
  a broken launcher can't spin forever.

Live state section `apps`: see docs/protocol.md.
"""

import asyncio
import contextlib
import itertools
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from couchd import youtube
from couchd.backends import BackendError
from couchd.chromium import CdpError, Chromium, PageSetup
from couchd.commands import CommandError, arg
from couchd.display import Display, FakeDisplay
from couchd.kodi_rpc import KodiRpc
from couchd.network import Connectivity, FakeConnectivity
from couchd.services import Context, Service

log = logging.getLogger(__name__)

AppKind = Literal["launcher", "chromium", "kodi"]
# What the error screen says: no internet, Plex server unreachable, the app stopped, the app
# didn't start, or it keeps stopping (the crash limit).
ErrorKind = Literal["offline", "server", "crashed", "failed", "repeated"]

OPENING_DELAY = 0.8  # the Opening screen shows at least this long, and paints before a handoff
PAGE_LOAD_WAIT = 10.0  # a new tab comes to the front once loaded, or after this long
ERROR_TTL = 120.0  # an error screen nobody answered goes away by itself
BROWSER_WAIT = 20.0  # how long Chromium gets to answer on DevTools once started
SESSION_GRACE = 15.0  # the session down this long: restart it
DRAW_WAIT = 2.0  # back from Kodi, the launcher draws a frame within this long, or restart
RETRY_MIN, RETRY_MAX = 0.25, 1.0  # DevTools connect backoff; a refused connect is cheap
CRASH_LIMIT, CRASH_WINDOW = 3, 120.0
SESSION_POLL = 5.0  # how often the session is checked on

PAUSE_MEDIA = 'document.querySelectorAll("video, audio").forEach((m) => m.pause())'
VISIBILITY = "document.visibilityState"
# How far a loading tab has got: its readyState ("blank" while a new tab is still
# about:blank, which is "complete" until navigation starts), and for the "opened" timeline
# log its first
# paint, how many of YouTube's TV elements (ytlr-*) there are, and how many images loaded.
PROBE = """(() => {
  const p = performance.getEntriesByName("first-contentful-paint")[0];
  let yt = 0, img = 0;
  for (const e of document.querySelectorAll("*")) if (e.localName.startsWith("ytlr-")) yt++;
  for (const i of document.images) if (i.complete && i.naturalWidth) img++;
  return {rs: location.href === "about:blank" ? "blank" : document.readyState,
          fcp: p ? Math.round(p.startTime) : null, yt, img};
})()"""


@dataclass(frozen=True)
class App:
    id: str
    name: str
    kind: AppKind
    url: str = ""  # Chromium apps: what to open
    match: str = ""  # Chromium apps: a tab whose URL starts with this is this app
    setup: PageSetup | None = None  # Chromium apps: user agent and script for the tab

    def public(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "kind": self.kind}


DEFAULT_APPS = (
    App("launcher", "Home", "launcher"),
    App("youtube", "YouTube", "chromium", youtube.URL, youtube.MATCH, youtube.SETUP),
    App("plex", "Plex", "kodi"),  # Kodi profile and the PM4K addon: #22
)


class CrashLimiter:
    """Counts recent crashes per key, for capping restart loops."""

    def __init__(
        self,
        limit: int = CRASH_LIMIT,
        window: float = CRASH_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._times: dict[str, deque[float]] = defaultdict(deque)

    def record(self, key: str) -> None:
        self._times[key].append(self._clock())

    def too_many(self, key: str) -> bool:
        times = self._times[key]
        while times and self._clock() - times[0] > self.window:
            times.popleft()
        return len(times) >= self.limit


class AppService(Service):
    name = "apps"

    def __init__(
        self,
        ctx: Context,
        display: Display,
        chromium: Chromium,
        *,
        apps: tuple[App, ...] = DEFAULT_APPS,
        connectivity: Connectivity | None = None,
        kodi_rpc: KodiRpc | None = None,
        opening_delay: float = OPENING_DELAY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if [a.kind for a in apps].count("launcher") != 1:
            raise ValueError("exactly one launcher app")
        self.ctx = ctx
        self.display = display
        self.chromium = chromium
        self.apps = {app.id: app for app in apps}
        self.launcher = next(a for a in apps if a.kind == "launcher")
        self.connectivity = connectivity  # None: don't check before opening
        self.kodi_rpc = kodi_rpc  # None: Pause doesn't reach Kodi (dev mode has no Kodi)
        self.opening_delay = opening_delay
        self._marks: dict[str, float] | None = None  # the open in progress, for its log
        self._clock = clock
        self.crashes = CrashLimiter(clock=clock)

        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._pages: dict[str, str] = {}  # app id -> tab (target id)
        self._foreground = self.launcher.id
        self._opening: str | None = None
        self._error: dict[str, str] | None = None
        self._error_expiry: asyncio.Task | None = None
        self._abort = asyncio.Event()  # Home or Back while an app opens
        self._kodi: str | None = None  # the Kodi app on screen
        self._browser_ready = asyncio.Event()  # Chromium is up and attached
        self._wake = asyncio.Event()
        self._down_since = clock()
        self._chromium_gen = 0  # bumped whenever we start or stop Chromium
        self._adopted = False  # looked for a Chromium left running by a previous couchd
        self._draw_ids = itertools.count(1)
        self._draw_checks: dict[int, asyncio.Future] = {}

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        await self.display.start()
        self.chromium.on_page_gone(self._page_gone)
        self.chromium.on_page_message(self._page_message)
        if await self.display.owner() == "kodi":
            # couchd restarted while Plex was up: leave it there and watch it.
            kodi = next(a for a in self.apps.values() if a.kind == "kodi")
            self._kodi = self._foreground = kodi.id
            await self.display.watch_kodi(self._kodi_exit_callback(kodi.id))
        commands = self.ctx.commands
        commands.add("apps.launch", self._launch_command)
        commands.add("apps.close", self._close_command)
        commands.add("apps.home", self._home_command)
        commands.add("apps.clear_error", self._clear_error_command)
        commands.add("ui.drawn", self._drawn_command)
        if isinstance(self.display, FakeDisplay):
            commands.add("apps.dev.kodi_exit", self._dev_kodi_exit)
            commands.add("apps.dev.crash", self._dev_crash)
        if isinstance(self.connectivity, FakeConnectivity):
            commands.add("apps.dev.network", self._dev_network)
        self._publish()
        self._spawn(self._session_loop())
        self._spawn(self._watch_keys())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("apps.")
        self.ctx.commands.remove_prefix("ui.drawn")
        self._set_error(None)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.chromium.close()
        await self.display.stop()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            log.error("app manager task failed", exc_info=e)

    # --- public operations ---------------------------------------------------------------

    @property
    def foreground(self) -> str:
        return self._foreground

    def page(self, app_id: str | None) -> str | None:
        """The tab (DevTools target id) of a Chromium app that has one open."""
        if app_id is None or self.apps.get(app_id, self.launcher).kind != "chromium":
            return None
        return self._pages.get(app_id)

    async def launch(self, app_id: str) -> None:
        """Start `app_id`, or bring it to the front if it's already running."""
        app = self._app(app_id)
        if app.kind == "launcher":
            await self.home()
            return
        if self.crashes.too_many(app.id):
            self._fail(app.id, "repeated", f"{app.name} keeps stopping")
            self._publish()
            raise CommandError(f"{app.name} keeps stopping. Try again in a minute.")
        self._abort.clear()
        if app.kind == "chromium":
            await self._leave_kodi()
            async with self._lock:
                await self._launch_chromium(app)
        else:
            # If the launcher's connection drops during the switch (it restarting, say), the
            # command that asked for Plex is cancelled (couchd/api.py). The handoff
            # has to finish anyway, or Kodi is on screen while the remote still goes to the
            # launcher. So it runs as our own task, and only the wait for it can be cancelled.
            await asyncio.shield(self._own(self._launch_kodi_locked(app)))

    async def _launch_kodi_locked(self, app: App) -> None:
        async with self._lock:
            await self._launch_kodi(app)

    def _own(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        """A task that outlives its caller: stop() cancels it, and its error (which the
        caller sees, if it's still waiting) isn't logged as the app manager's."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled():
                t.exception()  # retrieved, so an abandoned failure isn't reported twice

        task.add_done_callback(done)
        return task

    async def home(self) -> None:
        if self._opening is not None:
            self._abort.set()  # stop waiting on the app that's opening; it holds the lock
        async with self._lock:
            self._set_error(None)
            if self._kodi is not None:
                await self._stop_kodi()
            else:
                await self._show(self.launcher.id)
            self._publish()

    async def close(self, app_id: str) -> None:
        app = self._app(app_id)
        if app.kind == "launcher":
            raise CommandError("The launcher can't be closed")
        async with self._lock:
            if app.kind == "kodi":
                if self._kodi == app.id:
                    await self._stop_kodi()
            elif (target := self._pages.pop(app.id, None)) is not None:
                if self._foreground == app.id:
                    await self._show(self.launcher.id)
                with contextlib.suppress(CdpError):
                    await self.chromium.close_page(target)
            self._publish()

    def clear_error(self) -> None:
        self._set_error(None)
        self._publish()

    # --- Chromium apps -------------------------------------------------------------------

    async def _leave_kodi(self) -> None:
        if self._kodi is not None:
            async with self._lock:
                if self._kodi is not None:
                    await self._stop_kodi()

    async def _launch_chromium(self, app: App) -> None:
        """Bring a tab to the front, opening it first if need be. Lock held."""
        self._set_error(None)
        try:
            if app.id not in self._pages and not await self._open_tab(app):
                if self._error is not None:
                    raise CommandError(self._error["message"])
                return  # given up on: Home or Back
            await self._show(app.id)
            self._mark("shown")
            self._log_timeline(app)
        except CdpError as e:
            raise CommandError(f"Couldn't switch to {app.name}") from e
        finally:
            self._opening = None
            if self._foreground == self.launcher.id:
                # Given up on, or it failed: its window mustn't stay over the launcher.
                await self._stop_chromium()
            self._publish()

    async def _open_tab(self, app: App) -> bool:
        """Open the app's tab behind the launcher's Opening screen and wait for it to load.
        False if the user gave up on it first. Lock held."""
        started = self._clock()
        self._marks = {"t0": started}
        await self._begin_opening(app)
        if not await self._unless_aborted(self._preflight(app)):
            return False
        self._mark("preflight_done")
        try:
            if not await self._unless_aborted(self._start_chromium()):
                return False
        except (BackendError, TimeoutError) as e:
            log.error("Chromium didn't start", extra={"app": app.id, "error": str(e)})
            self.crashes.record(app.id)
            self._fail(app.id, "failed", f"{app.name} didn't open")
            raise CommandError(f"Couldn't open {app.name}") from e
        try:
            # Chromium was just started for this, on a blank tab; load it there.
            blank = await self._blank_page()
            self._pages[app.id] = await self.chromium.open(
                app.url, app.setup, background=True, reuse=blank
            )
            self._mark("tab_opened")
        except CdpError as e:
            self._fail(app.id, "failed", f"{app.name} didn't open")
            raise CommandError(f"Couldn't open {app.name}") from e
        loaded = await self._unless_aborted(self._wait_loaded(self._pages[app.id]))
        self._mark("loaded")
        if loaded:
            loaded = await self._unless_aborted(self._linger(started))
            self._mark("lingered")
        return loaded

    async def _blank_page(self) -> str | None:
        """A blank tab nothing of ours is in yet, if Chromium has one."""
        ours = set(self._pages.values())
        with contextlib.suppress(CdpError):
            for page in await self.chromium.pages():
                if page.url == "about:blank" and page.id not in ours:
                    return page.id
        return None

    def _mark(self, name: str) -> None:
        """A step of the open in progress, for its timeline log."""
        if self._marks is not None and name not in self._marks:
            self._marks[name] = self._clock()

    def _log_timeline(self, app: App) -> None:
        marks, self._marks = self._marks, None
        if not marks:
            return
        t0 = marks.pop("t0")
        steps = {k: round((v - t0) * 1000) for k, v in marks.items()}
        log.info("opened", extra={"app": app.id, "ms": steps})

    async def _wait_loaded(self, target: str) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(PAGE_LOAD_WAIT):
                while True:
                    with contextlib.suppress(CdpError):
                        probe = await self.chromium.evaluate(target, PROBE)
                        if isinstance(probe, dict):
                            self._probe_marks(probe)
                            if probe.get("rs") == "complete":
                                return
                    await asyncio.sleep(0.2)
        log.info("tab still loading; showing it anyway", extra={"target": target})

    def _probe_marks(self, probe: dict[str, Any]) -> None:
        """First sightings while a tab loads: navigation under way, the DOM parsed, its
        first paint, YouTube's own elements, and the first images."""
        rs = probe.get("rs")
        if rs in ("loading", "interactive", "complete"):
            self._mark("navigating")
        if rs in ("interactive", "complete"):
            self._mark("dom_ready")
        if probe.get("fcp") is not None:
            self._mark("first_paint")
        yt = probe.get("yt") or 0
        if yt:
            self._mark("yt_elements")
        if yt >= 200:
            self._mark("yt_200_elements")
        img = probe.get("img") or 0
        if img:
            self._mark("first_image")
        if img >= 10:
            self._mark("10_images")

    async def _begin_opening(self, app: App) -> None:
        """Show the Opening screen: `opening` set, and the launcher in front. Lock held."""
        self._opening = app.id
        self._publish()
        if self._foreground != self.launcher.id:
            with contextlib.suppress(CdpError):
                await self._show(self.launcher.id)

    async def _linger(self, started: float) -> None:
        """Keep the Opening screen up for at least `opening_delay`, so it's seen, not a flash."""
        await asyncio.sleep(max(0.0, self.opening_delay - (self._clock() - started)))

    async def _unless_aborted(self, awaitable: Coroutine[Any, Any, Any]) -> Any:
        """Await it, unless Home or Back comes first: then cancel it and return False."""
        work = asyncio.ensure_future(awaitable)
        abort = asyncio.ensure_future(self._abort.wait())
        try:
            await asyncio.wait({work, abort}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            abort.cancel()
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
            log.info("stopped opening", extra={"app": self._opening})
            return False
        result = work.result()
        return True if result is None else result

    async def _preflight(self, app: App) -> bool:
        """Can the app work right now? If not, set the error that says why."""
        if self.connectivity is None:
            return True
        if app.kind == "chromium":
            if await self.connectivity.internet():
                return True
            self._fail(app.id, "offline", f"{app.name} needs the internet")
            return False
        if await self.connectivity.plex() != "unreachable":
            return True
        if await self.connectivity.internet():
            self._fail(app.id, "server", "Can't reach your Plex server")
        else:
            self._fail(app.id, "offline", f"{app.name} needs the network")
        return False

    async def _show(self, app_id: str) -> None:
        """Put an app's tab in front, or the launcher, which means stopping Chromium. Lock
        held."""
        if app_id == self.launcher.id:
            self._foreground = app_id
            # Said first: the launcher maps over Chromium as soon as it hears (its
            # src/stacking.rs), so it's back at once, not once Chromium has gone.
            self._publish()
            await self._stop_chromium()
        else:
            await self.chromium.activate(self._pages[app_id])
            self._foreground = app_id

    def _page_message(self, target_id: str, message: str) -> None:
        """A Chromium app's page asks for something. "home": it wants to leave."""
        app_id = next((a for a, t in self._pages.items() if t == target_id), None)
        if app_id is None:
            return
        if message == "home" and self._foreground == app_id:
            log.info("app asked to go home", extra={"app": app_id})
            self._spawn(self.home())

    def _page_gone(self, target_id: str, crashed: bool) -> None:
        if target_id in self._pages.values():
            self._spawn(self._handle_page_gone(target_id, crashed))

    async def _handle_page_gone(self, target_id: str, crashed: bool) -> None:
        async with self._lock:
            app_id = next((a for a, t in self._pages.items() if t == target_id), None)
            if app_id is None:
                return  # we closed it ourselves
            del self._pages[app_id]
            app = self.apps[app_id]
            if crashed:
                log.warning("tab crashed", extra={"app": app_id})
                self.crashes.record(app_id)
                with contextlib.suppress(CdpError):
                    await self.chromium.close_page(target_id)
            if crashed:
                self._fail(app_id, "crashed", f"{app.name} stopped unexpectedly")
            try:
                if self._foreground == app_id:
                    await self._show(self.launcher.id)
            except CdpError as e:
                log.warning("couldn't recover from a closed tab", extra={"error": str(e)})
            self._publish()

    # --- Kodi ----------------------------------------------------------------------------

    async def _launch_kodi(self, app: App) -> None:
        """Hand the screen to Kodi. Lock held."""
        if self._kodi is not None:
            return  # already on screen
        self._set_error(None)
        # The launcher, in front, shows the Opening screen while the switch happens. Kodi's
        # own splash looks the same (system/kodi/splash.png), so the handoff is one screen.
        started = self._clock()
        await self._begin_opening(app)
        ok = await self._unless_aborted(self._preflight(app))
        if ok:
            ok = await self._unless_aborted(self._linger(started))
        if not ok:
            self._opening = None
            self._publish()
            if self._error is not None:
                raise CommandError(self._error["message"])
            return
        try:
            await self.display.start_kodi(self._kodi_exit_callback(app.id))
            self._kodi = self._foreground = app.id
        except BackendError as e:
            log.error("Kodi didn't start", extra={"app": app.id, "error": str(e)})
            self.crashes.record(app.id)
            self._fail(app.id, "failed", f"{app.name} didn't start")
            await self._restore_session()
            raise CommandError(f"{app.name} didn't start") from e
        finally:
            self._opening = None
            self._publish()
        log.info("Kodi has the screen", extra={"app": app.id})

    async def _stop_kodi(self) -> None:
        """Back to the launcher's session. Lock held."""
        log.info("taking the screen back from Kodi", extra={"app": self._kodi})
        self._kodi = None  # so its exit isn't taken for a crash
        self._foreground = self.launcher.id
        self._publish()
        await self._restore_session()

    def _kodi_exit_callback(self, app_id: str) -> Callable[[bool], None]:
        return lambda failed: self._spawn(self._kodi_exited(app_id, failed))

    async def _kodi_exited(self, app_id: str, failed: bool) -> None:
        async with self._lock:
            if self._kodi != app_id:
                return  # we stopped it
            self._kodi = None
            self._foreground = self.launcher.id
            app = self.apps[app_id]
            if failed:
                self.crashes.record(app_id)
                self._fail(app_id, "crashed", f"{app.name} stopped unexpectedly")
            log.info("Kodi exited", extra={"app": app_id, "failed": failed})
            self._publish()
            await self._restore_session()

    async def _restore_session(self) -> None:
        self._down_since = self._clock()
        self._wake.set()
        try:
            await self.display.show_session()
        except BackendError as e:
            log.error("couldn't bring the launcher back", extra={"error": str(e)})
            return
        # The session waits behind Kodi (couchd/display.py), so the launcher is normally
        # still there: make sure it's in front.
        with contextlib.suppress(CdpError):
            await self._show(self.launcher.id)
        if not await self._launcher_draws():
            # cage didn't take the screen back from Kodi (it can fail its modeset when the
            # switch races Kodi's exit). Start it again: the slow way, but not black.
            log.warning("the launcher isn't drawing after Kodi; restarting the session")
            try:
                await self.display.restart_session()
            except BackendError as e:
                log.error("couldn't restart the launcher", extra={"error": str(e)})

    # --- the session ---------------------------------------------------------------------

    async def _session_loop(self) -> None:
        """Find a Chromium a previous couchd left running (once), then keep an eye on the
        session."""
        while True:
            if not self._adopted:
                self._adopted = True
                if await self.display.chromium_running():
                    await self._adopt_chromium()
            await self._sleep_or_wake(SESSION_POLL)
            if self._kodi is not None:
                continue
            if await self.display.owner() != "none":
                self._down_since = self._clock()
            else:
                await self._check_session()

    async def _sleep_or_wake(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()
        self._wake.clear()

    async def _check_session(self) -> None:
        """The session is down. If it stays down, start it, capped."""
        if self._clock() - self._down_since < SESSION_GRACE or self._kodi is not None:
            return
        if await self.display.owner() != "none":
            return
        if self.crashes.too_many("session"):
            return
        log.warning("the session is down; starting it")
        self.crashes.record("session")
        await self._restore_session()

    async def _attached(self) -> None:
        """Connected to Chromium: find our apps' tabs in it."""
        pages = await self.chromium.pages()
        self._pages = {}
        for page in pages:
            app = self._app_for_url(page.url)
            if app is not None and app.id not in self._pages:
                self._pages[app.id] = page.id
                if app.setup is not None:
                    # couchd restarted: the tab lost its setup with our old session.
                    try:
                        await self.chromium.prepare(page.id, app.setup)
                    except CdpError as e:
                        log.warning("couldn't set up a tab", extra={"app": app.id, "error": str(e)})
        self._browser_ready.set()

    async def _visible_app(self) -> str | None:
        for app_id, target in self._pages.items():
            with contextlib.suppress(CdpError):
                if await self.chromium.evaluate(target, VISIBILITY) == "visible":
                    return app_id
        return None

    def _app_for_url(self, url: str) -> App | None:
        for app in self.apps.values():
            if app.kind == "chromium" and app.match and url.startswith(app.match):
                return app
        return None

    # --- keys --------------------------------------------------------------------------

    async def _watch_keys(self) -> None:
        """Home goes home. Pause pauses the app in front (the TV sends it when it goes to
        standby). Other keys reach apps through couchd/remote.py."""
        with self.ctx.bus.subscribe() as sub:
            async for message in sub:
                data = message.get("data")
                if message.get("type") != "key" or not isinstance(data, dict):
                    continue
                try:
                    if data.get("key") == "home":
                        await self.home()
                    elif data.get("key") == "pause":
                        await self.pause()
                except (CommandError, BackendError) as e:
                    log.warning("key failed", extra={"key": data.get("key"), "error": str(e)})

    async def pause(self) -> None:
        """Pause media in the app in front: a Chromium app's tab, or Kodi."""
        async with self._lock:
            app = self.apps[self._foreground]
            if app.kind == "chromium" and app.id in self._pages:
                await self.chromium.evaluate(self._pages[app.id], PAUSE_MEDIA)
                log.info("paused", extra={"app": app.id})
            elif app.kind == "kodi" and self._kodi == app.id and self.kodi_rpc is not None:
                await self.kodi_rpc.pause()

    # --- state ---------------------------------------------------------------------------

    def _app(self, app_id: str) -> App:
        app = self.apps.get(app_id)
        if app is None:
            raise CommandError(f"unknown app {app_id!r}")
        return app

    def _fail(self, app_id: str, kind: ErrorKind, message: str) -> None:
        """Set the error the launcher's error screen shows. No toast: the screen says it."""
        log.info("app error", extra={"app": app_id, "kind": kind, "error": message})
        self._set_error({"app": app_id, "kind": kind, "message": message})

    def _set_error(self, error: dict[str, str] | None) -> None:
        if self._error_expiry is not None:
            self._error_expiry.cancel()
            self._error_expiry = None
        self._error = error
        if error is not None:
            self._error_expiry = asyncio.create_task(self._expire_error(error))

    async def _expire_error(self, error: dict[str, str]) -> None:
        await asyncio.sleep(ERROR_TTL)
        if self._error is error:
            self._error_expiry = None
            self.clear_error()

    # --- Chromium: only while one of its apps is open -------------------------------------

    async def _start_chromium(self) -> None:
        """Start Chromium over the launcher and attach to it. Lock held. Raises
        BackendError or TimeoutError if it doesn't come up."""
        if self._browser_ready.is_set():
            return
        self._chromium_gen += 1
        gen = self._chromium_gen
        self._mark("chromium_start")
        await self.display.start_chromium()
        self._mark("chromium_unit_up")
        delay = RETRY_MIN
        async with asyncio.timeout(BROWSER_WAIT):
            while True:
                try:
                    await self.chromium.connect()
                    break
                except CdpError:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RETRY_MAX)
        self._mark("chromium_connected")
        await self._attached()
        self._spawn(self._watch_chromium(gen))

    async def _stop_chromium(self) -> None:
        """Stop it: its window goes, and the launcher under it shows. Lock held."""
        self._chromium_gen += 1
        self._browser_ready.clear()
        self._pages = {}
        await self.chromium.close()
        await self.display.stop_chromium()

    async def _watch_chromium(self, gen: int) -> None:
        """Chromium going away on its own (a crash, a kill): back to the launcher."""
        await self.chromium.wait_closed()
        async with self._lock:
            if gen != self._chromium_gen:
                return  # we stopped it ourselves, or started another since
            self._chromium_gen += 1
            self._browser_ready.clear()
            self._pages = {}
            app = self.apps[self._foreground]
            if app.kind == "chromium":
                log.warning("lost Chromium", extra={"foreground": app.id})
                self.crashes.record(app.id)
                self._fail(app.id, "crashed", f"{app.name} stopped unexpectedly")
                self._foreground = self.launcher.id
            await self.display.stop_chromium()
            self._publish()

    async def _adopt_chromium(self) -> None:
        """couchd restarted while Chromium was up: take it over if one of our apps is on
        screen in it, or stop it."""
        async with self._lock:
            self._chromium_gen += 1
            gen = self._chromium_gen
            try:
                await self.chromium.connect()
                await self._attached()
            except CdpError as e:
                log.warning("couldn't take over Chromium", extra={"error": str(e)})
                await self._stop_chromium()
                return
            visible = await self._visible_app()
            if visible is None or self._kodi is not None:
                await self._stop_chromium()  # nothing of ours on screen in it
            else:
                self._foreground = visible
                self._spawn(self._watch_chromium(gen))
            self._publish()

    async def _launcher_draws(self) -> bool:
        """Does the launcher draw? It answers a `draw_check` with `ui.drawn` after its
        next frame, and it gets no frames while cage hasn't got the screen."""
        check = next(self._draw_ids)
        future = asyncio.get_running_loop().create_future()
        self._draw_checks[check] = future
        self.ctx.bus.publish("draw_check", {"id": check})
        try:
            async with asyncio.timeout(DRAW_WAIT):
                await future
        except TimeoutError:
            return False
        finally:
            self._draw_checks.pop(check, None)
        return True

    async def _drawn_command(self, args: dict[str, Any]) -> None:
        future = self._draw_checks.get(arg(args, "id", int))
        if future is not None and not future.done():
            future.set_result(None)

    def _publish(self) -> None:
        running = [self.launcher.id, *(a for a in self.apps if a in self._pages or a == self._kodi)]
        self.ctx.state.update(
            "apps",
            {
                "list": [app.public() for app in self.apps.values()],
                "foreground": self._foreground,
                "running": running,
                "opening": self._opening,
                "error": self._error,
                "browser": "connected" if self._browser_ready.is_set() else "disconnected",
            },
        )

    # --- commands ------------------------------------------------------------------------

    async def _launch_command(self, args: dict[str, Any]) -> None:
        await self.launch(arg(args, "app", str))

    async def _close_command(self, args: dict[str, Any]) -> None:
        await self.close(arg(args, "app", str))

    async def _home_command(self, args: dict[str, Any]) -> None:
        await self.home()

    async def _clear_error_command(self, args: dict[str, Any]) -> None:
        self.clear_error()

    async def _dev_kodi_exit(self, args: dict[str, Any]) -> None:
        assert isinstance(self.display, FakeDisplay)
        crashed = args.get("crashed", False)
        if not isinstance(crashed, bool):
            raise CommandError("'crashed' must be true or false")
        if not self.display.kodi_running:
            raise CommandError("Kodi isn't running")
        self.display.exit_kodi(crashed=crashed)

    async def _dev_crash(self, args: dict[str, Any]) -> None:
        """Dev mode only: act as if an app crashed (its tab, or the pretend Kodi)."""
        app = self._app(arg(args, "app", str))
        if app.kind == "kodi":
            await self._dev_kodi_exit({"crashed": True})
            return
        target = self._pages.get(app.id)
        if target is None:
            raise CommandError(f"{app.name} isn't open")
        self._page_gone(target, True)  # what Chromium's Target.targetCrashed leads to

    async def _dev_network(self, args: dict[str, Any]) -> None:
        """Dev mode only: pretend the internet or the Plex server is gone (or back)."""
        assert isinstance(self.connectivity, FakeConnectivity)
        if "internet" in args:
            self.connectivity.online = arg(args, "internet", bool)
        if "plex" in args:
            plex = arg(args, "plex", str)
            if plex not in ("ok", "unreachable", "unknown"):
                raise CommandError("'plex' must be ok, unreachable or unknown")
            self.connectivity.plex_reach = plex  # type: ignore[assignment]

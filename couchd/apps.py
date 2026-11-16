"""Apps and what's on screen: couchd decides.

Three apps today. The launcher and YouTube are tabs in the kiosk Chromium, driven over
DevTools (couchd/chromium.py). Plex is Kodi, which needs the whole screen, so launching it
hands the display over (couchd/display.py): the launcher shows its Opening screen, the
browser session stops, Kodi starts, and when Kodi exits the browser session comes back.

Rules:
- `home` always ends on the launcher, from anywhere, and the remote's Home key does it too.
  So does a Chromium app asking to leave (YouTube's Back from its top level).
  The Pause key (also sent when the TV goes to standby) pauses the app in front: a Chromium
  app over DevTools, Kodi over its JSON-RPC (couchd/kodi_rpc.py).
- An app going to the background has its media paused. Its tab stays open, so bringing it
  back to the front is instant.
- Opening an app shows the launcher's Opening screen (#19) until the app is ready to look
  at: a new YouTube tab loads in the background and comes to the front once it has loaded
  (or after `PAGE_LOAD_WAIT`), and Kodi gets the screen after `opening_delay`. Home or Back
  while it opens gives up on it.
- Before opening, couchd checks the app can work: YouTube needs the internet, Plex its server
  (couchd/network.py). If not, the launcher shows an error screen instead.
- If an app dies (a crashed tab, Kodi failing, the browser going away under it), we go back
  to the launcher and set `error` for the error screen. Nothing restarts an app on its own;
  after `CRASH_LIMIT` crashes in `CRASH_WINDOW` seconds, launching it is refused for a while.
  An error clears itself after `ERROR_TTL`, on Home, or when anything is launched.
- If the browser session is gone and nothing brings it back, we restart it, capped the same
  way, so a broken Chromium can't spin forever.

Live state section `apps`: see docs/protocol.md.
"""

import asyncio
import contextlib
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

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

LOOPBACK = {"127.0.0.1", "localhost", "::1"}

OPENING_DELAY = 0.8  # the Opening screen shows at least this long, and paints before a handoff
PAGE_LOAD_WAIT = 10.0  # a new tab comes to the front once loaded, or after this long
ERROR_TTL = 120.0  # an error screen nobody answered goes away by itself
BROWSER_WAIT = 20.0  # how long a launch waits for the browser to come back from Kodi
SESSION_GRACE = 15.0  # browser unreachable this long, and its session is down: restart it
RETRY_MIN, RETRY_MAX = 0.25, 1.0  # DevTools reconnect backoff; a refused connect is cheap
CRASH_LIMIT, CRASH_WINDOW = 3, 120.0

PAUSE_MEDIA = 'document.querySelectorAll("video, audio").forEach((m) => m.pause())'
VISIBILITY = "document.visibilityState"
# A new tab is about:blank (and "complete") until navigation starts.
READY = 'location.href === "about:blank" ? "loading" : document.readyState'


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
        launcher_url: str | None = None,
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
        self._launcher_url = launcher_url
        self.connectivity = connectivity  # None: don't check before opening
        self.kodi_rpc = kodi_rpc  # None: Pause doesn't reach Kodi (dev mode has no Kodi)
        self.opening_delay = opening_delay
        self._clock = clock
        self.crashes = CrashLimiter(clock=clock)

        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._pages: dict[str, str] = {}  # app id -> tab (target id), launcher included
        self._foreground = self.launcher.id
        self._seen_browser = False  # first attach: find out which tab is in front
        self._opening: str | None = None
        self._error: dict[str, str] | None = None
        self._error_expiry: asyncio.Task | None = None
        self._abort = asyncio.Event()  # Home or Back while an app opens
        self._kodi: str | None = None  # the Kodi app on screen
        self._handoff = False  # handing the screen to Kodi: losing the browser is expected
        self._browser_ready = asyncio.Event()
        self._wake = asyncio.Event()
        self._down_since = clock()

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
        if isinstance(self.display, FakeDisplay):
            commands.add("apps.dev.kodi_exit", self._dev_kodi_exit)
            commands.add("apps.dev.crash", self._dev_crash)
        if isinstance(self.connectivity, FakeConnectivity):
            commands.add("apps.dev.network", self._dev_network)
        self._publish()
        self._spawn(self._browser_loop())
        self._spawn(self._watch_keys())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("apps.")
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
            await self._ensure_browser()
            async with self._lock:
                await self._launch_chromium(app)
        else:
            async with self._lock:
                await self._launch_kodi(app)

    async def home(self) -> None:
        if self._opening is not None:
            self._abort.set()  # stop waiting on the app that's opening; it holds the lock
        async with self._lock:
            self._set_error(None)
            if self._kodi is not None:
                await self._stop_kodi()
            elif self.launcher.id in self._pages:
                await self._show(self.launcher.id)
            else:
                self._foreground = self.launcher.id  # in front once the browser is back
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

    async def _ensure_browser(self) -> None:
        if self._kodi is not None:
            async with self._lock:
                if self._kodi is not None:
                    await self._stop_kodi()
        if not self._browser_ready.is_set():
            try:
                async with asyncio.timeout(BROWSER_WAIT):
                    await self._browser_ready.wait()
            except TimeoutError:
                raise CommandError("The browser isn't responding") from None

    async def _launch_chromium(self, app: App) -> None:
        """Bring a tab to the front, opening it first if need be. Lock held."""
        self._set_error(None)
        try:
            if app.id not in self._pages and not await self._open_tab(app):
                if self._error is not None:
                    raise CommandError(self._error["message"])
                return  # given up on: Home or Back
            await self._show(app.id)
        except CdpError as e:
            raise CommandError(f"Couldn't switch to {app.name}") from e
        finally:
            self._opening = None
            self._publish()

    async def _open_tab(self, app: App) -> bool:
        """Open the app's tab behind the launcher's Opening screen and wait for it to load.
        False if the user gave up on it first. Lock held."""
        started = self._clock()
        await self._begin_opening(app)
        if not await self._unless_aborted(self._preflight(app)):
            return False
        try:
            self._pages[app.id] = await self.chromium.open(app.url, app.setup, background=True)
        except CdpError as e:
            self._fail(app.id, "failed", f"{app.name} didn't open")
            raise CommandError(f"Couldn't open {app.name}") from e
        loaded = await self._unless_aborted(self._wait_loaded(self._pages[app.id]))
        if loaded:
            loaded = await self._unless_aborted(self._linger(started))
        return loaded

    async def _wait_loaded(self, target: str) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(PAGE_LOAD_WAIT):
                while True:
                    with contextlib.suppress(CdpError):
                        if await self.chromium.evaluate(target, READY) == "complete":
                            return
                    await asyncio.sleep(0.2)
        log.info("tab still loading; showing it anyway", extra={"target": target})

    async def _begin_opening(self, app: App) -> None:
        """Show the Opening screen: `opening` set, and the launcher in front. Lock held."""
        self._opening = app.id
        self._publish()
        if self._foreground != self.launcher.id and self.launcher.id in self._pages:
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
        """Put a tab in front and pause whatever was in front before. Lock held."""
        previous = self._foreground
        await self.chromium.activate(self._pages[app_id])
        self._foreground = app_id
        if previous != app_id and previous in self._pages and previous != self.launcher.id:
            with contextlib.suppress(CdpError):
                await self.chromium.evaluate(self._pages[previous], PAUSE_MEDIA)

    def _page_message(self, target_id: str, message: str) -> None:
        """A Chromium app's page asks for something. "home": it wants to leave."""
        app_id = next((a for a, t in self._pages.items() if t == target_id), None)
        if app_id is None or app_id == self.launcher.id:
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
            try:
                if app.kind == "launcher":
                    await self._reopen_launcher()
                else:
                    if crashed:
                        self._fail(app_id, "crashed", f"{app.name} stopped unexpectedly")
                    if self._foreground == app_id and self.launcher.id in self._pages:
                        await self._show(self.launcher.id)
            except CdpError as e:
                log.warning("couldn't recover from a closed tab", extra={"error": str(e)})
            self._publish()

    async def _reopen_launcher(self) -> None:
        if self.crashes.too_many(self.launcher.id):
            log.error("the launcher keeps crashing; not reopening it for now")
            return
        url = self._launcher_url or self.ctx.state.get("system").get("url")
        if not url:
            return
        self._pages[self.launcher.id] = await self.chromium.open(url)
        if self._foreground not in self._pages or self._foreground == self.launcher.id:
            await self._show(self.launcher.id)

    # --- Kodi ----------------------------------------------------------------------------

    async def _launch_kodi(self, app: App) -> None:
        """Hand the screen to Kodi. Lock held."""
        if self._kodi is not None:
            return  # already on screen
        self._set_error(None)
        # The launcher, in front, shows the Opening screen while the switch happens. Kodi's
        # own splash looks the same (ui/assets/opening-plex.png), so the handoff is one screen.
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
        self._handoff = True
        try:
            await self.display.start_kodi(self._kodi_exit_callback(app.id))
            self._kodi = self._foreground = app.id
        except BackendError as e:
            log.error("Kodi didn't start", extra={"app": app.id, "error": str(e)})
            self.crashes.record(app.id)
            self._fail(app.id, "failed", f"{app.name} didn't start")
            await self._restore_browser()
            raise CommandError(f"{app.name} didn't start") from e
        finally:
            self._handoff = False
            self._opening = None
            self._publish()
        log.info("Kodi has the screen", extra={"app": app.id})

    async def _stop_kodi(self) -> None:
        """Back to the browser session. Lock held."""
        log.info("taking the screen back from Kodi", extra={"app": self._kodi})
        self._kodi = None  # so its exit isn't taken for a crash
        self._foreground = self.launcher.id
        self._publish()
        await self._restore_browser()

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
            await self._restore_browser()

    async def _restore_browser(self) -> None:
        self._down_since = self._clock()
        self._wake.set()
        try:
            await self.display.show_browser()
        except BackendError as e:
            log.error("couldn't bring the launcher back", extra={"error": str(e)})

    # --- the browser connection ----------------------------------------------------------

    async def _browser_loop(self) -> None:
        delay = RETRY_MIN
        warned = False
        while True:
            if self._kodi is not None:
                await self._sleep_or_wake(60)
                continue
            try:
                await self.chromium.connect()
            except CdpError as e:
                if not warned:
                    log.info("waiting for Chromium", extra={"error": str(e)})
                    warned = True
                await self._check_session()
                await self._sleep_or_wake(delay)
                delay = min(delay * 2, RETRY_MAX)
                continue
            delay, warned = RETRY_MIN, False
            try:
                async with self._lock:
                    await self._attached()
            except CdpError as e:
                log.warning("couldn't take over Chromium", extra={"error": str(e)})
                await self.chromium.close()
            await self.chromium.wait_closed()
            async with self._lock:
                self._detached()

    async def _sleep_or_wake(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()
        self._wake.clear()

    async def _check_session(self) -> None:
        """The browser is unreachable. If its session is down for good, start it, capped."""
        if self._clock() - self._down_since < SESSION_GRACE or self._kodi is not None:
            return
        if await self.display.owner() != "none":
            return
        if self.crashes.too_many("session"):
            return
        log.warning("the browser session is down; starting it")
        self.crashes.record("session")
        await self._restore_browser()

    async def _attached(self) -> None:
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
        if self.launcher.id not in self._pages:
            await self._reopen_launcher()
        if not self._seen_browser:
            self._foreground = await self._visible_app() or self.launcher.id
            self._seen_browser = True
        if self._foreground not in self._pages:
            self._foreground = self.launcher.id
        if self._foreground in self._pages:
            await self.chromium.activate(self._pages[self._foreground])
        self._browser_ready.set()
        self._publish()

    def _detached(self) -> None:
        self._browser_ready.clear()
        self._pages = {}
        self._down_since = self._clock()
        if not (self._handoff or self._kodi is not None):
            app = self.apps[self._foreground]
            log.warning("lost the browser", extra={"foreground": app.id})
            self.crashes.record("browser")
            if app.kind == "chromium":
                self._fail(app.id, "crashed", f"{app.name} stopped unexpectedly")
            # The session restarts on the launcher.
            self._foreground = self.launcher.id
        self._publish()

    async def _visible_app(self) -> str | None:
        for app_id, target in self._pages.items():
            with contextlib.suppress(CdpError):
                if await self.chromium.evaluate(target, VISIBILITY) == "visible":
                    return app_id
        return None

    def _app_for_url(self, url: str) -> App | None:
        parts = urlsplit(url)
        if parts.scheme == "http" and parts.hostname in LOOPBACK:
            return self.launcher
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

    def _publish(self) -> None:
        running = [a for a in self.apps if a in self._pages or a == self._kodi]
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

"""Who owns the screen: the browser session (cage + Chromium) or Kodi.

Only one program can drive the HDMI output at a time: the one on the console (VT) in front.
On the Pi each is a systemd unit with a logind session on its own VT:

- `couch-session.service`: cage + Chromium on tty1, the launcher and YouTube. Enabled at
  boot, and left running while Plex is up.
- `couch-kodi.service`: Kodi on DRM/GBM on tty2, for Plex (#22). Started only by couchd. It
  switches the console to tty2 as it starts, which pauses cage (logind takes the screen and
  input from it), and back to tty1 however it ends, which gives them back. So leaving Plex
  shows the launcher as it was, without starting cage and Chromium again. The unit also
  starts the session (OnSuccess/OnFailure) in case it wasn't running, so the launcher comes
  back even if couchd isn't.

couchd runs as `couch`, and a polkit rule (`system/couch.rules`) lets that user start, stop
and reset just these two units.

`FakeDisplay` is the dev-mode stand-in: the dev Chromium window stays up, and "Kodi" is a
pretend process that exits when told to (`apps.dev.kodi_exit`). It lives here rather than in
fakes.py because nothing else uses it.
"""

import asyncio
import contextlib
import logging
from abc import abstractmethod
from collections.abc import Callable
from typing import Literal

from couchd.backends import Backend, BackendError

log = logging.getLogger(__name__)

Owner = Literal["browser", "kodi", "none"]
# Called once when Kodi stops, for any reason, with whether it failed (crashed, killed).
KodiExit = Callable[[bool], None]

SESSION_UNIT = "couch-session.service"
KODI_UNIT = "couch-kodi.service"
SYSTEMCTL_TIMEOUT = 30.0
KODI_START_TIMEOUT = 10.0


class Display(Backend):
    @abstractmethod
    async def owner(self) -> Owner:
        """What has the screen right now. "none" means neither is running (a stuck screen)."""

    @abstractmethod
    async def show_browser(self) -> None:
        """Put the browser session in front: stop Kodi if it's running, and start the session
        if it isn't."""

    @abstractmethod
    async def restart_browser(self) -> None:
        """Start the browser session again from scratch (behind the splash)."""

    @abstractmethod
    async def start_kodi(self, on_exit: KodiExit) -> None:
        """Start Kodi in front of the browser session. Returns once Kodi is running."""

    @abstractmethod
    async def watch_kodi(self, on_exit: KodiExit) -> None:
        """Kodi was already running when couchd started: call `on_exit` when it stops."""


class SystemdDisplay(Display):
    def __init__(self) -> None:
        super().__init__()
        self._watchers: set[asyncio.Task] = set()

    async def stop(self) -> None:
        # Stop watching only. Restarting couchd must not take Plex off the screen.
        for task in list(self._watchers):
            task.cancel()
        await asyncio.gather(*self._watchers, return_exceptions=True)

    async def owner(self) -> Owner:
        if await self._state(KODI_UNIT) in ("active", "activating", "reloading"):
            return "kodi"
        if await self._state(SESSION_UNIT) in ("active", "activating", "reloading"):
            return "browser"
        return "none"

    async def show_browser(self) -> None:
        if await self._state(KODI_UNIT) in ("active", "activating", "reloading"):
            # Its ExecStopPost switches the console back to tty1, where the session is.
            code, out = await _systemctl("stop", KODI_UNIT)
            if code != 0:
                log.warning("couldn't stop Kodi", extra={"detail": out or code})
        # After a burst of crashes systemd refuses to start the unit until it is reset.
        await _systemctl("reset-failed", SESSION_UNIT)
        code, out = await _systemctl("start", SESSION_UNIT)
        if code != 0:
            raise BackendError(f"couldn't start the launcher: {out or code}")

    async def restart_browser(self) -> None:
        await _systemctl("reset-failed", SESSION_UNIT)
        code, out = await _systemctl("restart", SESSION_UNIT)
        if code != 0:
            raise BackendError(f"couldn't restart the launcher: {out or code}")

    async def start_kodi(self, on_exit: KodiExit) -> None:
        await _systemctl("reset-failed", KODI_UNIT)
        task = self._watch(on_exit)
        # `start --wait` only returns when the unit stops again, so poll until it's up.
        try:
            async with asyncio.timeout(KODI_START_TIMEOUT):
                while await self._state(KODI_UNIT) != "active":
                    if task.done():
                        raise BackendError("Kodi didn't start")
                    await asyncio.sleep(0.2)
        except TimeoutError:
            raise BackendError("Kodi didn't start in time") from None

    async def watch_kodi(self, on_exit: KodiExit) -> None:
        self._watch(on_exit)

    def _watch(self, on_exit: KodiExit) -> asyncio.Task:
        async def wait() -> None:
            # Starting an active unit does nothing, so this just waits for it to stop.
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "start",
                "--wait",
                KODI_UNIT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await process.communicate()
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                raise
            failed = process.returncode != 0
            if failed:
                log.warning("Kodi failed", extra={"detail": err.decode(errors="replace").strip()})
            on_exit(failed)

        task = asyncio.create_task(wait())
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)
        return task

    async def _state(self, unit: str) -> str:
        _, out = await _systemctl("is-active", unit)
        return out


async def _systemctl(*args: str) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        return 127, str(e)
    try:
        async with asyncio.timeout(SYSTEMCTL_TIMEOUT):
            out, _ = await process.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return 124, f"systemctl {' '.join(args)} timed out"
    return process.returncode or 0, out.decode(errors="replace").strip()


class FakeDisplay(Display):
    """Dev mode: the browser is always there; "Kodi" runs until `exit_kodi()`."""

    def __init__(self, *, speed: float = 1.0) -> None:
        super().__init__()
        self.speed = speed
        self.kodi_running = False
        self.kodi_starts = 0
        self.browser_restarts = 0
        self.fail_next_start = False
        self._on_exit: KodiExit | None = None

    async def owner(self) -> Owner:
        return "kodi" if self.kodi_running else "browser"

    async def show_browser(self) -> None:
        if self.kodi_running:
            self.exit_kodi(crashed=False)

    async def restart_browser(self) -> None:
        self.browser_restarts += 1

    async def start_kodi(self, on_exit: KodiExit) -> None:
        await asyncio.sleep(0.5 * self.speed)
        if self.fail_next_start:
            self.fail_next_start = False
            raise BackendError("Kodi didn't start")
        self.kodi_running = True
        self.kodi_starts += 1
        self._on_exit = on_exit
        log.info("fake Kodi is on screen; apps.dev.kodi_exit ends it")

    async def watch_kodi(self, on_exit: KodiExit) -> None:
        self._on_exit = on_exit

    def exit_kodi(self, *, crashed: bool) -> None:
        if not self.kodi_running:
            return
        self.kodi_running = False
        on_exit, self._on_exit = self._on_exit, None
        if on_exit is not None:
            on_exit(crashed)

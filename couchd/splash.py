"""The last stretch of the boot splash: from cage's start to the launcher's first frame (#23).

The boot splash (#4) is drawn on the framebuffer until cage takes the screen. cage then
shows black until a Wayland client draws, and Chromium needs several seconds to start and
paint the launcher. To bridge that, `couch-session-start` (cage's client) first shows the
Startup frame, `ui/brand/splash.png`, with swayimg, and only then starts Chromium. cage
stacks each newly mapped window on top, so Chromium covers the splash when it maps.

The splash must then go: left running under Chromium it would cost the compositor a
fullscreen blend on every frame, video included. The launcher says when it has drawn its
first frame (`ui.first_frame`), and this service ends swayimg `HANDOVER_DELAY` seconds
later, enough for that frame to reach the screen. Before, the swayimg window was the only
thing to look at; after, it was under Chromium's, so the hand-over can't be seen.

couch-session-start writes swayimg's pid to `$XDG_RUNTIME_DIR/couch-splash.pid`. couchd
runs as the same user, so it can signal it. In dev mode there's no such file and the
command does nothing.
"""

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from couchd.services import Context, Service

log = logging.getLogger(__name__)

HANDOVER_DELAY = 0.5
PIDFILE = "couch-splash.pid"
SPLASH_COMMS = frozenset({"swayimg"})  # what the pid must belong to before we signal it


def default_pidfile() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / PIDFILE


def uptime() -> float | None:
    """Seconds since the kernel started, the clock the boot timing uses."""
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


class SplashService(Service):
    name = "splash"

    def __init__(
        self,
        ctx: Context,
        *,
        pidfile: Path | None = None,
        delay: float = HANDOVER_DELAY,
        proc: Path = Path("/proc"),
        kill: Callable[[int, int], None] = os.kill,
    ) -> None:
        self.ctx = ctx
        self.pidfile = pidfile or default_pidfile()
        self.delay = delay
        self._proc = proc
        self._kill = kill
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.ctx.commands.add("ui.first_frame", self._first_frame_command)

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("ui.first_frame")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _first_frame_command(self, args: dict[str, Any]) -> None:
        log.info(
            "launcher drew its first frame",
            extra={"uptime": uptime(), "page_ms": (args or {}).get("at")},
        )
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._hand_over())

    async def _hand_over(self) -> None:
        await asyncio.sleep(self.delay)
        self.end_splash()

    def end_splash(self) -> bool:
        """End the splash if it's still up. True if one was ended."""
        try:
            pid = int(self.pidfile.read_text().strip())
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            log.warning("unreadable splash pidfile", extra={"path": str(self.pidfile)})
            self._unlink()
            return False
        self._unlink()
        try:
            comm = (self._proc / str(pid) / "comm").read_text().strip()
        except OSError:
            return False  # already gone
        if comm not in SPLASH_COMMS:
            return False  # the pid was reused by something else
        started = time.monotonic()
        try:
            self._kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        except PermissionError:
            log.warning("not allowed to end the splash", extra={"pid": pid})
            return False
        log.info(
            "splash handed over",
            extra={"uptime": uptime(), "ms": round((time.monotonic() - started) * 1000, 1)},
        )
        return True

    def _unlink(self) -> None:
        with contextlib.suppress(OSError):
            self.pidfile.unlink()

"""Wi-Fi on or off, from the `network.wifi` setting (#31), and joining networks (#78).

Couch is meant to be on Ethernet, so Wi-Fi is off unless someone turns it on in Settings >
Network. couchd applies the setting when it starts and whenever it changes. The radio can
only be switched once the Wi-Fi chip is up (on the Pi it appears a few seconds into the
boot), so until the backend reports a radio the setting waits.

One exception, so nobody is cut off: when the setting was never touched and the Pi is
using Wi-Fi to reach the network (no Ethernet), couchd keeps Wi-Fi on and records that as
the setting, instead of switching off the only connection. A Pi set up for Wi-Fi in
Raspberry Pi Imager stays reachable.

Joining from the TV: `wifi.scan` looks for networks, `wifi.connect` joins one (with a
password for a new secured one) and answers once it's connected, or with what went wrong
("Wrong password"), and `wifi.forget` deletes what's saved for one. One join at a time; a
second waits its turn. A join runs to the end even if whoever asked goes away, so a new
profile that fails is always cleaned up (couchd/networkmanager.py). Passwords are never
logged, nor put in state or an error.

Live state section `wifi`: available, enabled, connection ("off" | "disconnected" |
"connecting" | "connected"), ssid and signal (0-100) while connected, primary (Couch's
traffic goes over Wi-Fi), scanning, and networks: what the radio sees, strongest first, each
{ssid, signal, secure, known, active}. The toggle itself is the setting (`settings.set
network.wifi`).
"""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import asdict
from typing import Any

from couchd.backends import BackendError, WifiBackend, WifiStatus
from couchd.commands import CommandError, arg
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SETTING = "network.wifi"  # couchd/settings.py


class WifiService(Service):
    name = "wifi"

    def __init__(self, ctx: Context, backend: WifiBackend) -> None:
        self.ctx = ctx
        self.backend = backend
        self._unwatch = None
        self._lock = asyncio.Lock()  # one switch at a time, always towards the latest setting
        self._joining = asyncio.Lock()  # one join or forget at a time
        self._tasks: set[asyncio.Task] = set()
        self._first = True  # the startup check (see the module docstring) hasn't run yet

    async def start(self) -> None:
        await self.backend.start()
        self.backend.on_change(self._on_backend)
        self._unwatch = self.ctx.settings.on_change(self._on_setting)
        self._sync()
        await self._apply(user=False)
        commands = self.ctx.commands
        commands.add("wifi.scan", self._scan)
        commands.add("wifi.connect", self._connect)
        commands.add("wifi.forget", self._forget)

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("wifi.")
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.backend.stop()

    # --- state -----------------------------------------------------------------------------

    def _sync(self) -> None:
        self.ctx.state.update(
            "wifi",
            {
                **asdict(self.backend.status()),
                "scanning": self.backend.scanning(),
                "networks": [asdict(n) for n in self.backend.networks()],
            },
        )

    def _on_backend(self) -> None:
        self._sync()
        status = self.backend.status()
        if status.available and status.enabled != self.ctx.settings.get(SETTING):
            # The radio appeared (or someone switched it behind our back): put it right.
            self._spawn(self._apply(user=False))

    def _on_setting(self, key: str, _value: object) -> None:
        if key == SETTING:
            self._spawn(self._apply(user=True))

    # --- switching ------------------------------------------------------------------------

    async def _apply(self, *, user: bool) -> None:
        async with self._lock:
            status = self.backend.status()
            if not status.available:
                log.info("no Wi-Fi radio yet; the setting waits for it")
                return
            if self._first:
                self._first = False
                if self._keep_on(status):
                    return
            wanted = self.ctx.settings.get(SETTING)
            if status.enabled == wanted:
                return
            try:
                await self.backend.set_enabled(wanted)
            except BackendError as e:
                log.error("couldn't switch Wi-Fi", extra={"on": wanted, "error": str(e)})
                if user:
                    self.ctx.bus.toast(str(e), "error")
                    # Show the radio as it is, not as asked.
                    self.ctx.settings.set(SETTING, self.backend.status().enabled)

    def _keep_on(self, status: WifiStatus) -> bool:
        """At startup: Wi-Fi is the only way onto the network and nobody chose "off"."""
        if self.ctx.settings.is_set(SETTING) or not (status.enabled and status.primary):
            return False
        log.warning("Wi-Fi is this Pi's only network; leaving it on")
        self.ctx.settings.set(SETTING, True)
        return True

    # --- networks -------------------------------------------------------------------------

    async def _scan(self, _args: dict[str, Any]) -> None:
        await self.backend.scan()

    async def _connect(self, args: dict[str, Any]) -> None:
        ssid = _ssid_arg(args)
        password = args.get("password")
        if password is not None and not isinstance(password, str):
            raise CommandError("'password' must be a string")
        # Shielded: if the command is cancelled (its WebSocket closed), the join still runs
        # to the end, and a new profile that failed is still deleted.
        await asyncio.shield(self._spawn(self._join(ssid, password or None)))

    async def _join(self, ssid: str, password: str | None) -> None:
        async with self._joining:
            await self.backend.connect(ssid, password)

    async def _forget(self, args: dict[str, Any]) -> None:
        ssid = _ssid_arg(args)
        async with self._joining:
            await self.backend.forget(ssid)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        # A failed join's BackendError is the answer to whoever asked; anything else is a bug.
        if error is not None and not isinstance(error, BackendError):
            log.error("Wi-Fi task failed", exc_info=error)


def _ssid_arg(args: dict[str, Any]) -> str:
    ssid = arg(args, "ssid", str)
    if not ssid or len(ssid.encode()) > 32:
        raise CommandError("'ssid' must be a Wi-Fi network's name")
    return ssid

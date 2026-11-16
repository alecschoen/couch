"""Wi-Fi on or off, from the `network.wifi` setting (#31).

Couch is meant to be on Ethernet, so Wi-Fi is off unless someone turns it on in Settings >
Network. couchd applies the setting when it starts and whenever it changes. The radio can
only be switched once the Wi-Fi chip is up (on the Pi it appears a few seconds into the
boot), so until the backend reports a radio the setting waits.

One exception, so nobody is cut off: when the setting was never touched and the Pi is
using Wi-Fi to reach the network (no Ethernet), couchd keeps Wi-Fi on and records that as
the setting, instead of switching off the only connection. A Pi set up for Wi-Fi in
Raspberry Pi Imager stays reachable.

Live state section `wifi`: available, enabled, connection ("off" | "disconnected" |
"connecting" | "connected"), ssid and signal (0-100) while connected, and primary (Couch's
traffic goes over Wi-Fi). The toggle itself is the setting (`settings.set network.wifi`).
"""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import asdict
from typing import Any

from couchd.backends import BackendError, WifiBackend, WifiStatus
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
        self._tasks: set[asyncio.Task] = set()
        self._first = True  # the startup check (see the module docstring) hasn't run yet

    async def start(self) -> None:
        await self.backend.start()
        self.backend.on_change(self._on_backend)
        self._unwatch = self.ctx.settings.on_change(self._on_setting)
        self._sync()
        await self._apply(user=False)

    async def stop(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.backend.stop()

    # --- state -----------------------------------------------------------------------------

    def _sync(self) -> None:
        self.ctx.state.update("wifi", asdict(self.backend.status()))

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

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

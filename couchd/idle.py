"""Idle detection, the hook for the idle clock (#20).

The couch counts as idle when nobody has touched the remote for `idle_after` seconds and
the launcher is in front. While YouTube or Plex is up nothing goes idle: an hour of film
has no key presses.

Activity is any remote key (`key` messages on the bus), a change of app, or the UI saying
so with `idle.activity`. Live state section `idle`: `{"idle": bool, "after": seconds}`.
`after` is the `display.dim_after` setting (0 = never), unless the service was given one.
Python code can also listen with `on_change`, e.g. to put the TV in standby later.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from couchd.services import Context, Service

log = logging.getLogger(__name__)

DIM_AFTER = "display.dim_after"  # a setting (couchd/settings.py)


class IdleService(Service):
    name = "idle"

    def __init__(
        self,
        ctx: Context,
        *,
        idle_after: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctx = ctx
        self._idle_after = idle_after
        self._unwatch = None
        self._clock = clock
        self._last = clock()
        self._idle = False
        self._foreground: str | None = None
        self._listeners: list[Callable[[bool], None]] = []
        self._poke = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def idle_after(self) -> float:
        """Seconds before going idle; 0 means never."""
        if self._idle_after is not None:
            return self._idle_after
        return float(self.ctx.settings.get(DIM_AFTER))

    @property
    def idle(self) -> bool:
        return self._idle

    def on_change(self, listener: Callable[[bool], None]) -> None:
        self._listeners.append(listener)

    def activity(self) -> None:
        self._last = self._clock()
        self._set(False)
        self._poke.set()

    async def start(self) -> None:
        self._foreground = self.ctx.state.get("apps").get("foreground")
        self.ctx.commands.add("idle.activity", self._activity_command)
        self._unwatch = self.ctx.settings.on_change(self._setting_changed)
        self._publish()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("idle.")
        if self._unwatch is not None:
            self._unwatch()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        with self.ctx.bus.subscribe() as sub:
            watcher = asyncio.create_task(self._watch_bus(sub))
            try:
                while True:
                    self._check()
                    wait = None  # idle already, or an app is up: sleep until something happens
                    if not self._idle and self._on_launcher() and self.idle_after > 0:
                        wait = max(self._last + self.idle_after - self._clock(), 0.05)
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(wait):
                            await self._poke.wait()
                    self._poke.clear()
            finally:
                watcher.cancel()

    async def _watch_bus(self, sub) -> None:
        async for message in sub:
            kind, data = message.get("type"), message.get("data")
            if not isinstance(data, dict):
                continue
            if kind == "key":
                self.activity()
            elif kind == "state" and "foreground" in data.get("apps", {}):
                foreground = data["apps"]["foreground"]
                if foreground != self._foreground:
                    self._foreground = foreground
                    self.activity()

    def _on_launcher(self) -> bool:
        return self._foreground in (None, "launcher")

    def _setting_changed(self, key: str, value) -> None:
        if key == DIM_AFTER and self._idle_after is None:
            self._publish()
            self._poke.set()

    def _check(self) -> None:
        after = self.idle_after
        due = after > 0 and self._clock() - self._last >= after
        self._set(self._on_launcher() and due)

    def _set(self, idle: bool) -> None:
        if idle == self._idle:
            return
        self._idle = idle
        log.info("idle" if idle else "active")
        self._publish()
        for listener in list(self._listeners):
            listener(idle)

    def _publish(self) -> None:
        self.ctx.state.update("idle", {"idle": self._idle, "after": self.idle_after})

    async def _activity_command(self, args: dict[str, Any]) -> None:
        self.activity()

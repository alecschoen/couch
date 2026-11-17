"""Idle detection: the idle clock and, if asked for, the TV turning itself off (#20).

The couch counts as idle when nobody has touched the remote for `display.dim_after` seconds
(a setting, 5 minutes by default; 0 = never) and the launcher is in front. While YouTube or
Plex is up nothing goes idle: an hour of film has no key presses.

Dim and the clock are one thing: "Dim to clock after" on the Settings screen. When `idle`
turns true the launcher covers itself with the dark idle clock (launcher/src/idle.rs); there is no
separate dimming step and no second timer.

Activity is any remote key (`key` messages on the bus), a change of app, the TV being turned
back on, or the UI saying so with `idle.activity`. The key that wakes the clock reaches the
UI before `idle` turns false (the bus is in order, and this service hears it at the same
time as the UI), so the launcher can swallow it: that press only wakes the clock.

With the `tv.off_when_idle` setting on, the TV is put in standby once the launcher has gone
`tv_off_after` (2 hours) without activity, once per idle stretch, and not if the TV is in
standby already. It's measured from the last activity, not from when the clock came up.

Live state section `idle`: `{"idle": bool, "after": seconds, "tv_off_after": seconds}`
(`tv_off_after` is 0 while the setting is off). Python code can listen with `on_change`.

For trying it out: the `COUCH_IDLE_AFTER` environment variable (seconds) overrides the
setting (couchd/wiring.py), and the `idle.now` command goes idle at once, on the launcher.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from couchd.commands import CommandError
from couchd.services import Context, Service

log = logging.getLogger(__name__)

DIM_AFTER = "display.dim_after"  # settings (couchd/settings.py)
TV_OFF = "tv.off_when_idle"
TV_OFF_AFTER = 2 * 3600.0


class IdleService(Service):
    name = "idle"

    def __init__(
        self,
        ctx: Context,
        *,
        idle_after: float | None = None,
        tv_off_after: float = TV_OFF_AFTER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctx = ctx
        self._idle_after = idle_after
        self._tv_off_after = tv_off_after
        self._unwatch = None
        self._clock = clock
        self._last = clock()
        self._idle = False
        self._tv_off_done = False  # this idle stretch already turned the TV off
        self._foreground: str | None = None
        self._tv_power: str | None = None
        self._listeners: list[Callable[[bool], None]] = []
        self._poke = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._standby_task: asyncio.Task | None = None

    @property
    def idle_after(self) -> float:
        """Seconds before going idle; 0 means never."""
        if self._idle_after is not None:
            return self._idle_after
        return float(self.ctx.settings.get(DIM_AFTER))

    @property
    def tv_off_after(self) -> float:
        """Seconds of idle before the TV goes to standby; 0 means never."""
        return self._tv_off_after if self.ctx.settings.get(TV_OFF) else 0.0

    @property
    def idle(self) -> bool:
        return self._idle

    def on_change(self, listener: Callable[[bool], None]) -> None:
        self._listeners.append(listener)

    def activity(self) -> None:
        self._last = self._clock()
        self._tv_off_done = False
        self._set(False)
        self._poke.set()

    def go_idle(self) -> None:
        """Idle now, as if the timeout had run out. Only on the launcher."""
        if self._on_launcher():
            self._set(True)

    async def start(self) -> None:
        self._foreground = self.ctx.state.get("apps").get("foreground")
        self._tv_power = self.ctx.state.get("tv").get("power")
        self.ctx.commands.add("idle.activity", self._activity_command)
        self.ctx.commands.add("idle.now", self._now_command)
        self._unwatch = self.ctx.settings.on_change(self._setting_changed)
        self._publish()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("idle.")
        if self._unwatch is not None:
            self._unwatch()
        for task in (self._task, self._standby_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # --- the state machine ---------------------------------------------------------------

    async def _run(self) -> None:
        with self.ctx.bus.subscribe() as sub:
            watcher = asyncio.create_task(self._watch_bus(sub))
            try:
                while True:
                    self._check()
                    wait = self._next_deadline()  # None: sleep until something happens
                    if wait is not None:
                        wait = max(wait - self._clock(), 0.05)
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(wait):
                            await self._poke.wait()
                    self._poke.clear()
            finally:
                watcher.cancel()

    def _next_deadline(self) -> float | None:
        if not self._on_launcher():
            return None
        deadlines = []
        if not self._idle and self.idle_after > 0:
            deadlines.append(self._last + self.idle_after)
        if self._tv_off_due_at() is not None:
            deadlines.append(self._tv_off_due_at())
        return min(deadlines, default=None)

    def _tv_off_due_at(self) -> float | None:
        after = self.tv_off_after
        if after <= 0 or self._tv_off_done or self._tv_power == "standby":
            return None
        return self._last + after

    def _check(self) -> None:
        now = self._clock()
        after = self.idle_after
        due = after > 0 and now - self._last >= after
        if due and self._on_launcher():
            self._set(True)
        elif not self._on_launcher():
            self._set(False)
        tv_off = self._tv_off_due_at()
        if tv_off is not None and now >= tv_off and self._on_launcher():
            self._tv_off_done = True
            self._standby_task = asyncio.create_task(self._tv_standby())

    async def _tv_standby(self) -> None:
        log.info("idle for a long time, turning the TV off")
        try:
            await self.ctx.commands.run("tv.standby")
        except CommandError as e:
            log.warning("couldn't turn the TV off", extra={"error": str(e)})

    async def _watch_bus(self, sub) -> None:
        async for message in sub:
            kind, data = message.get("type"), message.get("data")
            if not isinstance(data, dict):
                continue
            if kind == "key":
                self.activity()
            elif kind == "state":
                apps, tv = data.get("apps", {}), data.get("tv", {})
                if "foreground" in apps and apps["foreground"] != self._foreground:
                    self._foreground = apps["foreground"]
                    self.activity()
                if "power" in tv and tv["power"] != self._tv_power:
                    was, self._tv_power = self._tv_power, tv["power"]
                    if tv["power"] == "on" and was is not None:
                        self.activity()  # someone turned the TV on: they're here
                    else:
                        self._poke.set()

    def _on_launcher(self) -> bool:
        return self._foreground in (None, "launcher")

    def _setting_changed(self, key: str, value) -> None:
        if key == TV_OFF or (key == DIM_AFTER and self._idle_after is None):
            self._publish()
            self._poke.set()

    def _set(self, idle: bool) -> None:
        if idle == self._idle:
            return
        self._idle = idle
        log.info("idle" if idle else "active")
        self._publish()
        for listener in list(self._listeners):
            listener(idle)

    def _publish(self) -> None:
        self.ctx.state.update(
            "idle",
            {"idle": self._idle, "after": self.idle_after, "tv_off_after": self.tv_off_after},
        )

    async def _activity_command(self, args: dict[str, Any]) -> None:
        self.activity()

    async def _now_command(self, args: dict[str, Any]) -> None:
        if not self._on_launcher():
            raise CommandError("Only on the launcher")
        self.go_idle()

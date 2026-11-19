"""The sleep timer (#79): the TV goes off by itself after a while, like a TV's own Sleep key.

Set from the quick menu's Sleep card, where OK goes Off, 15, 30, 60, 90 minutes and back to
Off (CHOICES; couchd/overlay.py), or with the `sleep.set` command (the phone, curl), which
takes any number of minutes up to MAX_MINUTES. Setting it again starts it again from now.

One minute before the end (WARN_BEFORE) a toast says so over whatever is on screen, the
launcher, a Chromium app or Plex, as every toast does: "Turning off in 1 minute", "Press any
button to stay on". It stays up for that minute. Any remote button in that minute turns
the timer off, and "Sleep timer off" takes the toast's place. That press does nothing else
(the overlay asks `take_key` before a key goes anywhere, as for the quick menu), except
Home, which still goes home. A key from a launcher screen that has the raw keys (Settings'
remote test) and a volume change (the TV remote's volume buttons) turn it off too, but go
through. On the launcher the warning also wakes the idle clock, which would hide it.

At the end it does what the idle switch-off does (couchd/idle.py): `tv.standby`, and as
the TV goes to standby TvService pauses whatever is playing (couchd/tv.py). If the TV goes
to standby before that (its own remote), the timer is off: there's nothing left to do.

It's in memory only: a couchd restart turns it off.

Live state section `sleep`:

    {"minutes": 30, "ends_at": "2026-11-19T21:20:00+01:00", "warning": false}

`minutes` is what was picked (null while it's off), `ends_at` when the TV goes off (local
time, ISO 8601; null while off), `warning` true in the last minute.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from couchd.commands import CommandError, arg
from couchd.services import Context, Service

log = logging.getLogger(__name__)

CHOICES = (15, 30, 60, 90)  # the Sleep card's, after Off
MAX_MINUTES = 240
WARN_BEFORE = 1  # minutes
TOAST_ID = "sleep"  # the warning, and what replaces it, are the same toast
WARNING = ("Turning off in 1 minute", "Press any button to stay on")
CANCELLED = "Sleep timer off"


def next_choice(minutes: int | None) -> int | None:
    """What OK on the Sleep card picks next: the next of CHOICES, then Off. A time the
    phone set that isn't one of them goes on to the next one up."""
    return next((c for c in CHOICES if c > (minutes or 0)), None)


def local_now() -> datetime:
    return datetime.now().astimezone()


class SleepService(Service):
    name = "sleep"

    def __init__(
        self,
        ctx: Context,
        *,
        now: Callable[[], datetime] = local_now,
        minute: float = 60.0,
    ) -> None:
        """`now()` gives the wall clock, for `ends_at`; `minute` is how many seconds a minute
        of the timer lasts (tests make it short)."""
        self.ctx = ctx
        self.now = now
        self.minute = minute
        self._minutes: int | None = None
        self._ends_at: datetime | None = None
        self._warning = False
        self._timer: asyncio.Task | None = None
        self._standby: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self.ctx.commands.add("sleep.set", self._set_command)
        self.ctx.commands.add("sleep.cycle", self._cycle_command)
        self._publish()
        self._watcher = asyncio.create_task(self._watch_bus())

    async def stop(self) -> None:
        self._running = False
        self.ctx.commands.remove_prefix("sleep.")
        for task in (self._timer, self._watcher, self._standby):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @property
    def minutes(self) -> int | None:
        return self._minutes

    @property
    def warning(self) -> bool:
        return self._warning

    # --- setting it --------------------------------------------------------------------

    def set(self, minutes: int | None) -> None:
        """Start it for `minutes` from now, or turn it off (None or 0)."""
        was_warning = self._warning
        self._stop_timer()
        if not minutes:
            self._minutes = self._ends_at = None
            log.info("sleep timer off")
            if was_warning:
                self.ctx.bus.toast(CANCELLED, icon="moon", id=TOAST_ID)
        else:
            self._minutes = minutes
            self._ends_at = self.now() + timedelta(minutes=minutes)
            self._timer = asyncio.create_task(self._run(minutes))
            log.info("sleep timer set", extra={"minutes": minutes})
            if was_warning:  # the warning's toast mustn't stay up saying the wrong thing
                self.ctx.bus.toast(f"TV off at {self._ends_at:%H:%M}", icon="moon", id=TOAST_ID)
        self._publish()

    def cycle(self) -> None:
        """The Sleep card's OK."""
        self.set(next_choice(self._minutes))

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._warning = False

    # --- keys (from the overlay, before a key goes anywhere) -------------------------------

    def take_key(self, key: str) -> bool:
        """In the last minute any key turns the timer off. True if that's all it does: Home
        still goes home."""
        if not (self._running and self._warning):
            return False
        log.info("sleep timer: a key, staying on", extra={"key": key})
        self.set(None)
        return key != "home"

    # --- the timer -----------------------------------------------------------------------

    async def _run(self, minutes: int) -> None:
        warn_at = max(0.0, (minutes - WARN_BEFORE) * self.minute)
        await asyncio.sleep(warn_at)
        self._warning = True
        self._publish()
        log.info("sleep timer: a minute to go")
        left = minutes * self.minute - warn_at
        title, detail = WARNING
        self.ctx.bus.toast(title, icon="moon", detail=detail, ms=round(left * 1000), id=TOAST_ID)
        with contextlib.suppress(CommandError):  # on the launcher, the idle clock would hide it
            await self.ctx.commands.run("idle.activity")
        await asyncio.sleep(left)
        self._timer = None  # done: setting it off from here mustn't cancel this task
        self._warning = False
        self._minutes = self._ends_at = None
        self._publish()
        self._standby = asyncio.create_task(self._tv_standby())

    async def _tv_standby(self) -> None:
        log.info("sleep timer: turning the TV off")
        try:
            await self.ctx.commands.run("tv.standby")
        except CommandError as e:
            log.warning("couldn't turn the TV off", extra={"error": str(e)})

    async def _watch_bus(self) -> None:
        """Keys that didn't come past `take_key` (the raw keys), volume changes, and the TV
        going to standby on its own."""
        with self.ctx.bus.subscribe() as sub:
            async for message in sub:
                kind, data = message.get("type"), message.get("data")
                if self._minutes is None or not isinstance(data, dict):
                    continue
                if kind == "key" and data.get("raw") and self._warning:
                    self.take_key(str(data.get("key")))
                elif kind == "volume_changed" and self._warning:
                    log.info("sleep timer: the volume changed, staying on")
                    self.set(None)
                elif kind == "state" and data.get("tv", {}).get("power") == "standby":
                    log.info("sleep timer: the TV went to standby")
                    self._stop_timer()
                    self._minutes = self._ends_at = None
                    self._publish()

    def _publish(self) -> None:
        ends_at = self._ends_at.isoformat(timespec="seconds") if self._ends_at else None
        self.ctx.state.update(
            "sleep", {"minutes": self._minutes, "ends_at": ends_at, "warning": self._warning}
        )

    # --- commands --------------------------------------------------------------------------

    async def _set_command(self, args: dict[str, Any]) -> dict[str, Any]:
        minutes = arg(args, "minutes", int)
        if not 0 <= minutes <= MAX_MINUTES:
            raise CommandError(f"'minutes' must be 0 (off) to {MAX_MINUTES}")
        self.set(minutes)
        return self.ctx.state.get("sleep")

    async def _cycle_command(self, args: dict[str, Any]) -> dict[str, Any]:
        self.cycle()
        return self.ctx.state.get("sleep")

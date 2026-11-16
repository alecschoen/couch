"""Startup progress, for the launcher's Startup screen (#19).

The launcher appears as soon as the API is up, while the rest of couchd is still starting
and the TV is waking up over CEC. This service watches both and publishes how far along
that is, so the Startup screen can show progress and "Turning on the TV", then hand over
to Home.

Live state section `startup`:
- `done`: true once every service has started (or failed) and the TV is on. Startup is
  never held up for long: the TV gets `TV_WAIT` seconds to say it's on (a TV that ignores
  CEC shouldn't keep Home away), and after `LIMIT` seconds startup is done whatever else is
  still going.
- `progress`: 0 to 1.
- `step`: what's being waited for: "tv" (the TV is turning on), "services", or null when done.

Add it first, so it can count the services added after it.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from couchd.services import Context, Service

log = logging.getLogger(__name__)

TV_WAIT = 15.0
LIMIT = 30.0
SETTLED = ("running", "failed")


class StartupService(Service):
    name = "startup"

    def __init__(
        self,
        ctx: Context,
        *,
        tv_wait: float = TV_WAIT,
        limit: float = LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctx = ctx
        self.tv_wait = tv_wait
        self.limit = limit
        self._clock = clock
        self._started = clock()
        self._task: asyncio.Task | None = None
        # Published at once, so a launcher that loads before services start knows to wait.
        self.ctx.state.update("startup", {"done": False, "progress": 0.0, "step": "services"})

    async def start(self) -> None:
        self._started = self._clock()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        with self.ctx.bus.subscribe() as sub:
            while not self._check():
                remaining = self._next_deadline() - self._clock()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(max(remaining, 0.01)):
                        # Any state change may move things along; we re-read state anyway.
                        while (message := await sub.get()) is not None:
                            if message.get("type") == "state":
                                break
                        else:
                            return

    def _next_deadline(self) -> float:
        elapsed = self._clock() - self._started
        deadlines = [self.limit, self.tv_wait] if elapsed < self.tv_wait else [self.limit]
        return self._started + min(deadlines)

    def _check(self) -> bool:
        """Publish progress. True once startup is done."""
        services = {k: v for k, v in self.ctx.state.get("services").items() if k != self.name}
        settled = sum(1 for status in services.values() if status in SETTLED)
        elapsed = self._clock() - self._started
        tv_ready = self._tv_ready(services) or elapsed >= self.tv_wait
        done = (tv_ready and settled == len(services)) or elapsed >= self.limit
        steps = len(services) + 1
        progress = 1.0 if done else (settled + tv_ready) / steps
        step = None if done else "services" if tv_ready else "tv"
        self.ctx.state.update(
            "startup", {"done": done, "progress": round(progress, 3), "step": step}
        )
        if done:
            log.info(
                "startup done",
                extra={"seconds": round(elapsed, 1), "tv": self.ctx.state.get("tv").get("power")},
            )
        return done

    def _tv_ready(self, services: dict[str, str]) -> bool:
        if services.get("tv") is None or services["tv"] == "failed":
            return True  # no TV to wait for
        return self.ctx.state.get("tv").get("power") == "on"

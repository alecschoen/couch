"""The TV, over HDMI-CEC. Its remote's buttons are couchd/remote.py's."""

import asyncio
import contextlib
import logging
from typing import Any

from couchd.backends import BackendError, TvBackend
from couchd.commands import arg
from couchd.services import Context, Service
from couchd.systemd import powering_off

log = logging.getLogger(__name__)

STANDBY_ON_POWEROFF = "tv.standby_on_poweroff"  # a setting (couchd/settings.py)


class TvService(Service):
    """Live state section `tv`: power ("on" | "standby" | "unknown"), active (the TV shows
    Couch), standby_on_poweroff (the setting).

    On start it wakes the TV and switches it to Couch. When the TV goes to standby, whatever
    is playing gets a Pause, and the UI can dim to the idle clock off `tv.power`.
    """

    name = "tv"

    def __init__(
        self, ctx: Context, backend: TvBackend, *, powering_off=powering_off, wake_on_start=True
    ) -> None:
        self.ctx = ctx
        self.backend = backend
        self._powering_off = powering_off
        self._wake_on_start = wake_on_start
        self._power = backend.power()
        self._wake: asyncio.Task | None = None
        self._unwatch = None

    async def start(self) -> None:
        await self.backend.start()
        self.backend.on_change(self._sync)
        self._unwatch = self.ctx.settings.on_change(
            lambda key, _: key == STANDBY_ON_POWEROFF and self._sync()
        )
        self._power = self.backend.power()
        self._sync()
        self.ctx.commands.add("tv.power_on", self._power_on)
        self.ctx.commands.add("tv.standby", self._standby)
        self.ctx.commands.add("tv.set_standby_on_poweroff", self._set_standby_on_poweroff)
        if self._wake_on_start:
            self._wake = asyncio.create_task(self._wake_tv())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("tv.")
        if self._unwatch is not None:
            self._unwatch()
        if self._wake is not None:
            self._wake.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake
        if self._standby_on_poweroff() and await self._powering_off():
            log.info("powering off, putting the TV in standby")
            try:
                await self.backend.standby()
            except BackendError as e:
                log.warning("couldn't put the TV in standby", extra={"error": str(e)})
        await self.backend.stop()

    async def _wake_tv(self) -> None:
        try:
            await self.backend.power_on()
        except BackendError as e:
            log.warning("couldn't wake the TV", extra={"error": str(e)})

    def _standby_on_poweroff(self) -> bool:
        return self.ctx.settings.get(STANDBY_ON_POWEROFF)

    def _sync(self) -> None:
        power = self.backend.power()
        if self._power == "on" and power == "standby":
            # Turning the TV off mid-film shouldn't leave the film running behind it.
            log.info("TV went to standby, pausing")
            self.backend.key_tapped("pause")
        self._power = power
        self.ctx.state.update(
            "tv",
            {
                "power": power,
                "active": self.backend.active(),
                "standby_on_poweroff": self._standby_on_poweroff(),
            },
        )

    async def _power_on(self, args: dict[str, Any]) -> None:
        await self.backend.power_on()

    async def _standby(self, args: dict[str, Any]) -> None:
        await self.backend.standby()

    async def _set_standby_on_poweroff(self, args: dict[str, Any]) -> None:
        # Kept for older callers; the same as settings.set tv.standby_on_poweroff.
        self.ctx.settings.set(STANDBY_ON_POWEROFF, arg(args, "on", bool))

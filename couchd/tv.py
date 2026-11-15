"""The TV (over HDMI-CEC) and the remote's buttons."""

import logging
from typing import Any

from couchd.backends import TvBackend
from couchd.commands import CommandError, arg
from couchd.keys import REMOTE_KEYS
from couchd.services import Context, Service

log = logging.getLogger(__name__)


class TvService(Service):
    """Live state section `tv`: {"power": "on" | "standby" | "unknown"}."""

    name = "tv"

    def __init__(self, ctx: Context, backend: TvBackend) -> None:
        self.ctx = ctx
        self.backend = backend

    async def start(self) -> None:
        await self.backend.start()
        self.backend.on_change(self._sync)
        self._sync()
        self.ctx.commands.add("tv.power_on", self._power_on)
        self.ctx.commands.add("tv.standby", self._standby)

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("tv.")
        await self.backend.stop()

    def _sync(self) -> None:
        self.ctx.state.update("tv", {"power": self.backend.power()})

    async def _power_on(self, args: dict[str, Any]) -> None:
        await self.backend.power_on()

    async def _standby(self, args: dict[str, Any]) -> None:
        await self.backend.standby()


class RemoteService(Service):
    """Turns remote buttons into `key` messages for the UI.

    Buttons come from the TV backend (CEC), or from `remote.press`, which is how the dev-mode
    keyboard and a USB keyboard on the Pi act as the remote. Per-app routing and Back-hold =
    Home arrive with #8.
    """

    name = "remote"

    def __init__(self, ctx: Context, tv: TvBackend | None = None) -> None:
        self.ctx = ctx
        self.tv = tv

    async def start(self) -> None:
        if self.tv is not None:
            self.tv.on_key(self.press)
        self.ctx.commands.add("remote.press", self._press_command)

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("remote.")

    def press(self, key: str) -> None:
        if key not in REMOTE_KEYS:
            log.warning("unknown remote key ignored", extra={"key": key})
            return
        self.ctx.bus.publish("key", {"key": key})

    async def _press_command(self, args: dict[str, Any]) -> None:
        key = arg(args, "key", str)
        if key not in REMOTE_KEYS:
            raise CommandError(f"unknown key {key!r}")
        self.press(key)

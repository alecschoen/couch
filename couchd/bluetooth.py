"""Bluetooth speakers and headphones. The BlueZ backend lands with #12."""

from dataclasses import asdict
from typing import Any

from couchd.backends import BluetoothBackend
from couchd.commands import arg
from couchd.services import Context, Service


class BluetoothService(Service):
    """Live state section `bluetooth`: scanning, and devices (paired first, then by name)."""

    name = "bluetooth"

    def __init__(self, ctx: Context, backend: BluetoothBackend) -> None:
        self.ctx = ctx
        self.backend = backend

    async def start(self) -> None:
        await self.backend.start()
        self.backend.on_change(self._sync)
        self._sync()
        commands = self.ctx.commands
        commands.add("bluetooth.scan", self._scan)
        for action in ("pair", "connect", "disconnect", "forget"):
            commands.add(f"bluetooth.{action}", self._device_action(action))

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("bluetooth.")
        await self.backend.stop()

    def _sync(self) -> None:
        devices = sorted(self.backend.devices(), key=lambda d: (not d.paired, d.name.lower()))
        self.ctx.state.update(
            "bluetooth",
            {"scanning": self.backend.scanning(), "devices": [asdict(d) for d in devices]},
        )

    async def _scan(self, args: dict[str, Any]) -> None:
        if arg(args, "on", bool):
            await self.backend.start_scan()
        else:
            await self.backend.stop_scan()

    def _device_action(self, action: str):
        async def handler(args: dict[str, Any]) -> None:
            await getattr(self.backend, action)(arg(args, "address", str))

        return handler

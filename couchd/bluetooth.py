"""Bluetooth speakers and headphones: pairing, connecting, and sound that follows them.

The backend does the radio: BlueZ on the Pi (couchd/bluez.py), a fake in dev. This service
adds what the couch needs on top, the same for both:

- `bluetooth.pair` pairs, trusts and then connects an audio device: one press on the remote.
- When an audio device connects, sound moves to it once its output turns up in `audio`
  (an `audio.set_output`, so it becomes the chosen output). When it drops, where the sound
  goes is AudioService's rule, not ours, and its "Sound now on X" toast says so. Only a
  device the sound wasn't on gets a "X disconnected" toast, so one drop is one toast.
- The last audio device is remembered. At boot, and whenever it isn't connected, couchd
  keeps trying to reach it (backing off to once a minute), so a speaker that is switched
  back on reconnects by itself. Disconnecting it on purpose or forgetting it stops that, and
  so does turning the `auto_reconnect` setting off.
- Scanning stops by itself after SCAN_LIMIT seconds, in case the screen that asked is gone.
"""

import asyncio
import logging
import re
from collections.abc import Coroutine, Sequence
from dataclasses import asdict
from typing import Any

from couchd.audio import SAVED_OUTPUT
from couchd.backends import BackendError, BluetoothBackend, Device
from couchd.commands import CommandError, arg
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SAVED_LAST = "bluetooth.last"
AUTO_RECONNECT = "bluetooth.auto_reconnect"  # a setting (couchd/settings.py)

SCAN_LIMIT = 120.0
# How long a freshly connected speaker may take to show up as an audio output.
OUTPUT_WAIT = 15.0
# Seconds between reconnect attempts; the last one repeats.
RECONNECT_DELAYS = (0.0, 5.0, 10.0, 20.0, 30.0, 60.0)


class BluetoothService(Service):
    """Live state section `bluetooth`: scanning, devices (paired first, then by name), and
    auto_reconnect."""

    name = "bluetooth"

    def __init__(
        self,
        ctx: Context,
        backend: BluetoothBackend,
        *,
        reconnect_delays: Sequence[float] = RECONNECT_DELAYS,
        scan_limit: float = SCAN_LIMIT,
        output_wait: float = OUTPUT_WAIT,
    ) -> None:
        self.ctx = ctx
        self.backend = backend
        self.reconnect_delays = tuple(reconnect_delays)
        self.scan_limit = scan_limit
        self.output_wait = output_wait
        self._connected: dict[str, Device] = {}
        # One radio operation at a time: user commands and reconnect attempts take turns.
        self._busy = asyncio.Lock()
        self._poke = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._scan_timer: asyncio.Task | None = None
        self._unwatch = None

    async def start(self) -> None:
        await self.backend.start()
        # Devices connected before couchd (re)started aren't news: leave the sound alone.
        self._connected = {d.address: d for d in self.backend.devices() if d.connected}
        self.backend.on_change(self._sync)
        self._unwatch = self.ctx.settings.on_change(
            lambda key, _: key == AUTO_RECONNECT and self._sync()
        )
        self._sync()
        commands = self.ctx.commands
        commands.add("bluetooth.scan", self._scan)
        commands.add("bluetooth.pair", self._pair)
        commands.add("bluetooth.connect", self._connect)
        commands.add("bluetooth.disconnect", self._disconnect)
        commands.add("bluetooth.forget", self._forget)
        commands.add("bluetooth.set_auto_reconnect", self._set_auto_reconnect)
        self._spawn(self._reconnect_loop())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("bluetooth.")
        if self._unwatch is not None:
            self._unwatch()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.backend.stop()

    @property
    def auto_reconnect(self) -> bool:
        return self.ctx.settings.get(AUTO_RECONNECT)

    # --- state -----------------------------------------------------------------------------

    def _sync(self) -> None:
        devices = sorted(self.backend.devices(), key=lambda d: (not d.paired, d.name.lower()))
        self.ctx.state.update(
            "bluetooth",
            {
                "scanning": self.backend.scanning(),
                "devices": [asdict(d) for d in devices],
                "auto_reconnect": self.auto_reconnect,
            },
        )
        now = {d.address: d for d in devices if d.connected}
        for address, device in now.items():
            if address not in self._connected:
                self._on_connected(device)
        for address, device in self._connected.items():
            if address not in now:
                self._on_disconnected(device)
        self._connected = now
        self._poke.set()

    def _device(self, address: str) -> Device | None:
        return next((d for d in self.backend.devices() if d.address == address), None)

    # --- sound follows the speaker ---------------------------------------------------------

    def _on_connected(self, device: Device) -> None:
        log.info("device connected", extra={"device": device.address})
        if device.audio:
            self.ctx.saved.set(SAVED_LAST, device.address)
            self._spawn(self._switch_to(device))

    def _on_disconnected(self, device: Device) -> None:
        log.info("device disconnected", extra={"device": device.address})
        chosen = self.ctx.saved.get(SAVED_OUTPUT)
        carried_sound = isinstance(chosen, str) and _output_matches(chosen, device.address)
        if device.audio and not carried_sound:
            self.ctx.bus.toast(f"{device.name} disconnected", icon="bluetooth")

    async def _switch_to(self, device: Device) -> None:
        output = await self._wait_for_output(device)
        if output is None:
            log.warning("no audio output for device", extra={"device": device.address})
            return
        if self.ctx.state.get("audio").get("current") != output:
            await self._run("audio.set_output", output=output)

    async def _wait_for_output(self, device: Device) -> str | None:
        try:
            async with asyncio.timeout(self.output_wait):
                while True:
                    for output in self.ctx.state.get("audio").get("outputs", []):
                        if (
                            output.get("kind") == "bluetooth"
                            and output.get("available", True)
                            and _output_matches(output["id"], device.address)
                        ):
                            return output["id"]
                    await asyncio.sleep(0.2)
        except TimeoutError:
            return None

    async def _run(self, command: str, **args: Any) -> None:
        try:
            await self.ctx.commands.run(command, args)
        except CommandError as e:
            log.warning("couldn't move sound", extra={"command": command, "error": str(e)})

    # --- auto-reconnect --------------------------------------------------------------------

    def _reconnect_target(self) -> str | None:
        if not self.auto_reconnect or self.backend.scanning():
            return None
        last = self.ctx.saved.get(SAVED_LAST)
        if not last:
            return None
        devices = self.backend.devices()
        if any(d.connected and d.audio for d in devices):
            return None
        device = next((d for d in devices if d.address == last), None)
        return last if device is not None and device.paired else None

    async def _reconnect_loop(self) -> None:
        attempt = 0
        while True:
            if self._reconnect_target() is None:
                attempt = 0
                self._poke.clear()
                await self._poke.wait()
                continue
            await asyncio.sleep(self.reconnect_delays[min(attempt, len(self.reconnect_delays) - 1)])
            attempt += 1
            async with self._busy:
                target = self._reconnect_target()
                if target is None:
                    continue
                try:
                    await self.backend.connect(target)
                except BackendError as e:
                    log.debug("reconnect failed", extra={"device": target, "error": str(e)})
                else:
                    log.info("reconnected", extra={"device": target})

    # --- commands --------------------------------------------------------------------------

    async def _scan(self, args: dict[str, Any]) -> None:
        if arg(args, "on", bool):
            await self.backend.start_scan()
            if self._scan_timer is not None:
                self._scan_timer.cancel()
            self._scan_timer = self._spawn(self._stop_scan_later())
        else:
            if self._scan_timer is not None:
                self._scan_timer.cancel()
                self._scan_timer = None
            await self.backend.stop_scan()

    async def _stop_scan_later(self) -> None:
        await asyncio.sleep(self.scan_limit)
        self._scan_timer = None
        log.info("scan ran too long, stopping it")
        await self.backend.stop_scan()

    async def _pair(self, args: dict[str, Any]) -> None:
        address = arg(args, "address", str)
        async with self._busy:
            await self.backend.pair(address)
            device = self._device(address)
            # Even if it already shows as connected: pairing brings the link up but not the
            # sound, and the speaker hangs up again if nothing follows.
            if device is not None and device.audio:
                await self.backend.connect(address)

    async def _connect(self, args: dict[str, Any]) -> None:
        address = arg(args, "address", str)
        async with self._busy:
            await self.backend.connect(address)

    async def _disconnect(self, args: dict[str, Any]) -> None:
        address = arg(args, "address", str)
        async with self._busy:
            await self.backend.disconnect(address)
        # Disconnected on purpose: don't pull it straight back.
        self._clear_last(address)

    async def _forget(self, args: dict[str, Any]) -> None:
        address = arg(args, "address", str)
        async with self._busy:
            await self.backend.forget(address)
        self._clear_last(address)

    async def _set_auto_reconnect(self, args: dict[str, Any]) -> None:
        # Kept for older callers; the same as settings.set bluetooth.auto_reconnect.
        self.ctx.settings.set(AUTO_RECONNECT, arg(args, "on", bool))

    def _clear_last(self, address: str) -> None:
        if self.ctx.saved.get(SAVED_LAST) == address:
            self.ctx.saved.set(SAVED_LAST, None)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Bluetooth task failed", exc_info=task.exception())


def _output_matches(output_id: str, address: str) -> bool:
    """Is this audio output that device? Matches `bluetooth:AA:BB:...` as well as PipeWire
    names like `bluez_output.AA_BB_CC_DD_EE_FF.1`."""
    wanted = re.sub(r"[^0-9A-F]", "", address.upper())
    return bool(wanted) and wanted in re.sub(r"[^0-9A-F]", "", output_id.upper())

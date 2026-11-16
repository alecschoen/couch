"""The Pi itself: what Settings > About shows, and Settings > Power's Restart and Turn off.

Commands:
- `system.info` → `{"name", "device", "network": {"kind", "name", "address"} | null}`
- `system.restart`, `system.power_off`: reboot or shut down the Pi. On a shutdown the TV
  goes to standby too when the `tv.standby_on_poweroff` setting is on (couchd/tv.py).

couchd runs as the `couch` user; `system/couch.rules` lets it ask logind for these two.
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from couchd.backends import BackendError
from couchd.cec_tv import OSD_NAME
from couchd.services import Context, Service

log = logging.getLogger(__name__)

MODEL_FILE = Path("/proc/device-tree/model")
SYS_NET = Path("/sys/class/net")
# Any public address will do: `ip route get` only asks the kernel which way it would go.
PROBE_ADDRESS = "1.1.1.1"
# Give the UI a moment to show "Restarting" before the screen goes.
POWER_DELAY = 1.0


class SystemBackend(ABC):
    @abstractmethod
    async def device(self) -> str: ...

    @abstractmethod
    async def network(self) -> dict[str, Any] | None:
        """{"kind": "wifi" | "ethernet", "name": SSID or None, "address": IPv4} or None."""

    @abstractmethod
    async def restart(self) -> None: ...

    @abstractmethod
    async def power_off(self) -> None: ...


class SystemService(Service):
    name = "system"

    def __init__(self, ctx: Context, backend: SystemBackend, *, power_delay=POWER_DELAY) -> None:
        self.ctx = ctx
        self.backend = backend
        self.power_delay = power_delay
        self._pending: asyncio.Task | None = None

    async def start(self) -> None:
        self.ctx.commands.add("system.info", self._info)
        self.ctx.commands.add("system.restart", self._restart)
        self.ctx.commands.add("system.power_off", self._power_off)

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("system.")

    async def _info(self, args: dict[str, Any]) -> dict[str, Any]:
        device, network = await asyncio.gather(self.backend.device(), self.backend.network())
        return {"name": OSD_NAME, "device": device, "network": network}

    async def _restart(self, args: dict[str, Any]) -> None:
        self._later(self.backend.restart, "Restarting")

    async def _power_off(self, args: dict[str, Any]) -> None:
        self._later(self.backend.power_off, "Turning off")

    def _later(self, action, what: str) -> None:
        if self._pending is not None and not self._pending.done():
            return
        log.info(what.lower())
        self.ctx.bus.toast(what)

        async def go() -> None:
            await asyncio.sleep(self.power_delay)
            try:
                await action()
            except BackendError as e:
                log.warning("power action failed", extra={"error": str(e)})
                self.ctx.bus.toast(str(e), "error")

        self._pending = asyncio.create_task(go())


async def _run(*argv: str, limit: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as e:
        return 127, "", str(e)
    try:
        async with asyncio.timeout(limit):
            out, err = await proc.communicate()
    except TimeoutError:
        proc.kill()
        return 124, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def pretty_model(raw: str) -> str:
    """'Raspberry Pi 4 Model B Rev 1.5\\0' → 'Raspberry Pi 4 Model B'."""
    model = raw.replace("\0", "").strip()
    head, sep, _ = model.rpartition(" Rev ")
    return head if sep else model


def parse_route(text: str) -> tuple[str, str] | None:
    """(device, source address) from `ip -j route get` output."""
    try:
        routes = json.loads(text)
    except ValueError:
        return None
    if not routes or not isinstance(routes[0], dict):
        return None
    dev, src = routes[0].get("dev"), routes[0].get("prefsrc")
    return (dev, src) if dev and src else None


def parse_iw_ssid(text: str) -> str | None:
    """The SSID from `iw dev wlan0 link`."""
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "SSID" and value.strip():
            return value.strip()
    return None


class LinuxSystem(SystemBackend):
    async def device(self) -> str:
        try:
            raw = await asyncio.to_thread(MODEL_FILE.read_text, errors="replace")
            return pretty_model(raw)
        except OSError:
            return "Unknown device"

    async def network(self) -> dict[str, Any] | None:
        code, out, _ = await _run("ip", "-j", "-4", "route", "get", PROBE_ADDRESS)
        route = parse_route(out) if code == 0 else None
        if route is None:
            return None
        dev, address = route
        if not (SYS_NET / dev / "wireless").exists():
            return {"kind": "ethernet", "name": None, "address": address}
        return {"kind": "wifi", "name": await self._ssid(dev), "address": address}

    async def _ssid(self, dev: str) -> str | None:
        # iw isn't always installed; NetworkManager (Raspberry Pi OS) names the connection
        # after the network, which is close enough for About.
        code, out, _ = await _run("iw", "dev", dev, "link")
        if code == 0 and (ssid := parse_iw_ssid(out)):
            return ssid
        code, out, _ = await _run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", dev)
        return (out.strip() or None) if code == 0 else None

    async def restart(self) -> None:
        await self._systemctl("reboot")

    async def power_off(self) -> None:
        await self._systemctl("poweroff")

    async def _systemctl(self, verb: str) -> None:
        code, _, err = await _run("systemctl", "--no-ask-password", verb)
        if code != 0:
            log.warning("systemctl failed", extra={"verb": verb, "error": err.strip()})
            raise BackendError("Couch isn't allowed to do that. Check the polkit rule.")


class FakeSystem(SystemBackend):
    """Dev mode: made-up details, and power actions that only say what they'd do."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def device(self) -> str:
        return "Raspberry Pi 4 Model B (pretend)"

    async def network(self) -> dict[str, Any] | None:
        return {"kind": "wifi", "name": "Living room", "address": "192.168.1.42"}

    async def restart(self) -> None:
        self.calls.append("restart")
        log.info("dev mode: would reboot now")

    async def power_off(self) -> None:
        self.calls.append("power_off")
        log.info("dev mode: would power off now")

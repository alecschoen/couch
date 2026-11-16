"""Wi-Fi on the Pi: NetworkManager over the system D-Bus, with dbus-fast (#31).

Only the radio: on or off, and while it's on, which network it's on and how strong the
signal is. Joining a network is NetworkManager's own business; a network it already knows
reconnects by itself once the radio is on.

The D-Bus side is couchd/bluez.py's `DBusLink`, pointed at NetworkManager. Tests swap in an
in-memory NetworkManager (tests/fake_nm.py).

Turning the radio off is NetworkManager's `WirelessEnabled = false`. NetworkManager then
soft-blocks the radio (rfkill) and remembers the choice in its state file, which
`couch-wifi-boot` reads at the next boot (see system/README.md, "Wi-Fi").
"""

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any

from dbus_fast import Variant

from couchd.backends import BackendError, WifiBackend, WifiConnection, WifiStatus
from couchd.bluez import BluezError, BluezLink, DBusLink

log = logging.getLogger(__name__)

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
DEVICE = "org.freedesktop.NetworkManager.Device"
WIRELESS = "org.freedesktop.NetworkManager.Device.Wireless"
ACCESS_POINT = "org.freedesktop.NetworkManager.AccessPoint"
PROPERTIES = "org.freedesktop.DBus.Properties"

DEVICE_TYPE_WIFI = 2
# NMDeviceState: 20 unavailable, 30 disconnected, 40-90 on the way, 100 activated
DISCONNECTED = 30
ACTIVATED = 100
WIFI_CONNECTION_TYPE = "802-11-wireless"

SET_TIMEOUT = 15.0


class NmWifi(WifiBackend):
    def __init__(self, link: BluezLink | None = None) -> None:
        super().__init__()
        self.link = link if link is not None else DBusLink(service=NM)
        self._status = WifiStatus(available=False, enabled=False)
        self._device: str | None = None  # the Wi-Fi device's object path
        self._tasks: set[asyncio.Task] = set()
        self._reading = asyncio.Lock()
        self._again = False

    async def start(self) -> None:
        try:
            await self.link.open()
        except Exception as e:  # no system bus at all
            raise BackendError(f"no D-Bus system bus: {e}") from e
        self.link.on_signal(self._on_signal)
        self.link.on_presence(self._on_presence)
        if self.link.bluez_running():
            await self._refresh()
        else:
            log.warning("NetworkManager isn't running; Wi-Fi waits for it")

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.link.close()

    def status(self) -> WifiStatus:
        return self._status

    async def set_enabled(self, on: bool) -> None:
        log.info("turning Wi-Fi %s", "on" if on else "off")
        try:
            await self.link.call(
                NM_PATH,
                PROPERTIES,
                "Set",
                "ssv",
                [NM, "WirelessEnabled", Variant("b", on)],
                max_wait=SET_TIMEOUT,
            )
        except (BluezError, TimeoutError) as e:
            log.warning("couldn't switch Wi-Fi", extra={"on": on, "error": str(e)})
            if isinstance(e, BluezError) and "PermissionDenied" in e.name:
                raise BackendError("Couch isn't allowed to switch the Wi-Fi") from e
            raise BackendError(f"Couldn't turn the Wi-Fi {'on' if on else 'off'}") from e
        await self._refresh()

    # --- reading NetworkManager ----------------------------------------------------------

    def _on_presence(self, running: bool) -> None:
        if running:
            self._spawn(self._refresh())
        else:
            log.warning("NetworkManager went away")
            self._device = None
            self._set(WifiStatus(available=False, enabled=False))

    def _on_signal(self, path: str, interface: str, member: str, body: list[Any]) -> None:
        if interface == NM and member in ("DeviceAdded", "DeviceRemoved"):
            self._spawn(self._refresh())
        elif interface == PROPERTIES and member == "PropertiesChanged":
            changed_interface = body[0] if body else None
            if changed_interface in (NM, DEVICE, WIRELESS, ACCESS_POINT):
                self._spawn(self._refresh())

    async def _refresh(self) -> None:
        """Read everything again. Signals come in bursts: while a refresh runs, more asks
        only make it go round once more."""
        self._again = True
        if self._reading.locked():
            return
        async with self._reading:
            while self._again:
                self._again = False
                try:
                    self._set(await self._read())
                except (BluezError, TimeoutError) as e:
                    log.warning("couldn't read Wi-Fi state", extra={"error": str(e)})

    async def _read(self) -> WifiStatus:
        nm = await self._get_all(NM_PATH, NM)
        self._device = await self._find_device()
        if self._device is None:
            return WifiStatus(available=False, enabled=bool(nm.get("WirelessEnabled")))
        enabled = bool(nm.get("WirelessEnabled")) and bool(nm.get("WirelessHardwareEnabled", True))
        primary = nm.get("PrimaryConnectionType") == WIFI_CONNECTION_TYPE
        if not enabled:
            return WifiStatus(available=True, enabled=False, primary=primary)
        device = await self._get_all(self._device, DEVICE)
        state = int(device.get("State", 0))
        connection = _connection(state)
        ssid = signal = None
        if connection in ("connecting", "connected"):
            wireless = await self._get_all(self._device, WIRELESS)
            ap = wireless.get("ActiveAccessPoint")
            if ap and ap != "/":
                with contextlib.suppress(BluezError):  # the AP can vanish between calls
                    props = await self._get_all(ap, ACCESS_POINT)
                    ssid = _ssid(props.get("Ssid"))
                    if connection == "connected" and "Strength" in props:
                        signal = int(props["Strength"])
        return WifiStatus(
            available=True,
            enabled=True,
            connection=connection,
            ssid=ssid,
            signal=signal,
            primary=primary,
        )

    async def _find_device(self) -> str | None:
        (paths,) = await self.link.call(NM_PATH, NM, "GetDevices")
        for path in paths:
            with contextlib.suppress(BluezError):  # gone already
                (kind,) = await self.link.call(
                    path, PROPERTIES, "Get", "ss", [DEVICE, "DeviceType"]
                )
                if kind == DEVICE_TYPE_WIFI:
                    return path
        return None

    async def _get_all(self, path: str, interface: str) -> dict[str, Any]:
        (props,) = await self.link.call(path, PROPERTIES, "GetAll", "s", [interface])
        return props

    def _set(self, status: WifiStatus) -> None:
        if status != self._status:
            self._status = status
            self.changed()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _connection(state: int) -> WifiConnection:
    if state == ACTIVATED:
        return "connected"
    if DISCONNECTED < state < ACTIVATED:
        return "connecting"
    return "disconnected"  # also unavailable and deactivating: the radio is on, no network


def _ssid(raw: Any) -> str | None:
    """An SSID is bytes (`ay`), usually UTF-8."""
    if not raw:
        return None
    return bytes(raw).decode("utf-8", errors="replace")

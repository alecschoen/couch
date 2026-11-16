"""An in-memory NetworkManager behind the BluezLink interface, for testing NmWifi."""

import asyncio
import copy
from typing import Any

from couchd.bluez import BluezError, BluezLink, _plain
from couchd.networkmanager import ACCESS_POINT, DEVICE, NM, NM_PATH, PROPERTIES, WIRELESS

ETH0 = "/org/freedesktop/NetworkManager/Devices/1"
WLAN0 = "/org/freedesktop/NetworkManager/Devices/2"
AP = "/org/freedesktop/NetworkManager/AccessPoint/7"


class FakeNm(BluezLink):
    def __init__(self, *, running: bool = True, wifi: bool = True, enabled: bool = False) -> None:
        self.running = running
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            NM_PATH: {
                NM: {
                    "WirelessEnabled": enabled,
                    "WirelessHardwareEnabled": True,
                    "PrimaryConnectionType": "802-3-ethernet",
                }
            },
            ETH0: {DEVICE: {"DeviceType": 1, "Interface": "eth0", "State": 100}},
        }
        if wifi:
            self.add_wifi()
        self.calls: list[tuple[str, str, Any]] = []
        self.fail: dict[tuple[str, str], Exception] = {}
        self._signal_handlers = []
        self._presence_handlers = []

    # BluezLink

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def bluez_running(self) -> bool:
        return self.running

    def on_signal(self, handler) -> None:
        self._signal_handlers.append(handler)

    def on_presence(self, handler) -> None:
        self._presence_handlers.append(handler)

    def export_agent(self, path, agent) -> None:
        raise AssertionError("NetworkManager has no agent")

    async def call(self, path, interface, member, signature="", body=(), *, max_wait=10.0):
        body = _plain(list(body))
        await asyncio.sleep(0)  # a real bus call always yields
        self.calls.append((path, member, body))
        if not self.running:
            raise BluezError("org.freedesktop.DBus.Error.ServiceUnknown")
        if (path, member) in self.fail:
            raise self.fail[(path, member)]
        return getattr(self, f"_{member}")(path, *body)

    # test controls

    def sets(self) -> list[bool]:
        """Every WirelessEnabled couchd asked for."""
        return [body[2] for _, member, body in self.calls if member == "Set"]

    def add_wifi(self) -> None:
        self.objects[WLAN0] = {
            DEVICE: {"DeviceType": 2, "Interface": "wlan0", "State": 20},
            WIRELESS: {"ActiveAccessPoint": "/"},
        }
        if self._ready():
            self._signal(NM_PATH, NM, "DeviceAdded", [WLAN0])

    def join(self, ssid: str = "Living room", strength: int = 64, *, state: int = 100) -> None:
        """The radio found a network it knows (state 100: connected; 70: getting an address)."""
        self.objects[AP] = {ACCESS_POINT: {"Ssid": ssid.encode(), "Strength": strength}}
        self.update(WLAN0, WIRELESS, ActiveAccessPoint=AP)
        self.update(WLAN0, DEVICE, State=state)

    def update(self, path: str, interface: str, **changed: Any) -> None:
        self.objects[path][interface].update(changed)
        self._signal(path, PROPERTIES, "PropertiesChanged", [interface, dict(changed), []])

    def stop_running(self) -> None:
        self.running = False
        for handler in list(self._presence_handlers):
            handler(False)

    def start_running(self) -> None:
        self.running = True
        for handler in list(self._presence_handlers):
            handler(True)

    def _ready(self) -> bool:
        return hasattr(self, "_signal_handlers")

    def _signal(self, path: str, interface: str, member: str, body: list[Any]) -> None:
        for handler in list(self._signal_handlers):
            handler(path, interface, member, copy.deepcopy(body))

    # NetworkManager methods

    def _GetDevices(self, path):
        return [[p for p, ifaces in self.objects.items() if DEVICE in ifaces]]

    def _Get(self, path, interface, name):
        try:
            return [self.objects[path][interface][name]]
        except KeyError:
            raise BluezError("org.freedesktop.DBus.Error.UnknownObject", path) from None

    def _GetAll(self, path, interface):
        try:
            return [copy.deepcopy(self.objects[path][interface])]
        except KeyError:
            raise BluezError("org.freedesktop.DBus.Error.UnknownObject", path) from None

    def _Set(self, path, interface, name, value):
        assert (path, interface, name) == (NM_PATH, NM, "WirelessEnabled")
        nm = self.objects[NM_PATH][NM]
        nm["WirelessEnabled"] = value
        if not value and WLAN0 in self.objects:
            self.objects[WLAN0][WIRELESS]["ActiveAccessPoint"] = "/"
            self.objects[WLAN0][DEVICE]["State"] = 20
            if nm["PrimaryConnectionType"] == "802-11-wireless":
                nm["PrimaryConnectionType"] = ""
        self._signal(path, PROPERTIES, "PropertiesChanged", [NM, {"WirelessEnabled": value}, []])
        return []

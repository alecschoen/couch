"""An in-memory NetworkManager behind the BluezLink interface, for testing NmWifi.

It has an Ethernet device with a saved wired profile (which couchd must never touch), and a
Wi-Fi device with access points (`add_ap`), saved profiles (`add_profile`) and joining:
activating a profile ends connected, or failed the way NetworkManager fails (a wrong
password, out of range), after a moment.
"""

import asyncio
import copy
from typing import Any

from couchd.bluez import BluezError, BluezLink, _plain
from couchd.networkmanager import (
    ACCESS_POINT,
    ACTIVE,
    CONNECTION,
    DEVICE,
    NM,
    NM_PATH,
    PROPERTIES,
    SETTINGS,
    SETTINGS_PATH,
    WIRELESS,
)

ETH0 = "/org/freedesktop/NetworkManager/Devices/1"
WLAN0 = "/org/freedesktop/NetworkManager/Devices/2"
AP = "/org/freedesktop/NetworkManager/AccessPoint/7"
WIRED = "/org/freedesktop/NetworkManager/Settings/1"

# Access point security, as the flags NetworkManager reports: (Flags, WpaFlags, RsnFlags)
SECURITY = {
    "open": (0, 0, 0),
    "psk": (1, 0, 0x100),
    "wpa3": (1, 0, 0x400),
    "transition": (1, 0, 0x500),
    "owe": (0, 0, 0x800),
    "enterprise": (1, 0, 0x200),
    "wep": (1, 0, 0),
}


def _mentions(value: Any, path: str) -> bool:
    if isinstance(value, dict):
        return any(_mentions(k, path) or _mentions(v, path) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_mentions(v, path) for v in value)
    return value == path


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
            SETTINGS_PATH: {SETTINGS: {}},
        }
        # Saved profiles: path -> settings, secrets and all (GetSettings leaves them out).
        self.profiles: dict[str, dict[str, dict[str, Any]]] = {
            WIRED: {
                "connection": {"id": "Wired connection 1", "type": "802-3-ethernet"},
                "802-3-ethernet": {},
            }
        }
        self.calls: list[tuple[str, str, Any]] = []
        self.fail: dict[tuple[str, str], Exception] = {}
        # Joining: each network's password, the ones too far away, and whether joining
        # never finishes.
        self.passwords: dict[str, str] = {}
        self.out_of_range: set[str] = set()
        self.hang = False
        self.scan_busy = False
        self._next = 100
        self._signal_handlers = []
        self._presence_handlers = []
        if wifi:
            self.add_wifi()

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

    def touched(self, path: str, members: tuple[str, ...] | None = None) -> bool:
        """Did couchd call anything on this object, or pass it to a call? (Only `members`,
        if given.)"""
        return any(
            (members is None or member in members) and (p == path or _mentions(body, path))
            for p, member, body in self.calls
        )

    def add_wifi(self) -> None:
        self.objects[WLAN0] = {
            DEVICE: {"DeviceType": 2, "Interface": "wlan0", "State": 20},
            WIRELESS: {"ActiveAccessPoint": "/", "AccessPoints": [], "LastScan": 1000},
        }
        if self._ready():
            self._signal(NM_PATH, NM, "DeviceAdded", [WLAN0])

    def add_ap(
        self,
        ssid: str | bytes,
        strength: int = 50,
        security: str = "psk",
        *,
        path: str | None = None,
        mode: int = 2,
    ) -> str:
        """An access point the radio sees."""
        path = path or self._path("AccessPoint")
        flags, wpa, rsn = SECURITY[security]
        raw = ssid.encode() if isinstance(ssid, str) else ssid
        self.objects[path] = {
            ACCESS_POINT: {
                "Ssid": raw,
                "Strength": strength,
                "Flags": flags,
                "WpaFlags": wpa,
                "RsnFlags": rsn,
                "Mode": mode,
            }
        }
        aps = self.objects[WLAN0][WIRELESS]["AccessPoints"]
        if path not in aps:
            aps.append(path)
            self._signal(WLAN0, WIRELESS, "AccessPointAdded", [path])
        return path

    def remove_ap(self, path: str) -> None:
        self.objects.pop(path, None)
        self.objects[WLAN0][WIRELESS]["AccessPoints"].remove(path)
        self._signal(WLAN0, WIRELESS, "AccessPointRemoved", [path])

    def add_profile(
        self, ssid: str, *, psk: str | None = None, kind: str = "802-11-wireless", mode=None
    ) -> str:
        """A profile NetworkManager has saved (set up some other way)."""
        path = self._path("Settings")
        settings: dict[str, dict[str, Any]] = {
            "connection": {"id": ssid, "type": kind},
            kind: {"ssid": ssid.encode()},
        }
        if mode is not None:
            settings[kind]["mode"] = mode
        if psk is not None:
            settings["802-11-wireless-security"] = {"key-mgmt": "wpa-psk", "psk": psk}
        self.profiles[path] = settings
        self._signal(SETTINGS_PATH, SETTINGS, "NewConnection", [path])
        return path

    def wifi_profiles(self) -> dict[str, dict[str, Any]]:
        return {
            p: s for p, s in self.profiles.items() if s["connection"]["type"] == "802-11-wireless"
        }

    def finish_scan(self) -> None:
        wireless = self.objects[WLAN0][WIRELESS]
        self.update(WLAN0, WIRELESS, LastScan=wireless["LastScan"] + 1)

    def join(self, ssid: str = "Living room", strength: int = 64, *, state: int = 100) -> None:
        """The radio found a network it knows (state 100: connected; 70: getting an address)."""
        self.add_ap(ssid, strength, "open", path=AP)
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
        for handler in list(getattr(self, "_signal_handlers", ())):
            handler(path, interface, member, copy.deepcopy(body))

    def _path(self, kind: str) -> str:
        self._next += 1
        return f"/org/freedesktop/NetworkManager/{kind}/{self._next}"

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

    def _RequestScan(self, path, options):
        assert path == WLAN0
        if self.scan_busy:
            raise BluezError(
                "org.freedesktop.NetworkManager.Device.NotAllowed",
                "Scanning not allowed while already scanning",
            )
        return []

    def _ListConnections(self, path):
        assert path == SETTINGS_PATH
        return [list(self.profiles)]

    def _GetSettings(self, path):
        if path not in self.profiles:
            raise BluezError("org.freedesktop.DBus.Error.UnknownObject", path)
        settings = copy.deepcopy(self.profiles[path])
        settings.get("802-11-wireless-security", {}).pop("psk", None)  # secrets stay inside
        return [settings]

    def _Delete(self, path):
        if path not in self.profiles:
            raise BluezError("org.freedesktop.DBus.Error.UnknownObject", path)
        del self.profiles[path]
        if self.objects[WLAN0][DEVICE].get("Connection") == path:
            self._drop(reason=39)  # NM_DEVICE_STATE_REASON_CONNECTION_REMOVED
        self._signal(path, CONNECTION, "Removed", [])
        self._signal(SETTINGS_PATH, SETTINGS, "ConnectionRemoved", [path])
        return []

    def _ActivateConnection(self, path, profile, device, specific):
        assert device == WLAN0, "only ever on the Wi-Fi device"
        if profile not in self.profiles:
            raise BluezError("org.freedesktop.NetworkManager.UnknownConnection", profile)
        return [self._activate(profile, specific)]

    def _AddAndActivateConnection(self, path, settings, device, specific):
        assert device == WLAN0, "only ever on the Wi-Fi device"
        profile = self._path("Settings")
        self.profiles[profile] = settings
        self._signal(SETTINGS_PATH, SETTINGS, "NewConnection", [profile])
        return [profile, self._activate(profile, specific)]

    # Joining

    def _activate(self, profile: str, ap: str) -> str:
        active = self._path("ActiveConnection")
        self.objects[active] = {ACTIVE: {"State": 1, "Connection": profile}}
        self.objects[WLAN0][DEVICE].update(State=50, Connection=profile)
        if not self.hang:
            asyncio.get_running_loop().call_soon(self._outcome, active, profile, ap)
        return active

    def _outcome(self, active: str, profile: str, ap: str) -> None:
        settings = self.profiles.get(profile)
        if settings is None or active not in self.objects:
            return
        ssid = bytes(settings["802-11-wireless"]["ssid"]).decode()
        psk = settings.get("802-11-wireless-security", {}).get("psk")
        secured = self.objects[ap][ACCESS_POINT]["Flags"] & 1 if ap in self.objects else 0
        if ssid in self.out_of_range:
            self._fail(active, device_reason=53, reason=3)
        elif secured and psk != self.passwords.get(ssid):
            self._fail(active, device_reason=7, reason=9)
        else:
            self.objects[active][ACTIVE]["State"] = 2
            self.update(WLAN0, WIRELESS, ActiveAccessPoint=ap)
            self._device_state(100, 0)
            self._signal(active, ACTIVE, "StateChanged", [2, 1])

    def _fail(self, active: str, *, device_reason: int, reason: int) -> None:
        self._device_state(120, device_reason)
        self._signal(active, ACTIVE, "StateChanged", [4, reason])
        del self.objects[active]
        self._device_state(30, 0)
        self.objects[WLAN0][DEVICE].pop("Connection", None)

    def _drop(self, *, reason: int) -> None:
        self.objects[WLAN0][WIRELESS]["ActiveAccessPoint"] = "/"
        self.objects[WLAN0][DEVICE].pop("Connection", None)
        self._device_state(30, reason)

    def _device_state(self, new: int, reason: int) -> None:
        old = self.objects[WLAN0][DEVICE]["State"]
        self.objects[WLAN0][DEVICE]["State"] = new
        self._signal(WLAN0, DEVICE, "StateChanged", [new, old, reason])
        self._signal(WLAN0, PROPERTIES, "PropertiesChanged", [DEVICE, {"State": new}, []])

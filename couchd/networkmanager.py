"""Wi-Fi on the Pi: NetworkManager over the system D-Bus, with dbus-fast (#31, #78).

The radio on or off, which network it's on and how strong the signal is, the networks
nearby, and joining and forgetting them from the TV. A network NetworkManager already knows
reconnects by itself once the radio is on.

The D-Bus side is couchd/bluez.py's `DBusLink`, pointed at NetworkManager. Tests swap in an
in-memory NetworkManager (tests/fake_nm.py).

Turning the radio off is NetworkManager's `WirelessEnabled = false`. NetworkManager then
soft-blocks the radio (rfkill) and remembers the choice in its state file, which
`couch-wifi-boot` reads at the next boot (see system/README.md, "Wi-Fi").

Networks nearby are the Wi-Fi device's access points, one per SSID (the strongest). Hidden
networks (no SSID) and ad-hoc ones are left out, and so are networks Couch can't join with
just a password (802.1X "enterprise" and WEP), unless NetworkManager already has them saved,
set up some other way: those can be joined and forgotten like any other saved network.

Joining a saved network activates its profile on the Wi-Fi device. Joining a new one (or a
saved one with a new password) adds a profile for the whole system (WPA/WPA2/WPA3-personal
with its password, or open) and activates it; couchd then waits until NetworkManager says
it's connected or why it failed. A new profile that fails is deleted again, so a wrong
password is never kept; one that works replaces whatever was saved for that network.

couchd only ever touches Wi-Fi: every profile it activates or deletes is checked to be an
`802-11-wireless` one, always on the Wi-Fi device, because polkit can't tell NetworkManager's
actions apart by connection (system/couch.rules). Passwords go to NetworkManager and nowhere
else: never into the log, live state or an error.
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from dbus_fast import Variant

from couchd.backends import BackendError, Network, WifiBackend, WifiConnection, WifiStatus
from couchd.bluez import BluezError, BluezLink, DBusLink

log = logging.getLogger(__name__)

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
DEVICE = "org.freedesktop.NetworkManager.Device"
WIRELESS = "org.freedesktop.NetworkManager.Device.Wireless"
ACCESS_POINT = "org.freedesktop.NetworkManager.AccessPoint"
ACTIVE = "org.freedesktop.NetworkManager.Connection.Active"
SETTINGS = "org.freedesktop.NetworkManager.Settings"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
CONNECTION = "org.freedesktop.NetworkManager.Settings.Connection"
PROPERTIES = "org.freedesktop.DBus.Properties"

DEVICE_TYPE_WIFI = 2
# NMDeviceState: 20 unavailable, 30 disconnected, 40-90 on the way, 100 activated, 120 failed
DISCONNECTED = 30
ACTIVATED = 100
FAILED = 120
WIFI_CONNECTION_TYPE = "802-11-wireless"

# NMActiveConnectionState: 1 activating, 2 activated, 3 deactivating, 4 deactivated
ACTIVE_ACTIVATED = 2
ACTIVE_DEACTIVATING = 3
ACTIVE_GONE = 4

# NM80211ApFlags, NM80211ApSecurityFlags, NM80211Mode
AP_PRIVACY = 0x1
KEY_MGMT_PSK = 0x100
KEY_MGMT_802_1X = 0x200
KEY_MGMT_SAE = 0x400
KEY_MGMT_OWE = 0x800
KEY_MGMT_EAP_SUITE_B_192 = 0x2000
MODE_ADHOC = 1

# Why a connection failed: NMDeviceStateReason (the device going to "failed"), then
# NMActiveConnectionStateReason.
WRONG_PASSWORD = {7, 8}  # no secrets (the handshake failed), supplicant disconnected
NOT_FOUND = {53}  # SSID not found
NO_ANSWER = {11}  # supplicant timeout
NO_ADDRESS = {5, 15, 16, 17}  # IP config unavailable, DHCP start failed / error / failed
ACTIVE_WRONG_PASSWORD = {9, 10}  # no secrets, login failed
ACTIVE_TIMEOUT = {6}

SET_TIMEOUT = 15.0
CONNECT_TIMEOUT = 60.0  # NetworkManager itself gives up on DHCP after 45 s
SCAN_TIMEOUT = 15.0  # a scan that hasn't said it's done by now is taken as done

# A network's security, from its access point's flags.
OPEN, OWE, PSK, SAE, ENTERPRISE, WEP = "open", "owe", "psk", "sae", "enterprise", "wep"
NEEDS_PASSWORD = {PSK, SAE}
UNSUPPORTED = {ENTERPRISE, WEP}  # only listed, and joined, if saved already


@dataclass(frozen=True)
class _Ap:
    path: str
    ssid: str
    raw: bytes  # the SSID as the air has it, for a new profile
    signal: int
    security: str


class NmWifi(WifiBackend):
    def __init__(
        self,
        link: BluezLink | None = None,
        *,
        scan_timeout: float = SCAN_TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        super().__init__()
        self.link = link if link is not None else DBusLink(service=NM)
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self._status = WifiStatus(available=False, enabled=False)
        self._networks: list[Network] = []
        self._aps: dict[str, _Ap] = {}  # by SSID: its strongest access point
        self._saved: dict[str, str] | None = None  # profile path -> SSID; None: read again
        self._scanning = False
        self._scan_from: Any = None  # LastScan when the scan was asked for
        self._scan_timer: asyncio.Task | None = None
        self._device: str | None = None  # the Wi-Fi device's object path
        self._tasks: set[asyncio.Task] = set()
        self._reading = asyncio.Lock()
        self._again = False
        # While joining: NetworkManager's news about it.
        self._poke = asyncio.Event()
        self._device_reason: int | None = None
        self._active_reasons: dict[str, int] = {}

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

    def networks(self) -> list[Network]:
        return list(self._networks)

    def scanning(self) -> bool:
        return self._scanning

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

    # --- scanning --------------------------------------------------------------------------

    async def scan(self) -> None:
        device = self._wifi_device()
        try:
            wireless = await self._get_all(device, WIRELESS)
            self._scan_from = wireless.get("LastScan")
            await self.link.call(device, WIRELESS, "RequestScan", "a{sv}", [{}])
        except (BluezError, TimeoutError) as e:
            name = _error_name(e)
            if "PermissionDenied" in name:
                raise BackendError("Couch isn't allowed to look for Wi-Fi networks") from None
            # "Scanning not allowed while already scanning" (or straight after one): what
            # that scan finds turns up all the same.
            log.debug("scan not started", extra={"error": name})
        if self._scan_timer is not None:
            self._scan_timer.cancel()
        self._scan_timer = self._spawn(self._scan_done_later())
        if not self._scanning:
            self._scanning = True
            self.changed()

    async def _scan_done_later(self) -> None:
        await asyncio.sleep(self.scan_timeout)
        self._scan_timer = None
        self._end_scan()

    def _end_scan(self) -> None:
        timer, self._scan_timer = self._scan_timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        if self._scanning:
            self._scanning = False
            self.changed()

    # --- joining and forgetting ----------------------------------------------------------

    async def connect(self, ssid: str, password: str | None = None) -> None:
        device = self._wifi_device()
        ap = self._aps.get(ssid)
        if ap is None:
            raise BackendError(f"Can't find {ssid}. Is it in range?")
        saved = await self._saved_for(ssid)
        if ap.security in UNSUPPORTED and (password is not None or not saved):
            raise BackendError(f"Couch can't join {ssid}: it needs more than a password")
        if saved and password is None:
            log.info("joining a saved Wi-Fi network")
            self._device_reason = None
            (active,) = await self._change(
                NM_PATH, NM, "ActivateConnection", "ooo", [saved[0], device, ap.path], ssid=ssid
            )
            await self._wait(active, ssid, ap)
            return
        if ap.security in NEEDS_PASSWORD:
            _check_password(ssid, password, ap.security)
        log.info("joining a new Wi-Fi network", extra={"security": ap.security})
        self._device_reason = None
        path, active = await self._change(
            NM_PATH,
            NM,
            "AddAndActivateConnection",
            "a{sa{sv}}oo",
            [_profile(ap, password), device, ap.path],
            ssid=ssid,
        )
        try:
            await self._wait(active, ssid, ap)
        except BaseException:
            log.info("couldn't join, so the new profile goes again")
            await self._delete(path)
            raise
        for old in saved:  # the new password replaces what was saved
            await self._delete(old)

    async def forget(self, ssid: str) -> None:
        saved = await self._saved_for(ssid)
        if not saved:
            raise BackendError(f"{ssid} isn't saved")
        log.info("forgetting a Wi-Fi network", extra={"profiles": len(saved)})
        for path in saved:
            await self._change(path, CONNECTION, "Delete", ssid=ssid)
        self._saved = None
        await self._refresh()

    def _wifi_device(self) -> str:
        if self._device is None or not self._status.enabled:
            raise BackendError("Wi-Fi is off")
        return self._device

    async def _change(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
        *,
        ssid: str,
    ) -> list[Any]:
        """A call that changes connections, its failure put for the user. Only the error's
        name is logged, in case NetworkManager ever quotes a setting back in its text."""
        try:
            return await self.link.call(path, interface, member, signature, body or [])
        except (BluezError, TimeoutError) as e:
            name = _error_name(e)
            log.warning("NetworkManager refused", extra={"call": member, "error": name})
            if "PermissionDenied" in name:
                raise BackendError("Couch isn't allowed to change Wi-Fi networks") from None
            if member == "Delete":
                raise BackendError(f"Couldn't forget {ssid}") from None
            raise BackendError(f"Couldn't connect to {ssid}") from None

    async def _wait(self, active: str, ssid: str, ap: _Ap) -> None:
        """Until NetworkManager has the connection up, or has given up on it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        while True:
            self._poke.clear()
            try:
                (state,) = await self.link.call(active, PROPERTIES, "Get", "ss", [ACTIVE, "State"])
            except BluezError:
                state = ACTIVE_GONE  # NetworkManager has dropped it already
            if state == ACTIVE_ACTIVATED:
                log.info("joined the Wi-Fi network")
                await self._refresh()
                return
            if state >= ACTIVE_DEACTIVATING:
                raise self._failure(active, ssid, ap)
            left = deadline - loop.time()
            if left <= 0:
                log.warning("joining the Wi-Fi network timed out")
                raise BackendError(f"Couldn't connect to {ssid}")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(1.0, left)):
                    await self._poke.wait()

    def _failure(self, active: str, ssid: str, ap: _Ap) -> BackendError:
        device = self._device_reason
        reason = self._active_reasons.pop(active, None)
        log.warning(
            "couldn't join the Wi-Fi network", extra={"device_reason": device, "reason": reason}
        )
        secure = ap.security not in (OPEN, OWE)
        if secure and (device in WRONG_PASSWORD or reason in ACTIVE_WRONG_PASSWORD):
            return BackendError("Wrong password")
        if device in NOT_FOUND:
            return BackendError(f"Can't find {ssid}. Is it in range?")
        if device in NO_ANSWER or reason in ACTIVE_TIMEOUT:
            return BackendError(f"{ssid} didn't answer. Is it in range?")
        if device in NO_ADDRESS:
            return BackendError(f"{ssid} didn't give Couch an address")
        return BackendError(f"Couldn't connect to {ssid}")

    async def _delete(self, path: str) -> None:
        try:
            await self.link.call(path, CONNECTION, "Delete")
        except (BluezError, TimeoutError) as e:
            log.error("couldn't delete a Wi-Fi profile", extra={"error": _error_name(e)})
        self._saved = None

    async def _saved_for(self, ssid: str) -> list[str]:
        """The saved Wi-Fi profiles for this SSID, read afresh."""
        self._saved = None
        try:
            saved = await self._read_saved()
        except (BluezError, TimeoutError) as e:
            log.warning("couldn't read saved networks", extra={"error": _error_name(e)})
            raise BackendError("Couldn't read the saved Wi-Fi networks") from None
        return [path for path, name in saved.items() if name == ssid]

    async def _read_saved(self) -> dict[str, str]:
        """Saved Wi-Fi profiles, path -> SSID. Only `802-11-wireless` ones, never another
        kind: these are the only profiles couchd ever activates or deletes."""
        if self._saved is not None:
            return self._saved
        saved: dict[str, str] = {}
        (paths,) = await self.link.call(SETTINGS_PATH, SETTINGS, "ListConnections")
        for path in paths:
            with contextlib.suppress(BluezError):  # deleted meanwhile
                (settings,) = await self.link.call(path, CONNECTION, "GetSettings")
                ssid = _profile_ssid(settings)
                if ssid is not None:
                    saved[path] = ssid
        self._saved = saved
        return saved

    # --- reading NetworkManager ----------------------------------------------------------

    def _on_presence(self, running: bool) -> None:
        if running:
            self._spawn(self._refresh())
        else:
            log.warning("NetworkManager went away")
            self._device = None
            self._saved = None
            self._end_scan()
            self._set(WifiStatus(available=False, enabled=False), [], {})
        self._poke.set()

    def _on_signal(self, path: str, interface: str, member: str, body: list[Any]) -> None:
        if interface == NM and member in ("DeviceAdded", "DeviceRemoved"):
            self._spawn(self._refresh())
        elif interface == DEVICE and member == "StateChanged":
            if path == self._device and len(body) >= 3 and body[0] == FAILED:
                self._device_reason = int(body[2])
            self._poke.set()
        elif interface == ACTIVE and member == "StateChanged":
            if len(body) >= 2:
                self._active_reasons[path] = int(body[1])
            self._poke.set()
        elif interface == WIRELESS and member in ("AccessPointAdded", "AccessPointRemoved"):
            self._spawn(self._refresh())
        elif (interface == SETTINGS and member in ("NewConnection", "ConnectionRemoved")) or (
            interface == CONNECTION and member in ("Updated", "Removed")
        ):
            self._saved = None
            self._spawn(self._refresh())
        elif interface == PROPERTIES and member == "PropertiesChanged":
            changed_interface = body[0] if body else None
            if changed_interface in (NM, DEVICE, WIRELESS, ACCESS_POINT):
                self._spawn(self._refresh())
            if changed_interface in (DEVICE, ACTIVE):
                self._poke.set()

    async def _refresh(self) -> None:
        """Read everything again. Signals come in bursts: while a refresh runs, more asks
        only make it go round once more (and wait for that round, so a caller sees what it
        changed)."""
        self._again = True
        if self._reading.locked():
            async with self._reading:
                return
        async with self._reading:
            while self._again:
                self._again = False
                try:
                    self._set(*await self._read())
                except (BluezError, TimeoutError) as e:
                    log.warning("couldn't read Wi-Fi state", extra={"error": str(e)})

    async def _read(self) -> tuple[WifiStatus, list[Network], dict[str, _Ap]]:
        nm = await self._get_all(NM_PATH, NM)
        self._device = await self._find_device()
        if self._device is None:
            return WifiStatus(available=False, enabled=bool(nm.get("WirelessEnabled"))), [], {}
        enabled = bool(nm.get("WirelessEnabled")) and bool(nm.get("WirelessHardwareEnabled", True))
        primary = nm.get("PrimaryConnectionType") == WIFI_CONNECTION_TYPE
        if not enabled:
            return WifiStatus(available=True, enabled=False, primary=primary), [], {}
        device = await self._get_all(self._device, DEVICE)
        connection = _connection(int(device.get("State", 0)))
        wireless = await self._get_all(self._device, WIRELESS)
        if self._scanning and wireless.get("LastScan") != self._scan_from:
            self._end_scan()
        ssid = signal = None
        if connection in ("connecting", "connected"):
            ap = wireless.get("ActiveAccessPoint")
            if ap and ap != "/":
                with contextlib.suppress(BluezError):  # the AP can vanish between calls
                    props = await self._get_all(ap, ACCESS_POINT)
                    ssid = _ssid(props.get("Ssid"))
                    if connection == "connected" and "Strength" in props:
                        signal = int(props["Strength"])
        status = WifiStatus(
            available=True,
            enabled=True,
            connection=connection,
            ssid=ssid,
            signal=signal,
            primary=primary,
        )
        aps = await self._read_aps(wireless.get("AccessPoints", []))
        saved = set((await self._read_saved()).values())
        networks = [
            Network(
                ssid=ap.ssid,
                signal=ap.signal,
                secure=ap.security not in (OPEN, OWE),
                known=ap.ssid in saved,
                active=connection == "connected" and ap.ssid == ssid,
            )
            for ap in aps.values()
            if ap.security not in UNSUPPORTED or ap.ssid in saved
        ]
        networks.sort(key=lambda n: (-n.signal, n.ssid.lower()))
        return status, networks, aps

    async def _read_aps(self, paths: list[str]) -> dict[str, _Ap]:
        best: dict[str, _Ap] = {}
        for path in paths:
            try:
                props = await self._get_all(path, ACCESS_POINT)
            except BluezError:  # gone already
                continue
            ap = _access_point(path, props)
            if ap is not None and (ap.ssid not in best or ap.signal > best[ap.ssid].signal):
                best[ap.ssid] = ap
        return best

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

    def _set(self, status: WifiStatus, networks: list[Network], aps: dict[str, _Ap]) -> None:
        self._aps = aps
        if not status.enabled:
            self._end_scan()
        if status != self._status or networks != self._networks:
            self._status = status
            self._networks = networks
            self.changed()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _error_name(e: Exception) -> str:
    return e.name if isinstance(e, BluezError) else "timeout"


def _connection(state: int) -> WifiConnection:
    if state == ACTIVATED:
        return "connected"
    if DISCONNECTED < state < ACTIVATED:
        return "connecting"
    return "disconnected"  # also unavailable, deactivating and failed: the radio is on, no network


def _ssid(raw: Any) -> str | None:
    """An SSID is bytes (`ay`), usually UTF-8. A hidden network's is empty, or all zeros."""
    if not raw or not any(raw):
        return None
    return bytes(raw).decode("utf-8", errors="replace")


def _security(props: dict[str, Any]) -> str:
    mgmt = int(props.get("WpaFlags", 0)) | int(props.get("RsnFlags", 0))
    if mgmt & KEY_MGMT_PSK:
        return PSK  # WPA, WPA2, and WPA3 in transition mode
    if mgmt & KEY_MGMT_SAE:
        return SAE  # WPA3 only
    if mgmt & (KEY_MGMT_802_1X | KEY_MGMT_EAP_SUITE_B_192):
        return ENTERPRISE
    if mgmt & KEY_MGMT_OWE:
        return OWE  # "enhanced open": encrypted, but no password
    if int(props.get("Flags", 0)) & AP_PRIVACY:
        return WEP
    return OPEN


def _access_point(path: str, props: dict[str, Any]) -> _Ap | None:
    ssid = _ssid(props.get("Ssid"))
    if ssid is None or props.get("Mode") == MODE_ADHOC:
        return None
    return _Ap(
        path=path,
        ssid=ssid,
        raw=bytes(props["Ssid"]),
        signal=int(props.get("Strength", 0)),
        security=_security(props),
    )


def _profile_ssid(settings: dict[str, Any]) -> str | None:
    """A saved profile's SSID, if it's a Wi-Fi network to join (not a hotspot, nor any other
    kind of connection)."""
    if settings.get("connection", {}).get("type") != WIFI_CONNECTION_TYPE:
        return None
    wireless = settings.get(WIFI_CONNECTION_TYPE, {})
    if wireless.get("mode", "infrastructure") != "infrastructure":
        return None
    return _ssid(wireless.get("ssid"))


def _check_password(ssid: str, password: str | None, security: str) -> None:
    if not password:
        raise BackendError(f"{ssid} needs a password")
    if security == PSK:
        if not password.isascii():
            raise BackendError("That password has a character Wi-Fi doesn't allow")
        if not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9A-Fa-f]{64}", password)):
            raise BackendError("Wi-Fi passwords are 8 to 63 characters")


def _profile(ap: _Ap, password: str | None) -> dict[str, dict[str, Variant]]:
    """A new profile for this network, for the whole system (no `permissions`), with the
    password kept by NetworkManager: it reconnects by itself after a reboot."""
    settings = {
        "connection": {
            "id": Variant("s", ap.ssid),
            "type": Variant("s", WIFI_CONNECTION_TYPE),
        },
        WIFI_CONNECTION_TYPE: {
            "ssid": Variant("ay", ap.raw),
            "mode": Variant("s", "infrastructure"),
        },
    }
    if ap.security in NEEDS_PASSWORD:
        settings["802-11-wireless-security"] = {
            "key-mgmt": Variant("s", "wpa-psk" if ap.security == PSK else "sae"),
            "psk": Variant("s", password or ""),
        }
    elif ap.security == OWE:
        settings["802-11-wireless-security"] = {"key-mgmt": Variant("s", "owe")}
    return settings

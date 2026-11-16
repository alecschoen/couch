"""Fake hardware for `couchd --dev`: a TV, three kinds of audio output, Bluetooth devices
that turn up while scanning, a Wi-Fi radio, and a virtual keyboard that only logs. They
behave like the real thing closely enough to build every screen: things take a moment,
connecting a speaker adds an output, one speaker always fails to pair.

`speed` scales every delay; tests use 0.
"""

import asyncio
import logging
from dataclasses import replace

from couchd.backends import (
    AudioBackend,
    BackendError,
    BluetoothBackend,
    Device,
    KeyboardBackend,
    Output,
    TvBackend,
    TvPower,
    WifiBackend,
    WifiStatus,
)
from couchd.cec_tv import VOLUME_COMMANDS
from couchd.uinput import key_name

log = logging.getLogger(__name__)


class _Delays:
    def __init__(self, speed: float) -> None:
        self.speed = speed

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self.speed)


class FakeTv(TvBackend):
    """`power` is how it starts; dev mode starts it in standby, so the Startup screen shows
    "Turning on the TV" for `wake_time` seconds."""

    def __init__(
        self, *, speed: float = 1.0, power: TvPower = "on", wake_time: float = 0.5
    ) -> None:
        super().__init__()
        self._delays = _Delays(speed)
        self._power: TvPower = power
        self._wake_time = wake_time
        self._audio_wanted = False
        self.audio_status: tuple[int, bool] | None = None
        self.reports: list[tuple[int, bool]] = []  # what a real TV would have been sent

    def system_audio(self) -> bool:
        return self._audio_wanted and self._power == "on"

    async def set_system_audio(self, on: bool) -> None:
        if on != self._audio_wanted:
            self._audio_wanted = on
            log.info("fake TV audio system", extra={"on": on})
            self.changed()

    def set_audio_status(self, volume: int, muted: bool, *, report: bool = False) -> None:
        changed = self.audio_status != (volume, muted)
        self.audio_status = (volume, muted)
        if self.system_audio() and (report or changed):
            self.reports.append((volume, muted))

    async def simulate(self, frame: bytes) -> None:
        """Understands what the volume buttons send: User Control Pressed 0x41-0x43, 0x65,
        0x66 and User Control Released, to any address."""
        if len(frame) >= 3 and frame[1] == 0x44 and frame[2] in VOLUME_COMMANDS:
            self.volume_key_pressed(VOLUME_COMMANDS[frame[2]])
        elif len(frame) >= 2 and frame[1] == 0x45:
            self.volume_key_released()
        else:
            raise BackendError("The fake TV only simulates volume buttons")

    def power(self) -> TvPower:
        return self._power

    async def power_on(self) -> None:
        await self._delays.sleep(self._wake_time)
        self._power = "on"
        self.changed()

    async def standby(self) -> None:
        await self._delays.sleep(0.3)
        self._power = "standby"
        self.changed()


class FakeKeyboard(KeyboardBackend):
    """Records what it would have typed; in dev mode the browser window has the real keyboard."""

    def __init__(self) -> None:
        super().__init__()
        self.typed: list[int] = []

    def tap(self, code: int) -> None:
        log.debug("virtual keyboard", extra={"key": key_name(code)})
        self.typed.append(code)


class FakeAudio(AudioBackend):
    def __init__(self, *, speed: float = 1.0) -> None:
        super().__init__()
        self._delays = _Delays(speed)
        self._outputs = [
            Output("tv", "TV speakers", "tv"),
            Output("jack", "Headphone jack", "jack"),
        ]
        self._current = "tv"
        self._volume = {"tv": 40, "jack": 25}
        self._muted = False

    def outputs(self) -> list[Output]:
        return list(self._outputs)

    def current(self) -> str | None:
        return self._current

    def volume(self) -> int:
        return self._volume.get(self._current, 50)

    def muted(self) -> bool:
        return self._muted

    async def set_output(self, output_id: str) -> None:
        output = self._find(output_id)
        if output is None or not output.available:
            raise BackendError(f"{output_id} isn't available")
        await self._delays.sleep(0.3)
        self._current = output_id
        self.changed()

    async def set_volume(self, volume: int) -> None:
        self._volume[self._current] = max(0, min(100, volume))
        self.changed()

    async def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self.changed()

    # Called by FakeBluetooth, standing in for PipeWire noticing a Bluetooth sink.
    def bluetooth_connected(self, device: Device) -> None:
        output_id = f"bluetooth:{device.address}"
        if self._find(output_id) is None:
            self._outputs.append(Output(output_id, device.name, "bluetooth"))
            self._volume.setdefault(output_id, 30)
            self.changed()

    def bluetooth_disconnected(self, device: Device) -> None:
        output_id = f"bluetooth:{device.address}"
        self._outputs = [o for o in self._outputs if o.id != output_id]
        if self._current == output_id:
            self._current = "tv"
        self.changed()

    def _find(self, output_id: str) -> Output | None:
        return next((o for o in self._outputs if o.id == output_id), None)


# Devices that show up while scanning: (seconds after scan starts, device).
NEARBY = (
    (0.6, Device("FA:KE:00:00:00:02", "Living Room Soundbar", audio=True)),
    (1.2, Device("FA:KE:00:00:00:03", "Headphones", audio=True)),
    (1.8, Device("FA:KE:00:00:00:04", "Phone", audio=False)),
    (2.5, Device("FA:KE:00:00:00:05", "Old Speaker", audio=True)),
)
ALREADY_PAIRED = Device("FA:KE:00:00:00:01", "Kitchen Speaker", audio=True, paired=True)
REFUSES_TO_PAIR = "FA:KE:00:00:00:05"


class FakeBluetooth(BluetoothBackend):
    def __init__(self, audio: FakeAudio | None = None, *, speed: float = 1.0) -> None:
        super().__init__()
        self._audio = audio
        self._delays = _Delays(speed)
        self._devices: dict[str, Device] = {ALREADY_PAIRED.address: ALREADY_PAIRED}
        self._scan: asyncio.Task | None = None

    def devices(self) -> list[Device]:
        return list(self._devices.values())

    def scanning(self) -> bool:
        return self._scan is not None

    async def start_scan(self) -> None:
        if self._scan is None:
            self._scan = asyncio.create_task(self._discover())
            self.changed()

    async def stop_scan(self) -> None:
        if self._scan is None:
            return
        self._scan.cancel()
        self._scan = None
        self._devices = {a: d for a, d in self._devices.items() if d.paired}
        self.changed()

    async def stop(self) -> None:
        if self._scan is not None:
            self._scan.cancel()
            self._scan = None

    async def _discover(self) -> None:
        elapsed = 0.0
        for at, device in NEARBY:
            await self._delays.sleep(at - elapsed)
            elapsed = at
            if device.address not in self._devices:
                self._devices[device.address] = device
                self.changed()

    async def pair(self, address: str) -> None:
        device = self._get(address)
        await self._delays.sleep(1.5)
        if address == REFUSES_TO_PAIR:
            raise BackendError(f"{device.name} didn't accept the pairing")
        self._set(address, paired=True)

    async def connect(self, address: str) -> None:
        device = self._get(address)
        if not device.paired:
            raise BackendError(f"{device.name} isn't paired")
        await self._delays.sleep(0.8)
        device = self._set(address, connected=True)
        if device.audio and self._audio is not None:
            self._audio.bluetooth_connected(device)

    async def disconnect(self, address: str) -> None:
        device = self._get(address)
        await self._delays.sleep(0.3)
        device = self._set(address, connected=False)
        if self._audio is not None:
            self._audio.bluetooth_disconnected(device)

    async def forget(self, address: str) -> None:
        device = self._get(address)
        if device.connected:
            await self.disconnect(address)
        if self.scanning():
            self._set(address, paired=False)
        else:
            del self._devices[address]
            self.changed()

    def _get(self, address: str) -> Device:
        try:
            return self._devices[address]
        except KeyError:
            raise BackendError(f"no device {address}") from None

    def _set(self, address: str, **changes: bool) -> Device:
        device = replace(self._get(address), **changes)
        self._devices[address] = device
        self.changed()
        return device


class FakeWifi(WifiBackend):
    """A Wi-Fi radio that knows one network. Turned on, it finds it and connects."""

    NETWORK = "Living room"

    def __init__(self, *, enabled: bool = False, speed: float = 1.0) -> None:
        super().__init__()
        self._delays = _Delays(speed)
        self._status = WifiStatus(available=True, enabled=False)
        self._joining: asyncio.Task | None = None
        if enabled:
            self._status = self._connected()

    def status(self) -> WifiStatus:
        return self._status

    async def set_enabled(self, on: bool) -> None:
        if on == self._status.enabled:
            return
        if self._joining is not None:
            self._joining.cancel()
            self._joining = None
        if on:
            self._update(WifiStatus(available=True, enabled=True, connection="disconnected"))
            self._joining = asyncio.create_task(self._join())
        else:
            self._update(WifiStatus(available=True, enabled=False))

    async def stop(self) -> None:
        if self._joining is not None:
            self._joining.cancel()
            self._joining = None

    async def _join(self) -> None:
        await self._delays.sleep(0.8)
        self._update(replace(self._status, connection="connecting", ssid=self.NETWORK))
        await self._delays.sleep(1.2)
        self._update(self._connected())
        self._joining = None

    def _connected(self) -> WifiStatus:
        return WifiStatus(
            available=True, enabled=True, connection="connected", ssid=self.NETWORK, signal=72
        )

    def _update(self, status: WifiStatus) -> None:
        self._status = status
        self.changed()

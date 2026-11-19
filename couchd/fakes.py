"""Fake hardware for `couchd --dev`: a TV, three kinds of audio output, Bluetooth devices
that turn up while scanning, a Wi-Fi radio, a virtual keyboard that only logs, and phones
playing music to Couch (Spotify, AirPlay). They
behave like the real thing closely enough to build every screen: things take a moment,
connecting a speaker adds an output, one speaker always fails to pair.

`speed` scales every delay; tests use 0.
"""

import asyncio
import logging
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import replace

from couchd.backends import (
    AudioBackend,
    BackendError,
    BluetoothBackend,
    Device,
    KeyboardBackend,
    Output,
    Playback,
    SpeakerBackend,
    SpeakerControl,
    SpeakerSource,
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


# (title, artist, album, seconds, cover colour)
FAKE_TRACKS = (
    ("Harbour Lights", "The Night Ferries", "Crossings", 214, (0x2E, 0x5E, 0x8C)),
    ("Small Hours", "Marlow & Finch", "Lamplight", 187, (0xC2, 0x6A, 0x2E)),
    ("Paper Moons", "Okra Blue", "Paper Moons", 242, (0x6E, 0x3B, 0x8C)),
)


def solid_png(rgb: tuple[int, int, int], size: int = 64) -> bytes:
    """A square PNG of one colour: pretend cover art."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


class FakeSpeaker(SpeakerBackend):
    """A phone playing to Couch, started with `play()` (the `speaker.dev.play` command).
    Like the real ones, the AirPlay one can be paused and skipped from the remote and the
    Spotify one can't (only disconnected)."""

    def __init__(self, source: SpeakerSource, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self.source = source
        self._clock = clock
        self._enabled = False
        self._playback = Playback()
        self._track = 0
        self._covers = 0
        self.controlled: list[str] = []  # what the phone was asked to do

    def playback(self) -> Playback:
        return self._playback

    def cover(self) -> tuple[bytes, str] | None:
        if not self._playback.active:
            return None
        return solid_png(FAKE_TRACKS[self._track][4]), "image/png"

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def set_enabled(self, on: bool) -> None:
        self._enabled = on
        if not on:
            await self.disconnect()

    def play(self) -> None:
        """A phone starts playing (or resumes)."""
        if not self._playback.active:
            self._load(0)
        self._set_playing(True)

    async def control(self, command: SpeakerControl) -> None:
        if command not in self._playback.controls:
            raise BackendError("Spotify can't be controlled from Couch")
        self.controlled.append(command)
        if command == "play_pause":
            self._set_playing(not self._playback.playing)
        elif command in ("play", "pause"):
            self._set_playing(command == "play")
        else:
            self._load((self._track + (1 if command == "next" else -1)) % len(FAKE_TRACKS))
            self._set_playing(True)

    async def disconnect(self) -> None:
        if self._playback.active:
            self._playback = Playback()
            self.changed()

    def _load(self, index: int) -> None:
        title, artist, album, seconds, _ = FAKE_TRACKS[index]
        self._track = index
        self._covers += 1
        remote = frozenset({"play_pause", "play", "pause", "next", "previous", "disconnect"})
        self._playback = Playback(
            active=True,
            title=title,
            artist=artist,
            album=album,
            device="Pretend iPhone" if self.source == "airplay" else "Pretend Pixel",
            duration_ms=seconds * 1000,
            position_ms=0,
            position_at=self._now(),
            cover=self._covers,
            controls=remote if self.source == "airplay" else frozenset({"disconnect"}),
        )

    def _set_playing(self, playing: bool) -> None:
        p = self._playback
        position = p.position_ms or 0
        if p.playing and p.position_at is not None:
            position += self._now() - p.position_at
        position = min(position, p.duration_ms or position)
        self._playback = replace(p, playing=playing, position_ms=position, position_at=self._now())
        self.changed()

    def _now(self) -> int:
        return int(self._clock() * 1000)

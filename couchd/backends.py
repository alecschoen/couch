"""Hardware interfaces. Each has a real implementation for the Pi and a fake for `--dev`.

Services never touch hardware directly; they talk to one of these. A backend calls
`changed()` whenever anything it reports may have changed (a speaker connected, the TV went
to standby), and the service re-reads it and updates live state. Live state only publishes
the keys that actually differ, so a spurious `changed()` costs nothing.

Real backends land with their issues: CEC (`couchd/cec_tv.py`, #7), audio (#10), Bluetooth
(`couchd/bluez.py`, #12), the virtual keyboard (`couchd/uinput.py`, #8), Wi-Fi
(`couchd/networkmanager.py`, #31). The fakes are in `couchd/fakes.py`.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


class BackendError(Exception):
    """An operation failed in a way the user should hear about ("Pairing failed")."""


class Backend(ABC):  # noqa: B024 - lifecycle hooks are optional, the subclasses are abstract
    def __init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def start(self) -> None:  # noqa: B027
        """Open the device or connection."""

    async def stop(self) -> None:  # noqa: B027
        """Release it. Safe to call after a failed start."""


# --- TV (HDMI-CEC) -------------------------------------------------------------------------

TvPower = Literal["on", "standby", "unknown"]


KeyAction = Literal["press", "release", "tap"]
# The TV remote's volume buttons, which the TV sends to its Audio System (#11). "mute" is a
# toggle; "mute_on" and "mute_off" are CEC's Mute Function and Restore Volume Function.
VolumeKey = Literal["up", "down", "mute", "mute_on", "mute_off"]


class TvBackend(Backend):
    """Also the source of the remote's buttons. A button held on the remote is a `press`
    then a `release` (CEC's User Control Pressed / Released; the release names no key). A
    one-shot key with no release to come, such as CEC's <Play>, is a `tap`."""

    def __init__(self) -> None:
        super().__init__()
        self._key_listeners: list[Callable[[KeyAction, str | None], None]] = []
        self._volume_listeners: list[Callable[[VolumeKey | None], None]] = []

    def on_key(self, listener: Callable[[KeyAction, str | None], None]) -> None:
        self._key_listeners.append(listener)

    def key_pressed(self, key: str) -> None:
        """A remote button went down (a name from couchd.keys)."""
        self._key_event("press", key)

    def key_released(self) -> None:
        """Whatever button was down came back up."""
        self._key_event("release", None)

    def key_tapped(self, key: str) -> None:
        """A button press that has no release to come."""
        self._key_event("tap", key)

    def _key_event(self, action: KeyAction, key: str | None) -> None:
        for listener in list(self._key_listeners):
            listener(action, key)

    def on_volume_key(self, listener: Callable[[VolumeKey | None], None]) -> None:
        """Volume buttons the TV handed us as its Audio System; None is a release."""
        self._volume_listeners.append(listener)

    def volume_key_pressed(self, key: VolumeKey) -> None:
        for listener in list(self._volume_listeners):
            listener(key)

    def volume_key_released(self) -> None:
        for listener in list(self._volume_listeners):
            listener(None)

    def system_audio(self) -> bool:
        """True while the TV's speakers are muted and its volume buttons come to us."""
        return False

    async def set_system_audio(self, on: bool) -> None:
        """Be the TV's Audio System, because sound is going somewhere other than the TV
        (`on`), or give sound and the volume buttons back to the TV's speakers."""

    def set_audio_status(self, volume: int, muted: bool, *, report: bool = False) -> None:
        """The volume we have now, for the TV to show. Sent to it if it changed, or always
        with `report` (after a volume button, so the TV shows where it got to)."""

    async def simulate(self, frame: bytes) -> None:
        """Handle a CEC frame as if the TV had sent it. For trying things on the Pi."""
        raise BackendError("This TV backend can't simulate CEC frames")

    @abstractmethod
    def power(self) -> TvPower: ...

    def active(self) -> bool:
        """True while the TV is showing our input (not another input, not off)."""
        return self.power() == "on"

    @abstractmethod
    async def power_on(self) -> None:
        """Wake the TV and switch it to our input."""

    @abstractmethod
    async def standby(self) -> None: ...


# --- Virtual keyboard ---------------------------------------------------------------------


class KeyboardBackend(Backend):
    """Types keys into whatever app has the screen (Chromium, Kodi). Codes are Linux input
    event codes (couchd/uinput.py)."""

    @abstractmethod
    def tap(self, code: int) -> None:
        """Press and release one key."""


# --- Audio ---------------------------------------------------------------------------------

OutputKind = Literal["tv", "jack", "bluetooth", "usb"]


@dataclass(frozen=True)
class Output:
    id: str
    name: str
    kind: OutputKind
    available: bool = True


class AudioBackend(Backend):
    @abstractmethod
    def outputs(self) -> list[Output]: ...

    @abstractmethod
    def current(self) -> str | None:
        """Id of the output sound goes to now."""

    @abstractmethod
    def volume(self) -> int:
        """0-100, for the current output."""

    @abstractmethod
    def muted(self) -> bool: ...

    @abstractmethod
    async def set_output(self, output_id: str) -> None:
        """Move all sound to `output_id`, including streams already playing."""

    @abstractmethod
    async def set_volume(self, volume: int) -> None: ...

    @abstractmethod
    async def set_muted(self, muted: bool) -> None: ...


# --- Bluetooth -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Device:
    address: str
    name: str
    audio: bool
    paired: bool = False
    connected: bool = False


class BluetoothBackend(Backend):
    @abstractmethod
    def devices(self) -> list[Device]:
        """Paired devices, plus anything found while scanning."""

    @abstractmethod
    def scanning(self) -> bool: ...

    @abstractmethod
    async def start_scan(self) -> None: ...

    @abstractmethod
    async def stop_scan(self) -> None: ...

    @abstractmethod
    async def pair(self, address: str) -> None:
        """Pair and trust. Raises BackendError if the device refuses."""

    @abstractmethod
    async def connect(self, address: str) -> None: ...

    @abstractmethod
    async def disconnect(self, address: str) -> None: ...

    @abstractmethod
    async def forget(self, address: str) -> None: ...


# --- Wi-Fi ---------------------------------------------------------------------------------

WifiConnection = Literal["off", "disconnected", "connecting", "connected"]


@dataclass(frozen=True)
class WifiStatus:
    available: bool  # there is a Wi-Fi radio to switch (and NetworkManager is running)
    enabled: bool  # the radio is on
    connection: WifiConnection = "off"
    ssid: str | None = None  # while connected (or connecting)
    signal: int | None = None  # 0-100, while connected
    primary: bool = False  # Couch's network traffic goes over Wi-Fi (the default route)


class WifiBackend(Backend):
    """The Wi-Fi radio. Joining networks is NetworkManager's: a network it already knows
    reconnects by itself once the radio is on."""

    @abstractmethod
    def status(self) -> WifiStatus: ...

    @abstractmethod
    async def set_enabled(self, on: bool) -> None:
        """Turn the radio on or off. Raises BackendError if that's refused."""


# --- Speaker (Spotify Connect, AirPlay) ----------------------------------------------------

SpeakerSource = Literal["spotify", "airplay"]
# What a phone can be asked to do from the remote. "disconnect" ends its session instead.
SpeakerControl = Literal["play_pause", "play", "pause", "next", "previous"]


@dataclass(frozen=True)
class Playback:
    """What one receiver is playing. `active` is false while nobody is playing to it (the
    rest is then empty); paused is still active. `position_ms` was the position at
    `position_at` (Unix time, ms), so a UI moves it on by itself while `playing`. `cover`
    goes up whenever the cover art changes; 0 means none. `controls` are the
    SpeakerControls that work right now."""

    active: bool = False
    playing: bool = False
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    device: str | None = None  # the phone or computer playing, when it says
    duration_ms: int | None = None
    position_ms: int | None = None
    position_at: int | None = None
    cover: int = 0
    controls: frozenset[str] = frozenset()


class SpeakerBackend(Backend):
    """One way of playing to Couch from a phone: a receiver (librespot, shairport-sync)
    that couchd starts and stops, plus what it reports about what's playing."""

    source: SpeakerSource

    def installed(self) -> bool:
        """The receiver's program is there (librespot is a separate build, librespot/)."""
        return True

    @abstractmethod
    def playback(self) -> Playback: ...

    def cover(self) -> tuple[bytes, str] | None:
        """The current cover art and its content type, if there is one."""
        return None

    @abstractmethod
    async def set_enabled(self, on: bool) -> None:
        """Start the receiver (Couch shows up on phones) or stop it."""

    async def control(self, command: SpeakerControl) -> None:
        """Ask the phone to play, pause or skip. Raises BackendError if it can't be."""
        raise BackendError("Couch can't control this from the remote")

    @abstractmethod
    async def disconnect(self) -> None:
        """End the session, so the phone stops playing to Couch."""

    def handle_event(self, event: str, env: dict[str, str]) -> None:
        """An event from the receiver, through couch-speaker-event (system/)."""

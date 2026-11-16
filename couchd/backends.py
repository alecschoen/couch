"""Hardware interfaces. Each has a real implementation for the Pi and a fake for `--dev`.

Services never touch hardware directly; they talk to one of these. A backend calls
`changed()` whenever anything it reports may have changed (a speaker connected, the TV went
to standby), and the service re-reads it and updates live state. Live state only publishes
the keys that actually differ, so a spurious `changed()` costs nothing.

Real backends land with their issues: CEC (#7), audio (#10), Bluetooth (#12, couchd/bluez.py).
The fakes are in `couchd/fakes.py`.
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


class TvBackend(Backend):
    def __init__(self) -> None:
        super().__init__()
        self._key_listeners: list[Callable[[str], None]] = []

    def on_key(self, listener: Callable[[str], None]) -> None:
        self._key_listeners.append(listener)

    def key_pressed(self, key: str) -> None:
        """Called by the backend for each remote button (a name from couchd.keys)."""
        for listener in list(self._key_listeners):
            listener(key)

    @abstractmethod
    def power(self) -> TvPower: ...

    @abstractmethod
    async def power_on(self) -> None:
        """Wake the TV and switch it to our input."""

    @abstractmethod
    async def standby(self) -> None: ...


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

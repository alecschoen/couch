"""User settings: the things on the Settings screen, kept in one small file.

Every setting has a key (`display.evening`), a type and a default in `SCHEMA` below. Values
are checked against it on the way in, from the UI and from the file alike, so a bad value
never reaches the services that read it.

The file is `/var/lib/couch/settings.json` (next to `state.json`), written atomically and
debounced like the saved state. A value that is missing or no longer valid falls back to its
default; unknown keys in the file are kept as they are, so an older couchd doesn't lose a
newer one's settings.

    settings.get("display.evening")              # "sunset"
    settings.set("display.evening", "always")    # validated, saved, published
    settings.on_change(lambda key, value: ...)   # every change, from anywhere

The UI sees them in the live state section `settings` (key → value), and changes them with
the `settings.set` command. `settings.describe` lists the schema. See docs/protocol.md.

To add a setting, add a line to `SCHEMA`. Keys are `<area>.<name>`, lower case.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from couchd.commands import CommandError, Commands, arg
from couchd.state import LiveState, SavedState

log = logging.getLogger(__name__)

SECTION = "settings"


class SettingError(CommandError):
    pass


@dataclass(frozen=True)
class Setting:
    """`kind` is "bool", "int" or "choice". An int has min/max/step; a choice has choices,
    whose values are strings or ints."""

    key: str
    kind: str
    default: Any
    choices: tuple = ()
    min: int | None = None
    max: int | None = None
    step: int = 1
    label: str = ""

    def check(self, value: Any) -> Any:
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise SettingError(f"{self.key} must be true or false")
            return value
        if self.kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise SettingError(f"{self.key} must be a whole number")
            if not self.min <= value <= self.max or (value - self.min) % self.step:
                raise SettingError(
                    f"{self.key} must be {self.min}-{self.max} in steps of {self.step}"
                )
            return value
        if self.kind == "choice":
            # bool == int in Python, so True would pass for 1 without this.
            if isinstance(value, bool) or value not in self.choices:
                options = ", ".join(repr(c) for c in self.choices)
                raise SettingError(f"{self.key} must be one of {options}")
            return value
        raise AssertionError(self.kind)

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "type": self.kind, "default": self.default}
        if self.label:
            out["label"] = self.label
        if self.kind == "choice":
            out["choices"] = list(self.choices)
        if self.kind == "int":
            out.update(min=self.min, max=self.max, step=self.step)
        return out


def _schema(*settings: Setting) -> dict[str, Setting]:
    for s in settings:
        s.check(s.default)  # a broken default fails at import, in the tests
    return {s.key: s for s in settings}


SCHEMA: dict[str, Setting] = _schema(
    # Read by the launcher (launcher/src/state.rs has the same defaults).
    # "sunset" follows the sun where the Pi's timezone is (couchd/evening.py).
    Setting(
        "display.evening", "choice", "sunset", ("sunset", "always", "off"), label="Evening colors"
    ),
    Setting("display.motion", "choice", "auto", ("auto", "reduced", "full"), label="Reduce motion"),
    # Seconds without a key press on the launcher before it dims to the clock; 0 = never.
    Setting(
        "display.dim_after",
        "choice",
        300,
        (60, 120, 300, 600, 900, 1800, 3600, 0),
        label="Dim to clock after",
    ),
    # Highest volume couchd will set, on any output.
    Setting("audio.volume_limit", "int", 100, min=10, max=100, step=5, label="Volume limit"),
    # Kept for the CEC volume keys (System Audio Mode); nothing acts on it yet.
    Setting("audio.tv_remote_volume", "bool", True, label="TV remote controls volume"),
    # UI sounds (#49, launcher/src/sounds.rs): everywhere, only on the TV speakers, or off.
    Setting("audio.ui_sounds", "choice", "on", ("on", "tv", "off"), label="UI sounds"),
    Setting("bluetooth.auto_reconnect", "bool", True, label="Reconnect Bluetooth at start"),
    # The Wi-Fi radio (#31). Off unless asked for: Couch is meant to be on Ethernet.
    Setting("network.wifi", "bool", False, label="Wi-Fi"),
    # Couch as a speaker for Spotify Connect and AirPlay (#80, couchd/speaker.py).
    Setting("speaker.enabled", "bool", True, label="Spotify and AirPlay"),
    # When music starts: wake the TV and close YouTube or Plex; opening an app stops it.
    Setting("speaker.take_over", "bool", True, label="Music takes over the TV"),
    # The TV goes to standby after 2 hours idle on the launcher (couchd/idle.py).
    Setting("tv.off_when_idle", "bool", False, label="Turn the TV off when idle"),
    # The TV goes to standby when the Pi powers off (not on a reboot).
    Setting("tv.standby_on_poweroff", "bool", False, label="Turn the TV off too"),
    # For the updater (#24); nothing acts on it yet.
    Setting("updates.overnight", "bool", True, label="Install updates overnight"),
)

Listener = Callable[[str, Any], None]


class Settings:
    """The settings store. With `path=None` it keeps values in memory only (tests)."""

    def __init__(
        self,
        state: LiveState | None = None,
        path: Path | None = None,
        *,
        schema: dict[str, Setting] = SCHEMA,
        save_delay: float = 1.0,
    ) -> None:
        self.schema = schema
        self._state = state
        self._file = SavedState(path, save_delay=save_delay) if path is not None else None
        self._values: dict[str, Any] = {k: s.default for k, s in schema.items()}
        self._listeners: list[Listener] = []
        self._publish(self._values)

    @property
    def path(self) -> Path | None:
        return self._file.path if self._file else None

    def load(self) -> None:
        """Read the file. Bad values are logged and replaced by their defaults."""
        if self._file is None:
            return
        self._file.load()
        for key, setting in self.schema.items():
            raw = self._file.get(key, None)
            if raw is None:
                continue
            try:
                self._values[key] = setting.check(raw)
            except SettingError as e:
                log.warning("bad saved setting, using the default", extra={"error": str(e)})
                self._values[key] = setting.default
        self._publish(self._values)

    def adopt(self, saved: SavedState, keys: list[str]) -> None:
        """Move settings older couchd versions kept in state.json, if this file lacks them."""
        if self._file is None:
            return
        for key in keys:
            value = saved.get(key)
            if value is None or self._file.get(key) is not None:
                continue
            try:
                self.set(key, value)
            except SettingError:
                continue
            log.info("moved setting from state.json", extra={"setting": key})

    def get(self, key: str) -> Any:
        return self._values[self._setting(key).key]

    def values(self) -> dict[str, Any]:
        return dict(self._values)

    def is_set(self, key: str) -> bool:
        """Whether `key` was ever set (it's in the file), rather than left at its default."""
        self._setting(key)
        return self._file is not None and self._file.get(key) is not None

    def set(self, key: str, value: Any) -> bool:
        """Validate and store `value`. True if it changed. Raises SettingError if invalid."""
        value = self._setting(key).check(value)
        if self._file is not None:
            self._file.set(key, value)
        if self._values[key] == value:
            return False
        self._values[key] = value
        self._publish({key: value})
        for listener in list(self._listeners):
            try:
                listener(key, value)
            except Exception:
                log.exception("settings listener failed", extra={"setting": key})
        return True

    def on_change(self, listener: Listener) -> Callable[[], None]:
        """Call `listener(key, value)` after every change. Returns a function that stops it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def describe(self) -> list[dict[str, Any]]:
        return [s.describe() for s in self.schema.values()]

    def add_commands(self, commands: Commands) -> None:
        commands.add("settings.set", self._set_command)
        commands.add("settings.reset", self._reset_command)
        commands.add("settings.describe", self._describe_command)

    def _setting(self, key: Any) -> Setting:
        setting = self.schema.get(key) if isinstance(key, str) else None
        if setting is None:
            raise SettingError(f"unknown setting {key!r}")
        return setting

    def _publish(self, values: dict[str, Any]) -> None:
        if self._state is not None:
            self._state.update(SECTION, values)

    async def _set_command(self, args: dict[str, Any]) -> dict[str, Any]:
        key = arg(args, "key", str)
        if "value" not in args:
            raise SettingError("'value' is missing")
        self.set(key, args["value"])
        return {key: self.get(key)}

    async def _reset_command(self, args: dict[str, Any]) -> dict[str, Any]:
        key = arg(args, "key", str)
        self.set(key, self._setting(key).default)
        return {key: self.get(key)}

    async def _describe_command(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self.describe()

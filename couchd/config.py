"""Configuration: defaults in code, overridden by a TOML file.

On the Pi the file is `/etc/couch/config.toml` and is optional. Every key has a default, so
an empty or missing file gives a working setup. Unknown keys produce warnings, not errors,
so a stale config never stops the TV from booting. Bad values raise `ConfigError`.
"""

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

DEVICE_CONFIG_PATH = Path("/etc/couch/config.toml")
DEVICE_STATE_PATH = Path("/var/lib/couch/state.json")
# The repo checkout (or /opt/couch on the Pi) holds ui/ next to the couchd package.
BUNDLED_UI_DIR = Path(__file__).resolve().parent.parent / "ui"

LOG_LEVELS = ("debug", "info", "warning", "error")


class ConfigError(ValueError):
    """The config file can't be parsed or holds a value of the wrong type."""


@dataclass(frozen=True)
class ServerConfig:
    port: int = 8765


@dataclass(frozen=True)
class PathsConfig:
    ui: Path = BUNDLED_UI_DIR
    state: Path = DEVICE_STATE_PATH


@dataclass(frozen=True)
class LogConfig:
    level: str = "info"


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    log: LogConfig = field(default_factory=LogConfig)
    # Problems that didn't stop loading, such as unknown keys. Logged once logging is up.
    warnings: tuple[str, ...] = ()


def dev_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "couch" / "state.json"


def defaults(*, dev: bool = False) -> Config:
    config = Config()
    if dev:
        config = replace(config, paths=replace(config.paths, state=dev_state_path()))
    return config


def load_config(path: Path | None, *, dev: bool = False) -> Config:
    """Load `path` on top of the defaults. A missing file is fine; a broken one is not."""
    base = defaults(dev=dev)
    if path is None or not path.exists():
        return base
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: can't read: {e.strerror}") from e
    return _merge(base, raw, source=str(path))


def _merge(base: Config, raw: dict[str, Any], *, source: str) -> Config:
    warnings: list[str] = []
    sections: dict[str, Any] = {}
    known = {f.name: f for f in fields(Config) if f.name != "warnings"}
    for name, table in raw.items():
        if name not in known:
            warnings.append(f"{source}: unknown section [{name}] ignored")
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [{name}] must be a table")
        sections[name] = _merge_section(getattr(base, name), table, f"{source}: {name}", warnings)
    config = replace(base, **sections, warnings=tuple(warnings))
    _validate(config, source)
    return config


def _merge_section(section: Any, table: dict[str, Any], where: str, warnings: list[str]) -> Any:
    known = {f.name: f for f in fields(section)}
    values: dict[str, Any] = {}
    for key, value in table.items():
        if key not in known:
            warnings.append(f"{where}.{key}: unknown key ignored")
            continue
        values[key] = _coerce(value, known[key].type, f"{where}.{key}")
    return replace(section, **values)


def _coerce(value: Any, expected: Any, where: str) -> Any:
    # bool is an int subclass, so `port = true` must be rejected explicitly.
    if expected is int and isinstance(value, int) and not isinstance(value, bool):
        return value
    if expected is str and isinstance(value, str):
        return value
    if expected is Path and isinstance(value, str) and value:
        return Path(value).expanduser()
    wanted = {int: "an integer", str: "a string", Path: "a path string"}[expected]
    raise ConfigError(f"{where} must be {wanted}, got {value!r}")


def _validate(config: Config, source: str) -> None:
    if not 1 <= config.server.port <= 65535:
        raise ConfigError(f"{source}: server.port must be 1-65535, got {config.server.port}")
    if config.log.level not in LOG_LEVELS:
        choices = ", ".join(LOG_LEVELS)
        raise ConfigError(f"{source}: log.level must be one of {choices}, got {config.log.level!r}")

"""Two kinds of state.

`LiveState` is what the UI shows right now (current audio output, connected devices, which
app is up). It lives in memory, is grouped into sections, and every change goes out on the
bus as a patch.

`SavedState` is what must survive a reboot (preferred output, paired speakers). It is a
small JSON file, written atomically and debounced so a burst of changes is one SD card write.
"""

import asyncio
import contextlib
import copy
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from couchd.bus import EventBus

log = logging.getLogger(__name__)

_MISSING = object()


class LiveState:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._data: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._data)

    def get(self, section: str) -> dict[str, Any]:
        return copy.deepcopy(self._data.get(section, {}))

    def update(self, section: str, values: Mapping[str, Any]) -> None:
        """Merge `values` into `section` and publish only the keys that actually changed."""
        current = self._data.setdefault(section, {})
        changed = {k: v for k, v in values.items() if current.get(k, _MISSING) != v}
        if not changed:
            return
        changed = copy.deepcopy(changed)
        current.update(changed)
        self._bus.publish("state", {section: copy.deepcopy(changed)}, full=False)


class SavedState:
    def __init__(self, path: Path, *, save_delay: float = 1.0) -> None:
        self.path = path
        self._save_delay = save_delay
        self._data: dict[str, Any] = {}
        self._pending: asyncio.TimerHandle | None = None
        self._dirty = False

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except ValueError as e:
            bad = self.path.with_name(self.path.name + ".bad")
            log.warning("saved state unreadable, starting fresh", extra={"error": str(e)})
            with contextlib.suppress(OSError):
                self.path.replace(bad)
            return
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        json.dumps(value)  # fail now, not on the next write
        if self._data.get(key, _MISSING) == value:
            return
        self._data[key] = copy.deepcopy(value)
        self._dirty = True
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self._pending is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        self._pending = loop.call_later(self._save_delay, self.flush)

    def flush(self) -> None:
        """Write now if anything is waiting. Called on the timer and at shutdown."""
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if self._dirty:
            self._write()
            self._dirty = False

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

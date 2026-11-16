"""Updates (#24), couchd's side: what Settings > Updates shows, Check now, and the
"Updated to vX" toast.

The work is done by couch-update (couchd/updater.py), a root service couchd can't be, since
it runs install.sh. couchd reads its status file and starts `couch-update-now.service` for
Check now (system/couch.rules allows that one unit). couch-update asks couchd's /api/state
whether something is playing before it touches anything.

Live state section `updates`:
- `version`: the installed release (/opt/couch/VERSION), or null for an install from a checkout
- `state`: "idle", "checking", "downloading", "installing", "rolling-back" or "system"
- `result`: the last run's: "installed", "up-to-date", "rolled-back", "broken", "failed",
  "waiting", or null before the first; `message`: a sentence about it, or null
- `last_check`: unix time of the last check, or null; `latest`: the newest release seen
- `waiting`: why the last run waited ("something is playing"), or null
- `system`: the last system update, `{"at", "security": bool, "full": bool, "full_at"}`, or null
- `announce`: `{"id", "kind": "installed" | "rolled-back", "version", "kept"}` until the UI
  has shown it (command `updates.announced {id}`), else null

Commands: `updates.check` (Check now), `updates.announced` `{id}`.

`quiet_start()` is true for a little while after couch-update restarted couchd at night, so
TvService doesn't wake the TV at 3 am.
"""

import asyncio
import contextlib
import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from couchd.backends import BackendError
from couchd.commands import CommandError, arg
from couchd.services import Context, Service

log = logging.getLogger(__name__)

WORK = Path("/var/lib/couch-update")
STATUS_FILE = WORK / "status.json"
QUIET_FILE = WORK / "quiet-until"
VERSION_FILE = Path("/opt/couch/VERSION")
NOW_UNIT = "couch-update-now.service"
SAVED_ANNOUNCED = "updates.announced"
POLL = 15.0  # seconds between looks at the status file
POLL_BUSY = 1.0  # while a run is going
KICK_GRACE = 3.0  # after Check now, "checking" until the run says otherwise


def quiet_start(path: Path = QUIET_FILE, now: float | None = None) -> bool:
    """Whether couch-update asked couchd not to wake the TV (it holds a unix time)."""
    try:
        until = float(path.read_text().strip())
    except (OSError, ValueError):
        return False
    return (time.time() if now is None else now) < until


class UpdatesBackend(ABC):
    @abstractmethod
    def status(self) -> dict[str, Any]:
        """couch-update's status file, or {} if there is none yet."""

    @abstractmethod
    def version(self) -> str | None: ...

    @abstractmethod
    async def check_now(self) -> None:
        """Start a run. Raises BackendError if it can't."""


class SystemdUpdates(UpdatesBackend):
    def __init__(self, status_file: Path = STATUS_FILE, version_file: Path = VERSION_FILE):
        self.status_file = status_file
        self.version_file = version_file

    def status(self) -> dict[str, Any]:
        try:
            data = json.loads(self.status_file.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def version(self) -> str | None:
        try:
            return self.version_file.read_text().strip() or None
        except OSError:
            return None

    async def check_now(self) -> None:
        argv = ("systemctl", "--no-ask-password", "--no-block", "start", NOW_UNIT)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            async with asyncio.timeout(10):
                _, err = await proc.communicate()
        except (OSError, TimeoutError) as e:
            raise BackendError(f"Couldn't start the update check: {e}") from None
        if proc.returncode:
            log.warning("couldn't start %s", NOW_UNIT, extra={"error": err.decode().strip()})
            raise BackendError("Couch isn't allowed to check for updates. Check the polkit rule.")


class FakeUpdates(UpdatesBackend):
    """Dev mode: Check now pretends to look, finds a newer version the second time, and
    'installs' it, so the Settings row and the toast can be tried on the PC."""

    def __init__(self, *, step: float = 1.0) -> None:
        self.step = step
        self._status: dict[str, Any] = {"state": "idle", "current": "v0.1.0"}
        self._checks = 0
        self._task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def version(self) -> str | None:
        return self._status.get("current")

    async def check_now(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pretend())

    async def _pretend(self) -> None:
        self._checks += 1
        self._status.update(state="checking", last_check=time.time())
        await asyncio.sleep(self.step)
        if self._checks < 2:
            self._status.update(state="idle", result="up-to-date", message=None, latest="v0.1.0")
            return
        old, new = self._status["current"], f"v0.{self._checks}.0"
        self._status.update(state="installing", message=f"Installing {new}", latest=new)
        await asyncio.sleep(self.step)
        self._status.update(
            state="idle",
            result="installed",
            message=None,
            current=new,
            previous=old,
            event={"kind": "installed", "version": new, "at": time.time()},
        )


class UpdatesService(Service):
    name = "updates"

    def __init__(self, ctx: Context, backend: UpdatesBackend, *, poll: float = POLL) -> None:
        self.ctx = ctx
        self.backend = backend
        self.poll = poll
        self._task: asyncio.Task | None = None
        self._poke = asyncio.Event()
        self._last: dict[str, Any] | None = None
        self._kicked = -1e9  # when Check now last started a run (monotonic)

    async def start(self) -> None:
        self.ctx.commands.add("updates.check", self._check)
        self.ctx.commands.add("updates.announced", self._announced)
        self.refresh()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("updates.")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            busy = (self._last or {}).get("state") not in (None, "idle")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(POLL_BUSY if busy else self.poll):
                    await self._poke.wait()
            self._poke.clear()
            self.refresh()

    def refresh(self) -> None:
        status = self.backend.status()
        state = status.get("state") or "idle"
        if state == "idle" and time.monotonic() - self._kicked < KICK_GRACE:
            state = "checking"  # the unit hasn't written its first status yet
        section = {
            "version": self.backend.version(),
            "state": state,
            "result": status.get("result"),
            "message": status.get("message"),
            "last_check": status.get("last_check"),
            "latest": status.get("latest"),
            "waiting": status.get("waiting"),
            "system": status.get("system"),
            "announce": self._announcement(status.get("event")),
        }
        if section != self._last:
            self._last = section
            self.ctx.state.update("updates", section)

    def _announcement(self, event: Any) -> dict[str, Any] | None:
        if not isinstance(event, dict) or event.get("kind") not in ("installed", "rolled-back"):
            return None
        ident = f"{event['kind']}:{event.get('version')}:{event.get('at')}"
        if self.ctx.saved.get(SAVED_ANNOUNCED) == ident:
            return None
        return {
            "id": ident,
            "kind": event["kind"],
            "version": event.get("version"),
            "kept": event.get("kept"),
        }

    async def _check(self, args: dict[str, Any]) -> None:
        if (self._last or {}).get("state") not in (None, "idle"):
            return
        try:
            await self.backend.check_now()
        except BackendError as e:
            raise CommandError(str(e)) from None
        self._kicked = time.monotonic()
        self.refresh()

    async def _announced(self, args: dict[str, Any]) -> None:
        self.ctx.saved.set(SAVED_ANNOUNCED, arg(args, "id", str))
        self._last = None
        self.refresh()

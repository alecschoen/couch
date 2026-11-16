"""UpdatesService: couchd's side of the updates (Settings > Updates, Check now, the toast)."""

import asyncio
import json

import pytest

from couchd import updates
from couchd.backends import BackendError
from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.services import Context
from couchd.state import LiveState, SavedState
from couchd.updates import FakeUpdates, SystemdUpdates, UpdatesService, quiet_start


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


@pytest.fixture
def files(tmp_path):
    status, version = tmp_path / "status.json", tmp_path / "VERSION"
    return status, version, SystemdUpdates(status, version)


def write(path, data):
    path.write_text(json.dumps(data))


def test_quiet_start(tmp_path):
    f = tmp_path / "quiet-until"
    assert not quiet_start(f, now=100)
    f.write_text("200")
    assert quiet_start(f, now=100)
    assert not quiet_start(f, now=300)
    f.write_text("garbage")
    assert not quiet_start(f, now=100)


async def test_publishes_the_status_file(ctx, files):
    status, version, backend = files
    version.write_text("v0.2.0\n")
    write(status, {"state": "idle", "result": "up-to-date", "last_check": 5, "latest": "v0.2.0"})
    service = UpdatesService(ctx, backend)
    await service.start()
    section = ctx.state.get("updates")
    assert section["version"] == "v0.2.0"
    assert section["result"] == "up-to-date"
    assert section["latest"] == "v0.2.0"
    assert section["announce"] is None
    await service.stop()
    assert not any(n.startswith("updates.") for n in ctx.commands.names())


async def test_no_status_file_yet(ctx, files):
    _, _, backend = files
    service = UpdatesService(ctx, backend)
    await service.start()
    assert ctx.state.get("updates")["version"] is None
    assert ctx.state.get("updates")["state"] == "idle"
    assert ctx.state.get("updates")["result"] is None
    await service.stop()


async def test_announces_an_install_once(ctx, files, tmp_path):
    status, _, backend = files
    event = {"kind": "installed", "version": "v0.2.0", "at": 123.0}
    write(status, {"state": "idle", "result": "installed", "event": event})
    service = UpdatesService(ctx, backend)
    await service.start()
    announce = ctx.state.get("updates")["announce"]
    assert announce == {
        "id": "installed:v0.2.0:123.0",
        "kind": "installed",
        "version": "v0.2.0",
        "kept": None,
    }
    await ctx.commands.run("updates.announced", {"id": announce["id"]})
    assert ctx.state.get("updates")["announce"] is None
    await service.stop()
    ctx.saved.flush()
    # and after a restart it stays shown
    saved = SavedState(tmp_path / "state.json")
    saved.load()
    ctx2 = Context(ctx.bus, LiveState(ctx.bus), saved, Commands())
    service = UpdatesService(ctx2, backend)
    await service.start()
    assert ctx2.state.get("updates")["announce"] is None
    await service.stop()


async def test_announces_a_rollback(ctx, files):
    status, _, backend = files
    event = {"kind": "rolled-back", "version": "v0.3.0", "kept": "v0.2.0", "at": 9}
    write(status, {"result": "rolled-back", "event": event})
    service = UpdatesService(ctx, backend)
    await service.start()
    assert ctx.state.get("updates")["announce"]["kept"] == "v0.2.0"
    await service.stop()


async def test_sees_the_file_change(ctx, files):
    status, _, backend = files
    write(status, {"state": "idle"})
    service = UpdatesService(ctx, backend, poll=0.02)
    await service.start()
    write(status, {"state": "installing", "message": "Installing v0.2.0"})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ctx.state.get("updates")["state"] == "installing":
            break
    assert ctx.state.get("updates")["message"] == "Installing v0.2.0"
    await service.stop()


class Recording(SystemdUpdates):
    def __init__(self, *a, fail=False):
        super().__init__(*a)
        self.fail = fail
        self.calls = 0

    async def check_now(self):
        self.calls += 1
        if self.fail:
            raise BackendError("Couch isn't allowed to check for updates.")


async def test_check_now(ctx, tmp_path):
    backend = Recording(tmp_path / "status.json", tmp_path / "VERSION")
    service = UpdatesService(ctx, backend)
    await service.start()
    await ctx.commands.run("updates.check")
    assert backend.calls == 1
    # "checking" straight away, before the unit writes it
    assert ctx.state.get("updates")["state"] == "checking"
    await ctx.commands.run("updates.check")  # already going
    assert backend.calls == 1
    await service.stop()


async def test_check_now_refused(ctx, tmp_path):
    backend = Recording(tmp_path / "status.json", tmp_path / "VERSION", fail=True)
    service = UpdatesService(ctx, backend)
    await service.start()
    with pytest.raises(CommandError, match="allowed"):
        await ctx.commands.run("updates.check")
    assert ctx.state.get("updates")["state"] == "idle"
    await service.stop()


async def test_systemd_backend_starts_the_unit(monkeypatch, tmp_path):
    seen = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kw):
        seen.append(argv)
        return Proc()

    monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", fake_exec)
    await SystemdUpdates(tmp_path / "s", tmp_path / "v").check_now()
    assert seen == [
        ("systemctl", "--no-ask-password", "--no-block", "start", "couch-update-now.service")
    ]


async def test_fake_updates_in_dev(ctx, monkeypatch):
    monkeypatch.setattr(updates, "KICK_GRACE", 0.02)
    backend = FakeUpdates(step=0.01)
    service = UpdatesService(ctx, backend, poll=0.01)
    await service.start()
    assert ctx.state.get("updates")["version"] == "v0.1.0"

    async def check_until_idle():
        await ctx.commands.run("updates.check")
        await asyncio.sleep(0.05)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if ctx.state.get("updates")["state"] == "idle":
                return

    await check_until_idle()
    assert ctx.state.get("updates")["result"] == "up-to-date"
    await check_until_idle()
    section = ctx.state.get("updates")
    assert section["result"] == "installed"
    assert section["version"] == "v0.2.0"
    assert section["announce"]["version"] == "v0.2.0"
    await service.stop()

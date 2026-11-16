import asyncio
import signal

import pytest

from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.services import Context
from couchd.splash import SplashService
from couchd.state import LiveState, SavedState


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


def fake_proc(tmp_path, pid: int, comm: str):
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "comm").write_text(comm + "\n")
    return proc


async def test_ends_the_splash_after_the_launchers_first_frame(ctx, tmp_path):
    pidfile = tmp_path / "couch-splash.pid"
    pidfile.write_text("4242\n")
    killed: list[tuple[int, int]] = []
    splash = SplashService(
        ctx,
        pidfile=pidfile,
        delay=0.01,
        proc=fake_proc(tmp_path, 4242, "swayimg"),
        kill=lambda pid, sig: killed.append((pid, sig)),
    )
    await splash.start()
    try:
        await ctx.commands.run("ui.first_frame", {"at": 812})
        assert killed == []  # not before the frame has had time to reach the screen
        await asyncio.sleep(0.05)
        assert killed == [(4242, signal.SIGTERM)]
        assert not pidfile.exists()
        # A reload of the launcher sends it again; the splash is long gone.
        await ctx.commands.run("ui.first_frame", {})
        await asyncio.sleep(0.05)
        assert len(killed) == 1
    finally:
        await splash.stop()
    assert "ui.first_frame" not in ctx.commands.names()


def test_leaves_a_reused_pid_alone(ctx, tmp_path):
    pidfile = tmp_path / "couch-splash.pid"
    pidfile.write_text("4242")
    killed = []
    splash = SplashService(
        ctx,
        pidfile=pidfile,
        proc=fake_proc(tmp_path, 4242, "bash"),
        kill=lambda pid, sig: killed.append(pid),
    )
    assert not splash.end_splash()
    assert killed == []
    assert not pidfile.exists()


def test_no_splash_is_fine(ctx, tmp_path):
    def kill(pid, sig):
        raise AssertionError("nothing to kill")

    splash = SplashService(ctx, pidfile=tmp_path / "none.pid", proc=tmp_path, kill=kill)
    assert not splash.end_splash()
    (tmp_path / "junk.pid").write_text("not a pid")
    splash = SplashService(ctx, pidfile=tmp_path / "junk.pid", proc=tmp_path, kill=kill)
    assert not splash.end_splash()
    (tmp_path / "gone.pid").write_text("99")
    splash = SplashService(ctx, pidfile=tmp_path / "gone.pid", proc=tmp_path, kill=kill)
    assert not splash.end_splash()  # no /proc/99: it already exited


def test_a_process_that_exits_meanwhile(ctx, tmp_path):
    pidfile = tmp_path / "couch-splash.pid"
    pidfile.write_text("7")

    def kill(pid, sig):
        raise ProcessLookupError

    splash = SplashService(ctx, pidfile=pidfile, proc=fake_proc(tmp_path, 7, "swayimg"), kill=kill)
    assert not splash.end_splash()

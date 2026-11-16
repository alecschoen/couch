"""SystemService: About details, and Restart / Turn off."""

import asyncio

import pytest

from couchd import system
from couchd.backends import BackendError
from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.services import Context
from couchd.state import LiveState, SavedState
from couchd.system import FakeSystem, SystemService, parse_iw_ssid, parse_route, pretty_model


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


def test_pretty_model():
    assert pretty_model("Raspberry Pi 4 Model B Rev 1.5\0") == "Raspberry Pi 4 Model B"
    assert pretty_model("Some Board\0") == "Some Board"


def test_parse_route():
    out = '[{"dst":"1.1.1.1","gateway":"192.168.1.1","dev":"wlan0","prefsrc":"192.168.1.9"}]'
    assert parse_route(out) == ("wlan0", "192.168.1.9")
    assert parse_route("[]") is None
    assert parse_route("garbage") is None


def test_parse_iw_ssid():
    out = "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: Living room\n\tfreq: 5180\n"
    assert parse_iw_ssid(out) == "Living room"
    assert parse_iw_ssid("Not connected.\n") is None


async def test_info(ctx):
    service = SystemService(ctx, FakeSystem())
    await service.start()
    info = await ctx.commands.run("system.info")
    assert info["name"] == "Couch"
    assert info["network"]["kind"] == "wifi"
    await service.stop()
    assert "system.info" not in ctx.commands.names()


async def test_restart_toasts_then_acts_once(ctx):
    backend = FakeSystem()
    service = SystemService(ctx, backend, power_delay=0.01)
    await service.start()
    with ctx.bus.subscribe() as sub:
        await ctx.commands.run("system.restart")
        await ctx.commands.run("system.restart")  # a second press while waiting is ignored
        await asyncio.sleep(0.05)
        message = await sub.__anext__()
    assert message == {"type": "toast", "data": {"text": "Restarting", "level": "info"}}
    assert backend.calls == ["restart"]
    await service.stop()


async def test_a_refused_power_off_says_so(ctx):
    class Refused(FakeSystem):
        async def power_off(self):
            raise BackendError("not allowed")

    service = SystemService(ctx, Refused(), power_delay=0)
    await service.start()
    with ctx.bus.subscribe() as sub:
        await ctx.commands.run("system.power_off")
        await asyncio.sleep(0.02)
        texts = [(await sub.__anext__())["data"]["text"] for _ in range(2)]
    assert texts == ["Turning off", "not allowed"]
    await service.stop()


async def test_linux_systemctl_failure_is_a_backend_error(monkeypatch):
    async def fake_run(*argv, limit=5.0):
        assert argv == ("systemctl", "--no-ask-password", "reboot")
        return 1, "", "Access denied"

    monkeypatch.setattr(system, "_run", fake_run)
    with pytest.raises(BackendError):
        await system.LinuxSystem().restart()

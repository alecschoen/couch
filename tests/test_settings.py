"""The settings store, its commands, and the services that read it."""

import asyncio
import json
from dataclasses import replace

import pytest

from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.config import defaults
from couchd.daemon import Daemon
from couchd.fakes import FakeAudio
from couchd.idle import IdleService
from couchd.services import Context
from couchd.settings import SCHEMA, Setting, SettingError, Settings
from couchd.state import LiveState, SavedState


@pytest.fixture
def state():
    return LiveState(EventBus())


def test_defaults_are_published(state):
    settings = Settings(state)
    assert state.get("settings") == {k: s.default for k, s in SCHEMA.items()}
    assert settings.get("display.evening") == "sunset"


def test_set_validates_saves_and_publishes(state, tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(state, path, save_delay=0)
    seen = []
    settings.on_change(lambda k, v: seen.append((k, v)))
    assert settings.set("display.evening", "always") is True
    assert settings.set("display.evening", "always") is False
    settings.flush()
    assert json.loads(path.read_text())["display.evening"] == "always"
    assert state.get("settings")["display.evening"] == "always"
    assert seen == [("display.evening", "always")]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("display.evening", "dusk"),
        ("display.dim_after", 7),
        ("display.dim_after", True),
        ("audio.volume_limit", 101),
        ("audio.volume_limit", 82),
        ("audio.volume_limit", 80.0),
        ("bluetooth.auto_reconnect", 1),
        ("nope.nothing", True),
    ],
)
def test_bad_values_are_refused(state, key, value):
    settings = Settings(state)
    with pytest.raises(SettingError):
        settings.set(key, value)


def test_load_keeps_good_values_and_drops_bad_ones(state, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"display.evening": "off", "audio.volume_limit": 3, "from.the.future": "kept"})
    )
    settings = Settings(state, path, save_delay=0)
    settings.load()
    assert settings.get("display.evening") == "off"
    assert settings.get("audio.volume_limit") == 100
    settings.set("updates.overnight", False)
    settings.flush()
    assert json.loads(path.read_text())["from.the.future"] == "kept"


def test_a_broken_file_is_set_aside(state, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    settings = Settings(state, path)
    settings.load()
    assert settings.get("display.evening") == "sunset"
    assert (tmp_path / "settings.json.bad").exists()


def test_adopts_old_keys_from_state_json(state, tmp_path):
    saved = SavedState(tmp_path / "state.json")
    saved.set("tv.standby_on_poweroff", True)
    settings = Settings(state, tmp_path / "settings.json", save_delay=0)
    settings.load()
    settings.adopt(saved, ["tv.standby_on_poweroff", "bluetooth.auto_reconnect"])
    assert settings.get("tv.standby_on_poweroff") is True
    assert settings.get("bluetooth.auto_reconnect") is True
    # Once the settings file has it, state.json no longer counts.
    saved.set("tv.standby_on_poweroff", False)
    settings.adopt(saved, ["tv.standby_on_poweroff"])
    assert settings.get("tv.standby_on_poweroff") is True


async def test_commands(state):
    settings = Settings(state)
    commands = Commands()
    settings.add_commands(commands)
    assert await commands.run("settings.set", {"key": "audio.volume_limit", "value": 80}) == {
        "audio.volume_limit": 80
    }
    assert await commands.run("settings.reset", {"key": "audio.volume_limit"}) == {
        "audio.volume_limit": 100
    }
    with pytest.raises(CommandError, match="unknown setting"):
        await commands.run("settings.set", {"key": "x", "value": 1})
    with pytest.raises(CommandError, match="missing"):
        await commands.run("settings.set", {"key": "audio.volume_limit"})
    described = await commands.run("settings.describe")
    limit = next(d for d in described if d["key"] == "audio.volume_limit")
    assert limit == {
        "key": "audio.volume_limit",
        "type": "int",
        "default": 100,
        "label": "Volume limit",
        "min": 10,
        "max": 100,
        "step": 5,
    }


def test_a_bad_default_fails_early():
    with pytest.raises(SettingError):
        Setting("x.y", "choice", "c", ("a", "b")).check("c")


# --- services reading settings --------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


async def test_idle_follows_dim_after(ctx):
    ctx.state.update("apps", {"foreground": "launcher"})
    service = IdleService(ctx)
    await service.start()
    try:
        assert ctx.state.get("idle")["after"] == 300
        ctx.settings.set("display.dim_after", 0)  # never
        assert ctx.state.get("idle")["after"] == 0
        service._last -= 10_000
        service._poke.set()
        await asyncio.sleep(0.05)
        assert not ctx.state.get("idle")["idle"]
        ctx.settings.set("display.dim_after", 60)
        for _ in range(50):
            if ctx.state.get("idle")["idle"]:
                break
            await asyncio.sleep(0.01)
        assert ctx.state.get("idle")["idle"]
    finally:
        await service.stop()


async def test_volume_limit(ctx):
    from couchd.audio import AudioService

    service = AudioService(ctx, FakeAudio(speed=0))
    await service.start()
    try:
        await ctx.commands.run("audio.set_volume", {"volume": 90})
        assert ctx.state.get("audio")["volume"] == 90
        ctx.settings.set("audio.volume_limit", 60)
        for _ in range(50):
            if ctx.state.get("audio")["volume"] == 60:
                break
            await asyncio.sleep(0.01)
        assert ctx.state.get("audio")["volume"] == 60
        await ctx.commands.run("audio.set_volume", {"volume": 100})
        assert ctx.state.get("audio")["volume"] == 60
    finally:
        await service.stop()


async def test_old_commands_write_the_setting(ctx):
    from couchd.bluetooth import BluetoothService
    from couchd.fakes import FakeBluetooth

    service = BluetoothService(ctx, FakeBluetooth(FakeAudio(speed=0), speed=0))
    await service.start()
    try:
        await ctx.commands.run("bluetooth.set_auto_reconnect", {"on": False})
        assert ctx.settings.get("bluetooth.auto_reconnect") is False
        assert ctx.state.get("bluetooth")["auto_reconnect"] is False
        ctx.settings.set("bluetooth.auto_reconnect", True)
        assert ctx.state.get("bluetooth")["auto_reconnect"] is True
    finally:
        await service.stop()


async def test_settings_survive_a_daemon_restart(tmp_path):
    config = defaults(dev=True)
    config = replace(
        config,
        server=replace(config.server, port=0),
        paths=replace(config.paths, state=tmp_path / "state.json"),
    )
    first = Daemon(config, dev=True, services=[])
    await first.start()
    await first.commands.run("settings.set", {"key": "display.evening", "value": "off"})
    await first.stop()
    assert json.loads((tmp_path / "settings.json").read_text())["display.evening"] == "off"

    second = Daemon(config, dev=True, services=[])
    await second.start()
    try:
        assert second.state.get("settings")["display.evening"] == "off"
    finally:
        await second.stop()


def test_ui_sounds_default_on_and_take_only_known_modes(state):
    settings = Settings(state)
    assert settings.get("audio.ui_sounds") == "on"
    settings.set("audio.ui_sounds", "tv")
    assert state.get("settings")["audio.ui_sounds"] == "tv"
    with pytest.raises(SettingError):
        settings.set("audio.ui_sounds", "loud")
    assert settings.get("audio.ui_sounds") == "tv"

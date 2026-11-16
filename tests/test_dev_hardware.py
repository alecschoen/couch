"""The fake backends and the services on top of them, driven through commands like the UI."""

import asyncio
import re
from pathlib import Path

import pytest

from couchd.audio import AudioService
from couchd.bluetooth import BluetoothService
from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.fakes import ALREADY_PAIRED, NEARBY, REFUSES_TO_PAIR, FakeAudio, FakeBluetooth, FakeTv
from couchd.keys import REMOTE_KEYS
from couchd.services import Context, ServiceRegistry
from couchd.state import LiveState, SavedState
from couchd.tv import RemoteService, TvService

ROOT = Path(__file__).resolve().parent.parent
SPEAKER = NEARBY[0][1]


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


@pytest.fixture
async def rig(ctx):
    tv = FakeTv(speed=0)
    audio = FakeAudio(speed=0)
    bluetooth = FakeBluetooth(audio, speed=0)
    registry = ServiceRegistry(ctx.state)
    for service in (
        TvService(ctx, tv),
        RemoteService(ctx, tv),
        AudioService(ctx, audio),
        BluetoothService(ctx, bluetooth),
    ):
        registry.add(service)
    await registry.start_all()
    yield ctx
    await registry.stop_all()


async def run(ctx, name, **args):
    return await ctx.commands.run(name, args)


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


# --- state the UI sees at startup ----------------------------------------------------------


async def test_everything_starts_and_reports(rig):
    snap = rig.state.snapshot()
    assert snap["services"] == {
        "tv": "running",
        "remote": "running",
        "audio": "running",
        "bluetooth": "running",
    }
    assert snap["tv"] == {"power": "on", "active": True, "standby_on_poweroff": False}
    assert [o["id"] for o in snap["audio"]["outputs"]] == ["tv", "jack"]
    assert snap["audio"]["current"] == "tv"
    assert snap["bluetooth"] == {
        "scanning": False,
        "devices": [_dict(ALREADY_PAIRED)],
        "auto_reconnect": True,
    }


def _dict(device):
    return {
        "address": device.address,
        "name": device.name,
        "audio": device.audio,
        "paired": device.paired,
        "connected": device.connected,
    }


# --- TV and remote -------------------------------------------------------------------------


async def test_tv_power(rig):
    await run(rig, "tv.standby")
    assert rig.state.get("tv") == {
        "power": "standby",
        "active": False,
        "standby_on_poweroff": False,
    }
    await run(rig, "tv.power_on")
    assert rig.state.get("tv")["power"] == "on"
    assert rig.state.get("tv")["active"]


async def test_tv_standby_pauses_whatever_is_playing(rig):
    sub = rig.bus.subscribe()
    await run(rig, "tv.standby")
    assert await sub.get() == {"type": "key", "data": {"key": "pause"}}


async def test_remote_press_becomes_a_key_message(rig):
    sub = rig.bus.subscribe()
    await run(rig, "remote.press", key="red")
    assert await sub.get() == {"type": "key", "data": {"key": "red"}}


async def test_remote_rejects_unknown_keys(rig):
    with pytest.raises(CommandError, match="unknown key"):
        await run(rig, "remote.press", key="self_destruct")


def test_keyboard_map_only_uses_known_remote_keys():
    js = (ROOT / "ui" / "js" / "keys.js").read_text()
    table = js.split("export const KEYBOARD = {", 1)[1].split("};", 1)[0]
    values = set(re.findall(r':\s*"([^"]+)"', table))
    assert values, "couldn't parse the KEYBOARD table"
    assert values <= REMOTE_KEYS
    # The ones the issue promises:
    for name in ("up", "down", "left", "right", "select", "back", "red", "green", "yellow", "blue"):
        assert name in values


# --- audio ---------------------------------------------------------------------------------


async def test_switch_output_saves_and_toasts(rig):
    sub = rig.bus.subscribe()
    await run(rig, "audio.set_output", output="jack")
    assert rig.state.get("audio")["current"] == "jack"
    assert rig.saved.get("audio.output") == "jack"
    messages = [m async for m in _drain(sub)]
    assert {"type": "toast", "data": {"text": "Sound now on Headphone jack", "level": "info"}} in (
        messages
    )


async def _drain(sub):
    while not sub._queue.empty():
        yield await sub.get()


async def test_saved_output_is_restored_on_start(ctx):
    ctx.saved.set("audio.output", "jack")
    service = AudioService(ctx, FakeAudio(speed=0))
    await service.start()
    assert ctx.state.get("audio")["current"] == "jack"


async def test_saved_bluetooth_output_that_is_gone_falls_back(ctx):
    ctx.saved.set("audio.output", "bluetooth:GO:NE")
    service = AudioService(ctx, FakeAudio(speed=0))
    await service.start()
    assert ctx.state.get("audio")["current"] == "tv"


@pytest.mark.parametrize(
    ("name", "args", "error"),
    [
        ("audio.set_output", {"output": "moon"}, "no output"),
        ("audio.set_output", {}, "'output' must be a string"),
        ("audio.set_volume", {"volume": 101}, "0-100"),
        ("audio.set_volume", {"volume": True}, "must be an integer"),
        ("audio.set_muted", {"muted": "yes"}, "true or false"),
    ],
)
async def test_audio_rejects_bad_args(rig, name, args, error):
    with pytest.raises(CommandError, match=error):
        await rig.commands.run(name, args)


async def test_volume_and_mute(rig):
    await run(rig, "audio.set_volume", volume=65)
    await run(rig, "audio.set_muted", muted=True)
    audio = rig.state.get("audio")
    assert (audio["volume"], audio["muted"]) == (65, True)


# --- Bluetooth -----------------------------------------------------------------------------


async def test_scan_finds_devices_and_stop_clears_unpaired(rig):
    await run(rig, "bluetooth.scan", on=True)
    await settle()
    bt = rig.state.get("bluetooth")
    assert bt["scanning"] is True
    names = [d["name"] for d in bt["devices"]]
    assert names[0] == ALREADY_PAIRED.name  # paired first
    assert set(names) == {ALREADY_PAIRED.name, *(d.name for _, d in NEARBY)}

    await run(rig, "bluetooth.scan", on=False)
    assert rig.state.get("bluetooth") == {
        "scanning": False,
        "devices": [_dict(ALREADY_PAIRED)],
        "auto_reconnect": True,
    }


async def test_pair_connect_adds_an_output_and_disconnect_falls_back(rig):
    await run(rig, "bluetooth.scan", on=True)
    await settle()
    await run(rig, "bluetooth.pair", address=SPEAKER.address)
    await run(rig, "bluetooth.connect", address=SPEAKER.address)

    output_id = f"bluetooth:{SPEAKER.address}"
    assert output_id in [o["id"] for o in rig.state.get("audio")["outputs"]]
    await run(rig, "audio.set_output", output=output_id)
    assert rig.state.get("audio")["current"] == output_id

    await run(rig, "bluetooth.disconnect", address=SPEAKER.address)
    audio = rig.state.get("audio")
    assert audio["current"] == "tv"
    assert output_id not in [o["id"] for o in audio["outputs"]]


async def test_pairing_failure_is_a_command_error(rig):
    await run(rig, "bluetooth.scan", on=True)
    await settle()
    with pytest.raises(CommandError, match="didn't accept"):
        await run(rig, "bluetooth.pair", address=REFUSES_TO_PAIR)


async def test_connect_needs_pairing(rig):
    await run(rig, "bluetooth.scan", on=True)
    await settle()
    with pytest.raises(CommandError, match="isn't paired"):
        await run(rig, "bluetooth.connect", address=SPEAKER.address)


async def test_forget(rig):
    await run(rig, "bluetooth.connect", address=ALREADY_PAIRED.address)
    await run(rig, "bluetooth.forget", address=ALREADY_PAIRED.address)
    assert rig.state.get("bluetooth")["devices"] == []
    assert [o["id"] for o in rig.state.get("audio")["outputs"]] == ["tv", "jack"]


async def test_unknown_device(rig):
    with pytest.raises(CommandError, match="no device"):
        await run(rig, "bluetooth.connect", address="00:00")


# --- commands disappear with their service -------------------------------------------------


async def test_commands_unregister_on_stop(ctx):
    registry = ServiceRegistry(ctx.state)
    registry.add(AudioService(ctx, FakeAudio(speed=0)))
    await registry.start_all()
    assert "audio.set_output" in ctx.commands.names()
    await registry.stop_all()
    assert ctx.commands.names() == []
    with pytest.raises(CommandError, match="unknown command"):
        await ctx.commands.run("audio.set_output", {"output": "tv"})

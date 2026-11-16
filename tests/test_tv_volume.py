"""The TV remote's volume buttons for the jack and Bluetooth (#11), against the fakes, plus
the `volume_changed` message and the volume commands they share with the quick menu."""

import asyncio

import pytest

from couchd.audio import AudioService
from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.fakes import FakeAudio, FakeTv
from couchd.services import Context
from couchd.state import LiveState, SavedState
from couchd.tv import TvService
from couchd.tv_volume import SETTING, TvVolumeService


async def settle():
    for _ in range(30):
        await asyncio.sleep(0)


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


@pytest.fixture
async def rig(ctx):
    tv = FakeTv(speed=0)
    audio = AudioService(ctx, FakeAudio(speed=0))
    services = [
        TvService(ctx, tv, wake_on_start=False),
        audio,
        TvVolumeService(ctx, tv, audio, repeat_delay=0.05, repeat_every=0.01),
    ]
    for service in services:
        await service.start()
    await settle()
    yield tv, audio
    for service in reversed(services):
        await service.stop()


def volume_events(sub):
    out = []
    while not sub._queue.empty():
        message = sub._queue.get_nowait()
        if message["type"] == "volume_changed":
            out.append(message["data"])
    return out


async def run(ctx, name, **args):
    return await ctx.commands.run(name, args)


async def press(tv, code, *, release=True):
    await tv.simulate(bytes([0x05, 0x44, code]))
    if release:
        await tv.simulate(bytes([0x05, 0x45]))
    await settle()


async def test_sound_on_the_tv_leaves_the_tv_its_buttons(ctx, rig):
    tv, audio = rig
    assert not tv.system_audio()
    assert ctx.state.get("tv")["system_audio"] is False
    sub = ctx.bus.subscribe()
    await press(tv, 0x41)
    assert volume_events(sub) == []
    assert audio.backend.volume() == 40


async def test_jack_takes_the_buttons_and_tv_gives_them_back(ctx, rig):
    tv, _ = rig
    await run(ctx, "audio.set_output", output="jack")
    await settle()
    assert tv.system_audio()
    assert ctx.state.get("tv")["system_audio"] is True
    await run(ctx, "audio.set_output", output="tv")
    await settle()
    assert not tv.system_audio()


async def test_volume_up_down_and_mute_change_the_jack(ctx, rig):
    tv, audio = rig
    await run(ctx, "audio.set_output", output="jack")
    await settle()
    sub = ctx.bus.subscribe()
    await press(tv, 0x41)
    assert audio.backend.volume() == 27
    await press(tv, 0x42)
    await press(tv, 0x42)
    assert audio.backend.volume() == 23
    await press(tv, 0x43)
    assert audio.backend.muted()
    await press(tv, 0x41)  # up unmutes
    assert not audio.backend.muted()
    await press(tv, 0x65)
    assert audio.backend.muted()
    await press(tv, 0x66)
    assert not audio.backend.muted()
    events = volume_events(sub)
    assert [e["volume"] for e in events] == [27, 25, 23, 23, 25, 25, 25]
    assert events[0] == {
        "output": "jack",
        "kind": "jack",
        "name": "Headphone jack",
        "volume": 27,
        "muted": False,
        "limit": 100,
        "source": "tv_remote",
    }
    # And the TV was told every time.
    assert tv.reports[-1] == (25, False)
    assert ctx.saved.get("audio.volumes")["jack"] == 25


async def test_volume_limit_is_respected(ctx, rig):
    tv, audio = rig
    ctx.settings.set("audio.volume_limit", 30)
    await run(ctx, "audio.set_output", output="jack")
    await run(ctx, "audio.set_volume", volume=29)
    await settle()
    sub = ctx.bus.subscribe()
    await press(tv, 0x41)
    await press(tv, 0x41)
    assert audio.backend.volume() == 30
    events = volume_events(sub)
    assert [(e["volume"], e["limit"]) for e in events] == [(30, 30), (30, 30)]
    assert tv.reports[-2:] == [(30, False), (30, False)]  # the TV shows it's at the top


async def test_held_button_repeats_when_the_tv_doesnt(ctx, rig):
    tv, audio = rig
    await run(ctx, "audio.set_output", output="jack")
    await settle()
    await press(tv, 0x41, release=False)
    await asyncio.sleep(0.1)
    await tv.simulate(bytes([0x05, 0x45]))
    after = audio.backend.volume()
    assert after > 27  # repeated after the delay
    await asyncio.sleep(0.05)
    assert audio.backend.volume() == after  # and stopped on release


async def test_tv_repeats_are_steps_and_cancel_our_repeat(ctx, rig):
    tv, audio = rig
    await run(ctx, "audio.set_output", output="jack")
    await settle()
    for _ in range(3):
        await press(tv, 0x42, release=False)
    await tv.simulate(bytes([0x05, 0x45]))
    await settle()
    assert audio.backend.volume() == 19


async def test_setting_off_gives_the_buttons_back(ctx, rig):
    tv, audio = rig
    await run(ctx, "audio.set_output", output="jack")
    await settle()
    ctx.settings.set(SETTING, False)
    await settle()
    assert not tv.system_audio()
    await press(tv, 0x41)
    assert audio.backend.volume() == 25
    ctx.settings.set(SETTING, True)
    await settle()
    assert tv.system_audio()


async def test_stop_gives_the_buttons_back(ctx, tmp_path):
    tv = FakeTv(speed=0)
    audio = AudioService(ctx, FakeAudio(speed=0))
    volume = TvVolumeService(ctx, tv, audio)
    await audio.start()
    await volume.start()
    await run(ctx, "audio.set_output", output="jack")
    await settle()
    assert tv.system_audio()
    await volume.stop()
    assert not tv.system_audio()
    await audio.stop()


# --- the commands, for the quick menu --------------------------------------------------------


async def test_volume_commands_send_volume_changed(ctx, rig):
    sub = ctx.bus.subscribe()
    await run(ctx, "audio.step_volume", by=5)
    await run(ctx, "audio.toggle_muted")
    await run(ctx, "audio.set_muted", muted=False)
    await run(ctx, "audio.set_volume", volume=10)
    events = volume_events(sub)
    assert [(e["volume"], e["muted"], e["source"]) for e in events] == [
        (45, False, "command"),
        (45, True, "command"),
        (45, False, "command"),
        (10, False, "command"),
    ]
    assert events[0]["output"] == "tv"
    with pytest.raises(CommandError):
        await run(ctx, "audio.step_volume", by=500)


async def test_lowering_the_limit_says_so(ctx, rig):
    sub = ctx.bus.subscribe()
    ctx.settings.set("audio.volume_limit", 20)
    await settle()
    assert [(e["volume"], e["source"]) for e in volume_events(sub)] == [(20, "limit")]


async def test_simulate_command_checks_the_frame(ctx, rig):
    with pytest.raises(CommandError, match="hex"):
        await run(ctx, "tv.simulate", frame="zz")
    await run(ctx, "tv.simulate", frame="05:44:41")  # sound on the TV: ignored, no error

"""AudioService: which output sound goes to, volume per output, toasts and the Red button.

Run against the dev-mode fake and against PipewireAudio on a pretend server, since both
have to behave the same.
"""

import asyncio

import pytest
from fakepulse import (
    PI_HDMI0,
    PI_JACK,
    SPEAKER,
    SPEAKER_ADDRESS,
    FakePulse,
    pi_hdmi,
    pi_jack,
    speaker,
    until,
)

from couchd import audio as audio_module
from couchd import pipewire
from couchd.audio import SAVED_OUTPUT, SAVED_VOLUMES, AudioService
from couchd.backends import BackendError, Device, Output
from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.fakes import FakeAudio
from couchd.pipewire import PipewireAudio
from couchd.services import Context
from couchd.state import LiveState, SavedState

BT = f"bluetooth:{SPEAKER_ADDRESS}"
HELD = 0.05  # ANNOUNCE_DELAY in these tests


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(pipewire, "SETTLE", 0)
    monkeypatch.setattr(pipewire, "RETRY_MIN", 0.01)
    monkeypatch.setattr(audio_module, "ANNOUNCE_DELAY", HELD)


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def toasts(sub):
    out = []
    while not sub._queue.empty():
        message = sub._queue.get_nowait()
        if message["type"] == "toast":
            out.append(message["data"]["text"])
    return out


async def run(ctx, name, **args):
    return await ctx.commands.run(name, args)


@pytest.fixture
async def fake(ctx):
    service = AudioService(ctx, FakeAudio(speed=0))
    await service.start()
    yield service
    await service.stop()


# --- picking the output --------------------------------------------------------------------


async def test_boot_is_quiet(ctx):
    sub = ctx.bus.subscribe()
    service = AudioService(ctx, FakeAudio(speed=0))
    await service.start()
    await settle()
    assert ctx.state.get("audio")["current"] == "tv"
    assert toasts(sub) == []
    await service.stop()


async def test_choosing_an_output_saves_it_and_toasts_once(ctx, fake):
    sub = ctx.bus.subscribe()
    await run(ctx, "audio.set_output", output="jack")
    await settle()
    assert ctx.state.get("audio")["current"] == "jack"
    assert ctx.saved.get(SAVED_OUTPUT) == "jack"
    assert toasts(sub) == ["Sound now on Headphone jack"]


async def test_choosing_the_current_output_says_nothing(ctx, fake):
    sub = ctx.bus.subscribe()
    await run(ctx, "audio.set_output", output="tv")
    await settle()
    assert toasts(sub) == []


async def test_no_saved_choice_means_the_tv(ctx):
    audio = FakeAudio(speed=0)
    audio._current = "jack"  # wherever PipeWire happened to start
    service = AudioService(ctx, audio)
    await service.start()
    assert audio.current() == "tv"
    await service.stop()


async def test_unavailable_output_cant_be_chosen(ctx, fake):
    fake.backend._outputs[1] = Output("jack", "Headphone jack", "jack", available=False)
    with pytest.raises(CommandError, match="isn't available"):
        await run(ctx, "audio.set_output", output="jack")
    assert ctx.saved.get(SAVED_OUTPUT) is None


async def test_failed_switch_keeps_the_old_choice(ctx, fake):
    await run(ctx, "audio.set_output", output="jack")

    async def broken(output_id):
        raise BackendError("PipeWire said no")

    fake.backend.set_output = broken
    with pytest.raises(BackendError):
        await fake.choose("tv")
    assert ctx.saved.get(SAVED_OUTPUT) == "jack"


async def test_a_stuck_output_is_not_retried_on_every_change(ctx):
    class Stuck(FakeAudio):
        calls = 0

        async def set_output(self, output_id):
            Stuck.calls += 1  # accepts, but sound stays where it was

    audio = Stuck(speed=0)
    audio._current = "jack"
    service = AudioService(ctx, audio)
    await service.start()
    for _ in range(5):
        audio.changed()
        await settle()
    assert Stuck.calls == 1
    await service.stop()


# --- volume per output ---------------------------------------------------------------------


async def test_volume_is_remembered_per_output(ctx, fake):
    await run(ctx, "audio.set_volume", volume=70)
    await run(ctx, "audio.set_output", output="jack")
    assert ctx.state.get("audio")["volume"] == 25  # the jack's own volume
    await run(ctx, "audio.set_volume", volume=10)
    await run(ctx, "audio.set_output", output="tv")
    assert ctx.state.get("audio")["volume"] == 70
    assert ctx.saved.get(SAVED_VOLUMES) == {"tv": 70, "jack": 10}


async def test_saved_volume_is_restored_at_boot(ctx):
    ctx.saved.set(SAVED_VOLUMES, {"tv": 55})
    service = AudioService(ctx, FakeAudio(speed=0))
    await service.start()
    assert ctx.state.get("audio")["volume"] == 55
    await service.stop()


# --- the Red button ------------------------------------------------------------------------


async def test_red_cycles_through_available_outputs(ctx, fake):
    sub = ctx.bus.subscribe()
    fake.backend.bluetooth_connected(_device())
    await settle()
    seen = []
    for _ in range(3):
        ctx.bus.publish("key", {"key": "red"})
        await settle()
        seen.append(ctx.state.get("audio")["current"])
    assert seen == ["jack", "bluetooth:FA:KE", "tv"]
    assert ctx.saved.get(SAVED_OUTPUT) == "tv"
    assert toasts(sub) == [
        "Sound now on Headphone jack",
        "Sound now on Desk Speaker",
        "Sound now on TV speakers",
    ]


async def test_red_skips_unavailable_outputs(ctx, fake):
    fake.backend._outputs[1] = Output("jack", "Headphone jack", "jack", available=False)
    sub = ctx.bus.subscribe()
    ctx.bus.publish("key", {"key": "red"})
    await settle()
    assert ctx.state.get("audio")["current"] == "tv"
    assert toasts(sub) == ["Sound is on TV speakers, the only output"]


async def test_other_keys_leave_sound_alone(ctx, fake):
    ctx.bus.publish("key", {"key": "green"})
    await settle()
    assert ctx.state.get("audio")["current"] == "tv"


async def test_cycle_command(ctx, fake):
    await run(ctx, "audio.cycle_output")
    assert ctx.state.get("audio")["current"] == "jack"


def _device():
    return Device("FA:KE", "Desk Speaker", audio=True, paired=True, connected=True)


# --- end to end on (pretend) PipeWire -----------------------------------------------------


@pytest.fixture
async def pi(ctx):
    server = FakePulse([pi_hdmi(volume=60), pi_jack(volume=25)], default=PI_JACK)
    audio = PipewireAudio(server)
    service = AudioService(ctx, audio)
    await service.start()
    await until(lambda: audio.current() == "tv")
    yield server, service
    await service.stop()


async def test_pipewire_comes_up_late_and_sound_goes_to_the_tv(pi):
    server, _ = pi
    assert server.default == PI_HDMI0


async def test_bluetooth_drops_out_to_the_tv_and_comes_back(ctx, pi):
    server, _ = pi
    sub = ctx.bus.subscribe()
    server.add_sink(speaker())
    await until(lambda: BT in [o["id"] for o in ctx.state.get("audio")["outputs"]])
    server.play(100, on=PI_HDMI0)
    await run(ctx, "audio.set_output", output=BT)
    assert server.stream_sinks() == {100: SPEAKER}

    # The speaker goes out of range. WirePlumber falls back to the jack (higher priority);
    # couchd puts sound on the TV, with one toast.
    server.remove_sink(SPEAKER)
    await until(lambda: server.default == PI_HDMI0)
    await asyncio.sleep(HELD * 2)
    assert server.stream_sinks() == {100: PI_HDMI0}
    assert ctx.saved.get(SAVED_OUTPUT) == BT  # still the user's choice

    # It's back: so is the sound.
    server.add_sink(speaker())
    await until(lambda: server.default == SPEAKER)
    assert server.stream_sinks() == {100: SPEAKER}
    await asyncio.sleep(HELD * 2)
    assert toasts(sub) == [
        "Sound now on Kitchen Speaker",
        "Sound now on TV speakers",
        "Sound now on Kitchen Speaker",
    ]


async def test_hdmi_blip_when_the_tv_switches_input_is_quiet(ctx, pi):
    # Seen on the Pi: as the TV switches to our input, the HDMI sink vanishes for ~0.65 s.
    # WirePlumber sends sound to the highest-priority sink (a connected speaker), and back.
    server, _ = pi
    server.add_sink(speaker())
    await until(lambda: BT in [o["id"] for o in ctx.state.get("audio")["outputs"]])
    sub = ctx.bus.subscribe()
    hdmi = server.sinks[PI_HDMI0]
    server.remove_sink(PI_HDMI0)
    await until(lambda: ctx.state.get("audio")["current"] == BT)
    server.add_sink(hdmi)
    await until(lambda: server.default == PI_HDMI0)
    await asyncio.sleep(HELD * 2)
    assert ctx.state.get("audio")["current"] == "tv"
    assert toasts(sub) == []


async def test_a_fallback_that_holds_is_announced_after_the_delay(ctx, pi):
    server, _ = pi
    sub = ctx.bus.subscribe()
    server.remove_sink(PI_HDMI0)  # the TV is really gone: the jack is all there is
    await until(lambda: ctx.state.get("audio")["current"] == "jack")
    assert toasts(sub) == []
    await asyncio.sleep(HELD * 2)
    assert toasts(sub) == ["Sound now on Headphone jack"]


async def test_pipewire_restart_is_quiet(ctx, pi):
    server, _ = pi
    sub = ctx.bus.subscribe()
    server.go_down()
    await until(lambda: ctx.state.get("audio")["outputs"] == [])
    server.come_up()
    await until(lambda: ctx.state.get("audio")["current"] == "tv")
    await settle()
    assert toasts(sub) == []

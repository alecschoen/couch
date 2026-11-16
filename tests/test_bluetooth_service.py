"""BluetoothService on the fakes: pairing in one press, sound following the speaker,
auto-reconnect, and the scan limit."""

import asyncio

import pytest

from couchd.audio import SAVED_OUTPUT, AudioService
from couchd.backends import BackendError
from couchd.bluetooth import SAVED_AUTO_RECONNECT, SAVED_LAST, BluetoothService, _output_matches
from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.fakes import ALREADY_PAIRED, NEARBY, FakeAudio, FakeBluetooth
from couchd.services import Context, ServiceRegistry
from couchd.state import LiveState, SavedState

SPEAKER = NEARBY[0][1]
PHONE = NEARBY[2][1]
KITCHEN = ALREADY_PAIRED
FAST = (0.01,)


class FlakyBluetooth(FakeBluetooth):
    """The kitchen speaker answers only while `in_range`."""

    def __init__(self, audio, *, speed=0):
        super().__init__(audio, speed=speed)
        self.in_range = True
        self.attempts = 0

    async def connect(self, address):
        self.attempts += 1
        if not self.in_range:
            raise BackendError("Couldn't reach it. Is it on?")
        await super().connect(address)


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


@pytest.fixture
def toasts(ctx):
    sub = ctx.bus.subscribe()

    def read():
        texts = []
        while not sub._queue.empty():
            message = sub._queue.get_nowait()
            if message["type"] == "toast":
                texts.append(message["data"]["text"])
        return texts

    return read


class Rig:
    def __init__(self, ctx, bluetooth_cls=FakeBluetooth, **options):
        self.ctx = ctx
        self.audio = FakeAudio(speed=0)
        self.bluetooth = bluetooth_cls(self.audio, speed=0)
        options.setdefault("reconnect_delays", FAST)
        options.setdefault("output_wait", 1.0)
        self.service = BluetoothService(ctx, self.bluetooth, **options)
        self.registry = ServiceRegistry(ctx.state)
        self.registry.add(AudioService(ctx, self.audio))
        self.registry.add(self.service)

    async def __aenter__(self):
        await self.registry.start_all()
        return self

    async def __aexit__(self, *exc):
        await self.registry.stop_all()

    async def run(self, name, **args):
        return await self.ctx.commands.run(name, args)

    def bt(self):
        return self.ctx.state.get("bluetooth")

    def connected(self):
        return {d["address"] for d in self.bt()["devices"] if d["connected"]}

    def sound(self):
        return self.ctx.state.get("audio")["current"]


async def settle(seconds=0.0):
    for _ in range(20):
        await asyncio.sleep(0)
    if seconds:
        await asyncio.sleep(seconds)
        for _ in range(20):
            await asyncio.sleep(0)


async def until(check, within=1.0):
    async with asyncio.timeout(within):
        while not check():  # noqa: ASYNC110 - polling state is the point
            await asyncio.sleep(0.005)


def out(device):
    return f"bluetooth:{device.address}"


# --- pairing and sound ---------------------------------------------------------------------


async def test_pair_connects_and_moves_sound(ctx, toasts):
    async with Rig(ctx) as rig:
        await rig.run("bluetooth.scan", on=True)
        await settle(0)
        rig.bluetooth._devices[SPEAKER.address] = SPEAKER  # found while scanning
        await rig.run("bluetooth.pair", address=SPEAKER.address)
        await until(lambda: rig.sound() == out(SPEAKER))
        assert rig.connected() == {SPEAKER.address}
        assert ctx.saved.get(SAVED_LAST) == SPEAKER.address
        assert f"Sound now on {SPEAKER.name}" in toasts()


async def test_pairing_a_phone_doesnt_connect_it(ctx):
    async with Rig(ctx) as rig:
        await rig.run("bluetooth.scan", on=True)
        rig.bluetooth._devices[PHONE.address] = PHONE
        await rig.run("bluetooth.pair", address=PHONE.address)
        assert rig.connected() == set()
        assert ctx.saved.get(SAVED_LAST) is None


async def test_disconnect_falls_back_with_a_toast(ctx, toasts):
    ctx.saved.set(SAVED_OUTPUT, "jack")
    async with Rig(ctx) as rig:
        assert rig.sound() == "jack"
        await rig.run("bluetooth.connect", address=KITCHEN.address)
        await until(lambda: rig.sound() == out(KITCHEN))
        toasts()
        await rig.run("bluetooth.disconnect", address=KITCHEN.address)
        await until(lambda: rig.sound() == "jack")
        assert toasts() == [f"{KITCHEN.name} disconnected", "Sound now on Headphone jack"]


async def test_a_later_choice_survives_the_disconnect(ctx):
    async with Rig(ctx) as rig:
        await rig.run("bluetooth.connect", address=KITCHEN.address)
        await until(lambda: rig.sound() == out(KITCHEN))
        await rig.run("audio.set_output", output="jack")
        await rig.run("bluetooth.disconnect", address=KITCHEN.address)
        await settle(0.05)
        assert rig.sound() == "jack"


async def test_no_output_for_the_device_leaves_sound_alone(ctx):
    async with Rig(ctx, output_wait=0.05) as rig:
        rig.bluetooth._audio = None  # PipeWire never shows a sink for it
        await rig.run("bluetooth.connect", address=KITCHEN.address)
        await settle(0.1)
        assert rig.sound() == "tv"


async def test_already_connected_at_start_isnt_news(ctx):
    audio = FakeAudio(speed=0)
    bluetooth = FakeBluetooth(audio, speed=0)
    await bluetooth.connect(KITCHEN.address)  # e.g. couchd restarted
    service = BluetoothService(ctx, bluetooth, reconnect_delays=FAST)
    registry = ServiceRegistry(ctx.state)
    registry.add(AudioService(ctx, audio))
    registry.add(service)
    await registry.start_all()
    await settle(0.05)
    assert ctx.state.get("audio")["current"] == "tv"
    await registry.stop_all()


def test_output_ids_match_by_address():
    assert _output_matches("bluetooth:AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF")
    assert _output_matches("bluez_output.AA_BB_CC_DD_EE_FF.1", "aa:bb:cc:dd:ee:ff")
    assert not _output_matches("bluez_output.AA_BB_CC_DD_EE_00.1", "AA:BB:CC:DD:EE:FF")
    assert not _output_matches("tv", "")


# --- auto-reconnect ------------------------------------------------------------------------


async def test_last_device_reconnects_at_boot(ctx):
    ctx.saved.set(SAVED_LAST, KITCHEN.address)
    async with Rig(ctx) as rig:
        await until(lambda: rig.connected() == {KITCHEN.address})
        await until(lambda: rig.sound() == out(KITCHEN))


async def test_keeps_trying_until_it_comes_back_in_range(ctx):
    ctx.saved.set(SAVED_LAST, KITCHEN.address)
    async with Rig(ctx, FlakyBluetooth, reconnect_delays=(0.0, 0.01, 0.02)) as rig:
        rig.bluetooth.in_range = False
        await until(lambda: rig.bluetooth.attempts >= 4)
        assert rig.connected() == set()
        rig.bluetooth.in_range = True
        await until(lambda: rig.connected() == {KITCHEN.address})
        attempts = rig.bluetooth.attempts
        await settle(0.05)
        assert rig.bluetooth.attempts == attempts  # connected: stops trying


async def test_reconnects_after_the_speaker_drops(ctx):
    async with Rig(ctx, FlakyBluetooth) as rig:
        await rig.run("bluetooth.connect", address=KITCHEN.address)
        rig.bluetooth.in_range = False
        await rig.bluetooth.disconnect(KITCHEN.address)  # walked out of range, not a command
        await until(lambda: rig.bluetooth.attempts >= 3)
        rig.bluetooth.in_range = True
        await until(lambda: rig.connected() == {KITCHEN.address})


async def test_disconnecting_on_purpose_stops_reconnecting(ctx):
    async with Rig(ctx, FlakyBluetooth) as rig:
        await rig.run("bluetooth.connect", address=KITCHEN.address)
        await rig.run("bluetooth.disconnect", address=KITCHEN.address)
        assert ctx.saved.get(SAVED_LAST) is None
        await settle(0.05)
        assert rig.connected() == set()
        assert rig.bluetooth.attempts == 1


async def test_forget_stops_reconnecting(ctx):
    ctx.saved.set(SAVED_LAST, KITCHEN.address)
    async with Rig(ctx) as rig:
        await until(lambda: rig.connected() == {KITCHEN.address})
        await rig.run("bluetooth.forget", address=KITCHEN.address)
        assert ctx.saved.get(SAVED_LAST) is None


async def test_auto_reconnect_setting(ctx):
    ctx.saved.set(SAVED_LAST, KITCHEN.address)
    ctx.saved.set(SAVED_AUTO_RECONNECT, False)
    async with Rig(ctx, FlakyBluetooth) as rig:
        assert rig.bt()["auto_reconnect"] is False
        await settle(0.05)
        assert rig.bluetooth.attempts == 0
        await rig.run("bluetooth.set_auto_reconnect", on=True)
        assert rig.bt()["auto_reconnect"] is True
        await until(lambda: rig.connected() == {KITCHEN.address})


async def test_no_reconnecting_while_scanning(ctx):
    ctx.saved.set(SAVED_LAST, KITCHEN.address)
    ctx.saved.set(SAVED_AUTO_RECONNECT, False)
    async with Rig(ctx, FlakyBluetooth) as rig:
        await rig.run("bluetooth.scan", on=True)
        await rig.run("bluetooth.set_auto_reconnect", on=True)
        await settle(0.05)
        assert rig.bluetooth.attempts == 0
        await rig.run("bluetooth.scan", on=False)
        await until(lambda: rig.connected() == {KITCHEN.address})


async def test_another_speaker_connected_means_no_reconnect(ctx):
    ctx.saved.set(SAVED_LAST, KITCHEN.address)
    ctx.saved.set(SAVED_AUTO_RECONNECT, False)
    async with Rig(ctx, FlakyBluetooth) as rig:
        await rig.run("bluetooth.scan", on=True)
        rig.bluetooth._devices[SPEAKER.address] = SPEAKER
        await rig.run("bluetooth.pair", address=SPEAKER.address)
        await rig.run("bluetooth.scan", on=False)
        await rig.run("bluetooth.set_auto_reconnect", on=True)
        await settle(0.05)
        assert rig.connected() == {SPEAKER.address}
        assert ctx.saved.get(SAVED_LAST) == SPEAKER.address


# --- scanning ------------------------------------------------------------------------------


async def test_scan_stops_by_itself(ctx):
    async with Rig(ctx, scan_limit=0.02) as rig:
        await rig.run("bluetooth.scan", on=True)
        assert rig.bt()["scanning"] is True
        await until(lambda: rig.bt()["scanning"] is False)


async def test_scan_off_cancels_the_limit(ctx):
    async with Rig(ctx, scan_limit=0.05) as rig:
        await rig.run("bluetooth.scan", on=True)
        await rig.run("bluetooth.scan", on=False)
        await rig.run("bluetooth.scan", on=True)
        await settle(0.03)
        assert rig.bt()["scanning"] is True
        await until(lambda: rig.bt()["scanning"] is False)


async def test_stop_cleans_up(ctx):
    ctx.saved.set(SAVED_LAST, KITCHEN.address)
    rig = Rig(ctx, FlakyBluetooth)
    async with rig:
        rig.bluetooth.in_range = False
        await rig.run("bluetooth.scan", on=True)
    assert rig.service._tasks == set()
    assert not [n for n in ctx.commands.names() if n.startswith("bluetooth.")]

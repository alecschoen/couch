"""WifiService: the `network.wifi` setting drives the radio (#31)."""

import asyncio

import pytest
from fake_nm import FakeNm

from couchd.bluez import BluezError
from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.fakes import FakeWifi
from couchd.networkmanager import NM, NM_PATH, NmWifi
from couchd.services import Context
from couchd.settings import Settings
from couchd.state import LiveState, SavedState
from couchd.wifi import SETTING, WifiService


async def settle():
    for _ in range(40):
        await asyncio.sleep(0)


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    state = LiveState(bus)
    settings = Settings(state, tmp_path / "settings.json", save_delay=0)
    settings.load()
    return Context(bus, state, SavedState(tmp_path / "state.json"), Commands(), settings)


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


async def started(ctx, nm):
    service = WifiService(ctx, NmWifi(nm))
    await service.start()
    return service


async def test_off_by_default(ctx):
    nm = FakeNm(enabled=True)
    service = await started(ctx, nm)
    assert nm.sets() == [False]
    assert ctx.state.get("wifi")["enabled"] is False
    assert ctx.settings.get(SETTING) is False
    await service.stop()


async def test_already_off_is_left_alone(ctx):
    nm = FakeNm(enabled=False)
    service = await started(ctx, nm)
    assert nm.sets() == []
    await service.stop()


async def test_the_setting_turns_it_on_and_off(ctx):
    nm = FakeNm()
    service = await started(ctx, nm)
    ctx.settings.set(SETTING, True)
    await settle()
    assert nm.sets() == [True]
    nm.join("Living room", 80)
    await settle()
    wifi = ctx.state.get("wifi")
    assert (wifi["connection"], wifi["ssid"], wifi["signal"]) == ("connected", "Living room", 80)
    ctx.settings.set(SETTING, False)
    await settle()
    assert nm.sets() == [True, False]
    assert ctx.state.get("wifi")["connection"] == "off"
    await service.stop()


async def test_saved_on_turns_it_on_at_start(ctx):
    ctx.settings.set(SETTING, True)
    nm = FakeNm()
    service = await started(ctx, nm)
    assert nm.sets() == [True]
    await service.stop()


async def test_waits_for_the_chip(ctx):
    nm = FakeNm(wifi=False, enabled=True)
    service = await started(ctx, nm)
    assert nm.sets() == []
    assert ctx.state.get("wifi")["available"] is False
    nm.add_wifi()
    await settle()
    assert nm.sets() == [False]
    await service.stop()


async def test_waits_for_networkmanager(ctx):
    nm = FakeNm(running=False, enabled=True)
    service = await started(ctx, nm)
    nm.start_running()
    await settle()
    assert nm.sets() == [False]
    await service.stop()


async def test_switched_on_elsewhere_is_put_back(ctx):
    nm = FakeNm()
    service = await started(ctx, nm)
    nm.update(NM_PATH, NM, WirelessEnabled=True)
    await settle()
    assert nm.sets() == [False]
    assert nm.objects[NM_PATH][NM]["WirelessEnabled"] is False
    await service.stop()


async def test_wifi_only_pi_keeps_wifi_when_never_set(ctx):
    nm = FakeNm(enabled=True)
    nm.objects[NM_PATH][NM]["PrimaryConnectionType"] = "802-11-wireless"
    service = await started(ctx, nm)
    assert nm.sets() == []
    assert ctx.settings.get(SETTING) is True
    assert ctx.settings.is_set(SETTING)
    await service.stop()


async def test_wifi_only_pi_obeys_an_explicit_off(ctx):
    ctx.settings.set(SETTING, False)
    nm = FakeNm(enabled=True)
    nm.objects[NM_PATH][NM]["PrimaryConnectionType"] = "802-11-wireless"
    service = await started(ctx, nm)
    assert nm.sets() == [False]
    await service.stop()


async def test_a_refused_switch_toasts_and_shows_the_truth(ctx, toasts):
    nm = FakeNm()
    service = await started(ctx, nm)
    nm.fail[(NM_PATH, "Set")] = BluezError(
        "org.freedesktop.NetworkManager.PermissionDenied", "Not authorized"
    )
    ctx.settings.set(SETTING, True)
    await settle()
    assert toasts() == ["Couch isn't allowed to switch the Wi-Fi"]
    assert ctx.settings.get(SETTING) is False
    await service.stop()


async def test_a_failure_at_start_is_only_logged(ctx, toasts):
    nm = FakeNm(enabled=True)
    nm.fail[(NM_PATH, "Set")] = TimeoutError()
    service = await started(ctx, nm)
    assert toasts() == []
    assert ctx.settings.get(SETTING) is False
    await service.stop()


async def test_dev_fake_joins_after_a_moment(ctx):
    service = WifiService(ctx, FakeWifi(speed=0.01))
    await service.start()
    assert ctx.state.get("wifi")["enabled"] is False
    ctx.settings.set(SETTING, True)
    for _ in range(50):
        await asyncio.sleep(0.005)
        if ctx.state.get("wifi")["connection"] == "connected":
            break
    wifi = ctx.state.get("wifi")
    assert (wifi["connection"], wifi["ssid"]) == ("connected", FakeWifi.NETWORK)
    await service.stop()


def test_is_set_tells_a_choice_from_the_default(tmp_path):
    settings = Settings(None, tmp_path / "settings.json", save_delay=0)
    settings.load()
    assert settings.is_set(SETTING) is False
    settings.set(SETTING, False)
    assert settings.is_set(SETTING) is True
    reloaded = Settings(None, tmp_path / "settings.json", save_delay=0)
    settings.flush()
    reloaded.load()
    assert reloaded.is_set(SETTING) is True
    assert Settings().is_set(SETTING) is False

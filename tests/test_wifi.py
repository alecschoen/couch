"""WifiService: the `network.wifi` setting drives the radio (#31); joining networks (#78)."""

import asyncio

import pytest
from fake_nm import FakeNm

from couchd.bluez import BluezError
from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.fakes import NETWORKS, OUT_OF_REACH, WIFI_PASSWORD, FakeWifi
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


# --- joining networks (#78) ------------------------------------------------------------------


async def turned_on(ctx, nm):
    ctx.settings.set(SETTING, True)
    service = WifiService(ctx, NmWifi(nm, scan_timeout=0.05, connect_timeout=0.5))
    await service.start()
    return service


async def test_commands_come_and_go_with_the_service(ctx):
    service = await started(ctx, FakeNm())
    assert {"wifi.scan", "wifi.connect", "wifi.forget"} <= set(ctx.commands.names())
    await service.stop()
    assert not [n for n in ctx.commands.names() if n.startswith("wifi.")]


async def test_state_lists_networks_and_scanning(ctx):
    nm = FakeNm()
    service = await turned_on(ctx, nm)
    nm.add_ap("Upstairs", 60)
    nm.add_profile("Upstairs")
    await settle()
    wifi = ctx.state.get("wifi")
    assert wifi["scanning"] is False
    assert wifi["networks"] == [
        {"ssid": "Upstairs", "signal": 60, "secure": True, "known": True, "active": False}
    ]
    await ctx.commands.run("wifi.scan")
    assert ctx.state.get("wifi")["scanning"] is True
    nm.finish_scan()
    await settle()
    assert ctx.state.get("wifi")["scanning"] is False
    await service.stop()


async def test_connect_answers_once_connected(ctx):
    nm = FakeNm()
    service = await turned_on(ctx, nm)
    nm.add_ap("Upstairs", 60)
    nm.passwords["Upstairs"] = "password1"
    await settle()
    await ctx.commands.run("wifi.connect", {"ssid": "Upstairs", "password": "password1"})
    wifi = ctx.state.get("wifi")
    assert (wifi["connection"], wifi["ssid"]) == ("connected", "Upstairs")
    assert wifi["networks"][0]["active"] is True
    await ctx.commands.run("wifi.forget", {"ssid": "Upstairs"})
    await settle()
    assert ctx.state.get("wifi")["networks"][0]["known"] is False
    await service.stop()


async def test_a_failed_join_is_the_commands_error(ctx, toasts):
    nm = FakeNm()
    service = await turned_on(ctx, nm)
    nm.add_ap("Upstairs", 60)
    nm.passwords["Upstairs"] = "password1"
    await settle()
    with pytest.raises(CommandError, match=r"^Wrong password$"):
        await ctx.commands.run("wifi.connect", {"ssid": "Upstairs", "password": "secret-guess"})
    assert toasts() == []  # the launcher says it, next to the network
    assert "secret-guess" not in repr(ctx.state.get("wifi"))
    await service.stop()


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"ssid": ""},
        {"ssid": 5},
        {"ssid": "x" * 33},
        {"ssid": "Upstairs", "password": 12345678},
    ],
)
async def test_connect_checks_its_args(ctx, args):
    service = await turned_on(ctx, FakeNm())
    with pytest.raises(CommandError, match=r"'ssid'|'password'"):
        await ctx.commands.run("wifi.connect", args)
    if "password" not in args:
        with pytest.raises(CommandError, match="'ssid'"):
            await ctx.commands.run("wifi.forget", args)
    await service.stop()


async def test_a_join_outlives_whoever_asked(ctx):
    nm = FakeNm()
    service = await turned_on(ctx, nm)
    nm.add_ap("Upstairs", 60)
    nm.hang = True
    await settle()
    asking = asyncio.create_task(
        ctx.commands.run("wifi.connect", {"ssid": "Upstairs", "password": "password1"})
    )
    await settle()
    assert nm.wifi_profiles()
    asking.cancel()  # the launcher's WebSocket closed
    with pytest.raises(asyncio.CancelledError):
        await asking
    assert nm.wifi_profiles()  # the join is still going
    await asyncio.sleep(0.6)  # ... until it gives up, and cleans up
    assert nm.wifi_profiles() == {}
    await service.stop()


async def test_one_join_at_a_time(ctx):
    nm = FakeNm()
    service = await turned_on(ctx, nm)
    nm.add_ap("Upstairs", 60, "open")
    nm.add_ap("Café", 50, "open")
    await settle()
    first = ctx.commands.run("wifi.connect", {"ssid": "Upstairs"})
    second = ctx.commands.run("wifi.connect", {"ssid": "Café"})
    await asyncio.gather(first, second)
    added = [b[0]["connection"]["id"] for _, m, b in nm.calls if m == "AddAndActivateConnection"]
    assert added == ["Upstairs", "Café"]
    assert ctx.state.get("wifi")["ssid"] == "Café"
    await service.stop()


async def test_dev_fake_joins_forgets_and_says_wrong_password(ctx):
    ctx.settings.set(SETTING, True)
    service = WifiService(ctx, FakeWifi(enabled=True, speed=0.001))
    await service.start()
    names = [n["ssid"] for n in ctx.state.get("wifi")["networks"]]
    assert names[0] == FakeWifi.NETWORK and len(names) == len(NETWORKS)
    with pytest.raises(CommandError, match="Wrong password"):
        await ctx.commands.run("wifi.connect", {"ssid": "Upstairs", "password": "not it at all"})
    await ctx.commands.run("wifi.connect", {"ssid": "Upstairs", "password": WIFI_PASSWORD})
    wifi = ctx.state.get("wifi")
    assert (wifi["connection"], wifi["ssid"]) == ("connected", "Upstairs")
    with pytest.raises(CommandError, match="didn't answer"):
        await ctx.commands.run("wifi.connect", {"ssid": OUT_OF_REACH, "password": WIFI_PASSWORD})
    await ctx.commands.run("wifi.forget", {"ssid": "Upstairs"})
    assert ctx.state.get("wifi")["connection"] == "disconnected"
    await ctx.commands.run("wifi.scan")
    assert ctx.state.get("wifi")["scanning"] is True
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

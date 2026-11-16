"""NmWifi against an in-memory NetworkManager (tests/fake_nm.py)."""

import asyncio

import pytest
from fake_nm import WLAN0, FakeNm

from couchd.backends import BackendError, WifiStatus
from couchd.bluez import BluezError
from couchd.networkmanager import DEVICE, NM_PATH, WIRELESS, NmWifi


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
async def rig():
    nm = FakeNm()
    backend = NmWifi(nm)
    changes = []
    backend.on_change(lambda: changes.append(backend.status()))
    await backend.start()
    yield nm, backend, changes
    await backend.stop()


async def test_off_radio_reads_as_off(rig):
    _, backend, _ = rig
    assert backend.status() == WifiStatus(available=True, enabled=False)


async def test_no_wifi_chip_is_unavailable():
    backend = NmWifi(FakeNm(wifi=False))
    await backend.start()
    assert backend.status() == WifiStatus(available=False, enabled=False)
    await backend.stop()


async def test_the_chip_appearing_later_makes_it_available():
    nm = FakeNm(wifi=False)
    backend = NmWifi(nm)
    await backend.start()
    nm.add_wifi()
    await settle()
    assert backend.status().available is True
    await backend.stop()


async def test_turning_on_sets_wireless_enabled(rig):
    nm, backend, _ = rig
    await backend.set_enabled(True)
    assert nm.sets() == [True]
    assert backend.status() == WifiStatus(available=True, enabled=True, connection="disconnected")


async def test_connected_shows_the_network_and_signal(rig):
    nm, backend, _ = rig
    await backend.set_enabled(True)
    nm.join("Living room", 64)
    await settle()
    assert backend.status() == WifiStatus(
        available=True, enabled=True, connection="connected", ssid="Living room", signal=64
    )


async def test_connecting_shows_the_network_but_no_signal(rig):
    nm, backend, _ = rig
    await backend.set_enabled(True)
    nm.join("Living room", 64, state=70)
    await settle()
    status = backend.status()
    assert (status.connection, status.ssid, status.signal) == ("connecting", "Living room", None)


async def test_turning_off_clears_the_network(rig):
    nm, backend, _ = rig
    await backend.set_enabled(True)
    nm.join()
    await settle()
    await backend.set_enabled(False)
    assert nm.sets() == [True, False]
    assert backend.status() == WifiStatus(available=True, enabled=False)


async def test_primary_when_traffic_goes_over_wifi():
    nm = FakeNm(enabled=True)
    nm.objects[NM_PATH]["org.freedesktop.NetworkManager"]["PrimaryConnectionType"] = (
        "802-11-wireless"
    )
    backend = NmWifi(nm)
    await backend.start()
    assert backend.status().primary is True
    await backend.stop()


async def test_hardware_switch_off_reads_as_off():
    nm = FakeNm(enabled=True)
    nm.objects[NM_PATH]["org.freedesktop.NetworkManager"]["WirelessHardwareEnabled"] = False
    backend = NmWifi(nm)
    await backend.start()
    assert backend.status().enabled is False
    await backend.stop()


async def test_switched_elsewhere_is_noticed(rig):
    nm, backend, changes = rig
    nm.update(NM_PATH, "org.freedesktop.NetworkManager", WirelessEnabled=True)
    await settle()
    assert backend.status().enabled is True
    assert changes[-1].enabled is True


async def test_signal_bursts_coalesce(rig):
    nm, backend, _ = rig
    await backend.set_enabled(True)
    nm.calls.clear()
    for _ in range(4):
        nm.update(WLAN0, DEVICE, State=100)
    await settle()
    reads = [c for c in nm.calls if c[1] == "GetAll" and c[2] == ["org.freedesktop.NetworkManager"]]
    assert 1 <= len(reads) <= 2


async def test_permission_denied_says_so(rig):
    nm, backend, _ = rig
    nm.fail[(NM_PATH, "Set")] = BluezError(
        "org.freedesktop.NetworkManager.PermissionDenied", "Not authorized"
    )
    with pytest.raises(BackendError, match="isn't allowed"):
        await backend.set_enabled(True)
    assert backend.status().enabled is False


async def test_other_failures_are_plain(rig):
    nm, backend, _ = rig
    nm.fail[(NM_PATH, "Set")] = TimeoutError()
    with pytest.raises(BackendError, match="Couldn't turn the Wi-Fi on"):
        await backend.set_enabled(True)


async def test_networkmanager_going_away_and_back(rig):
    nm, backend, _ = rig
    nm.stop_running()
    assert backend.status().available is False
    nm.start_running()
    await settle()
    assert backend.status().available is True


async def test_not_running_at_start_waits():
    nm = FakeNm(running=False)
    backend = NmWifi(nm)
    await backend.start()
    assert backend.status().available is False
    assert nm.calls == []
    nm.start_running()
    await settle()
    assert backend.status().available is True
    await backend.stop()


async def test_odd_ssids_dont_break_reading(rig):
    nm, backend, _ = rig
    await backend.set_enabled(True)
    nm.objects["/ap"] = {
        "org.freedesktop.NetworkManager.AccessPoint": {"Ssid": b"\xffcaf\xc3\xa9", "Strength": 5}
    }
    nm.update(WLAN0, WIRELESS, ActiveAccessPoint="/ap")
    nm.update(WLAN0, DEVICE, State=100)
    await settle()
    assert backend.status().ssid == "�café"

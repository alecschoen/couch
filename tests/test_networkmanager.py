"""NmWifi against an in-memory NetworkManager (tests/fake_nm.py)."""

import asyncio
import logging

import pytest
from fake_nm import ETH0, WIRED, WLAN0, FakeNm

from couchd.backends import BackendError, Network, WifiStatus
from couchd.bluez import BluezError
from couchd.networkmanager import ACCESS_POINT, DEVICE, NM_PATH, WIRELESS, NmWifi


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


# --- networks nearby, joining and forgetting (#78) ------------------------------------------

CHANGING = ("Delete", "ActivateConnection", "AddAndActivateConnection", "Update", "Update2")


@pytest.fixture
async def on():
    """The radio on, with nothing joined yet."""
    nm = FakeNm(enabled=True)
    backend = NmWifi(nm, scan_timeout=0.05, connect_timeout=0.5)
    await backend.start()
    yield nm, backend
    await backend.stop()


def calls(nm, *members):
    return [(path, body) for path, member, body in nm.calls if member in members]


async def test_networks_one_per_ssid_strongest_first(on):
    nm, backend = on
    nm.add_ap("Upstairs", 40)
    nm.add_ap("Upstairs", 70)
    nm.add_ap("Café", 55, "open")
    nm.add_ap(b"", 90)  # hidden
    nm.add_ap(b"\x00\x00\x00", 90)  # hidden too
    nm.add_ap("Printer", 80, "open", mode=1)  # ad-hoc
    await settle()
    assert backend.networks() == [
        Network("Upstairs", 70, secure=True, known=False),
        Network("Café", 55, secure=False, known=False),
    ]


async def test_networks_say_secure_known_and_active(on):
    nm, backend = on
    nm.add_profile("Living room")
    nm.add_ap("Enhanced", 30, "owe")
    nm.add_ap("Kitchen", 20, "wpa3")
    nm.join("Living room", 64)
    await settle()
    assert backend.networks() == [
        Network("Living room", 64, secure=False, known=True, active=True),
        Network("Enhanced", 30, secure=False, known=False),
        Network("Kitchen", 20, secure=True, known=False),
    ]


async def test_enterprise_and_wep_only_when_saved(on):
    nm, backend = on
    nm.add_ap("Office", 60, "enterprise")
    nm.add_ap("Old router", 50, "wep")
    nm.add_ap("Campus", 40, "enterprise")
    nm.add_profile("Campus")
    await settle()
    assert [n.ssid for n in backend.networks()] == ["Campus"]
    with pytest.raises(BackendError, match="needs more than a password"):
        await backend.connect("Office", "hunter2hunter2")
    assert calls(nm, *CHANGING) == []


async def test_a_hotspot_profile_or_another_kind_isnt_known(on):
    nm, backend = on
    nm.add_ap("Living room", 60)
    nm.add_profile("Living room", mode="ap")
    nm.add_profile("Living room", kind="802-3-ethernet")
    await settle()
    assert backend.networks()[0].known is False


async def test_no_networks_while_off(rig):
    nm, backend, _ = rig
    nm.add_ap("Upstairs", 40)
    await settle()
    assert backend.networks() == []
    with pytest.raises(BackendError, match="Wi-Fi is off"):
        await backend.scan()
    with pytest.raises(BackendError, match="Wi-Fi is off"):
        await backend.connect("Upstairs", "password1")


async def test_networks_come_and_go(on):
    nm, backend = on
    path = nm.add_ap("Upstairs", 40)
    await settle()
    assert [n.ssid for n in backend.networks()] == ["Upstairs"]
    nm.update(path, ACCESS_POINT, Strength=75)
    await settle()
    assert backend.networks()[0].signal == 75
    nm.remove_ap(path)
    await settle()
    assert backend.networks() == []


async def test_scan_asks_and_ends_when_the_scan_is_in(on):
    nm, backend = on
    await backend.scan()
    assert calls(nm, "RequestScan") == [(WLAN0, [{}])]
    assert backend.scanning() is True
    nm.finish_scan()
    await settle()
    assert backend.scanning() is False


async def test_scan_already_running_is_fine_and_times_out(on):
    nm, backend = on
    nm.scan_busy = True
    await backend.scan()
    assert backend.scanning() is True
    await asyncio.sleep(0.1)
    assert backend.scanning() is False


async def test_scan_refused_says_so(on):
    nm, backend = on
    nm.fail[(WLAN0, "RequestScan")] = BluezError(
        "org.freedesktop.NetworkManager.PermissionDenied", "Not authorized"
    )
    with pytest.raises(BackendError, match="isn't allowed to look"):
        await backend.scan()


async def test_joining_a_saved_network_activates_its_profile(on):
    nm, backend = on
    ap = nm.add_ap("Upstairs", 60)
    profile = nm.add_profile("Upstairs", psk="saved-password")
    nm.passwords["Upstairs"] = "saved-password"
    await settle()
    await backend.connect("Upstairs")
    assert calls(nm, "ActivateConnection") == [(NM_PATH, [profile, WLAN0, ap])]
    assert calls(nm, "AddAndActivateConnection") == []
    assert backend.status().connection == "connected"
    assert backend.status().ssid == "Upstairs"
    assert list(nm.wifi_profiles()) == [profile]


async def test_joining_a_new_network_saves_it_for_the_whole_system(on):
    nm, backend = on
    ap = nm.add_ap("Upstairs", 60)
    nm.passwords["Upstairs"] = "correct horse"
    await settle()
    await backend.connect("Upstairs", "correct horse")
    [(_, (settings, device, specific))] = calls(nm, "AddAndActivateConnection")
    assert (device, specific) == (WLAN0, ap)
    assert settings == {
        "connection": {"id": "Upstairs", "type": "802-11-wireless"},
        "802-11-wireless": {"ssid": b"Upstairs", "mode": "infrastructure"},
        "802-11-wireless-security": {"key-mgmt": "wpa-psk", "psk": "correct horse"},
    }
    # No "permissions": the profile is everyone's, so it's back after a reboot.
    assert backend.status().connection == "connected"
    assert [n.known for n in backend.networks()] == [True]


async def test_wpa3_uses_sae_and_open_has_no_security(on):
    nm, backend = on
    nm.add_ap("Kitchen", 60, "wpa3")
    nm.add_ap("Café", 50, "open")
    nm.add_ap("Transition", 40, "transition")
    nm.add_ap("Enhanced", 30, "owe")
    nm.passwords.update(Kitchen="sae-password", Transition="mixed-password")
    await settle()
    await backend.connect("Kitchen", "sae-password")
    await backend.connect("Café")
    await backend.connect("Transition", "mixed-password")
    await backend.connect("Enhanced")
    added = [body[0] for _, body in calls(nm, "AddAndActivateConnection")]
    assert added[0]["802-11-wireless-security"]["key-mgmt"] == "sae"
    assert "802-11-wireless-security" not in added[1]
    assert added[2]["802-11-wireless-security"]["key-mgmt"] == "wpa-psk"
    assert added[3]["802-11-wireless-security"] == {"key-mgmt": "owe"}


async def test_wrong_password_says_so_and_isnt_saved(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    nm.passwords["Upstairs"] = "the right one"
    await settle()
    with pytest.raises(BackendError, match=r"^Wrong password$"):
        await backend.connect("Upstairs", "not the one")
    assert nm.wifi_profiles() == {}
    await settle()
    assert backend.status().connection == "disconnected"


async def test_out_of_range_says_so_and_isnt_saved(on):
    nm, backend = on
    nm.add_ap("Garden", 5, "open")
    nm.out_of_range.add("Garden")
    await settle()
    with pytest.raises(BackendError, match=r"Can't find Garden\. Is it in range"):
        await backend.connect("Garden")
    assert nm.wifi_profiles() == {}


async def test_a_join_that_never_finishes_gives_up_and_cleans_up(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    nm.hang = True
    await settle()
    with pytest.raises(BackendError, match="Couldn't connect to Upstairs"):
        await backend.connect("Upstairs", "password1")
    assert nm.wifi_profiles() == {}


async def test_a_cancelled_join_still_cleans_up(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    nm.hang = True
    await settle()
    join = asyncio.create_task(backend.connect("Upstairs", "password1"))
    for _ in range(50):
        await asyncio.sleep(0)
        if nm.wifi_profiles():
            break
    assert nm.wifi_profiles()
    join.cancel()
    with pytest.raises(asyncio.CancelledError):
        await join
    assert nm.wifi_profiles() == {}


async def test_a_new_password_replaces_the_saved_one_once_it_works(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    old = nm.add_profile("Upstairs", psk="old password")
    nm.passwords["Upstairs"] = "new password"
    await settle()
    with pytest.raises(BackendError, match="Wrong password"):
        await backend.connect("Upstairs")  # what's saved is out of date
    with pytest.raises(BackendError, match="Wrong password"):
        await backend.connect("Upstairs", "still wrong")
    assert list(nm.wifi_profiles()) == [old]  # kept until a new one works
    await backend.connect("Upstairs", "new password")
    [(path, settings)] = nm.wifi_profiles().items()
    assert path != old
    assert settings["802-11-wireless-security"]["psk"] == "new password"


async def test_passwords_are_checked_before_asking(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    await settle()
    with pytest.raises(BackendError, match="Upstairs needs a password"):
        await backend.connect("Upstairs")
    with pytest.raises(BackendError, match="8 to 63 characters"):
        await backend.connect("Upstairs", "short")
    with pytest.raises(BackendError, match="8 to 63 characters"):
        await backend.connect("Upstairs", "x" * 64)
    with pytest.raises(BackendError, match="a character Wi-Fi doesn't allow"):
        await backend.connect("Upstairs", "pässwörter")
    assert calls(nm, *CHANGING) == []
    nm.passwords["Upstairs"] = "ab" * 32  # 64 hex digits: the key itself
    await backend.connect("Upstairs", "ab" * 32)
    assert len(calls(nm, "AddAndActivateConnection")) == 1


async def test_a_network_not_in_sight_cant_be_joined(on):
    _, backend = on
    with pytest.raises(BackendError, match=r"Can't find Upstairs\. Is it in range"):
        await backend.connect("Upstairs", "password1")


async def test_joining_refused_says_so(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    await settle()
    nm.fail[(NM_PATH, "AddAndActivateConnection")] = BluezError(
        "org.freedesktop.NetworkManager.PermissionDenied", "Not authorized"
    )
    with pytest.raises(BackendError, match="isn't allowed to change Wi-Fi networks"):
        await backend.connect("Upstairs", "password1")


async def test_forget_deletes_every_wifi_profile_for_it_and_nothing_else(on):
    nm, backend = on
    nm.add_ap("Living room", 60)
    first = nm.add_profile("Living room")
    second = nm.add_profile("Living room")
    wired_same_name = nm.add_profile("Living room", kind="802-3-ethernet")
    other = nm.add_profile("Upstairs")
    await settle()
    await backend.forget("Living room")
    assert [path for path, _ in calls(nm, "Delete")] == [first, second]
    assert set(nm.profiles) == {WIRED, wired_same_name, other}
    assert backend.networks()[0].known is False


async def test_forget_what_isnt_saved_says_so(on):
    _, backend = on
    with pytest.raises(BackendError, match="Upstairs isn't saved"):
        await backend.forget("Upstairs")


async def test_forgetting_the_network_its_on_leaves_it(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60, "open")
    await settle()
    await backend.connect("Upstairs")
    assert backend.status().connection == "connected"
    await backend.forget("Upstairs")
    await settle()
    assert backend.status().connection == "disconnected"


async def test_ethernet_is_never_touched(on):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    nm.add_profile("Upstairs")
    nm.passwords["Upstairs"] = "password1"
    await settle()
    with pytest.raises(BackendError):
        await backend.connect("Upstairs")
    with pytest.raises(BackendError):
        await backend.connect("Upstairs", "wrong password")
    await backend.connect("Upstairs", "password1")
    await backend.forget("Upstairs")
    assert len(calls(nm, *CHANGING)) >= 4
    assert not nm.touched(WIRED, CHANGING)
    assert not nm.touched(ETH0, CHANGING)
    assert WIRED in nm.profiles


async def test_the_password_goes_nowhere_but_networkmanager(on, caplog):
    nm, backend = on
    nm.add_ap("Upstairs", 60)
    nm.passwords["Upstairs"] = "the-real-secret"
    nm.fail[(NM_PATH, "AddAndActivateConnection")] = BluezError(
        "org.freedesktop.NetworkManager.InvalidProperty", "psk: the-wrong-secret is invalid"
    )
    await settle()
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BackendError) as refused:
        await backend.connect("Upstairs", "the-wrong-secret")
    del nm.fail[(NM_PATH, "AddAndActivateConnection")]
    with pytest.raises(BackendError) as wrong:
        await backend.connect("Upstairs", "the-wrong-secret")
    await backend.connect("Upstairs", "the-real-secret")
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    told = str(refused.value) + str(wrong.value) + repr(backend.status()) + repr(backend.networks())
    for secret in ("the-wrong-secret", "the-real-secret"):
        assert secret not in logged
        assert secret not in told

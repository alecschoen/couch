"""BluezBluetooth against an in-memory BlueZ (tests/fake_bluez.py)."""

import asyncio

import pytest
from fake_bluez import A2DP_SINK, A2DP_SOURCE, HCI0, FakeBluez

from couchd.backends import BackendError
from couchd.bluez import (
    ADAPTER,
    AGENT_CAPABILITY,
    AGENT_PATH,
    AgentRejected,
    BluezBluetooth,
    BluezError,
    PairingAgent,
    is_audio,
)

SPEAKER = "AA:BB:CC:00:00:01"
PHONE = "AA:BB:CC:00:00:02"
STRANGER = "AA:BB:CC:00:00:03"


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
async def rig():
    bluez = FakeBluez()
    backend = BluezBluetooth(bluez, paired_grace=0.05)
    changes = []
    backend.on_change(lambda: changes.append(1))
    await backend.start()
    yield bluez, backend
    await backend.stop()


def by_address(backend):
    return {d.address: d for d in backend.devices()}


# --- startup -------------------------------------------------------------------------------


async def test_start_registers_the_agent_and_powers_the_radio(rig):
    bluez, _ = rig
    assert bluez.registered == (AGENT_PATH, AGENT_CAPABILITY)
    assert AGENT_CAPABILITY == "NoInputNoOutput"
    assert bluez.default_agent == AGENT_PATH
    assert bluez.objects[HCI0][ADAPTER]["Powered"] is True
    assert bluez.agent is not None


async def test_paired_devices_are_listed_at_start():
    bluez = FakeBluez()
    bluez.add_device(SPEAKER, "Kitchen Speaker", Paired=True, Trusted=True, UUIDs=[A2DP_SINK])
    bluez.add_device(STRANGER, "Someone's phone")  # cached, not paired: hidden
    backend = BluezBluetooth(bluez)
    await backend.start()
    [device] = backend.devices()
    assert (device.address, device.name, device.audio, device.paired) == (
        SPEAKER,
        "Kitchen Speaker",
        True,
        True,
    )
    await backend.stop()


async def test_starts_without_bluetoothd_and_attaches_when_it_appears():
    bluez = FakeBluez(running=False)
    backend = BluezBluetooth(bluez)
    await backend.start()
    assert backend.devices() == []
    with pytest.raises(BackendError, match="isn't available"):
        await backend.connect(SPEAKER)

    bluez.add_device(SPEAKER, "Kitchen Speaker", Paired=True)
    bluez.start_running()
    await settle()
    assert bluez.registered == (AGENT_PATH, AGENT_CAPABILITY)
    assert [d.address for d in backend.devices()] == [SPEAKER]
    await backend.stop()


async def test_adapter_plugged_in_later_is_used():
    bluez = FakeBluez(adapter=False)
    backend = BluezBluetooth(bluez)
    await backend.start()
    bluez.add_adapter()
    await settle()
    assert bluez.objects[HCI0][ADAPTER]["Powered"] is True
    await backend.start_scan()
    assert bluez.discovering
    await backend.stop()


async def test_a_blocked_radio_is_switched_on_once_it_can_be():
    bluez = FakeBluez()
    bluez.fail[(HCI0, "Set")] = BluezError("org.bluez.Error.Failed")  # rfkill soft block
    bluez.add_device(SPEAKER, "Soundbar", Paired=True)
    backend = BluezBluetooth(bluez)
    await backend.start()  # still starts
    with pytest.raises(BackendError, match="Bluetooth is switched off"):
        await backend.start_scan()
    assert not backend.scanning()
    with pytest.raises(BackendError, match="switched off"):
        await backend.connect(SPEAKER)

    del bluez.fail[(HCI0, "Set")]  # unblocked
    await backend.start_scan()
    assert bluez.objects[HCI0][ADAPTER]["Powered"] is True
    assert bluez.discovering
    await backend.stop()


async def test_bluetoothd_restart_reregisters_and_resumes_scanning(rig):
    bluez, backend = rig
    await backend.start_scan()
    bluez.add_device(SPEAKER, "Speaker")
    bluez.stop_running()
    assert backend.devices() == []
    assert backend.scanning()  # the user still wants it

    bluez.start_running()
    await settle()
    assert bluez.registered == (AGENT_PATH, AGENT_CAPABILITY)
    assert bluez.discovering
    assert [d.address for d in backend.devices()] == [SPEAKER]


async def test_stop_ends_discovery_and_unregisters(rig):
    bluez, backend = rig
    await backend.start_scan()
    await backend.stop()
    assert not bluez.discovering
    assert bluez.registered is None
    assert bluez.closed


# --- scanning ------------------------------------------------------------------------------


async def test_scan_lists_named_devices_only_while_scanning(rig):
    bluez, backend = rig
    await backend.start_scan()
    assert backend.scanning()
    assert bluez.members()[-2:] == ["SetDiscoveryFilter", "StartDiscovery"]
    bluez.add_device(SPEAKER, "Soundbar", UUIDs=[A2DP_SINK])
    bluez.add_device(PHONE, "Phone", UUIDs=[A2DP_SOURCE])
    bluez.add_device(STRANGER)  # a nameless beacon
    found = by_address(backend)
    assert set(found) == {SPEAKER, PHONE}
    assert found[SPEAKER].audio and not found[PHONE].audio

    await backend.stop_scan()
    assert not backend.scanning()
    assert not bluez.discovering
    assert backend.devices() == []


async def test_a_name_arriving_later_shows_the_device(rig):
    bluez, backend = rig
    await backend.start_scan()
    path = bluez.add_device(SPEAKER)
    assert backend.devices() == []
    bluez.update(path, Name="Headphones", Alias="Headphones")
    assert [d.name for d in backend.devices()] == ["Headphones"]


async def test_scan_failure_is_reported(rig):
    bluez, backend = rig
    bluez.fail[(HCI0, "StartDiscovery")] = BluezError("org.bluez.Error.NotReady")
    with pytest.raises(BackendError, match="can't scan"):
        await backend.start_scan()
    assert not backend.scanning()


@pytest.mark.parametrize(
    ("props", "audio"),
    [
        ({"UUIDs": [A2DP_SINK]}, True),
        ({"UUIDs": [A2DP_SINK.upper()]}, True),
        ({"UUIDs": [A2DP_SOURCE]}, False),
        ({"Icon": "audio-headphones"}, True),
        ({"Icon": "phone"}, False),
        ({"Class": 0x240418}, True),  # Audio/Video, headphones
        ({"Class": 0x5A020C}, False),  # Phone, smartphone
        ({}, False),
    ],
)
def test_is_audio(props, audio):
    assert is_audio(props) is audio


# --- pairing -------------------------------------------------------------------------------


async def test_pair_accepts_only_that_device_and_trusts_it(rig):
    bluez, backend = rig
    await backend.start_scan()
    path = bluez.add_device(SPEAKER, "Soundbar", UUIDs=[A2DP_SINK])
    await backend.pair(SPEAKER)
    device = by_address(backend)[SPEAKER]
    assert device.paired
    assert bluez.objects[path]["org.bluez.Device1"]["Trusted"] is True
    # Scanning paused for the pairing and came back after.
    members = bluez.members()
    i = members.index("Pair")
    assert members[i - 1] == "StopDiscovery" and "StartDiscovery" in members[i:]
    assert bluez.discovering
    assert bluez.agent.expected is None  # nobody else gets a yes afterwards


async def test_legacy_pairing_gets_pin_0000(rig):
    bluez, backend = rig
    bluez.pair_asks = "pin"
    bluez.add_device(SPEAKER, "Old Speaker", Class=0x240404)
    await backend.start_scan()
    await backend.pair(SPEAKER)
    assert by_address(backend)[SPEAKER].paired


async def test_pairing_an_already_paired_device_just_trusts(rig):
    bluez, backend = rig
    bluez.add_device(SPEAKER, "Soundbar", Paired=True)
    await backend.pair(SPEAKER)
    assert "Pair" not in bluez.members()
    assert bluez.members()[-1] == "Set"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (BluezError("org.bluez.Error.AuthenticationFailed"), "Soundbar didn't accept the pairing"),
        (BluezError("org.bluez.Error.AuthenticationRejected"), "didn't accept the pairing"),
        (
            BluezError("org.bluez.Error.ConnectionAttemptFailed", "Page Timeout"),
            "Couldn't reach Soundbar. Is it on and in pairing mode?",
        ),
        (BluezError("org.bluez.Error.InProgress"), "Already busy with Soundbar"),
        (BluezError("org.bluez.Error.Failed", "weird"), "Couldn't pair Soundbar"),
        (TimeoutError(), "Soundbar didn't answer"),
    ],
)
async def test_pair_errors_read_well(rig, error, message):
    bluez, backend = rig
    path = bluez.add_device(SPEAKER, "Soundbar")
    await backend.start_scan()
    bluez.fail[(path, "Pair")] = error
    with pytest.raises(BackendError, match=message):
        await backend.pair(SPEAKER)
    assert bluez.agent.expected is None
    assert bluez.discovering  # resumed even though pairing failed


async def test_pairing_finished_by_the_device_counts(rig):
    # Seen with real headphones: our Pair fails while theirs, at the same moment, succeeds.
    bluez, backend = rig
    path = bluez.add_device(SPEAKER, "Soundbar")
    bluez.fail[(path, "Pair")] = BluezError("org.bluez.Error.AuthenticationFailed")
    asyncio.get_running_loop().call_later(0.01, lambda: bluez.update(path, Paired=True))
    await backend.pair(SPEAKER)  # no error
    assert bluez.objects[path]["org.bluez.Device1"]["Trusted"] is True
    assert bluez.agent.expected is None


async def test_pair_already_exists_counts_as_paired(rig):
    bluez, backend = rig
    path = bluez.add_device(SPEAKER, "Soundbar")
    bluez.fail[(path, "Pair")] = BluezError("org.bluez.Error.AlreadyExists")
    await backend.pair(SPEAKER)  # no error
    assert bluez.objects[path]["org.bluez.Device1"]["Trusted"] is True


def test_agent_says_no_to_strangers():
    agent = PairingAgent(is_paired=lambda path: path == "/paired")
    agent.expect("/mine")
    agent.request_confirmation("/mine", 1)
    agent.request_authorization("/mine")
    assert agent.request_pin_code("/mine") == "0000"
    assert agent.request_passkey("/mine") == 0
    for ask in (
        lambda: agent.request_confirmation("/stranger", 1),
        lambda: agent.request_authorization("/stranger"),
        lambda: agent.request_pin_code("/stranger"),
        lambda: agent.request_passkey("/stranger"),
        lambda: agent.authorize_service("/stranger", "uuid"),
    ):
        with pytest.raises(AgentRejected):
            ask()
    agent.authorize_service("/paired", "uuid")
    agent.authorize_service("/mine", "uuid")
    agent.expect(None)
    with pytest.raises(AgentRejected):
        agent.request_confirmation("/mine", 1)


# --- connect, disconnect, forget -----------------------------------------------------------


async def test_connect_and_disconnect(rig):
    bluez, backend = rig
    bluez.add_device(SPEAKER, "Soundbar", Paired=True, UUIDs=[A2DP_SINK])
    await backend.connect(SPEAKER)
    assert by_address(backend)[SPEAKER].connected
    await backend.disconnect(SPEAKER)
    assert not by_address(backend)[SPEAKER].connected


async def test_connect_asks_bluez_even_when_the_link_is_up(rig):
    # Right after pairing, Connected is true but no audio profile is: Connect must still go out.
    bluez, backend = rig
    bluez.add_device(SPEAKER, "Soundbar", Paired=True, Connected=True, UUIDs=[A2DP_SINK])
    await backend.connect(SPEAKER)
    assert bluez.members().count("Connect") == 1


async def test_connect_needs_pairing(rig):
    bluez, backend = rig
    bluez.add_device(SPEAKER, "Soundbar")
    with pytest.raises(BackendError, match="Soundbar isn't paired"):
        await backend.connect(SPEAKER)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            BluezError("org.bluez.Error.Failed", "Host is down"),
            "Couldn't reach Soundbar. Is it on?",
        ),
        (
            BluezError("org.bluez.Error.Failed", "br-connection-profile-unavailable"),
            "Couldn't connect Soundbar: sound isn't ready",
        ),
        (BluezError("org.bluez.Error.AlreadyConnected"), None),
    ],
)
async def test_connect_errors(rig, error, message):
    bluez, backend = rig
    path = bluez.add_device(SPEAKER, "Soundbar", Paired=True)
    bluez.fail[(path, "Connect")] = error
    if message is None:
        await backend.connect(SPEAKER)
    else:
        with pytest.raises(BackendError, match=message):
            await backend.connect(SPEAKER)


async def test_forget_removes_the_device(rig):
    bluez, backend = rig
    path = bluez.add_device(SPEAKER, "Soundbar", Paired=True, Connected=True)
    await backend.forget(SPEAKER)
    assert (HCI0, "RemoveDevice", [path]) in bluez.calls
    assert backend.devices() == []


async def test_unknown_device(rig):
    _, backend = rig
    with pytest.raises(BackendError, match="no device"):
        await backend.pair("00:11:22:33:44:55")


async def test_changes_are_announced(rig):
    bluez, backend = rig
    seen = []
    backend.on_change(lambda: seen.append([d.connected for d in backend.devices()]))
    path = bluez.add_device(SPEAKER, "Soundbar", Paired=True)
    bluez.update(path, Connected=True)
    assert seen[-1] == [True]

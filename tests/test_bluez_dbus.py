"""The real D-Bus layer (DBusLink + the exported agent) against a mock BlueZ on a private
dbus-daemon. No radio, no system bus: it checks what goes over the wire, which the in-memory
fake can't. Skipped where dbus-daemon isn't installed."""

import asyncio
import shutil
import subprocess

import pytest
from dbus_fast import BusType, DBusError, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from couchd.backends import BackendError
from couchd.bluez import AGENT_PATH, BluezBluetooth, DBusLink

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon")

HCI0 = "/org/bluez/hci0"
SPEAKER = "AA:BB:CC:00:00:01"
SPEAKER_PATH = f"{HCI0}/dev_AA_BB_CC_00_00_01"
A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"

CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={path}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.fixture
def bus_address(tmp_path):
    config = tmp_path / "bus.conf"
    config.write_text(CONFIG.format(path=tmp_path / "bus"))
    daemon = subprocess.Popen(
        ["dbus-daemon", "--nofork", "--print-address", f"--config-file={config}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    address = daemon.stdout.readline().strip()
    yield address
    daemon.terminate()
    daemon.wait(5)


# --- a mock bluetoothd ---------------------------------------------------------------------


class AgentManager(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.AgentManager1")
        self.registered = None
        self.default = None

    @method()
    def RegisterAgent(self, agent: "o", capability: "s") -> None:
        self.registered = (agent, capability)

    @method()
    def RequestDefaultAgent(self, agent: "o") -> None:
        self.default = agent

    @method()
    def UnregisterAgent(self, agent: "o") -> None:
        self.registered = None


class Adapter(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Adapter1")
        self.powered = False
        self.discovering = False
        self.filter = None

    @dbus_property()
    def Powered(self) -> "b":
        return self.powered

    @Powered.setter
    def Powered(self, value: "b"):
        self.powered = value

    @dbus_property(access=PropertyAccess.READ)
    def Discovering(self) -> "b":
        return self.discovering

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":
        return "DC:A6:32:00:00:00"

    @method()
    def SetDiscoveryFilter(self, filter: "a{sv}") -> None:
        self.filter = {k: v.value for k, v in filter.items()}

    @method()
    def StartDiscovery(self) -> None:
        self.discovering = True
        self.emit_properties_changed({"Discovering": True})

    @method()
    def StopDiscovery(self) -> None:
        self.discovering = False
        self.emit_properties_changed({"Discovering": False})

    @method()
    def RemoveDevice(self, device: "o") -> None:
        self.removed = device


class Device(ServiceInterface):
    def __init__(self, bluez):
        super().__init__("org.bluez.Device1")
        self.bluez = bluez
        self.paired = False
        self.trusted = False
        self.connected = False
        self.connect_error = None

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":
        return SPEAKER

    @dbus_property(access=PropertyAccess.READ)
    def Name(self) -> "s":
        return "Soundbar"

    @dbus_property(access=PropertyAccess.READ)
    def Alias(self) -> "s":
        return "Soundbar"

    @dbus_property(access=PropertyAccess.READ)
    def Adapter(self) -> "o":
        return HCI0

    @dbus_property(access=PropertyAccess.READ)
    def UUIDs(self) -> "as":
        return [A2DP_SINK]

    @dbus_property(access=PropertyAccess.READ)
    def Paired(self) -> "b":
        return self.paired

    @dbus_property(access=PropertyAccess.READ)
    def Connected(self) -> "b":
        return self.connected

    @dbus_property()
    def Trusted(self) -> "b":
        return self.trusted

    @Trusted.setter
    def Trusted(self, value: "b"):
        self.trusted = value

    @method()
    async def Pair(self) -> None:
        # Like bluetoothd: ask the registered agent, fail if it says no.
        reply = await self.bluez.ask_agent("RequestConfirmation", "ou", [SPEAKER_PATH, 123456])
        if reply.message_type == MessageType.ERROR:
            raise DBusError("org.bluez.Error.AuthenticationRejected", "agent said no")
        self.paired = True
        self.emit_properties_changed({"Paired": True})

    @method()
    def Connect(self) -> None:
        if self.connect_error:
            raise DBusError(*self.connect_error)
        self.connected = True
        self.emit_properties_changed({"Connected": True})


class MockBluez:
    def __init__(self, address):
        self.address = address
        self.agents = AgentManager()
        self.adapter = Adapter()
        self.device = Device(self)
        self.client = None  # the unique name of couchd's connection

    async def start(self):
        self.bus = await MessageBus(bus_address=self.address, bus_type=BusType.SESSION).connect()
        self.bus.export("/org/bluez", self.agents)
        self.bus.export(HCI0, self.adapter)
        self.bus.export(SPEAKER_PATH, self.device)
        await self.bus.request_name("org.bluez")
        return self

    def stop(self):
        self.bus.disconnect()

    async def ask_agent(self, member, signature, body):
        return await self.bus.call(
            Message(
                destination=self.client,
                path=self.agents.registered[0],
                interface="org.bluez.Agent1",
                member=member,
                signature=signature,
                body=body,
            )
        )


async def until(check, within=2.0):
    async with asyncio.timeout(within):
        while not check():  # noqa: ASYNC110 - polling state is the point
            await asyncio.sleep(0.01)


@pytest.fixture
async def rig(bus_address):
    bluez = await MockBluez(bus_address).start()
    link = DBusLink(bus_address)
    backend = BluezBluetooth(link)
    await backend.start()
    bluez.client = link._bus.unique_name
    yield bluez, backend
    await backend.stop()
    bluez.stop()


# --- tests ---------------------------------------------------------------------------------


async def test_start_over_the_wire(rig):
    bluez, backend = rig
    assert bluez.agents.registered == (AGENT_PATH, "NoInputNoOutput")
    assert bluez.agents.default == AGENT_PATH
    await until(lambda: bluez.adapter.powered)
    assert backend.devices() == []  # not paired, not scanning


async def test_scan_pair_connect(rig):
    bluez, backend = rig
    await backend.start_scan()
    assert bluez.adapter.discovering
    assert bluez.adapter.filter == {"DuplicateData": False}
    [device] = backend.devices()
    assert (device.name, device.audio, device.paired) == ("Soundbar", True, False)

    await backend.pair(SPEAKER)  # bluez -> our agent -> yes
    await until(lambda: bluez.device.trusted)
    await until(lambda: backend.devices()[0].paired)

    await backend.connect(SPEAKER)
    await until(lambda: backend.devices()[0].connected)


async def test_the_agent_rejects_pairing_nobody_asked_for(rig):
    bluez, _ = rig
    # Someone's phone tries to pair with the TV: bluetoothd asks our agent, which says no.
    reply = await bluez.ask_agent("RequestConfirmation", "ou", [SPEAKER_PATH, 1])
    assert reply.message_type == MessageType.ERROR
    assert reply.error_name == "org.bluez.Error.Rejected"
    reply = await bluez.ask_agent("AuthorizeService", "os", [SPEAKER_PATH, A2DP_SINK])
    assert reply.error_name == "org.bluez.Error.Rejected"


async def test_error_replies_become_messages(rig):
    bluez, backend = rig
    await backend.start_scan()
    await backend.pair(SPEAKER)
    await until(lambda: backend.devices()[0].paired)
    bluez.device.connect_error = ("org.bluez.Error.Failed", "br-connection-profile-unavailable")
    with pytest.raises(BackendError, match="sound isn't ready"):
        await backend.connect(SPEAKER)


async def test_bluetoothd_restart(rig, bus_address):
    bluez, backend = rig
    await backend.start_scan()
    await until(lambda: backend.devices() != [])
    bluez.stop()
    await until(lambda: backend.devices() == [])

    again = await MockBluez(bus_address).start()
    again.client = bluez.client
    await until(lambda: again.agents.registered == (AGENT_PATH, "NoInputNoOutput"))
    await until(lambda: again.adapter.discovering)  # scanning picked up where it was
    assert [d.address for d in backend.devices()] == [SPEAKER]
    again.stop()


async def test_variants_in_properties_changed_are_unwrapped(rig):
    bluez, backend = rig
    await backend.start_scan()
    await until(lambda: backend.devices() != [])
    bluez.device.connected = True
    bluez.device.emit_properties_changed({"Connected": True})
    await until(lambda: backend.devices()[0].connected is True)
    assert isinstance(backend._objects[SPEAKER_PATH]["org.bluez.Device1"]["Connected"], bool)

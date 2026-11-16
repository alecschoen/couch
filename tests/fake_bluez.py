"""An in-memory BlueZ behind the BluezLink interface, for testing BluezBluetooth."""

import copy
from typing import Any

from couchd.bluez import (
    ADAPTER,
    AGENT_MANAGER,
    DEVICE,
    OBJECT_MANAGER,
    PROPERTIES,
    BluezError,
    BluezLink,
    PairingAgent,
    _plain,
)

HCI0 = "/org/bluez/hci0"
A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"
A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"


def device_path(address: str, adapter: str = HCI0) -> str:
    return f"{adapter}/dev_{address.replace(':', '_')}"


class FakeBluez(BluezLink):
    def __init__(self, *, running: bool = True, adapter: bool = True) -> None:
        self.running = running
        self.objects: dict[str, dict[str, dict[str, Any]]] = {"/org/bluez": {AGENT_MANAGER: {}}}
        if adapter:
            self.objects[HCI0] = {ADAPTER: {"Address": "DC:A6:32:00:00:00", "Powered": False}}
        self.calls: list[tuple[str, str, Any]] = []  # (path, member, body)
        self.fail: dict[tuple[str, str], Exception] = {}
        self.agent: PairingAgent | None = None
        self.registered: tuple[str, str] | None = None
        self.default_agent: str | None = None
        self.discovering = False
        self.closed = False
        # What a device asks the agent while pairing: "confirm", "pin" or None.
        self.pair_asks: str | None = "confirm"
        self._signal_handlers = []
        self._presence_handlers = []

    # BluezLink

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    def bluez_running(self) -> bool:
        return self.running

    def on_signal(self, handler) -> None:
        self._signal_handlers.append(handler)

    def on_presence(self, handler) -> None:
        self._presence_handlers.append(handler)

    def export_agent(self, path: str, agent: PairingAgent) -> None:
        self.agent = agent

    async def call(self, path, interface, member, signature="", body=(), *, max_wait=10.0):
        body = _plain(list(body))
        self.calls.append((path, member, body))
        if not self.running:
            raise BluezError("org.freedesktop.DBus.Error.ServiceUnknown")
        if (path, member) in self.fail:
            raise self.fail[(path, member)]
        return getattr(self, f"_{member}")(path, *body)

    # test controls

    def members(self) -> list[str]:
        return [member for _, member, _ in self.calls]

    def add_device(self, address: str, name: str | None = None, **props: Any) -> str:
        path = device_path(address)
        props = {"Address": address, "Adapter": HCI0, "Paired": False, "Trusted": False,
                 "Connected": False, **props}  # fmt: skip
        if name is not None:
            props.setdefault("Name", name)
            props.setdefault("Alias", name)
        else:
            props.setdefault("Alias", address.replace(":", "-"))
        self.objects[path] = {DEVICE: props}
        self._signal("/", OBJECT_MANAGER, "InterfacesAdded", [path, copy.deepcopy({DEVICE: props})])
        return path

    def add_adapter(self, path: str = HCI0) -> None:
        props = {"Address": "DC:A6:32:00:00:00", "Powered": False}
        self.objects[path] = {ADAPTER: props}
        self._signal("/", OBJECT_MANAGER, "InterfacesAdded", [path, {ADAPTER: dict(props)}])

    def update(self, path: str, interface: str = DEVICE, **changed: Any) -> None:
        self.objects[path][interface].update(changed)
        self._signal(path, PROPERTIES, "PropertiesChanged", [interface, dict(changed), []])

    def stop_running(self) -> None:
        self.running = False
        self.registered = self.default_agent = None
        self.discovering = False
        for handler in list(self._presence_handlers):
            handler(False)

    def start_running(self) -> None:
        self.running = True
        for handler in list(self._presence_handlers):
            handler(True)

    def _signal(self, path: str, interface: str, member: str, body: list[Any]) -> None:
        for handler in list(self._signal_handlers):
            handler(path, interface, member, copy.deepcopy(body))

    # BlueZ methods

    def _GetManagedObjects(self, path):
        return [copy.deepcopy(self.objects)]

    def _RegisterAgent(self, path, agent, capability):
        if self.registered is not None:
            raise BluezError("org.bluez.Error.AlreadyExists")
        self.registered = (agent, capability)
        return []

    def _RequestDefaultAgent(self, path, agent):
        self.default_agent = agent
        return []

    def _UnregisterAgent(self, path, agent):
        self.registered = None
        return []

    def _Set(self, path, interface, name, value):
        self.update(path, interface, **{name: value})
        return []

    def _SetDiscoveryFilter(self, path, filter):
        return []

    def _StartDiscovery(self, path):
        self.discovering = True
        self.update(path, ADAPTER, Discovering=True)
        return []

    def _StopDiscovery(self, path):
        self.discovering = False
        self.update(path, ADAPTER, Discovering=False)
        return []

    def _Pair(self, path):
        assert self.agent is not None
        try:
            if self.pair_asks == "confirm":
                self.agent.request_confirmation(path, 123456)
            elif self.pair_asks == "pin":
                assert self.agent.request_pin_code(path) == "0000"
        except Exception as e:
            raise BluezError("org.bluez.Error.AuthenticationRejected", str(e)) from e
        self.update(path, Paired=True)
        return []

    def _Connect(self, path):
        self.update(path, Connected=True)
        return []

    def _Disconnect(self, path):
        self.update(path, Connected=False)
        return []

    def _RemoveDevice(self, path, device):
        del self.objects[device]
        self._signal("/", OBJECT_MANAGER, "InterfacesRemoved", [device, [DEVICE]])
        return []

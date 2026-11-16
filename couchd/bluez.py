"""Bluetooth on the Pi: BlueZ over the system D-Bus, with dbus-fast.

Three layers, so everything but the wire can be tested without a radio:

- `BluezLink`: the handful of D-Bus operations we need (call a method, watch BlueZ's
  signals, notice bluetoothd come and go, export the pairing agent). `DBusLink` is the real
  one. Tests swap in an in-memory BlueZ.
- `PairingAgent`: what to answer when BlueZ asks about pairing. A TV has only a remote, so
  it registers as `NoInputNoOutput` (no PIN to type, no code to confirm) and says yes only
  to the device the user picked on screen. A stranger's phone gets no for an answer.
- `BluezBluetooth`: the BluetoothBackend. It mirrors BlueZ's objects (the adapter and its
  devices) from GetManagedObjects, keeps them current from InterfacesAdded/Removed and
  PropertiesChanged, and starts over when bluetoothd restarts.

Auto-reconnect and switching sound are BluetoothService's job (couchd/bluetooth.py), so the
dev fakes get them too.
"""

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from typing import Any

from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from couchd.backends import BackendError, BluetoothBackend, Device

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
AGENT = "org.bluez.Agent1"
AGENT_MANAGER = "org.bluez.AgentManager1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"

AGENT_PATH = "/couch/agent"
AGENT_CAPABILITY = "NoInputNoOutput"

CALL_TIMEOUT = 10.0
CONNECT_TIMEOUT = 30.0
# Pairing can wait on the other side (a speaker that wants its button held).
PAIR_TIMEOUT = 60.0

# Profiles an audio *output* offers: A2DP sink, headset, hands-free, LE Audio. A phone
# offers the source side (110a, 111f) and shouldn't count as a speaker.
AUDIO_UUIDS = {
    f"0000{short}-0000-1000-8000-00805f9b34fb" for short in ("110b", "1108", "111e", "184e", "1850")
}
AUDIO_VIDEO_MAJOR_CLASS = 0x04


class BluezError(Exception):
    """A D-Bus error reply, e.g. `org.bluez.Error.AuthenticationFailed`."""

    def __init__(self, name: str, text: str = "") -> None:
        super().__init__(f"{name}: {text}" if text else name)
        self.name = name
        self.text = text


SignalHandler = Callable[[str, str, str, list[Any]], None]  # path, interface, member, body


class BluezLink(ABC):
    """The D-Bus operations BluezBluetooth needs. Values come back as plain Python (no
    Variants), so the logic above doesn't care what is on the other end."""

    @abstractmethod
    async def open(self) -> None:
        """Connect to the bus and start watching BlueZ."""

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def bluez_running(self) -> bool: ...

    @abstractmethod
    def on_signal(self, handler: SignalHandler) -> None:
        """Every signal BlueZ sends."""

    @abstractmethod
    def on_presence(self, handler: Callable[[bool], None]) -> None:
        """bluetoothd appeared (True) or went away (False)."""

    @abstractmethod
    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Iterable[Any] = (),
        *,
        max_wait: float = CALL_TIMEOUT,
    ) -> list[Any]:
        """Call a BlueZ method. Raises BluezError on an error reply, TimeoutError."""

    @abstractmethod
    def export_agent(self, path: str, agent: "PairingAgent") -> None: ...


# --- the pairing agent ---------------------------------------------------------------------


class AgentRejected(Exception):
    """Answer BlueZ with org.bluez.Error.Rejected."""


class PairingAgent:
    """BlueZ's questions, answered for a TV with a remote and nothing to type on.

    couchd tells it which device it is pairing (`expect`). That device gets a yes to every
    question, and the PIN 0000 if it is old enough to want one. Already-paired devices may
    use their services. Anything else is rejected, so nobody pairs with the TV from their
    phone on the other side of the wall.
    """

    LEGACY_PIN = "0000"

    def __init__(self, is_paired: Callable[[str], bool] = lambda path: False) -> None:
        self.is_paired = is_paired
        self.expected: str | None = None

    def expect(self, device_path: str | None) -> None:
        self.expected = device_path

    def _check(self, device: str, question: str) -> None:
        if device != self.expected:
            log.warning("rejected pairing request", extra={"device": device, "ask": question})
            raise AgentRejected(f"{device} isn't being paired")
        log.info("accepted pairing request", extra={"device": device, "ask": question})

    def request_pin_code(self, device: str) -> str:
        self._check(device, "pin")
        return self.LEGACY_PIN

    def request_passkey(self, device: str) -> int:
        self._check(device, "passkey")
        return 0

    def request_confirmation(self, device: str, passkey: int) -> None:
        self._check(device, "confirm")

    def request_authorization(self, device: str) -> None:
        self._check(device, "authorize")

    def authorize_service(self, device: str, uuid: str) -> None:
        if device == self.expected or self.is_paired(device):
            return
        log.warning("rejected service", extra={"device": device, "uuid": uuid})
        raise AgentRejected(f"{device} isn't paired")

    def display_pin_code(self, device: str, pincode: str) -> None:
        log.info("pairing shows a PIN", extra={"device": device})

    def display_passkey(self, device: str, passkey: int, entered: int) -> None:
        log.info("pairing shows a passkey", extra={"device": device})

    def cancel(self) -> None:
        log.info("pairing cancelled by BlueZ")

    def release(self) -> None:
        log.info("pairing agent released by BlueZ")


# --- the backend ---------------------------------------------------------------------------


def is_audio(props: dict[str, Any]) -> bool:
    """Speakers, headphones, soundbars: what the TV can send sound to."""
    if any(uuid.lower() in AUDIO_UUIDS for uuid in props.get("UUIDs", ())):
        return True
    if str(props.get("Icon", "")).startswith("audio-"):
        return True
    cls = props.get("Class")
    return isinstance(cls, int) and (cls >> 8) & 0x1F == AUDIO_VIDEO_MAJOR_CLASS


class BluezBluetooth(BluetoothBackend):
    def __init__(self, link: BluezLink | None = None, *, paired_grace: float = 3.0) -> None:
        super().__init__()
        self._link = link if link is not None else DBusLink()
        self._paired_grace = paired_grace
        self._objects: dict[str, dict[str, dict[str, Any]]] = {}
        self._adapter: str | None = None
        self._scan_wanted = False
        self._discovering = False  # we hold a discovery session with BlueZ
        self._tasks: set[asyncio.Task] = set()
        self.agent = PairingAgent(self._is_paired)

    # lifecycle

    async def start(self) -> None:
        await self._link.open()
        self._link.on_signal(self._on_signal)
        self._link.on_presence(self._on_presence)
        self._link.export_agent(AGENT_PATH, self.agent)
        if self._link.bluez_running():
            await self._attach()
        else:
            log.warning("bluetoothd isn't running; waiting for it")

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._link.bluez_running():
            if self._discovering and self._adapter:
                with contextlib.suppress(BluezError, TimeoutError):
                    await self._link.call(self._adapter, ADAPTER, "StopDiscovery")
            with contextlib.suppress(BluezError, TimeoutError):
                await self._link.call(
                    "/org/bluez", AGENT_MANAGER, "UnregisterAgent", "o", [AGENT_PATH]
                )
        self._discovering = False
        await self._link.close()

    async def _attach(self) -> None:
        """bluetoothd is there: read everything, register the agent, power the radio."""
        (objects,) = await self._link.call("/", OBJECT_MANAGER, "GetManagedObjects")
        self._objects = objects
        self._discovering = False
        await self._register_agent()
        adapters = sorted(p for p, ifaces in objects.items() if ADAPTER in ifaces)
        if adapters:
            await self._use_adapter(adapters[0])
        else:
            log.warning("no Bluetooth adapter yet")
        self.changed()

    async def _register_agent(self) -> None:
        try:
            await self._link.call(
                "/org/bluez", AGENT_MANAGER, "RegisterAgent", "os", [AGENT_PATH, AGENT_CAPABILITY]
            )
        except BluezError as e:
            if e.name != "org.bluez.Error.AlreadyExists":
                raise
        await self._link.call("/org/bluez", AGENT_MANAGER, "RequestDefaultAgent", "o", [AGENT_PATH])

    async def _use_adapter(self, path: str) -> None:
        self._adapter = path
        log.info("using Bluetooth adapter", extra={"adapter": path})
        try:
            await self._power_on()
            if self._scan_wanted:
                await self._start_discovery()
        except BackendError as e:
            log.warning("Bluetooth adapter isn't ready", extra={"error": str(e)})

    async def _power_on(self) -> None:
        """Switch the radio on if it isn't. Tried again before every scan, pair and connect,
        so a radio that was blocked (rfkill) at startup works once it is unblocked."""
        if self._adapter is None:
            raise BackendError("Bluetooth isn't available")
        if self._objects.get(self._adapter, {}).get(ADAPTER, {}).get("Powered"):
            return
        try:
            await self._set(self._adapter, ADAPTER, "Powered", "b", True)
        except BackendError as e:
            raise BackendError("Bluetooth is switched off") from e

    def _on_presence(self, running: bool) -> None:
        if running:
            log.info("bluetoothd is back")
            self._spawn(self._attach())
        else:
            log.warning("bluetoothd went away")
            self._objects = {}
            self._adapter = None
            self._discovering = False
            self.changed()

    def _on_signal(self, path: str, interface: str, member: str, body: list[Any]) -> None:
        if interface == OBJECT_MANAGER and member == "InterfacesAdded":
            obj, ifaces = body
            self._objects.setdefault(obj, {}).update(ifaces)
            if ADAPTER in ifaces and self._adapter is None:
                self._spawn(self._use_adapter(obj))
        elif interface == OBJECT_MANAGER and member == "InterfacesRemoved":
            obj, names = body
            entry = self._objects.get(obj, {})
            for name in names:
                entry.pop(name, None)
            if not entry:
                self._objects.pop(obj, None)
            if obj == self._adapter and ADAPTER in names:
                log.warning("Bluetooth adapter went away", extra={"adapter": obj})
                self._adapter = None
                self._discovering = False
        elif interface == PROPERTIES and member == "PropertiesChanged":
            iface, changed, invalidated = body
            props = self._objects.get(path, {}).get(iface)
            if props is None:
                return
            props.update(changed)
            for name in invalidated:
                props.pop(name, None)
        else:
            return
        self.changed()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Bluetooth setup failed", exc_info=task.exception())

    # what the service reads

    def devices(self) -> list[Device]:
        found = []
        for props in self._device_props():
            paired = bool(props.get("Paired"))
            connected = bool(props.get("Connected"))
            # While scanning, BlueZ reports every beacon and fitness band in the street.
            # Only show what has a name; nameless things can't be picked anyway.
            if not (paired or connected or (self._scan_wanted and props.get("Name"))):
                continue
            address = props["Address"]
            found.append(
                Device(
                    address=address,
                    name=props.get("Alias") or props.get("Name") or address,
                    audio=is_audio(props),
                    paired=paired,
                    connected=connected,
                )
            )
        return found

    def scanning(self) -> bool:
        return self._scan_wanted

    def _device_props(self) -> Iterable[dict[str, Any]]:
        if self._adapter is None:
            return
        for ifaces in self._objects.values():
            props = ifaces.get(DEVICE)
            if props is not None and props.get("Adapter") == self._adapter and "Address" in props:
                yield props

    def _is_paired(self, path: str) -> bool:
        return bool(self._objects.get(path, {}).get(DEVICE, {}).get("Paired"))

    # commands

    async def start_scan(self) -> None:
        if self._scan_wanted:
            return
        self._scan_wanted = True
        self.changed()
        if self._adapter is None:
            return
        try:
            await self._power_on()
            await self._start_discovery()
        except BackendError:
            self._scan_wanted = False
            self.changed()
            raise

    async def stop_scan(self) -> None:
        if not self._scan_wanted:
            return
        self._scan_wanted = False
        await self._stop_discovery()
        self.changed()

    async def _start_discovery(self) -> None:
        if self._discovering or self._adapter is None:
            return
        # One PropertiesChanged per device, not one per advertisement.
        with contextlib.suppress(BluezError):
            await self._link.call(
                self._adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}", [_filter()]
            )
        try:
            await self._link.call(self._adapter, ADAPTER, "StartDiscovery")
        except BluezError as e:
            if e.name != "org.bluez.Error.InProgress":
                log.warning("can't scan", extra={"error": str(e)})
                raise BackendError("Bluetooth can't scan right now") from e
        except TimeoutError as e:
            raise BackendError("Bluetooth can't scan right now") from e
        self._discovering = True

    async def _stop_discovery(self) -> None:
        if not self._discovering or self._adapter is None:
            self._discovering = False
            return
        self._discovering = False
        try:
            await self._link.call(self._adapter, ADAPTER, "StopDiscovery")
        except (BluezError, TimeoutError) as e:
            log.info("StopDiscovery failed", extra={"error": str(e)})

    @contextlib.asynccontextmanager
    async def _quiet(self) -> AsyncIterator[None]:
        """Pause scanning: on the Pi's radio, discovery makes pairing and connecting flaky."""
        paused = self._discovering
        if paused:
            await self._stop_discovery()
        try:
            yield
        finally:
            if paused and self._scan_wanted:
                with contextlib.suppress(BackendError):
                    await self._start_discovery()

    async def pair(self, address: str) -> None:
        path, props = self._find(address)
        name = _name(props)
        if not props.get("Paired"):
            await self._power_on()
            self.agent.expect(path)
            try:
                async with self._quiet():
                    await self._device_call(path, "Pair", name, "pair", max_wait=PAIR_TIMEOUT)
            except BackendError:
                # Some speakers start pairing from their side at the same moment. BlueZ's own
                # attempt then fails (AuthenticationFailed) while theirs goes through, and our
                # agent is still there to say yes to it.
                if not await self._becomes_paired(path):
                    raise
                log.info("paired from the device's side", extra={"device": address})
            finally:
                self.agent.expect(None)
        if not props.get("Trusted"):
            # Trusted lets the speaker connect back by itself when it is switched on.
            try:
                await self._set(path, DEVICE, "Trusted", "b", True)
            except BackendError:
                log.warning("couldn't mark device trusted", extra={"device": address})

    async def _becomes_paired(self, path: str) -> bool:
        try:
            async with asyncio.timeout(self._paired_grace):
                while not self._is_paired(path):  # noqa: ASYNC110 - Paired arrives as a signal
                    await asyncio.sleep(min(0.1, self._paired_grace / 5))
        except TimeoutError:
            return False
        return True

    async def connect(self, address: str) -> None:
        path, props = self._find(address)
        name = _name(props)
        if not props.get("Paired"):
            raise BackendError(f"{name} isn't paired")
        # No shortcut on Connected: it only means the radio link is up, which pairing alone
        # does too. Connect brings up the audio profiles; if they're up already BlueZ says
        # AlreadyConnected, which counts as done.
        await self._power_on()
        async with self._quiet():
            await self._device_call(path, "Connect", name, "connect", max_wait=CONNECT_TIMEOUT)

    async def disconnect(self, address: str) -> None:
        path, props = self._find(address)
        await self._device_call(path, "Disconnect", _name(props), "disconnect")

    async def forget(self, address: str) -> None:
        path, props = self._find(address)
        assert self._adapter is not None  # _find found a device on it
        try:
            await self._link.call(self._adapter, ADAPTER, "RemoveDevice", "o", [path])
        except (BluezError, TimeoutError) as e:
            raise _explain(e, _name(props), "forget") from e

    def _find(self, address: str) -> tuple[str, dict[str, Any]]:
        if self._adapter is None:
            raise BackendError("Bluetooth isn't available")
        for path, ifaces in self._objects.items():
            props = ifaces.get(DEVICE)
            if props and props.get("Adapter") == self._adapter and props.get("Address") == address:
                return path, props
        raise BackendError(f"no device {address}")

    async def _device_call(
        self, path: str, member: str, name: str, verb: str, *, max_wait: float = CALL_TIMEOUT
    ) -> None:
        try:
            await self._link.call(path, DEVICE, member, max_wait=max_wait)
        except BluezError as e:
            if e.name in _ALREADY_DONE.get(member, ()):
                return
            log.warning(f"{member} failed", extra={"device": path, "error": str(e)})
            raise _explain(e, name, verb) from e
        except TimeoutError as e:
            log.warning(f"{member} timed out", extra={"device": path})
            raise _explain(e, name, verb) from e

    async def _set(self, path: str, interface: str, prop: str, sig: str, value: Any) -> None:
        try:
            await self._link.call(
                path, PROPERTIES, "Set", "ssv", [interface, prop, Variant(sig, value)]
            )
        except (BluezError, TimeoutError) as e:
            log.warning("couldn't set property", extra={"property": prop, "error": str(e)})
            raise BackendError(f"Bluetooth refused to set {prop}") from e


_ALREADY_DONE = {
    "Pair": ("org.bluez.Error.AlreadyExists",),
    "Connect": ("org.bluez.Error.AlreadyConnected",),
    "Disconnect": ("org.bluez.Error.NotConnected",),
}


def _filter() -> dict[str, Any]:
    return {"DuplicateData": Variant("b", False)}


def _name(props: dict[str, Any]) -> str:
    return props.get("Alias") or props.get("Name") or props.get("Address", "the device")


def _explain(error: Exception, name: str, verb: str) -> BackendError:
    """A BlueZ error as something to put on the TV."""
    if isinstance(error, TimeoutError):
        return BackendError(f"{name} didn't answer")
    assert isinstance(error, BluezError)
    short = error.name.removeprefix("org.bluez.Error.")
    text = error.text.lower()
    if short.startswith("Authentication"):
        return BackendError(f"{name} didn't accept the pairing")
    if "profile-unavailable" in text or "protocol not available" in text:
        # BlueZ connects audio through PipeWire; without it there is nothing to connect to.
        return BackendError(f"Couldn't connect {name}: sound isn't ready")
    if (
        short == "ConnectionAttemptFailed"
        or "page-timeout" in text
        or "page timeout" in text
        or "host is down" in text
    ):
        hint = "Is it on and in pairing mode?" if verb == "pair" else "Is it on?"
        return BackendError(f"Couldn't reach {name}. {hint}")
    if short == "InProgress":
        return BackendError(f"Already busy with {name}")
    if short == "DoesNotExist":
        return BackendError(f"{name} is gone")
    return BackendError(f"Couldn't {verb} {name}")


# --- the real D-Bus link -------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Strip dbus-fast Variants, all the way down."""
    if isinstance(value, Variant):
        return _plain(value.value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class DBusLink(BluezLink):
    """BlueZ on the system bus (or `bus_address`, which tests point at a private bus)."""

    def __init__(self, bus_address: str | None = None) -> None:
        self._address = bus_address
        self._bus: MessageBus | None = None
        self._owner: str | None = None
        self._signal_handlers: list[SignalHandler] = []
        self._presence_handlers: list[Callable[[bool], None]] = []

    async def open(self) -> None:
        bus = MessageBus(bus_address=self._address, bus_type=BusType.SYSTEM)
        self._bus = await bus.connect()
        self._bus.add_message_handler(self._on_message)
        rules = (
            f"type='signal',sender='{BLUEZ}'",
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
            f"member='NameOwnerChanged',arg0='{BLUEZ}'",
        )
        for rule in rules:
            await self._bus_call("AddMatch", "s", [rule])
        try:
            (self._owner,) = await self._bus_call("GetNameOwner", "s", [BLUEZ])
        except BluezError:
            self._owner = None

    async def close(self) -> None:
        if self._bus is None:
            return
        self._bus.disconnect()
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2):
                await self._bus.wait_for_disconnect()
        self._bus = None

    def bluez_running(self) -> bool:
        return self._owner is not None

    def on_signal(self, handler: SignalHandler) -> None:
        self._signal_handlers.append(handler)

    def on_presence(self, handler: Callable[[bool], None]) -> None:
        self._presence_handlers.append(handler)

    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Iterable[Any] = (),
        *,
        max_wait: float = CALL_TIMEOUT,
    ) -> list[Any]:
        return await self._send(BLUEZ, path, interface, member, signature, body, max_wait)

    async def _bus_call(self, member: str, signature: str, body: list[Any]) -> list[Any]:
        return await self._send(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            member,
            signature,
            body,
            CALL_TIMEOUT,
        )

    async def _send(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str,
        body: Iterable[Any],
        max_wait: float,
    ) -> list[Any]:
        if self._bus is None:
            raise BluezError("org.freedesktop.DBus.Error.Disconnected", "not connected")
        message = Message(
            destination=destination,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=list(body),
        )
        async with asyncio.timeout(max_wait):
            reply = await self._bus.call(message)
        if reply.message_type == MessageType.ERROR:
            text = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
            raise BluezError(reply.error_name or "unknown", text)
        return _plain(reply.body)

    def _on_message(self, message: Any) -> None:
        if message.message_type != MessageType.SIGNAL:
            return
        if message.member == "NameOwnerChanged" and message.sender == "org.freedesktop.DBus":
            name, _old, new = message.body
            if name == BLUEZ:
                self._owner = new or None
                for handler in list(self._presence_handlers):
                    _safely(handler, bool(new))
            return
        if self._owner is None or message.sender != self._owner:
            return
        body = _plain(message.body)
        for handler in list(self._signal_handlers):
            _safely(handler, message.path, message.interface, message.member, body)

    def export_agent(self, path: str, agent: PairingAgent) -> None:
        self._bus.export(path, _AgentInterface(agent))


def _safely(handler: Callable[..., None], *args: Any) -> None:
    try:
        handler(*args)
    except Exception:
        log.exception("Bluetooth signal handler failed")


def _ask(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except AgentRejected as e:
        raise DBusError("org.bluez.Error.Rejected", str(e)) from e


# The string annotations are D-Bus signatures, which dbus-fast reads; hence the noqa.
class _AgentInterface(ServiceInterface):
    """org.bluez.Agent1 on the bus, answering with a PairingAgent."""

    def __init__(self, agent: PairingAgent) -> None:
        super().__init__(AGENT)
        self.agent = agent

    @method()
    def Release(self) -> None:
        self.agent.release()

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        return _ask(self.agent.request_pin_code, device)

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: F821
        self.agent.display_pin_code(device, pincode)

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        return _ask(self.agent.request_passkey, device)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: F821
        self.agent.display_passkey(device, passkey, entered)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821
        _ask(self.agent.request_confirmation, device, passkey)

    @method()
    def RequestAuthorization(self, device: "o") -> None:  # noqa: F821
        _ask(self.agent.request_authorization, device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: F821
        _ask(self.agent.authorize_service, device, uuid)

    @method()
    def Cancel(self) -> None:
        self.agent.cancel()

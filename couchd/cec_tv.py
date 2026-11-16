"""The real TV backend: Couch as an HDMI-CEC Playback device.

What it does on the bus:

- Claims a Playback logical address (normally 4, *Playback 1*) with OSD name "Couch". The
  kernel then answers Give OSD Name, Give Physical Address, Give Device Vendor ID and Get CEC
  Version itself; we answer them too in case a message slips through.
- `power_on()`: Image View On to the TV, then Active Source, so the TV wakes and switches to us.
- Answers Give Device Power Status (always on), Menu Request (activated, which some TVs need
  before they forward remote keys), and Request Active Source (when we're the source the TV
  should show, e.g. the TV was just turned on with its own remote).
- Follows Standby, Report Power Status, Routing Change, Routing Information, Set Stream Path and
  other devices' Active Source to keep `power()` and `active()` true.
- Turns User Control Pressed into key names (couchd.keys). Key repeat, hold-Back-for-Home and
  per-app routing are #8; this is the plain mapping.
- Feature Aborts any other message addressed to us, as the spec requires.

Every frame in and out is logged at debug level, for the TV compatibility notes (#9).
"""

import asyncio
import contextlib
import errno
import logging

from couchd import cec
from couchd.backends import BackendError, TvBackend, TvPower

log = logging.getLogger(__name__)

OSD_NAME = "Couch"
CLAIM_TIMEOUT = 5.0
TX_TIMEOUT = 3.0
POWER_POLL_INTERVAL = 15.0

# CEC 1.4 UI command codes (User Control Pressed) -> couchd.keys names.
UI_COMMANDS = {
    0x00: "select",
    0x01: "up",
    0x02: "down",
    0x03: "left",
    0x04: "right",
    0x09: "home",  # Root Menu
    0x0D: "back",  # Exit
    **{0x20 + n: str(n) for n in range(10)},
    0x2B: "select",  # Enter
    0x44: "play",
    0x45: "stop",
    0x46: "pause",
    0x48: "rewind",
    0x49: "fast_forward",
    0x60: "play",  # Play Function
    0x61: "play_pause",  # Pause-Play Function
    0x64: "stop",  # Stop Function
    0x71: "blue",  # F1
    0x72: "red",  # F2
    0x73: "green",  # F3
    0x74: "yellow",  # F4
}
PLAY_MODES = {0x24: "play", 0x25: "pause"}  # <Play> forward, still
DECK_CONTROL_MODES = {1: "fast_forward", 2: "rewind", 3: "stop"}  # skip fwd, skip back, stop
POWER_STATUS = {
    cec.POWER_ON: "on",
    cec.POWER_TO_ON: "on",
    cec.POWER_STANDBY: "standby",
    cec.POWER_TO_STANDBY: "standby",
}


def routes_to(path: int, phys_addr: int) -> bool:
    """True if a stream path to `path` ends at (or passes through a switch to) `phys_addr`."""
    if path in (0, cec.NO_PHYS_ADDR) or phys_addr == cec.NO_PHYS_ADDR:
        return False
    for shift in (12, 8, 4, 0):
        nibble = (path >> shift) & 0xF
        if nibble == 0:
            return path & ((1 << (shift + 4)) - 1) == 0
        if nibble != (phys_addr >> shift) & 0xF:
            return False
    return True


class CecTv(TvBackend):
    def __init__(
        self,
        adapter: cec.Adapter | None = None,
        *,
        osd_name: str = OSD_NAME,
        vendor_id: int = cec.VENDOR_BROADCOM,
        claim_timeout: float = CLAIM_TIMEOUT,
        tx_timeout: float = TX_TIMEOUT,
        poll_interval: float = POWER_POLL_INTERVAL,
    ) -> None:
        super().__init__()
        self.adapter = adapter if adapter is not None else cec.KernelAdapter()
        self.osd_name = osd_name
        self.vendor_id = vendor_id
        self.claim_timeout = claim_timeout
        self.tx_timeout = tx_timeout
        self.poll_interval = poll_interval

        self._power: TvPower = "unknown"
        # We're the source the TV should show. Survives TV standby: the TV comes back on the
        # input it was on (the KDL-40V55 does, without asking who's active), and if it does
        # ask, we say "us". Cleared when the user picks another input.
        self._ours = False
        self._log_addr: int | None = None
        self._phys_addr = cec.NO_PHYS_ADDR
        self._claimed = asyncio.Event()
        self._pending: dict[int, asyncio.Future[cec.Received]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._reading = False
        self._handlers = {
            cec.STANDBY: self._on_standby,
            cec.REPORT_POWER_STATUS: self._on_report_power_status,
            cec.GIVE_DEVICE_POWER_STATUS: self._on_give_power_status,
            cec.GIVE_OSD_NAME: self._on_give_osd_name,
            cec.GIVE_PHYSICAL_ADDR: self._on_give_physical_addr,
            cec.GIVE_DEVICE_VENDOR_ID: self._on_give_vendor_id,
            cec.GET_CEC_VERSION: self._on_get_cec_version,
            cec.ACTIVE_SOURCE: self._on_active_source,
            cec.ROUTING_CHANGE: self._on_routing_change,
            cec.ROUTING_INFORMATION: self._on_stream_path,
            cec.SET_STREAM_PATH: self._on_stream_path,
            cec.REQUEST_ACTIVE_SOURCE: self._on_request_active_source,
            cec.MENU_REQUEST: self._on_menu_request,
            cec.USER_CONTROL_PRESSED: self._on_user_control_pressed,
            cec.PLAY: self._on_play,
            cec.DECK_CONTROL: self._on_deck_control,
            # Replies and announcements: nothing to do, but never Feature Abort them.
            cec.USER_CONTROL_RELEASED: None,
            cec.FEATURE_ABORT: self._on_feature_abort,
            cec.SET_OSD_NAME: None,
            cec.CEC_VERSION: None,
            cec.REPORT_PHYSICAL_ADDR: None,
            cec.DEVICE_VENDOR_ID: None,
            cec.MENU_STATUS: None,
            cec.INACTIVE_SOURCE: None,
        }

    # --- TvBackend ----------------------------------------------------------------------

    def power(self) -> TvPower:
        return self._power

    def active(self) -> bool:
        return self._ours and self._power == "on"

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.adapter.open()
        try:
            self.adapter.claim(self.osd_name, self.vendor_id)
            loop.add_reader(self.adapter.fileno(), self._drain)
        except BaseException:
            self.adapter.close()
            raise
        self._reading = True
        self._spawn(self._poll_power())
        self._drain()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        if self._reading:
            asyncio.get_running_loop().remove_reader(self.adapter.fileno())
            self._reading = False
        self.adapter.close()

    async def power_on(self) -> None:
        await self._wait_claimed()
        self._set(ours=True)
        await self._transmit(cec.TV, cec.IMAGE_VIEW_ON)
        await self._announce()
        self._set(power="on")

    async def standby(self) -> None:
        await self._wait_claimed()
        await self._transmit(cec.TV, cec.STANDBY)
        self._set(power="standby")

    # --- reading ------------------------------------------------------------------------

    def _drain(self) -> None:
        try:
            while (event := self.adapter.event()) is not None:
                self._on_event(event)
            while (received := self.adapter.receive()) is not None:
                self._on_received(received)
        except OSError as e:
            # The node went away (or worse). Stop watching rather than spin on it.
            log.error("CEC device failed, giving up on it", extra={"error": str(e)})
            if self._reading:
                asyncio.get_running_loop().remove_reader(self.adapter.fileno())
                self._reading = False
            self._claimed.clear()
            self._set(power="unknown")

    def _on_event(self, event: cec.Event) -> None:
        if event.kind == cec.EVENT_LOST_MSGS:
            log.warning("CEC messages lost", extra={"count": event.lost_msgs})
            return
        if event.kind != cec.EVENT_STATE_CHANGE:
            return
        self._phys_addr = event.phys_addr
        if event.log_addr_mask:
            self._log_addr = self.adapter.log_addr()
            if not self._claimed.is_set():
                log.info(
                    "claimed CEC address",
                    extra={
                        "logical": self._log_addr,
                        "physical": cec.format_phys_addr(event.phys_addr),
                    },
                )
            self._claimed.set()
            self._send_soon(cec.TV, cec.GIVE_DEVICE_POWER_STATUS)
        else:
            if self._claimed.is_set():
                log.info(
                    "lost CEC address",
                    extra={"physical": cec.format_phys_addr(event.phys_addr)},
                )
            self._log_addr = None
            self._claimed.clear()
            # No address means no HDMI hotplug: the TV is unplugged, off at the wall, or (on
            # the KDL-40V55) just powering on or switching back to our input, for under a
            # second. The address comes back by itself when hotplug does.
            self._set(power="unknown")

    def _on_received(self, received: cec.Received) -> None:
        if received.sequence:
            future = self._pending.pop(received.sequence, None)
            if future is not None and not future.done():
                future.set_result(received)
            return
        data = received.data
        log.debug("cec rx", extra={"frame": cec.describe(data)})
        if len(data) < 2:
            return  # a poll
        source, dest, opcode, args = data[0] >> 4, data[0] & 0xF, data[1], data[2:]
        if opcode not in self._handlers:
            if dest != cec.BROADCAST:
                self._send_soon(source, cec.FEATURE_ABORT, opcode, cec.ABORT_UNRECOGNIZED_OP)
            return
        handler = self._handlers[opcode]
        if handler is None:
            return
        try:
            handler(source, args)
        except IndexError:
            log.debug("short CEC message ignored", extra={"frame": cec.describe(data)})

    # --- message handlers -----------------------------------------------------------------

    def _on_standby(self, source: int, args: bytes) -> None:
        self._set(power="standby")

    def _on_report_power_status(self, source: int, args: bytes) -> None:
        if source != cec.TV:
            return
        self._set(power=POWER_STATUS.get(args[0], "unknown"))

    def _on_give_power_status(self, source: int, args: bytes) -> None:
        self._send_soon(source, cec.REPORT_POWER_STATUS, cec.POWER_ON)

    def _on_give_osd_name(self, source: int, args: bytes) -> None:
        self._send_soon(source, cec.SET_OSD_NAME, *self.osd_name.encode("ascii", "replace")[:14])

    def _on_give_physical_addr(self, source: int, args: bytes) -> None:
        pa = self._phys_addr
        self._send_soon(
            cec.BROADCAST, cec.REPORT_PHYSICAL_ADDR, pa >> 8, pa & 0xFF, cec.PRIM_DEVTYPE_PLAYBACK
        )

    def _on_give_vendor_id(self, source: int, args: bytes) -> None:
        v = self.vendor_id
        self._send_soon(cec.BROADCAST, cec.DEVICE_VENDOR_ID, v >> 16, (v >> 8) & 0xFF, v & 0xFF)

    def _on_get_cec_version(self, source: int, args: bytes) -> None:
        self._send_soon(source, cec.CEC_VERSION, cec.VERSION_1_4)

    def _on_active_source(self, source: int, args: bytes) -> None:
        if (args[0] << 8 | args[1]) != self._phys_addr:
            self._set(ours=False)

    def _on_routing_change(self, source: int, args: bytes) -> None:
        self._route(args[2] << 8 | args[3])

    def _on_stream_path(self, source: int, args: bytes) -> None:
        self._route(args[0] << 8 | args[1])

    def _route(self, path: int) -> None:
        if routes_to(path, self._phys_addr):
            self._send_announce()
            self._set(power="on", ours=True)
        else:
            self._set(ours=False)

    def _on_request_active_source(self, source: int, args: bytes) -> None:
        if source == cec.TV:
            self._set(power="on")
        if self._ours:
            self._send_announce()

    def _on_menu_request(self, source: int, args: bytes) -> None:
        status = cec.MENU_DEACTIVATED if args[0] == cec.MENU_DEACTIVATE else cec.MENU_ACTIVATED
        self._send_soon(source, cec.MENU_STATUS, status)

    def _on_user_control_pressed(self, source: int, args: bytes) -> None:
        # The TV only forwards keys to the input it's showing.
        self._set(power="on", ours=True)
        key = UI_COMMANDS.get(args[0])
        if key is None:
            log.debug("unmapped remote button", extra={"code": f"0x{args[0]:02x}"})
            return
        self.key_pressed(key)

    def _on_play(self, source: int, args: bytes) -> None:
        if key := PLAY_MODES.get(args[0]):
            self.key_pressed(key)

    def _on_deck_control(self, source: int, args: bytes) -> None:
        if key := DECK_CONTROL_MODES.get(args[0]):
            self.key_pressed(key)

    def _on_feature_abort(self, source: int, args: bytes) -> None:
        opcode = cec.OPCODE_NAMES.get(args[0], f"0x{args[0]:02x}")
        log.debug("CEC feature abort", extra={"device": source, "opcode": opcode})

    # --- sending -----------------------------------------------------------------------

    async def _wait_claimed(self) -> None:
        try:
            async with asyncio.timeout(self.claim_timeout):
                await self._claimed.wait()
        except TimeoutError:
            raise BackendError("Can't reach the TV over HDMI-CEC") from None

    async def _announce(self) -> None:
        pa = self._phys_addr
        await self._transmit(cec.BROADCAST, cec.ACTIVE_SOURCE, pa >> 8, pa & 0xFF)

    def _send_announce(self) -> None:
        pa = self._phys_addr
        self._send_soon(cec.BROADCAST, cec.ACTIVE_SOURCE, pa >> 8, pa & 0xFF)

    async def _transmit(self, dest: int, opcode: int, *args: int) -> cec.Received:
        source = self._log_addr if self._log_addr is not None else cec.UNREGISTERED
        data = bytes([source << 4 | dest, opcode, *args])
        log.debug("cec tx", extra={"frame": cec.describe(data)})
        try:
            sequence = self.adapter.transmit(data)
        except OSError as e:
            if e.errno == errno.ENONET:
                raise BackendError("Can't reach the TV over HDMI-CEC") from None
            raise BackendError(f"HDMI-CEC send failed: {e.strerror}") from e
        future = asyncio.get_running_loop().create_future()
        self._pending[sequence] = future
        try:
            async with asyncio.timeout(self.tx_timeout):
                result = await future
        except TimeoutError:
            raise BackendError("The TV didn't answer") from None
        finally:
            self._pending.pop(sequence, None)
        if not result.tx_status & cec.TX_OK:
            if dest != cec.BROADCAST and result.tx_status & cec.TX_NACK:
                raise BackendError("The TV didn't answer")
            raise BackendError(f"HDMI-CEC send failed (status 0x{result.tx_status:02x})")
        return result

    def _send_soon(self, dest: int, opcode: int, *args: int) -> None:
        """Fire and forget, for replies. Failures are only logged."""

        async def send() -> None:
            try:
                await self._transmit(dest, opcode, *args)
            except BackendError as e:
                log.debug(
                    "CEC reply not delivered",
                    extra={"opcode": cec.OPCODE_NAMES.get(opcode, opcode), "error": str(e)},
                )
                if opcode == cec.GIVE_DEVICE_POWER_STATUS and dest == cec.TV:
                    self._set(power="unknown")

        self._spawn(send())

    async def _poll_power(self) -> None:
        # The TV doesn't always say when it turns on by its own remote, so ask now and then.
        while True:
            await asyncio.sleep(self.poll_interval)
            if self._claimed.is_set():
                self._send_soon(cec.TV, cec.GIVE_DEVICE_POWER_STATUS)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _set(self, *, power: TvPower | None = None, ours: bool | None = None) -> None:
        before = (self._power, self.active())
        if power is not None:
            self._power = power
        if ours is not None:
            self._ours = ours
        after = (self._power, self.active())
        if after != before:
            log.info("TV", extra={"power": after[0], "showing_couch": after[1]})
            self.changed()

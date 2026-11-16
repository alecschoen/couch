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
- Turns User Control Pressed / Released into key presses and releases (couchd.keys names),
  and <Play> / <Deck Control> into taps. Key repeat, hold-Back-for-Home and per-app routing
  are couchd/remote.py's.
- Feature Aborts any other message addressed to us, as the spec requires.

The Audio System (#11). While sound goes to the jack or Bluetooth (`set_system_audio(True)`,
from couchd/tv_volume.py), Couch is also the TV's Audio System, logical address 5:

- With an adapter that holds two addresses, it claims Playback and Audio System together.
  The Pi 4's vc4 holds one, so there the one address becomes the Audio System's for as
  long as sound is away from the TV, and Couch's Playback role (Active Source, remote keys)
  goes on from address 5. The TV forwards keys to the active source's address.
- While the TV shows Couch, System Audio Mode is on: <Set System Audio Mode> [On] is
  broadcast, the TV mutes its own speakers and sends its volume keys (User Control Pressed
  Volume Up / Down / Mute) to address 5. They come out of `on_volume_key`, and the answer
  goes back as <Report Audio Status> (`set_audio_status`). On another input, or with sound
  back on the TV, it's off again, so the TV's speakers and volume are its own.
- Answers <Give Audio Status>, <Give System Audio Mode Status> and <System Audio Mode
  Request> (on if we want it, Feature Abort [Refused] if not; off when asked).

`simulate(frame)` handles a frame as if it had come off the bus, so the TV's side of a
conversation can be tried on the Pi; the kernel won't let cec-ctl on the same adapter send
to our own address.

Every frame in and out is logged at debug level, for the TV compatibility notes (#9).
"""

import asyncio
import contextlib
import errno
import logging

from couchd import cec
from couchd.backends import BackendError, TvBackend, TvPower, VolumeKey

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
# User Control Pressed codes for volume. The TV sends them to the Audio System (address 5).
VOLUME_COMMANDS: dict[int, VolumeKey] = {
    0x41: "up",
    0x42: "down",
    0x43: "mute",  # a toggle
    0x65: "mute_on",  # Mute Function
    0x66: "mute_off",  # Restore Volume Function
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
        self._log_addr: int | None = None  # our main address: Playback, or 5 when that's all
        self._audio_addr: int | None = None  # 5 while we hold the Audio System's address
        self._reply_from: int | None = None  # the address the message being handled was for
        # The Audio System: wanted while sound is away from the TV; System Audio Mode is
        # what the TV was last told (on: its speakers are muted and volume keys come to us).
        self._audio_wanted = False
        self._system_audio = False
        self._audio_status: tuple[int, bool] = (0, False)
        self._reported: tuple[int, bool] | None = None
        self._roles: tuple[str, ...] = (cec.PLAYBACK,)
        self._reclaiming = False
        self._claiming = asyncio.Lock()
        self._volume_held = False
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
            cec.SET_SYSTEM_AUDIO_MODE: self._on_set_system_audio_mode,
            # Replies and announcements: nothing to do, but never Feature Abort them.
            cec.USER_CONTROL_RELEASED: self._on_user_control_released,
            cec.FEATURE_ABORT: self._on_feature_abort,
            cec.SET_OSD_NAME: None,
            cec.CEC_VERSION: None,
            cec.REPORT_PHYSICAL_ADDR: None,
            cec.DEVICE_VENDOR_ID: None,
            cec.MENU_STATUS: None,
            cec.INACTIVE_SOURCE: None,
            cec.REPORT_AUDIO_STATUS: None,
            cec.SYSTEM_AUDIO_MODE_STATUS: None,
        }
        # Only for the Audio System's address; to any other they're Feature Aborted.
        self._audio_handlers = {
            cec.GIVE_AUDIO_STATUS: self._on_give_audio_status,
            cec.GIVE_SYSTEM_AUDIO_MODE_STATUS: self._on_give_system_audio_mode_status,
            cec.SYSTEM_AUDIO_MODE_REQUEST: self._on_system_audio_mode_request,
        }

    # --- TvBackend ----------------------------------------------------------------------

    def power(self) -> TvPower:
        return self._power

    def active(self) -> bool:
        return self._ours and self._power == "on"

    def system_audio(self) -> bool:
        return self._system_audio

    async def set_system_audio(self, on: bool) -> None:
        if on == self._audio_wanted:
            return
        self._audio_wanted = on
        log.info("TV audio system", extra={"wanted": on})
        if not on:
            await self._update_system_audio()  # tell the TV first, while we still have 5
        roles = self._roles_for(on)
        if roles != self._roles:
            await self._reclaim(roles)
        await self._update_system_audio()

    def set_audio_status(self, volume: int, muted: bool, *, report: bool = False) -> None:
        self._audio_status = (max(0, min(100, volume)), muted)
        if self._system_audio and (report or self._audio_status != self._reported):
            self._send_audio_status(cec.TV)

    async def simulate(self, frame: bytes) -> None:
        if len(frame) < 1:
            raise BackendError("an empty frame")
        log.info("simulated cec rx", extra={"frame": cec.describe(frame)})
        self._on_received(cec.Received(frame))

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.adapter.open()
        try:
            self.adapter.claim(self.osd_name, self.vendor_id, self._roles)
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
        async with self._claiming:  # not while our address is changing under us
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
        mask = event.log_addr_mask
        if mask:
            addrs = [a for a in range(16) if mask & (1 << a)]
            has_5 = cec.AUDIO_SYSTEM in addrs
            self._audio_addr = cec.AUDIO_SYSTEM if cec.AUDIO in self._roles and has_5 else None
            others = [a for a in addrs if a != self._audio_addr]
            self._log_addr = others[0] if others else addrs[0]
            if cec.AUDIO in self._roles and not has_5:
                # Another Audio System (a soundbar) has 5. It's the TV's, not ours.
                log.warning("CEC address 5 is taken; Couch can't be the TV's Audio System")
                self._roles = (cec.PLAYBACK,)
                self._spawn(self._reclaim_quietly(self._roles))
            if not self._claimed.is_set():
                log.info(
                    "claimed CEC address",
                    extra={
                        "logical": ",".join(str(a) for a in addrs),
                        "physical": cec.format_phys_addr(event.phys_addr),
                    },
                )
            self._claimed.set()
            self._reclaiming = False
            if self._system_audio:  # whatever the TV thought, we'll tell it again
                self._system_audio = False
                self.changed()
            self._send_soon(cec.TV, cec.GIVE_DEVICE_POWER_STATUS)
            self._spawn(self._update_system_audio())
        else:
            if self._claimed.is_set():
                log.info(
                    "lost CEC address",
                    extra={"physical": cec.format_phys_addr(event.phys_addr)},
                )
            self._log_addr = self._audio_addr = None
            self._claimed.clear()
            if self._system_audio:
                self._system_audio = False
                self.changed()
            if self._reclaiming:
                return  # our own doing: the new address follows at once
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
        self._reply_from = dest if dest != cec.BROADCAST else None
        if opcode in self._audio_handlers and dest == self._audio_addr:
            handler = self._audio_handlers[opcode]
        elif opcode in self._handlers:
            handler = self._handlers[opcode]
        else:
            if dest != cec.BROADCAST:
                self._send_soon(source, cec.FEATURE_ABORT, opcode, cec.ABORT_UNRECOGNIZED_OP)
            return
        if handler is None:
            return
        try:
            handler(source, args)
        except IndexError:
            log.debug("short CEC message ignored", extra={"frame": cec.describe(data)})
        finally:
            self._reply_from = None

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
        audio = self._reply_from is not None and self._reply_from == self._audio_addr
        devtype = cec.PRIM_DEVTYPE_AUDIOSYSTEM if audio else cec.PRIM_DEVTYPE_PLAYBACK
        self._send_soon(cec.BROADCAST, cec.REPORT_PHYSICAL_ADDR, pa >> 8, pa & 0xFF, devtype)

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
        if volume := VOLUME_COMMANDS.get(args[0]):
            # In System Audio Mode these come whatever input the TV shows.
            log.debug("remote volume button", extra={"code": f"0x{args[0]:02x}", "key": volume})
            self._let_go_of_keys()
            self._volume_held = True
            self.volume_key_pressed(volume)
            return
        # The TV only forwards other keys to the input it's showing.
        self._set(power="on", ours=True)
        key = UI_COMMANDS.get(args[0])
        if key is None:
            log.debug("unmapped remote button", extra={"code": f"0x{args[0]:02x}"})
            # A new button down means the last one is up, whether or not we know this one.
            self._let_go_of_keys()
            return
        log.debug("remote button", extra={"code": f"0x{args[0]:02x}", "key": key})
        if self._volume_held:
            self._volume_held = False
            self.volume_key_released()
        self.key_pressed(key)

    def _on_user_control_released(self, source: int, args: bytes) -> None:
        self._let_go_of_keys()

    def _let_go_of_keys(self) -> None:
        if self._volume_held:
            self._volume_held = False
            self.volume_key_released()
        else:
            self.key_released()

    # --- the Audio System -------------------------------------------------------------------

    def _roles_for(self, audio: bool) -> tuple[str, ...]:
        if not audio:
            return (cec.PLAYBACK,)
        if self.adapter.max_log_addrs() >= 2:
            return (cec.PLAYBACK, cec.AUDIO)
        return (cec.AUDIO,)

    async def _reclaim(self, roles: tuple[str, ...]) -> None:
        async with self._claiming:
            log.info("claiming CEC addresses", extra={"roles": ",".join(roles)})
            self._roles = roles
            self._reclaiming = True
            self._claimed.clear()
            try:
                self.adapter.claim(self.osd_name, self.vendor_id, roles)
                await self._wait_claimed()
            except OSError as e:
                raise BackendError(f"Couldn't claim a CEC address: {e.strerror}") from e
            finally:
                self._reclaiming = False
            if self._ours:
                # Keys follow the active source's logical address, which may have changed.
                await self._announce()

    async def _reclaim_quietly(self, roles: tuple[str, ...]) -> None:
        try:
            await self._reclaim(roles)
        except BackendError as e:
            log.warning("couldn't claim a CEC address", extra={"error": str(e)})

    def _system_audio_wanted(self) -> bool:
        return self._audio_wanted and self._audio_addr is not None and self.active()

    async def _update_system_audio(self) -> None:
        on = self._system_audio_wanted()
        if on == self._system_audio or self._audio_addr is None:
            return
        self._system_audio = on
        log.info("System Audio Mode", extra={"on": on})
        self.changed()
        try:
            await self._transmit(
                cec.BROADCAST, cec.SET_SYSTEM_AUDIO_MODE, int(on), source=self._audio_addr
            )
            if on:
                self._send_audio_status(cec.TV)
        except BackendError as e:
            log.warning("couldn't set System Audio Mode", extra={"on": on, "error": str(e)})

    def _send_audio_status(self, dest: int) -> None:
        volume, muted = self._audio_status
        self._reported = self._audio_status
        status = (0x80 if muted else 0) | volume
        self._send_soon(dest, cec.REPORT_AUDIO_STATUS, status, source=self._audio_addr)

    def _on_give_audio_status(self, source: int, args: bytes) -> None:
        self._send_audio_status(source)

    def _on_give_system_audio_mode_status(self, source: int, args: bytes) -> None:
        self._send_soon(source, cec.SYSTEM_AUDIO_MODE_STATUS, int(self._system_audio))

    def _on_system_audio_mode_request(self, source: int, args: bytes) -> None:
        if len(args) < 2:  # no operand: the TV wants its own speakers back
            self._spawn(self._tv_takes_audio())
            return
        if not self._system_audio_wanted():
            self._send_soon(
                source, cec.FEATURE_ABORT, cec.SYSTEM_AUDIO_MODE_REQUEST, cec.ABORT_REFUSED
            )
            return
        self._system_audio = False  # say it again, even if we had
        self._spawn(self._update_system_audio())

    async def _tv_takes_audio(self) -> None:
        if self._system_audio:
            self._system_audio = False
            self.changed()
        try:
            await self._transmit(
                cec.BROADCAST, cec.SET_SYSTEM_AUDIO_MODE, 0, source=self._audio_addr
            )
        except BackendError as e:
            log.debug("couldn't turn System Audio Mode off", extra={"error": str(e)})

    def _on_set_system_audio_mode(self, source: int, args: bytes) -> None:
        # Another Audio System, or the TV, saying what the mode is. If it's off, the TV is
        # back on its own speakers: stop thinking we have the volume keys.
        if source != self._audio_addr and not args[0] and self._system_audio:
            self._system_audio = False
            self.changed()

    def _on_play(self, source: int, args: bytes) -> None:
        if key := PLAY_MODES.get(args[0]):
            self.key_tapped(key)

    def _on_deck_control(self, source: int, args: bytes) -> None:
        if key := DECK_CONTROL_MODES.get(args[0]):
            self.key_tapped(key)

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

    async def _transmit(
        self, dest: int, opcode: int, *args: int, source: int | None = None
    ) -> cec.Received:
        if source is None or source not in (self._log_addr, self._audio_addr):
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

    def _send_soon(self, dest: int, opcode: int, *args: int, source: int | None = None) -> None:
        """Fire and forget, for replies. Failures are only logged. A reply goes out from the
        address the message being handled was for."""
        if source is None:
            source = self._reply_from

        async def send() -> None:
            try:
                await self._transmit(dest, opcode, *args, source=source)
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
            if self._audio_addr is not None and self._claimed.is_set():
                self._spawn(self._update_system_audio())

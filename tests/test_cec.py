"""The CEC backend against a fake adapter, plus the kernel struct layouts."""

import asyncio
import ctypes
import errno
import os
from collections import deque

import pytest

from couchd import cec
from couchd.backends import BackendError
from couchd.bus import EventBus
from couchd.cec_tv import CecTv, routes_to
from couchd.commands import Commands
from couchd.services import Context
from couchd.state import LiveState, SavedState
from couchd.systemd import jobs_power_off
from couchd.tv import STANDBY_ON_POWEROFF, TvService

US = 0x4  # Playback 1
PA = 0x1000  # 1.0.0.0, TV input 1


class FakeAdapter:
    """A CEC adapter on a bus with a TV at 0. Records what we send; acks unless told not to."""

    def __init__(self) -> None:
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self._events: deque[cec.Event] = deque()
        self._rx: deque[cec.Received] = deque()
        self._seq = 0
        self._la: int | None = None
        self.sent: list[bytes] = []
        self.claimed: tuple[str, int] | None = None
        self.nack = False
        self.opened = self.closed = False

    # test controls
    def connect(self, phys_addr: int = PA, log_addr: int = US) -> None:
        self._la = log_addr
        self._push_event(cec.Event(cec.EVENT_STATE_CHANGE, phys_addr, 1 << log_addr))

    def disconnect(self) -> None:
        self._la = None
        self._push_event(cec.Event(cec.EVENT_STATE_CHANGE, cec.NO_PHYS_ADDR, 0))

    def from_tv(self, dest: int, *payload: int) -> None:
        self._push_rx(cec.Received(bytes([cec.TV << 4 | dest, *payload])))

    def clear(self) -> None:
        self.sent.clear()

    # Adapter
    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True
        os.close(self._r)
        os.close(self._w)

    def fileno(self) -> int:
        return self._r

    def claim(self, osd_name: str, vendor_id: int) -> None:
        self.claimed = (osd_name, vendor_id)

    def phys_addr(self) -> int:
        return PA

    def log_addr(self) -> int | None:
        return self._la

    def transmit(self, data: bytes) -> int:
        if self._la is None:
            raise OSError(errno.ENONET, "no logical address")
        self._seq += 1
        self.sent.append(data)
        broadcast = data[0] & 0xF == cec.BROADCAST
        status = cec.TX_NACK if self.nack and not broadcast else cec.TX_OK
        self._push_rx(cec.Received(data, self._seq, tx_status=status))
        return self._seq

    def receive(self) -> cec.Received | None:
        if self._rx:
            return self._rx.popleft()
        self._settle()
        return None

    def event(self) -> cec.Event | None:
        if self._events:
            return self._events.popleft()
        self._settle()
        return None

    def _push_rx(self, received: cec.Received) -> None:
        self._rx.append(received)
        os.write(self._w, b"x")

    def _push_event(self, event: cec.Event) -> None:
        self._events.append(event)
        os.write(self._w, b"x")

    def _settle(self) -> None:
        if not self._rx and not self._events:
            try:
                while os.read(self._r, 64):
                    pass
            except BlockingIOError:
                pass


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
async def adapter():
    return FakeAdapter()


@pytest.fixture
async def tv(adapter):
    backend = CecTv(adapter, claim_timeout=0.05, tx_timeout=0.5, poll_interval=3600)
    await backend.start()
    adapter.connect()
    await settle()
    adapter.clear()
    yield backend
    await backend.stop()


def frame(source: int, dest: int, *payload: int) -> bytes:
    return bytes([source << 4 | dest, *payload])


# --- claiming --------------------------------------------------------------------------------


async def test_claims_a_playback_address_named_couch(adapter):
    backend = CecTv(adapter, poll_interval=3600)
    await backend.start()
    assert adapter.opened
    assert adapter.claimed == ("Couch", cec.VENDOR_BROADCOM)
    adapter.connect()
    await settle()
    # First thing once we have an address: ask whether the TV is on.
    assert adapter.sent == [frame(US, cec.TV, cec.GIVE_DEVICE_POWER_STATUS)]
    await backend.stop()
    assert adapter.closed


async def test_power_on_before_an_address_is_claimed_fails(adapter):
    backend = CecTv(adapter, claim_timeout=0.01, poll_interval=3600)
    await backend.start()
    with pytest.raises(BackendError, match="Can't reach the TV"):
        await backend.power_on()
    await backend.stop()


async def test_losing_the_address_makes_power_unknown(tv, adapter):
    adapter.from_tv(cec.BROADCAST, cec.REQUEST_ACTIVE_SOURCE)
    await settle()
    assert tv.power() == "on"
    adapter.disconnect()
    await settle()
    assert tv.power() == "unknown"
    with pytest.raises(BackendError):
        await tv.power_on()


# --- waking and standby ----------------------------------------------------------------------


async def test_power_on_wakes_the_tv_and_switches_to_us(tv, adapter):
    changes = []
    tv.on_change(lambda: changes.append(tv.power()))
    await tv.power_on()
    assert adapter.sent == [
        frame(US, cec.TV, cec.IMAGE_VIEW_ON),
        frame(US, cec.BROADCAST, cec.ACTIVE_SOURCE, 0x10, 0x00),
    ]
    assert tv.power() == "on"
    assert tv.active()
    assert changes == ["on"]


async def test_power_on_reports_a_tv_that_doesnt_answer(tv, adapter):
    adapter.nack = True
    with pytest.raises(BackendError, match="didn't answer"):
        await tv.power_on()


async def test_standby_command_goes_to_the_tv(tv, adapter):
    await tv.power_on()
    adapter.clear()
    await tv.standby()
    assert adapter.sent == [frame(US, cec.TV, cec.STANDBY)]
    assert tv.power() == "standby"
    assert not tv.active()


async def test_standby_from_the_tv(tv, adapter):
    await tv.power_on()
    adapter.from_tv(cec.BROADCAST, cec.STANDBY)
    await settle()
    assert tv.power() == "standby"
    assert not tv.active()


@pytest.mark.parametrize(
    ("status", "power"),
    [(0, "on"), (1, "standby"), (2, "on"), (3, "standby"), (9, "unknown")],
)
async def test_report_power_status(tv, adapter, status, power):
    adapter.from_tv(US, cec.REPORT_POWER_STATUS, status)
    await settle()
    assert tv.power() == power


async def test_unanswered_power_query_makes_power_unknown(tv, adapter):
    adapter.from_tv(US, cec.REPORT_POWER_STATUS, cec.POWER_ON)
    await settle()
    adapter.nack = True
    tv._send_soon(cec.TV, cec.GIVE_DEVICE_POWER_STATUS)
    await settle()
    assert tv.power() == "unknown"


# --- answering the TV ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "reply"),
    [
        ((cec.GIVE_DEVICE_POWER_STATUS,), frame(US, cec.TV, cec.REPORT_POWER_STATUS, 0)),
        ((cec.GIVE_OSD_NAME,), frame(US, cec.TV, cec.SET_OSD_NAME, *b"Couch")),
        (
            (cec.GIVE_PHYSICAL_ADDR,),
            frame(US, cec.BROADCAST, cec.REPORT_PHYSICAL_ADDR, 0x10, 0x00, 4),
        ),
        (
            (cec.GIVE_DEVICE_VENDOR_ID,),
            frame(US, cec.BROADCAST, cec.DEVICE_VENDOR_ID, 0x18, 0xC0, 0x86),
        ),
        ((cec.GET_CEC_VERSION,), frame(US, cec.TV, cec.CEC_VERSION, cec.VERSION_1_4)),
        ((cec.MENU_REQUEST, cec.MENU_QUERY), frame(US, cec.TV, cec.MENU_STATUS, 0)),
        ((cec.MENU_REQUEST, cec.MENU_DEACTIVATE), frame(US, cec.TV, cec.MENU_STATUS, 1)),
        ((0x1A, 1), frame(US, cec.TV, cec.FEATURE_ABORT, 0x1A, 0)),  # Give Deck Status
    ],
)
async def test_answers_the_tv(tv, adapter, query, reply):
    adapter.from_tv(US, *query)
    await settle()
    assert adapter.sent == [reply]


async def test_unknown_broadcasts_and_replies_are_not_aborted(tv, adapter):
    adapter.from_tv(cec.BROADCAST, 0x72, 1)  # Set System Audio Mode
    adapter.from_tv(US, cec.FEATURE_ABORT, cec.GIVE_OSD_NAME, 0)
    adapter.from_tv(US, cec.USER_CONTROL_RELEASED)
    adapter.from_tv(US)  # a poll
    adapter.from_tv(US, cec.REPORT_POWER_STATUS)  # too short
    await settle()
    assert adapter.sent == []


# --- which input the TV shows ----------------------------------------------------------------


async def test_set_stream_path_to_us_makes_us_active_source(tv, adapter):
    adapter.from_tv(cec.BROADCAST, cec.SET_STREAM_PATH, 0x10, 0x00)
    await settle()
    assert adapter.sent == [frame(US, cec.BROADCAST, cec.ACTIVE_SOURCE, 0x10, 0x00)]
    assert tv.active()
    assert tv.power() == "on"


async def test_switching_input_away_and_back(tv, adapter):
    await tv.power_on()
    adapter.from_tv(cec.BROADCAST, cec.ROUTING_CHANGE, 0x10, 0x00, 0x20, 0x00)
    await settle()
    assert not tv.active()
    adapter.clear()
    adapter.from_tv(cec.BROADCAST, cec.ROUTING_CHANGE, 0x20, 0x00, 0x10, 0x00)
    await settle()
    assert tv.active()
    assert adapter.sent == [frame(US, cec.BROADCAST, cec.ACTIVE_SOURCE, 0x10, 0x00)]


async def test_another_active_source_takes_over(tv, adapter):
    await tv.power_on()
    adapter._push_rx(cec.Received(frame(0x8, cec.BROADCAST, cec.ACTIVE_SOURCE, 0x20, 0x00)))
    await settle()
    assert not tv.active()
    # ...and when the TV later asks who's active, it isn't us any more.
    adapter.clear()
    adapter.from_tv(cec.BROADCAST, cec.REQUEST_ACTIVE_SOURCE)
    await settle()
    assert adapter.sent == []


async def test_tv_turned_on_by_its_own_remote_comes_back_to_us(tv, adapter):
    await tv.power_on()
    adapter.from_tv(cec.BROADCAST, cec.STANDBY)
    await settle()
    adapter.clear()
    adapter.from_tv(cec.BROADCAST, cec.REQUEST_ACTIVE_SOURCE)
    await settle()
    assert adapter.sent == [frame(US, cec.BROADCAST, cec.ACTIVE_SOURCE, 0x10, 0x00)]
    assert tv.power() == "on"
    assert tv.active()


async def test_tv_back_on_our_input_after_a_hotplug_blip(tv, adapter):
    # What the KDL-40V55 does when turned on with its own remote: hotplug drops for under a
    # second, then it reports power on and shows the last input. It never asks who's active.
    await tv.power_on()
    adapter.from_tv(cec.BROADCAST, cec.STANDBY)
    await settle()
    adapter.disconnect()
    await settle()
    assert tv.power() == "unknown"
    assert not tv.active()
    adapter.connect()
    await settle()
    adapter.from_tv(US, cec.REPORT_POWER_STATUS, cec.POWER_ON)
    await settle()
    assert tv.power() == "on"
    assert tv.active()


@pytest.mark.parametrize(
    ("path", "ours", "expected"),
    [
        (0x1000, 0x1000, True),
        (0x1000, 0x1200, True),  # through a switch on input 1
        (0x1100, 0x1200, False),
        (0x2000, 0x1000, False),
        (0x0000, 0x1000, False),  # the TV itself
        (0x1000, 0xFFFF, False),
        (0x1200, 0x1000, False),
    ],
)
def test_routes_to(path, ours, expected):
    assert routes_to(path, ours) is expected


# --- remote buttons --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "key"),
    [
        ((cec.USER_CONTROL_PRESSED, 0x00), "select"),
        ((cec.USER_CONTROL_PRESSED, 0x01), "up"),
        ((cec.USER_CONTROL_PRESSED, 0x0D), "back"),
        ((cec.USER_CONTROL_PRESSED, 0x25), "5"),
        ((cec.USER_CONTROL_PRESSED, 0x46), "pause"),
        ((cec.USER_CONTROL_PRESSED, 0x72), "red"),
        ((cec.PLAY, 0x24), "play"),
        ((cec.DECK_CONTROL, 3), "stop"),
    ],
)
async def test_remote_buttons_become_keys(tv, adapter, payload, key):
    keys = []
    tv.on_key(keys.append)
    adapter.from_tv(US, *payload)
    await settle()
    assert keys == [key]
    assert adapter.sent == []


async def test_unmapped_button_is_ignored_but_marks_us_active(tv, adapter):
    keys = []
    tv.on_key(keys.append)
    adapter.from_tv(US, cec.USER_CONTROL_PRESSED, 0x6B)  # Power Toggle
    await settle()
    assert keys == []
    assert tv.active()


# --- the service on top ----------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


async def _service(ctx, adapter, *, powering_off=False):
    async def fake_powering_off():
        return powering_off

    backend = CecTv(adapter, claim_timeout=0.5, tx_timeout=0.5, poll_interval=3600)
    service = TvService(ctx, backend, powering_off=fake_powering_off)
    adapter.connect()
    await service.start()
    await settle()
    return service


async def test_service_wakes_the_tv_on_start(ctx, adapter):
    service = await _service(ctx, adapter)
    assert frame(US, cec.TV, cec.IMAGE_VIEW_ON) in adapter.sent
    assert ctx.state.get("tv") == {"power": "on", "active": True, "standby_on_poweroff": False}
    await service.stop()


async def test_tv_standby_presses_pause(ctx, adapter):
    service = await _service(ctx, adapter)
    keys = []
    service.backend.on_key(keys.append)
    adapter.from_tv(cec.BROADCAST, cec.STANDBY)
    await settle()
    assert keys == ["pause"]
    assert ctx.state.get("tv")["power"] == "standby"
    await service.stop()


@pytest.mark.parametrize(
    ("setting", "powering_off", "sends_standby"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_standby_on_poweroff(ctx, adapter, setting, powering_off, sends_standby):
    service = await _service(ctx, adapter, powering_off=powering_off)
    await ctx.commands.run("tv.set_standby_on_poweroff", {"on": setting})
    assert ctx.state.get("tv")["standby_on_poweroff"] is setting
    assert ctx.settings.get(STANDBY_ON_POWEROFF) is setting
    adapter.clear()
    await service.stop()
    assert (frame(US, cec.TV, cec.STANDBY) in adapter.sent) is sends_standby


def test_jobs_power_off():
    assert jobs_power_off("1 poweroff.target start waiting\n2 couchd.service stop running\n")
    assert jobs_power_off("7 halt.target start waiting")
    assert not jobs_power_off("3 reboot.target start waiting\n4 couchd.service stop running")
    assert not jobs_power_off("")


# --- kernel ABI ------------------------------------------------------------------------------


def test_struct_layouts_match_linux_cec_h():
    # Sizes and ioctl numbers from a C program built against linux/cec.h (x86_64 and arm64).
    assert ctypes.sizeof(cec._Caps) == 76
    assert ctypes.sizeof(cec._LogAddrs) == 92
    assert ctypes.sizeof(cec._Msg) == 56
    assert ctypes.sizeof(cec._Event) == 80
    assert cec._LogAddrs.osd_name.offset == 16
    assert cec._LogAddrs.features.offset == 43
    assert cec._Msg.msg.offset == 32
    assert cec._Msg.tx_status.offset == 50
    assert cec.ADAP_G_CAPS == 0xC04C6100
    assert cec.ADAP_G_PHYS_ADDR == 0x80026101
    assert cec.ADAP_G_LOG_ADDRS == 0x805C6103
    assert cec.ADAP_S_LOG_ADDRS == 0xC05C6104
    assert cec.TRANSMIT == 0xC0386105
    assert cec.RECEIVE == 0xC0386106
    assert cec.DQEVENT == 0xC0506107
    assert cec.S_MODE == 0x40046109


def test_describe():
    assert cec.describe(bytes([0x40, 0x04])) == "4->0 image_view_on"
    assert cec.describe(bytes([0x4F, 0x82, 0x10, 0x00])) == "4->f active_source 10:00"
    assert cec.describe(bytes([0x04])) == "0->4 poll"
    assert cec.describe(bytes([0x04, 0xFE])) == "0->4 0xfe"
    assert cec.format_phys_addr(0x1000) == "1.0.0.0"
    assert cec.format_phys_addr(0xFFFF) == "f.f.f.f"


def test_missing_device_fails_to_open(tmp_path):
    with pytest.raises(OSError):
        cec.KernelAdapter(str(tmp_path / "cec9")).open()

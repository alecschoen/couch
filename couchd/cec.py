"""The kernel HDMI-CEC API (`/dev/cecN`, linux/cec.h), spoken directly through ioctls.

No libcec: the kernel already does the bus timing, logical address claiming and the simple
replies (Give OSD Name, Give Physical Address, Give Device Vendor ID, Get CEC Version) once
we tell it who we are. This module is only the thin, synchronous device layer. The protocol
logic lives in `couchd.cec_tv`, which talks to an `Adapter` so tests can swap in a fake.

The file descriptor is non-blocking. A transmit returns a sequence number at once; its result
(acked or not) comes back later through `receive()` with that sequence number set.
"""

import ctypes
import errno
import fcntl
import os
import select
from dataclasses import dataclass
from typing import Protocol

# --- addresses and opcodes (CEC 1.4) -------------------------------------------------------

TV = 0x0
BROADCAST = 0xF
UNREGISTERED = 0xF
NO_PHYS_ADDR = 0xFFFF

FEATURE_ABORT = 0x00
IMAGE_VIEW_ON = 0x04
STANDBY = 0x36
PLAY = 0x41
DECK_CONTROL = 0x42
USER_CONTROL_PRESSED = 0x44
USER_CONTROL_RELEASED = 0x45
GIVE_OSD_NAME = 0x46
SET_OSD_NAME = 0x47
ROUTING_CHANGE = 0x80
ROUTING_INFORMATION = 0x81
ACTIVE_SOURCE = 0x82
GIVE_PHYSICAL_ADDR = 0x83
REPORT_PHYSICAL_ADDR = 0x84
REQUEST_ACTIVE_SOURCE = 0x85
SET_STREAM_PATH = 0x86
DEVICE_VENDOR_ID = 0x87
VENDOR_COMMAND = 0x89
GIVE_DEVICE_VENDOR_ID = 0x8C
MENU_REQUEST = 0x8D
MENU_STATUS = 0x8E
GIVE_DEVICE_POWER_STATUS = 0x8F
REPORT_POWER_STATUS = 0x90
INACTIVE_SOURCE = 0x9D
CEC_VERSION = 0x9E
GET_CEC_VERSION = 0x9F

ABORT_UNRECOGNIZED_OP = 0
POWER_ON, POWER_STANDBY, POWER_TO_ON, POWER_TO_STANDBY = 0, 1, 2, 3
MENU_ACTIVATE, MENU_DEACTIVATE, MENU_QUERY = 0, 1, 2
MENU_ACTIVATED, MENU_DEACTIVATED = 0, 1
VERSION_1_4 = 5
PRIM_DEVTYPE_PLAYBACK = 4
LOG_ADDR_TYPE_PLAYBACK = 3
ALL_DEVTYPE_PLAYBACK = 0x10

# Broadcom, the Pi's SoC maker; what libcec on a Pi reports too.
VENDOR_BROADCOM = 0x18C086

OPCODE_NAMES = {
    v: n
    for n, v in {
        "feature_abort": FEATURE_ABORT,
        "image_view_on": IMAGE_VIEW_ON,
        "standby": STANDBY,
        "play": PLAY,
        "deck_control": DECK_CONTROL,
        "user_control_pressed": USER_CONTROL_PRESSED,
        "user_control_released": USER_CONTROL_RELEASED,
        "give_osd_name": GIVE_OSD_NAME,
        "set_osd_name": SET_OSD_NAME,
        "routing_change": ROUTING_CHANGE,
        "routing_information": ROUTING_INFORMATION,
        "active_source": ACTIVE_SOURCE,
        "give_physical_addr": GIVE_PHYSICAL_ADDR,
        "report_physical_addr": REPORT_PHYSICAL_ADDR,
        "request_active_source": REQUEST_ACTIVE_SOURCE,
        "set_stream_path": SET_STREAM_PATH,
        "device_vendor_id": DEVICE_VENDOR_ID,
        "vendor_command": VENDOR_COMMAND,
        "give_device_vendor_id": GIVE_DEVICE_VENDOR_ID,
        "menu_request": MENU_REQUEST,
        "menu_status": MENU_STATUS,
        "give_device_power_status": GIVE_DEVICE_POWER_STATUS,
        "report_power_status": REPORT_POWER_STATUS,
        "inactive_source": INACTIVE_SOURCE,
        "cec_version": CEC_VERSION,
        "get_cec_version": GET_CEC_VERSION,
    }.items()
}


def describe(data: bytes) -> str:
    """'4->0 image_view_on' style, for logs."""
    if not data:
        return "<empty>"
    head = f"{data[0] >> 4:x}->{data[0] & 0xF:x}"
    if len(data) == 1:
        return f"{head} poll"
    name = OPCODE_NAMES.get(data[1], f"0x{data[1]:02x}")
    args = data[2:].hex(":")
    return f"{head} {name} {args}".rstrip()


def format_phys_addr(pa: int) -> str:
    if pa == NO_PHYS_ADDR:
        return "f.f.f.f"
    return ".".join(f"{(pa >> shift) & 0xF:x}" for shift in (12, 8, 4, 0))


# --- what the backend needs from a device --------------------------------------------------

TX_OK = 1 << 0
TX_NACK = 1 << 2

EVENT_STATE_CHANGE = 1
EVENT_LOST_MSGS = 2


@dataclass(frozen=True)
class Received:
    data: bytes
    # Non-zero for the result of one of our transmits; zero for a message from another device.
    sequence: int = 0
    tx_status: int = 0
    rx_status: int = 0


@dataclass(frozen=True)
class Event:
    kind: int
    phys_addr: int = NO_PHYS_ADDR
    log_addr_mask: int = 0
    lost_msgs: int = 0


class Adapter(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def fileno(self) -> int:
        """Readable whenever receive() or event() may have something."""
        ...

    def claim(self, osd_name: str, vendor_id: int) -> None:
        """Ask for a Playback logical address. Done when a state-change event says so."""
        ...

    def phys_addr(self) -> int: ...

    def log_addr(self) -> int | None:
        """Our logical address, or None while we have none."""
        ...

    def transmit(self, data: bytes) -> int:
        """Queue a message; returns its sequence number. Raises OSError (ENONET: no address)."""
        ...

    def receive(self) -> Received | None: ...

    def event(self) -> Event | None: ...


# --- linux/cec.h -------------------------------------------------------------------------

MAX_MSG_SIZE = 16
MAX_LOG_ADDRS = 4

CAP_LOG_ADDRS = 1 << 2
CAP_TRANSMIT = 1 << 3

MODE_INITIATOR = 1 << 0
MODE_EXCL_FOLLOWER = 2 << 4

LOG_ADDRS_FL_ALLOW_UNREG_FALLBACK = 1 << 0


class _Caps(ctypes.Structure):
    _fields_ = (
        ("driver", ctypes.c_char * 32),
        ("name", ctypes.c_char * 32),
        ("available_log_addrs", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
    )


class _LogAddrs(ctypes.Structure):
    _fields_ = (
        ("log_addr", ctypes.c_uint8 * MAX_LOG_ADDRS),
        ("log_addr_mask", ctypes.c_uint16),
        ("cec_version", ctypes.c_uint8),
        ("num_log_addrs", ctypes.c_uint8),
        ("vendor_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("osd_name", ctypes.c_char * 15),
        ("primary_device_type", ctypes.c_uint8 * MAX_LOG_ADDRS),
        ("log_addr_type", ctypes.c_uint8 * MAX_LOG_ADDRS),
        ("all_device_types", ctypes.c_uint8 * MAX_LOG_ADDRS),
        ("features", (ctypes.c_uint8 * 12) * MAX_LOG_ADDRS),
    )


class _Msg(ctypes.Structure):
    _fields_ = (
        ("tx_ts", ctypes.c_uint64),
        ("rx_ts", ctypes.c_uint64),
        ("len", ctypes.c_uint32),
        ("timeout", ctypes.c_uint32),
        ("sequence", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("msg", ctypes.c_uint8 * MAX_MSG_SIZE),
        ("reply", ctypes.c_uint8),
        ("rx_status", ctypes.c_uint8),
        ("tx_status", ctypes.c_uint8),
        ("tx_arb_lost_cnt", ctypes.c_uint8),
        ("tx_nack_cnt", ctypes.c_uint8),
        ("tx_low_drive_cnt", ctypes.c_uint8),
        ("tx_error_cnt", ctypes.c_uint8),
    )


class _Event(ctypes.Structure):
    _fields_ = (
        ("ts", ctypes.c_uint64),
        ("event", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        # union { state_change {u16 phys_addr, log_addr_mask, have_conn_info}; lost_msgs; raw }
        ("raw", ctypes.c_uint32 * 16),
    )


def _ioc(direction: int, nr: int, size: int) -> int:
    # asm-generic/ioctl.h, which arm64 and x86_64 both use: dir:2 size:14 type:8 nr:8
    return (direction << 30) | (size << 16) | (ord("a") << 8) | nr


_W, _R = 1, 2
ADAP_G_CAPS = _ioc(_R | _W, 0, ctypes.sizeof(_Caps))
ADAP_G_PHYS_ADDR = _ioc(_R, 1, 2)
ADAP_G_LOG_ADDRS = _ioc(_R, 3, ctypes.sizeof(_LogAddrs))
ADAP_S_LOG_ADDRS = _ioc(_R | _W, 4, ctypes.sizeof(_LogAddrs))
TRANSMIT = _ioc(_R | _W, 5, ctypes.sizeof(_Msg))
RECEIVE = _ioc(_R | _W, 6, ctypes.sizeof(_Msg))
DQEVENT = _ioc(_R | _W, 7, ctypes.sizeof(_Event))
S_MODE = _ioc(_W, 9, 4)


class KernelAdapter:
    """/dev/cecN through ioctls. Needs read/write on the node (group `video` on the Pi)."""

    def __init__(self, path: str = "/dev/cec0") -> None:
        self.path = path
        self._fd: int | None = None
        self._epoll: select.epoll | None = None

    def open(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            caps = _Caps()
            fcntl.ioctl(fd, ADAP_G_CAPS, caps)
            needed = CAP_LOG_ADDRS | CAP_TRANSMIT
            if caps.capabilities & needed != needed:
                raise OSError(errno.ENOTSUP, f"{self.path} can't claim addresses and transmit")
            # Exclusive follower: every message not answered by the kernel comes to us, and
            # the kernel stops Feature Aborting them on our behalf.
            fcntl.ioctl(fd, S_MODE, ctypes.c_uint32(MODE_INITIATOR | MODE_EXCL_FOLLOWER))
            # Messages are POLLIN, events POLLPRI. asyncio only watches for readable, so an
            # epoll fd watching both stands in for the device.
            epoll = select.epoll()
            epoll.register(fd, select.EPOLLIN | select.EPOLLPRI)
        except BaseException:
            os.close(fd)
            raise
        self._fd, self._epoll = fd, epoll

    def close(self) -> None:
        if self._epoll is not None:
            self._epoll.close()
            self._epoll = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def fileno(self) -> int:
        assert self._epoll is not None
        return self._epoll.fileno()

    def claim(self, osd_name: str, vendor_id: int) -> None:
        # Addresses outlive the fd, so a restart finds the old ones still set; the kernel
        # refuses a new set (EBUSY) until they're cleared.
        fcntl.ioctl(self._dev(), ADAP_S_LOG_ADDRS, _LogAddrs())
        la = _LogAddrs()
        la.cec_version = VERSION_1_4
        la.num_log_addrs = 1
        la.vendor_id = vendor_id
        la.flags = LOG_ADDRS_FL_ALLOW_UNREG_FALLBACK
        la.osd_name = osd_name.encode("ascii", "replace")[:14]
        la.primary_device_type[0] = PRIM_DEVTYPE_PLAYBACK
        la.log_addr_type[0] = LOG_ADDR_TYPE_PLAYBACK
        la.all_device_types[0] = ALL_DEVTYPE_PLAYBACK
        fcntl.ioctl(self._dev(), ADAP_S_LOG_ADDRS, la)

    def phys_addr(self) -> int:
        pa = ctypes.c_uint16()
        fcntl.ioctl(self._dev(), ADAP_G_PHYS_ADDR, pa)
        return pa.value

    def log_addr(self) -> int | None:
        la = _LogAddrs()
        fcntl.ioctl(self._dev(), ADAP_G_LOG_ADDRS, la)
        if not la.log_addr_mask or la.log_addr[0] == 0xFF:
            return None
        return la.log_addr[0]

    def transmit(self, data: bytes) -> int:
        if not 1 <= len(data) <= MAX_MSG_SIZE:
            raise ValueError(f"CEC message must be 1-16 bytes, got {len(data)}")
        msg = _Msg()
        msg.len = len(data)
        ctypes.memmove(msg.msg, data, len(data))
        fcntl.ioctl(self._dev(), TRANSMIT, msg)
        return msg.sequence

    def receive(self) -> Received | None:
        msg = _Msg()
        try:
            fcntl.ioctl(self._dev(), RECEIVE, msg)
        except BlockingIOError:
            self._settle()
            return None
        return Received(bytes(msg.msg[: msg.len]), msg.sequence, msg.tx_status, msg.rx_status)

    def event(self) -> Event | None:
        ev = _Event()
        try:
            fcntl.ioctl(self._dev(), DQEVENT, ev)
        except BlockingIOError:
            self._settle()
            return None
        if ev.event == EVENT_STATE_CHANGE:
            word = ev.raw[0]
            return Event(ev.event, phys_addr=word & 0xFFFF, log_addr_mask=word >> 16)
        if ev.event == EVENT_LOST_MSGS:
            return Event(ev.event, lost_msgs=ev.raw[0])
        return Event(ev.event)

    def _settle(self) -> None:
        # Let epoll drop the drained fd from its ready list, so the loop stops waking up.
        if self._epoll is not None:
            self._epoll.poll(0)

    def _dev(self) -> int:
        if self._fd is None:
            raise OSError(errno.EBADF, f"{self.path} is not open")
        return self._fd

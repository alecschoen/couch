"""A virtual keyboard on /dev/uinput, for typing the remote's buttons into Chromium and Kodi.

Both see it as an ordinary USB keyboard: cage (wlroots) and Kodi on GBM pick input devices
up through libinput and udev, including ones that appear after they started.

Every key is a tap (down, then up straight away). couchd does its own key repeat, so a key
is never left down here: a lost CEC release can't leave an app scrolling forever, and the
compositor's and Kodi's own repeat never kick in.

The device needs python-evdev (Debian's `python3-evdev`, which install.sh adds) and write
access to /dev/uinput (install.sh's udev rule gives it to the `input` group). It is imported
only when the device opens, so dev mode and the tests don't need either.
"""

import logging

from couchd.backends import BackendError, KeyboardBackend

log = logging.getLogger(__name__)

DEVICE_NAME = "Couch remote"

# linux/input-event-codes.h. Only what the keymaps in couchd/remote.py use.
EV_KEY = 0x01
KEY_ESC = 1
KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8, KEY_9, KEY_0 = range(2, 12)
KEY_BACKSPACE = 14
KEY_ENTER = 28
KEY_SPACE = 57
KEY_HOME = 102
KEY_UP = 103
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_DOWN = 108
KEY_BACK = 158
KEY_PLAYPAUSE = 164
KEY_STOPCD = 166
KEY_REWIND = 168
KEY_PLAYCD = 200
KEY_PAUSECD = 201
KEY_FASTFORWARD = 208

DIGITS = (KEY_0, KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8, KEY_9)

KEY_NAMES = {v: n for n, v in globals().items() if n.startswith("KEY_") and isinstance(v, int)}

ALL_KEYS = tuple(sorted(KEY_NAMES))


def key_name(code: int) -> str:
    return KEY_NAMES.get(code, f"key {code}")


class UinputKeyboard(KeyboardBackend):
    def __init__(self, keys: tuple[int, ...] = ALL_KEYS, path: str = "/dev/uinput") -> None:
        super().__init__()
        self.keys = keys
        self.path = path
        self._device = None

    async def start(self) -> None:
        try:
            import evdev  # the Pi's python3-evdev; dev mode never gets here
        except ImportError:
            raise BackendError("python-evdev isn't installed (apt install python3-evdev)") from None
        try:
            self._device = evdev.UInput(
                {EV_KEY: list(self.keys)}, name=DEVICE_NAME, devnode=self.path
            )
        except (OSError, evdev.UInputError) as e:
            raise BackendError(f"can't create the virtual keyboard on {self.path}: {e}") from e
        log.info("virtual keyboard ready", extra={"device": self._device.device.path})

    async def stop(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None

    def tap(self, code: int) -> None:
        if self._device is None:
            return
        try:
            self._device.write(EV_KEY, code, 1)
            self._device.syn()
            self._device.write(EV_KEY, code, 0)
            self._device.syn()
        except OSError as e:
            log.warning("virtual keyboard write failed", extra={"error": str(e)})

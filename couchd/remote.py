"""The remote: buttons from the TV become keys for whichever app has the screen.

Where a key goes depends on the app in front (`apps.foreground`):

- The launcher gets `key` messages over the WebSocket, and nothing is typed.
- Chromium apps and Kodi get the key typed on a virtual keyboard (couchd/uinput.py). The
  `key` message still goes out, with `app` saying who it was for, so couchd's own services
  see every key; the launcher, hidden behind the app, ignores it.
- Some keys are couchd's wherever you are and are never typed into an app: `home` (back to
  the launcher, couchd/apps.py), `red` (next audio output, couchd/audio.py), `blue` (the
  quick menu, couchd/overlay.py), and `green` and `yellow` (kept for couchd's own uses).
- While the quick menu is open, the overlay takes the keys it uses before anything else
  (`KeyTaker.take_key`): they go to no app, and their `key` message says `overlay: true`
  so the launcher leaves them alone too.
- A launcher screen can claim the raw keys for a while (`remote.raw_keys`, for Settings'
  remote test): couchd's colour-key actions then stand down. Their `key` messages say
  `raw: true`, the overlay takes nothing, and couchd/audio.py lets Red be. Home still goes
  home. The claim lapses `RAW_KEYS_FOR` seconds after it was made or after the last key, so a
  launcher that goes away mid-test can't leave the colour keys dead.

Timing is couchd's, because TVs differ in whether, and how often, they repeat a held button:

- Arrows repeat while held: the first repeat after `delay`, then faster and faster, down to
  one every `fastest`. The TV's own repeats are ignored.
- Back and OK act on release. Held for `hold_after`, Back is Home instead (the Sony doesn't
  forward its Home button, so this is the way home), and OK is a long press: `select` with
  `hold: true` (in an app, a plain Enter). The release then does nothing.
- Everything else acts on press, once.
- A button whose release never comes (CEC frames do get lost) is let go after `max_hold`.

`remote.press` acts like a quick press and release of one button, for curl and tests;
`hold: true` makes it a long press of OK or Back. With `keyboard: true` it is a key typed on
a keyboard into the launcher (launcher/ui/main.slint): it goes to the launcher whatever is in front,
so a key the virtual keyboard typed into a launcher that wasn't supposed to have the screen
can't come back to be typed again. `remote.hold` holds a button down for some seconds,
through the same press and release as the TV's, to try repeat with curl.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from couchd import uinput
from couchd.backends import BackendError, KeyAction, KeyboardBackend, TvBackend
from couchd.commands import CommandError, arg
from couchd.keys import REMOTE_KEYS
from couchd.services import Context, Service

log = logging.getLogger(__name__)

REPEATING = frozenset({"up", "down", "left", "right"})
ON_RELEASE = frozenset({"back", "select"})  # these wait to see whether they're held
COUCHD_KEYS = frozenset({"home", "red", "green", "yellow", "blue"})
RAW_KEYS_FOR = 15.0  # seconds a claim on the raw keys lasts without a key
LAUNCHER = "launcher"

_COMMON = {
    "up": uinput.KEY_UP,
    "down": uinput.KEY_DOWN,
    "left": uinput.KEY_LEFT,
    "right": uinput.KEY_RIGHT,
    "select": uinput.KEY_ENTER,
    "play": uinput.KEY_PLAYCD,
    "pause": uinput.KEY_PAUSECD,
    "play_pause": uinput.KEY_PLAYPAUSE,
    "stop": uinput.KEY_STOPCD,
    "rewind": uinput.KEY_REWIND,
    "fast_forward": uinput.KEY_FASTFORWARD,
    **{str(n): code for n, code in enumerate(uinput.DIGITS)},
}
# What each kind of app gets typed. Back is the browser's Back key in Chromium (history back
# on an ordinary page) and Backspace in Kodi (its default keymap's Back).
KEYMAPS: dict[str, dict[str, int]] = {
    "chromium": {**_COMMON, "back": uinput.KEY_BACK},
    "kodi": {**_COMMON, "back": uinput.KEY_BACKSPACE},
}
# Per-app changes on top of its kind's keymap, by app id. YouTube's TV interface takes
# Escape as Back, as a TV's Return; BrowserBack would be history back, and the first entry
# in its tab is about:blank. Its Back from Home goes to the launcher (couchd/youtube.js).
APP_KEYMAPS: dict[str, dict[str, int]] = {
    "youtube": {"back": uinput.KEY_ESC},
    "ytmusic": {"back": uinput.KEY_ESC},  # handled by couchd/ytmusic.js
}


class KeyTaker(Protocol):
    """Gets every key first (the overlay, couchd/overlay.py). True: the key is taken."""

    def take_key(self, key: str, *, repeat: bool = False, hold: bool = False) -> bool: ...


@dataclass(frozen=True)
class Timing:
    delay: float = 0.4  # held this long before the first repeat
    first: float = 0.2  # then the gap between repeats starts here...
    speedup: float = 0.85  # ...shrinking by this much each repeat...
    fastest: float = 0.06  # ...down to this
    hold_after: float = 0.8  # Back or OK held this long is Home or a long press
    max_hold: float = 15.0  # no release from the TV after this long: let go


class RemoteService(Service):
    name = "remote"

    def __init__(
        self,
        ctx: Context,
        tv: TvBackend | None = None,
        keyboard: KeyboardBackend | None = None,
        *,
        timing: Timing = Timing(),
        overlay: KeyTaker | None = None,
    ) -> None:
        self.ctx = ctx
        self.tv = tv
        self.keyboard = keyboard
        self.timing = timing
        self.overlay = overlay
        self._held: str | None = None
        self._held_task: asyncio.Task | None = None
        self._held_long = False  # the held Back or OK already acted as a long press
        self._keyboard_up = False
        self._raw_until = 0.0  # loop time a screen's claim on the raw keys lapses

    async def start(self) -> None:
        if self.keyboard is not None:
            try:
                await self.keyboard.start()
                self._keyboard_up = True
            except BackendError as e:
                # The launcher still works without it; apps just won't get keys.
                log.error("no virtual keyboard; apps won't get the remote", extra={"error": str(e)})
        if self.tv is not None:
            self.tv.on_key(self._on_tv_key)
        self.ctx.commands.add("remote.press", self._press_command)
        self.ctx.commands.add("remote.hold", self._hold_command)
        self.ctx.commands.add("remote.raw_keys", self._raw_keys_command)

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("remote.")
        self._let_go()
        if self.keyboard is not None and self._keyboard_up:
            await self.keyboard.stop()
            self._keyboard_up = False

    # --- from the TV -------------------------------------------------------------------

    def _on_tv_key(self, action: KeyAction, key: str | None) -> None:
        if action == "release":
            self.release()
        elif key is not None and action == "press":
            self.press(key)
        elif key is not None:
            self.tap(key)

    def press(self, key: str) -> None:
        """A button went down. Acts now, or for Back and OK on release or hold."""
        if not self._known(key):
            return
        if key == self._held:
            return  # the TV repeating a held button; we do our own repeat
        self.release()  # a new button down means the last one is up
        self._held = key
        self._held_long = False
        if key not in ON_RELEASE:
            self._send(key)
        self._held_task = asyncio.create_task(self._hold(key))

    def release(self) -> None:
        """The button that was down came up."""
        key = self._let_go()
        if key in ON_RELEASE and not self._held_long:
            self._send(key)

    def tap(self, key: str) -> None:
        """A press and release in one, with no hold."""
        if self._known(key):
            self.release()
            self._send(key)

    def _let_go(self) -> str | None:
        key, self._held = self._held, None
        if self._held_task is not None:
            self._held_task.cancel()
            self._held_task = None
        return key

    async def _hold(self, key: str) -> None:
        t = self.timing
        loop = asyncio.get_running_loop()
        deadline = loop.time() + t.max_hold
        if key in ON_RELEASE:
            await asyncio.sleep(t.hold_after)
            self._held_long = True
            self._send_hold(key)
        elif key in REPEATING:
            await asyncio.sleep(t.delay)
            gap = t.first
            while loop.time() < deadline:
                self._send(key, repeat=True)
                await asyncio.sleep(gap)
                gap = max(t.fastest, gap * t.speedup)
        await asyncio.sleep(max(0.0, deadline - loop.time()))
        log.warning("no release from the TV; letting go", extra={"key": key})
        self._held = None
        self._held_task = None

    def _known(self, key: str) -> bool:
        if key not in REMOTE_KEYS:
            log.warning("unknown remote key ignored", extra={"key": key})
            return False
        return True

    # --- routing -------------------------------------------------------------------------

    def _send_hold(self, key: str, *, keyboard: bool = False) -> None:
        if key == "back":
            self._send("home", keyboard=keyboard)
        else:
            self._send(key, hold=True, keyboard=keyboard)

    def _send(
        self, key: str, *, repeat: bool = False, hold: bool = False, keyboard: bool = False
    ) -> None:
        apps = self.ctx.state.get("apps")
        foreground = apps.get("foreground") or LAUNCHER
        app_id = LAUNCHER if keyboard else foreground
        kind = next((a["kind"] for a in apps.get("list", ()) if a["id"] == app_id), LAUNCHER)
        raw = self.raw_keys and app_id == LAUNCHER
        if raw:  # each key keeps the claim alive
            self._raw_until = asyncio.get_running_loop().time() + RAW_KEYS_FOR
        taken = (
            not raw
            and self.overlay is not None
            and self.overlay.take_key(key, repeat=repeat, hold=hold)
        )
        message = {"key": key, "repeat": repeat, "hold": hold, "app": app_id}
        if taken:
            message["overlay"] = True
        if raw:
            message["raw"] = True
        self.ctx.bus.publish("key", message)
        if taken or kind == LAUNCHER or key in COUCHD_KEYS:
            return
        code = APP_KEYMAPS.get(app_id, {}).get(key, KEYMAPS.get(kind, {}).get(key))
        if code is None:
            return
        if self.keyboard is None or not self._keyboard_up:
            log.debug("no virtual keyboard for key", extra={"key": key, "app": app_id})
            return
        self.keyboard.tap(code)

    @property
    def raw_keys(self) -> bool:
        """True while a launcher screen has the raw keys: couchd's colour keys stand down."""
        return self._raw_until > 0 and asyncio.get_running_loop().time() < self._raw_until

    # --- commands --------------------------------------------------------------------------

    async def _raw_keys_command(self, args: dict[str, Any]) -> None:
        on = args.get("on")
        if not isinstance(on, bool):
            raise CommandError("'on' must be true or false")
        self._raw_until = asyncio.get_running_loop().time() + RAW_KEYS_FOR if on else 0.0

    async def _press_command(self, args: dict[str, Any]) -> None:
        key = arg(args, "key", str)
        if key not in REMOTE_KEYS:
            raise CommandError(f"unknown key {key!r}")
        flags = {name: args.get(name, False) for name in ("keyboard", "hold")}
        for name, value in flags.items():
            if not isinstance(value, bool):
                raise CommandError(f"'{name}' must be true or false")
        if flags["hold"] and key not in ON_RELEASE:
            raise CommandError(f"only {' and '.join(sorted(ON_RELEASE))} can be held")
        self.release()
        if flags["hold"]:
            self._send_hold(key, keyboard=flags["keyboard"])
        else:
            self._send(key, keyboard=flags["keyboard"])

    async def _hold_command(self, args: dict[str, Any]) -> None:
        key = arg(args, "key", str)
        if key not in REMOTE_KEYS:
            raise CommandError(f"unknown key {key!r}")
        seconds = args.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int | float):
            raise CommandError("'seconds' must be a number")
        if not 0 < seconds <= self.timing.max_hold:
            raise CommandError(f"'seconds' must be more than 0 and at most {self.timing.max_hold}")
        self.press(key)
        try:
            await asyncio.sleep(seconds)
        finally:
            if self._held == key:
                self.release()

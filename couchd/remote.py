"""The remote: buttons from the TV become keys for whichever app has the screen.

Where a key goes depends on the app in front (`apps.foreground`):

- The launcher gets `key` messages over the WebSocket, and nothing is typed.
- Chromium apps and Kodi get the key typed on a virtual keyboard (couchd/uinput.py). The
  `key` message still goes out, with `app` saying who it was for, so couchd's own services
  see every key; the launcher, hidden behind the app, ignores it.
- Some keys are couchd's wherever you are and are never typed into an app: `home` (back to
  the launcher, couchd/apps.py), `red` (next audio output, couchd/audio.py), `blue` (the
  quick menu: a `quick_menu` message for the UI), and `green` and `yellow` (kept for
  couchd's own uses).

Timing is couchd's, because TVs differ in whether, and how often, they repeat a held button:

- Arrows repeat while held: the first repeat after `delay`, then faster and faster, down to
  one every `fastest`. The TV's own repeats are ignored.
- Back acts on release. Held for `home_after`, it's Home instead, and the release does
  nothing. (The Sony doesn't forward its Home button, so this is the way home.)
- Everything else acts on press, once.
- A button whose release never comes (CEC frames do get lost) is let go after `max_hold`.

`remote.press` acts like a quick press and release of one button, for curl and tests. With
`keyboard: true` it is a key typed on a keyboard into the launcher (ui/js/keys.js): it goes
to the launcher whatever is in front, so a key the virtual keyboard typed into a launcher
that wasn't supposed to have the screen can't come back to be typed again.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from couchd import uinput
from couchd.backends import BackendError, KeyAction, KeyboardBackend, TvBackend
from couchd.commands import CommandError, arg
from couchd.keys import REMOTE_KEYS
from couchd.services import Context, Service

log = logging.getLogger(__name__)

REPEATING = frozenset({"up", "down", "left", "right"})
COUCHD_KEYS = frozenset({"home", "red", "green", "yellow", "blue"})
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
# Per-app changes on top of its kind's keymap, by app id. YouTube's are #21's.
APP_KEYMAPS: dict[str, dict[str, int]] = {}


@dataclass(frozen=True)
class Timing:
    delay: float = 0.4  # held this long before the first repeat
    first: float = 0.2  # then the gap between repeats starts here...
    speedup: float = 0.85  # ...shrinking by this much each repeat...
    fastest: float = 0.06  # ...down to this
    home_after: float = 0.8  # Back held this long is Home
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
    ) -> None:
        self.ctx = ctx
        self.tv = tv
        self.keyboard = keyboard
        self.timing = timing
        self._held: str | None = None
        self._held_task: asyncio.Task | None = None
        self._went_home = False  # the held Back already turned into Home
        self._keyboard_up = False

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
        """A button went down. Acts now, or on release for Back."""
        if not self._known(key):
            return
        if key == self._held:
            return  # the TV repeating a held button; we do our own repeat
        self.release()  # a new button down means the last one is up
        self._held = key
        self._went_home = False
        if key != "back":
            self._send(key)
        self._held_task = asyncio.create_task(self._hold(key))

    def release(self) -> None:
        """The button that was down came up."""
        key = self._let_go()
        if key == "back" and not self._went_home:
            self._send("back")

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
        if key == "back":
            await asyncio.sleep(t.home_after)
            self._went_home = True
            self._send("home")
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

    def _send(self, key: str, *, repeat: bool = False, keyboard: bool = False) -> None:
        apps = self.ctx.state.get("apps")
        foreground = apps.get("foreground") or LAUNCHER
        app_id = LAUNCHER if keyboard else foreground
        kind = next((a["kind"] for a in apps.get("list", ()) if a["id"] == app_id), LAUNCHER)
        self.ctx.bus.publish("key", {"key": key, "repeat": repeat, "app": app_id})
        if key == "blue" and not repeat:
            self.ctx.bus.publish("quick_menu", {"app": app_id})
        if kind == LAUNCHER or key in COUCHD_KEYS:
            return
        code = APP_KEYMAPS.get(app_id, {}).get(key, KEYMAPS.get(kind, {}).get(key))
        if code is None:
            return
        if self.keyboard is None or not self._keyboard_up:
            log.debug("no virtual keyboard for key", extra={"key": key, "app": app_id})
            return
        self.keyboard.tap(code)

    # --- commands --------------------------------------------------------------------------

    async def _press_command(self, args: dict[str, Any]) -> None:
        key = arg(args, "key", str)
        if key not in REMOTE_KEYS:
            raise CommandError(f"unknown key {key!r}")
        keyboard = args.get("keyboard", False)
        if not isinstance(keyboard, bool):
            raise CommandError("'keyboard' must be true or false")
        self.release()
        self._send(key, keyboard=keyboard)

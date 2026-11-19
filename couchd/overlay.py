"""Overlays over every app (#18): the quick menu, toasts and the volume panel.

couchd owns them, because only couchd sees the remote and knows what's in front. Each app
just draws what it's told, through its *host*:

- `launcher`: the launcher itself, from the live state section `overlay` and the `toast`
  and `volume_panel` messages (launcher/src/overlay.rs).
- `chromium`: a Chromium app in front (YouTube). couchd calls into the page over DevTools;
  the page gets the same overlays as web code, in a shadow root (couchd/overlay_page.py).
- `kodi`: Plex. Toasts are Kodi's own notifications (GUI.ShowNotification). The quick menu
  and the volume panel are drawn by service.couch through skin.couch, from overlay state
  couchd sends with JSON-RPC NotifyAll (couchd/kodi.py). If service.couch isn't up to
  draw it, Blue takes you back to the launcher and opens the menu there instead: Plex
  stops, but the menu still works. Without service.couch the volume panel is a
  notification too.

The quick menu (Blue): Home, Sound, Volume, Bluetooth and Sleep, on a sheet in the app's
colour. While it's open the remote is the menu's: couchd doesn't type keys into the app
(RemoteService asks `take_key` first), and the launcher ignores them (`overlay: true` on the
key message).

    Left / Right   move between the cards
    OK             Home: go home. Sound: next output. Volume: mute or unmute.
                   Bluetooth: the launcher's Bluetooth screen. Sleep: the next sleep
                   timer, Off, 15, 30, 60, 90 minutes (couchd/sleep.py).
    Up / Down      on Volume: louder, quieter (by VOLUME_STEP)
    Back, Blue     close it
    Home           closes it, and goes home as always

Other keys (play/pause, Red) still do what they always do. The menu closes by itself after
MENU_TIMEOUT without a key, and whenever the app in front changes.

The volume panel comes up for each `volume_changed` (#11), unless the menu is open: its
Volume card says the same.

The on-screen keyboard (#77): a text field getting focus in a Chromium app's page opens it,
on a sheet in the app's colour (couchd/keyboard.py has its keys). The page says which field
has focus (couchd/fields.js, through the `__couchd` binding); while the keyboard is open it
takes the remote's keys like the menu does, and couchd types into the field over DevTools
(Input.insertText, and Input.dispatchKeyEvent for Backspace and Enter), so the page sees real
typing. After each change couchd reads the field back, so the sheet shows its real value.
It closes on Back (the field loses focus), on its action key (Enter, then the same), when the
field loses focus, and when the app in front changes. Not in YouTube's TV interface, which has
a keyboard of its own (NO_KEYBOARD).

Toasts: anything on the bus (`ctx.bus.toast`, the `overlay.toast` command, POST /api/toast)
goes to the app in front too. They go after TOAST_MS, or the toast's own `ms`. So does the
volume panel, after the last change.

The sleep timer's last minute (#79): before anything else, `take_key` asks the sleep timer,
which takes any key then (couchd/sleep.py): the key turns the timer off and does nothing else,
in the menu, the keyboard or the app. Except Home, which still goes home.
"""

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from couchd import overlay_page
from couchd.bus import TOAST_ICONS, TOAST_LEVELS
from couchd.chromium import CdpError, Chromium
from couchd.commands import CommandError, arg
from couchd.keyboard import ARROWS, Keyboard, Op, read_field
from couchd.kodi import Kodi, KodiError
from couchd.services import Context, Service
from couchd.sleep import CHOICES as SLEEP_CHOICES
from couchd.sleep import SleepService

log = logging.getLogger(__name__)

Host = Literal["launcher", "chromium", "kodi"]

TOAST_MS = 2400  # ui/js/overlay-view.js has the same
MENU_TIMEOUT = 30.0
VOLUME_STEP = 5
KODI_TITLE = "Couch"
KODI_IMAGES = {"info": "info", "warning": "warning", "error": "error"}

ITEMS = ("home", "sound", "volume", "bluetooth", "sleep")
MINUS = "\u2212"  # the Volume card's "<minus> 60 +", as in the design
SLEEP_HINT = "OK: " + " \u00b7 ".join(["Off", *map(str, SLEEP_CHOICES)])  # "OK: Off · 15 · 30…"
PALETTES = {"youtube": "yt", "ytmusic": "yt", "plex": "plex"}  # app id -> ui/css/tokens.css palette
NO_KEYBOARD = frozenset({"youtube"})  # YouTube's TV interface has its own keyboard
EVENING_STARTS, EVENING_ENDS = 18, 7  # without couchd/evening.py's `dark`, as it does


# --- the volume panel's source ---------------------------------------------------------------
# AudioService's message for every volume or mute change couchd makes (#11): the TV remote's
# buttons, a command, the limit. Not an output switch putting that output's volume back: the
# "Sound now on X" toast says that. Its `data` gives `volume` and `muted` (and `output`);
# anything left out comes from the `audio` state.
VOLUME_EVENT = "volume_changed"


@dataclass(frozen=True)
class VolumeChange:
    output: str | None
    volume: int
    muted: bool


def volume_from_event(data: Any, audio: dict[str, Any]) -> VolumeChange | None:
    """The volume_changed message, read leniently."""
    if not isinstance(data, dict):
        return None
    volume = data.get("volume", audio.get("volume"))
    if isinstance(volume, bool) or not isinstance(volume, int | float):
        return None
    muted = data.get("muted", audio.get("muted", False))
    output = data.get("output", audio.get("current"))
    return VolumeChange(output if isinstance(output, str) else None, round(volume), bool(muted))


# --- the quick menu -----------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """What a key in the open menu asks for."""

    kind: Literal["home", "cycle_output", "volume", "mute", "bluetooth", "sleep"]
    step: int = 0


class QuickMenu:
    """The menu's state and its keys. Knows nothing of hosts or couchd; tests drive it."""

    def __init__(self) -> None:
        self.open = False
        self.app: str | None = None
        self.host: Host | None = None
        self.focus = ITEMS[0]

    def show(self, app: str, host: Host) -> None:
        self.open, self.app, self.host, self.focus = True, app, host, ITEMS[0]

    def hide(self) -> None:
        self.open, self.app, self.host = False, None, None

    def key(self, key: str) -> tuple[bool, Action | None]:
        """(taken, action). An open menu takes the keys it uses, and closes on Back,
        Blue and Home (which it lets through, so Home still goes home)."""
        if not self.open:
            return False, None
        if key in ("back", "blue"):
            self.hide()
            return True, None
        if key == "home":
            self.hide()
            return False, None
        i = ITEMS.index(self.focus)
        if key in ("left", "right"):
            i = max(0, min(len(ITEMS) - 1, i + (1 if key == "right" else -1)))
            self.focus = ITEMS[i]
            return True, None
        if key in ("up", "down"):
            if self.focus == "volume":
                return True, Action("volume", VOLUME_STEP if key == "up" else -VOLUME_STEP)
            return True, None
        if key == "select":
            match self.focus:
                case "home":
                    self.hide()
                    return True, Action("home")
                case "sound":
                    return True, Action("cycle_output")
                case "volume":
                    return True, Action("mute")
                case "sleep":
                    return True, Action("sleep")
                case _:
                    self.hide()
                    return True, Action("bluetooth")
        return False, None


def local_time(iso: Any) -> str | None:
    """ "21:20", from the sleep timer's `ends_at`."""
    try:
        return f"{datetime.fromisoformat(iso):%H:%M}" if isinstance(iso, str) else None
    except ValueError:
        return None


def is_evening(mode: Any, dark: Any, now: datetime) -> bool:
    """The `display.evening` setting with the live `evening.dark`, as the launcher does."""
    if mode == "always":
        return True
    if mode == "off":
        return False
    if isinstance(dark, bool):
        return dark
    return now.hour >= EVENING_STARTS or now.hour < EVENING_ENDS


# --- the service ----------------------------------------------------------------------------


class OverlayService(Service):
    name = "overlay"

    def __init__(
        self,
        ctx: Context,
        *,
        chromium: Chromium | None = None,
        pages: Callable[[str], str | None] | None = None,
        kodi: Kodi | None = None,
        go_home: Callable[[], Any] | None = None,
        sleep: SleepService | None = None,
        now: Callable[[], datetime] = datetime.now,
        menu_timeout: float = MENU_TIMEOUT,
    ) -> None:
        """`pages(app_id)` gives a Chromium app's tab (AppService.page); `go_home()` takes the
        screen back to the launcher (AppService.home); `sleep` sees every key first."""
        self.ctx = ctx
        self.chromium = chromium
        self.pages = pages or (lambda app_id: None)
        self.kodi = kodi
        self.go_home = go_home
        self.sleep = sleep
        self.now = now
        self.menu_timeout = menu_timeout
        self.menu = QuickMenu()
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._timeout: asyncio.TimerHandle | None = None
        self._menu_target: str | None = None  # the tab the menu is drawn in
        self._kodi_menu = False  # service.couch has a menu up
        self._opening = False  # asking Kodi whether it can show the menu
        self._pending: dict[str, tuple[str, Any]] = {}  # kind -> (target, payload) for Chromium
        self._pushing: asyncio.Task | None = None
        self.keyboard = Keyboard()
        self._kb_target: str | None = None  # the tab the keyboard is drawn in, while it's open
        self._dismissed: str | None = None  # the field the keyboard last closed for
        self._ops: deque[tuple[str, str, Op]] = deque()  # (tab, field id, op), in order
        self._typing: asyncio.Task | None = None

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._publish_menu()
        commands = self.ctx.commands
        commands.add("overlay.toast", self._toast_command)
        commands.add("overlay.menu", self._menu_command)
        if self.chromium is not None:
            self.chromium.on_page_message(self._page_message)
        self._spawn(self._watch_bus())

    async def stop(self) -> None:
        self._running = False
        self.ctx.commands.remove_prefix("overlay.")
        self._cancel_timeout()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            log.error("overlay task failed", exc_info=e)

    # --- keys (called by RemoteService, before a key goes anywhere) ---------------------------

    def take_key(self, key: str, *, repeat: bool = False, hold: bool = False) -> bool:
        """True if the overlay takes the key: it then goes to no app."""
        if not self._running:
            return False
        if self.sleep is not None and not repeat and self.sleep.take_key(key):
            return True  # turned the sleep timer off, and that's all
        if self.keyboard.open:
            return self._keyboard_key(key, repeat=repeat)
        if not self.menu.open:
            if key != "blue" or repeat:
                return False
            if self._front()[0] == "kodi":
                if not self._opening:  # asks Kodi first
                    self._opening = True
                    self._spawn(self.open_menu())
            else:
                self._open_here()
            return True
        if repeat and key not in ("up", "down", "left", "right"):
            return True
        taken, action = self.menu.key(key)
        if self.menu.open:
            self._arm_timeout()
        self._menu_changed()
        if action is not None:
            self._spawn(self._act(action))
        return taken

    # --- the quick menu --------------------------------------------------------------------

    async def open_menu(self) -> None:
        """Open the menu over the app in front. Over Kodi, ask service.couch first."""
        try:
            if self._front()[0] != "kodi":
                self._open_here()
                return
            app = self.ctx.state.get("apps").get("foreground") or "launcher"
            if self.kodi is not None and await self.kodi.can_host():
                self._show(app, "kodi")
                return
            # Kodi can't draw it: back to the launcher, and the menu there.
            log.info("quick menu: Kodi can't show it; opening it on the launcher")
            if self.go_home is not None:
                await self.go_home()
            self._show("launcher", "launcher")
        finally:
            self._opening = False

    def _open_here(self) -> None:
        """Over the launcher or a Chromium app: no need to ask anyone."""
        app = self.ctx.state.get("apps").get("foreground") or "launcher"
        host, target = self._front()
        self._show(app, "chromium" if host == "chromium" and target is not None else "launcher")

    def _show(self, app: str, host: Host) -> None:
        self.menu.show(app, host)
        self._arm_timeout()
        self._menu_changed()

    def close_menu(self) -> None:
        if self.menu.open:
            self.menu.hide()
            self._menu_changed()

    def _host_for(self, app: str, apps: dict[str, Any]) -> Host:
        kind = next((a["kind"] for a in apps.get("list", ()) if a["id"] == app), "launcher")
        return kind if kind in ("chromium", "kodi") else "launcher"

    def menu_state(self) -> dict[str, Any] | None:
        if not self.menu.open:
            return None
        audio = self.ctx.state.get("audio")
        app = self.menu.app or "launcher"
        title = self._app_name(app)
        outputs = {o["id"]: o["name"] for o in audio.get("outputs", ())}
        sound = outputs.get(audio.get("current"), "No sound")
        volume = "Muted" if audio.get("muted") else f"{MINUS} {audio.get('volume', 0)} +"
        sleep = self.ctx.state.get("sleep")
        minutes = sleep.get("minutes")
        values = {
            "home": ("Go to", "Home"),
            "sound": ("Sound", sound),
            "volume": ("Volume", volume),
            "bluetooth": ("Devices", "Bluetooth"),
            "sleep": ("Sleep", f"{minutes} min" if minutes else "Off"),
        }
        items = [{"id": i, "label": values[i][0], "value": values[i][1]} for i in ITEMS]
        sleep_card = items[ITEMS.index("sleep")]
        sleep_card["hint"] = SLEEP_HINT  # under the value, while it's focused
        if minutes and (ends := local_time(sleep.get("ends_at"))):
            sleep_card["detail"] = f"TV off at {ends}"
        return {
            "open": True,
            "app": app,
            "host": self.menu.host,
            "title": "Couch" if app == "launcher" else title,
            "palette": PALETTES.get(app),
            "evening": self._evening(),
            "focus": self.menu.focus,
            "items": items,
        }

    def _app_name(self, app: str) -> str:
        apps = self.ctx.state.get("apps")
        return next((a["name"] for a in apps.get("list", ()) if a["id"] == app), "Couch")

    def _evening(self) -> bool:
        return is_evening(
            self.ctx.settings.get("display.evening"),
            self.ctx.state.get("evening").get("dark"),
            self.now(),
        )

    def _publish_menu(self) -> dict[str, Any] | None:
        state = self.menu_state()
        self.ctx.state.update("overlay", {"menu": state})
        return state

    def _menu_changed(self) -> None:
        """Publish the menu and draw it in its host (or take it down where it was)."""
        state = self._publish_menu()
        if not self.menu.open:
            self._cancel_timeout()
        target = self.pages(self.menu.app) if state and state["host"] == "chromium" else None
        if self._menu_target is not None and self._menu_target != target:
            self._push(self._menu_target, "menu", None)
        self._menu_target = target
        if target is not None:
            self._push(target, "menu", state)
        kodi_menu = bool(state and state["host"] == "kodi")
        if kodi_menu or self._kodi_menu:
            self._kodi_menu = kodi_menu
            self._spawn(self._kodi_notify({"menu": state}))

    def _arm_timeout(self) -> None:
        self._cancel_timeout()
        loop = asyncio.get_running_loop()
        self._timeout = loop.call_later(self.menu_timeout, self._timed_out)

    def _cancel_timeout(self) -> None:
        if self._timeout is not None:
            self._timeout.cancel()
            self._timeout = None

    def _timed_out(self) -> None:
        self._timeout = None
        log.info("quick menu closed: no key for a while")
        self.close_menu()

    async def _act(self, action: Action) -> None:
        commands = self.ctx.commands
        try:
            match action.kind:
                case "home" | "bluetooth":
                    await commands.run("apps.home")
                    screen = "home" if action.kind == "home" else "bluetooth"
                    self.ctx.bus.publish("open_screen", {"screen": screen})
                case "cycle_output":
                    await commands.run("audio.cycle_output")
                case "volume":  # never above the limit; up unmutes, like the TV remote
                    await commands.run("audio.step_volume", {"by": action.step})
                case "mute":
                    await commands.run("audio.toggle_muted")
                case "sleep":
                    await commands.run("sleep.cycle")
        except CommandError as e:
            self.ctx.bus.toast(str(e), "error")

    # --- toasts and the volume panel -------------------------------------------------------

    async def _watch_bus(self) -> None:
        with self.ctx.bus.subscribe() as sub:
            async for message in sub:
                try:
                    self._on_message(message)
                except Exception:
                    log.exception("overlay: couldn't handle a message")

    def _on_message(self, message: dict[str, Any]) -> None:
        kind, data = message.get("type"), message.get("data")
        if kind == "toast" and isinstance(data, dict):
            self._toast_to_app(data)
        elif kind == "state" and not message.get("full") and isinstance(data, dict):
            apps = data.get("apps")
            moved = isinstance(apps, dict) and "foreground" in apps
            if moved and self.menu.open and (apps["foreground"] or "launcher") != self.menu.app:
                self.close_menu()
            if moved and self.keyboard.open and apps["foreground"] != self.keyboard.app:
                self.close_keyboard()
            if isinstance(data.get("audio"), dict) and self.menu.open:
                self._menu_changed()  # its Sound and Volume cards
            elif isinstance(data.get("sleep"), dict) and self.menu.open:
                self._menu_changed()  # its Sleep card
        elif kind == VOLUME_EVENT and not self.menu.open:  # an open menu's card says it
            change = volume_from_event(data, self.ctx.state.get("audio"))
            if change is not None:
                self.show_volume(change)

    def show_volume(self, change: VolumeChange) -> None:
        audio = self.ctx.state.get("audio")
        names = {o["id"]: o["name"] for o in audio.get("outputs", ())}
        panel = {
            "output": change.output,
            "name": names.get(change.output or "", "Volume"),
            "volume": change.volume,
            "muted": change.muted,
        }
        self.ctx.bus.publish("volume_panel", panel)
        host, target = self._front()
        if host == "chromium" and target is not None:
            self._push(target, "volume", panel)
        elif host == "kodi":
            self._spawn(self._kodi_volume(panel))

    def _toast_to_app(self, toast: dict[str, Any]) -> None:
        host, target = self._front()
        if host == "chromium" and target is not None:
            self._push(target, "toast", toast)
        elif host == "kodi" and self.kodi is not None:
            self._spawn(self._kodi_toast(toast))

    def _front(self) -> tuple[Host, str | None]:
        apps = self.ctx.state.get("apps")
        app = apps.get("foreground") or "launcher"
        host = self._host_for(app, apps)
        return host, self.pages(app) if host == "chromium" else None

    # --- Kodi -------------------------------------------------------------------------------

    async def _kodi_toast(self, toast: dict[str, Any]) -> None:
        """Kodi's own notification: Couch over the text, or the text over its `detail`."""
        assert self.kodi is not None
        image = KODI_IMAGES.get(toast.get("level", "info"), "info")
        title, text = KODI_TITLE, str(toast.get("text", ""))
        if toast.get("detail"):
            title, text = text, str(toast["detail"])
        ms = toast.get("ms")
        ms = ms if isinstance(ms, int) and not isinstance(ms, bool) and ms > 0 else TOAST_MS
        try:
            await self.kodi.show_notification(title, text, image, ms)
        except KodiError as e:
            log.info("couldn't toast in Kodi", extra={"error": str(e)})

    async def _kodi_volume(self, panel: dict[str, Any]) -> None:
        if self.kodi is None:
            return
        if await self.kodi.can_host():
            await self._kodi_notify({"volume": panel})
            return
        text = f"{panel['name']}: {'Muted' if panel['muted'] else panel['volume']}"
        with contextlib.suppress(KodiError):
            await self.kodi.show_notification(KODI_TITLE, text, "info", TOAST_MS)

    async def _kodi_notify(self, data: dict[str, Any]) -> None:
        if self.kodi is None:
            return
        try:
            await self.kodi.notify("overlay", {**data, "ms": TOAST_MS})
        except KodiError as e:
            log.info("couldn't reach Kodi's overlay", extra={"error": str(e)})

    # --- Chromium ---------------------------------------------------------------------------

    def _push(self, target: str, method: str, payload: Any) -> None:
        """Draw in a tab. Pushes are queued per kind, latest first: a burst of arrow keys
        sends only the newest menu, and a slow tab never holds up the remote."""
        if self.chromium is None:
            return
        key = f"{target}:{method}"
        self._pending.pop(key, None)
        self._pending[key] = (target, (method, payload))
        if self._pushing is None or self._pushing.done():
            self._pushing = self._spawn(self._drain())

    async def _drain(self) -> None:
        assert self.chromium is not None
        while self._pending:
            key = next(iter(self._pending))
            target, (method, payload) = self._pending.pop(key)
            try:
                drawn = await self.chromium.evaluate(target, overlay_page.call(method, payload))
                if drawn is not True:
                    # A page from before couchd started, or a frame without the script.
                    await self.chromium.evaluate(target, overlay_page.SCRIPT)
                    await self.chromium.evaluate(target, overlay_page.call(method, payload))
            except CdpError as e:
                log.info("couldn't draw the overlay", extra={"target": target, "error": str(e)})

    def _page_message(self, target: str, message: str) -> None:
        """A fresh document in a tab (a reload): draw an open menu again; the field the
        keyboard was for is gone with the old one. Or a text field got or lost focus."""
        if message == overlay_page.HELLO:
            if target == self._menu_target and self.menu.open:
                self._push(target, "menu", self.menu_state())
            if target == self._kb_target and self.keyboard.open:
                self.keyboard.hide()
                self._keyboard_changed()
        elif message.startswith(overlay_page.FIELD):
            try:
                info = json.loads(message[len(overlay_page.FIELD) :])
            except ValueError:
                return
            if isinstance(info, dict):
                self._field_report(target, info)

    # --- the on-screen keyboard (#77) -----------------------------------------------------------

    def _field_report(self, target: str, info: dict[str, Any]) -> None:
        """What couchd/fields.js says about the focused text field in a tab."""
        apps = self.ctx.state.get("apps")
        app = apps.get("foreground") or "launcher"
        if app in NO_KEYBOARD or self._host_for(app, apps) != "chromium":
            return
        if self.pages(app) != target:  # a tab behind the one in front
            return
        kb, field_id = self.keyboard, info.get("id")
        here = kb.open and kb.field is not None and kb.field.id == field_id
        if info.get("focused") is not True:
            if here:  # the field lost focus: nothing left to type into
                kb.hide()
                self._keyboard_changed()
            return
        if here:
            if kb.mirror(info):
                self._keyboard_changed()
            return
        if field_id == self._dismissed:  # still focused, just closed: until it's focused again
            return
        field = read_field(info)
        if field is None:
            return
        self.close_menu()
        kb.show(field, app)
        kb.mirror(info)
        self._kb_target = target
        log.info("keyboard opened", extra={"app": app, "kind": field.kind})
        self._keyboard_changed()

    def close_keyboard(self) -> None:
        """Close it, and take the focus from its field."""
        if self.keyboard.open:
            target, field = self._kb_target, self.keyboard.field
            self.keyboard.hide()
            self._keyboard_changed()
            if target is not None and field is not None:
                self._type(target, field.id, [Op("blur")])

    def _keyboard_key(self, key: str, *, repeat: bool) -> bool:
        if repeat and key not in ARROWS:
            return True
        target, field = self._kb_target, self.keyboard.field
        taken, ops = self.keyboard.key(key)
        self._keyboard_changed()
        if ops and target is not None and field is not None:
            self._type(target, field.id, ops)
        return taken

    def keyboard_state(self) -> dict[str, Any] | None:
        """What the page draws: the keyboard, in the app's colour."""
        kb = self.keyboard
        if not kb.open or kb.app is None:
            return None
        return {
            **kb.view(),
            "app": kb.app,
            "kicker": self._app_name(kb.app),
            "palette": PALETTES.get(kb.app),
            "evening": self._evening(),
        }

    def _keyboard_changed(self) -> None:
        """Draw it in its tab, or take it down; `overlay.keyboard` says it's open (but never
        what's typed)."""
        kb = self.keyboard
        state = self.keyboard_state()
        if state is not None and self._kb_target is not None:
            self._push(self._kb_target, "keyboard", state)
            summary = {"open": True, "app": kb.app, "kind": state["kind"], "title": state["title"]}
            self.ctx.state.update("overlay", {"keyboard": summary})
        elif self._kb_target is not None:
            log.info("keyboard closed", extra={"app": kb.app})
            self._push(self._kb_target, "keyboard", None)
            self._kb_target = None
            self._dismissed = kb.field.id if kb.field is not None else None
            self.ctx.state.update("overlay", {"keyboard": None})

    def _type(self, target: str, field_id: str, ops: list[Op]) -> None:
        """Do `ops` to the field, in order, after anything still to do."""
        if self.chromium is None:
            return
        self._ops.extend((target, field_id, op) for op in ops)
        if self._typing is None or self._typing.done():
            self._typing = self._spawn(self._typist())

    async def _typist(self) -> None:
        assert self.chromium is not None
        chromium = self.chromium
        while self._ops:
            target, field_id, op = self._ops.popleft()
            field = self.keyboard.field
            if op.kind in ("insert", "backspace") and (field is None or field.id != field_id):
                continue  # another field has the keyboard now
            try:
                match op.kind:
                    case "insert":
                        await chromium.insert_text(target, op.text)
                    case "backspace":
                        await chromium.press_key(target, "Backspace")
                    case "enter":
                        await chromium.press_key(target, "Enter")
                    case "replace":
                        if await chromium.evaluate(target, overlay_page.field("select", field_id)):
                            await chromium.insert_text(target, op.text)
                    case "blur":
                        await chromium.evaluate(target, overlay_page.field("blur", field_id))
                if op.kind in ("insert", "backspace") and not self._ops:
                    # Read it back: the sheet shows what the field really has.
                    info = await chromium.evaluate(target, overlay_page.field("state", field_id))
                    if isinstance(info, dict):
                        self._field_report(target, info)
            except CdpError as e:  # never what was typed: it may be a password
                log.info("couldn't type", extra={"op": op.kind, "error": str(e)})

    # --- commands ---------------------------------------------------------------------------

    async def _toast_command(self, args: dict[str, Any]) -> None:
        text = arg(args, "text", str).strip()
        if not text:
            raise CommandError("'text' can't be empty")
        level = args.get("level", "info")
        if level not in TOAST_LEVELS:
            raise CommandError(f"'level' must be one of {', '.join(TOAST_LEVELS)}")
        icon = args.get("icon")
        if icon is not None and icon not in TOAST_ICONS:
            raise CommandError(f"'icon' must be one of {', '.join(TOAST_ICONS)}")
        self.ctx.bus.toast(text, level, icon=icon)

    async def _menu_command(self, args: dict[str, Any]) -> None:
        if arg(args, "open", bool):
            if not self.menu.open:
                await self.open_menu()
        else:
            self.close_menu()

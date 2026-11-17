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

The quick menu (Blue): Home, Sound, Volume and Bluetooth, on a sheet in the app's colour.
While it's open the remote is the menu's: couchd doesn't type keys into the app (RemoteService
asks `take_key` first), and the launcher ignores them (`overlay: true` on the key message).

    Left / Right   move between the cards
    OK             Home: go home. Sound: next output. Volume: mute or unmute.
                   Bluetooth: the launcher's Bluetooth screen.
    Up / Down      on Volume: louder, quieter (by VOLUME_STEP)
    Back, Blue     close it
    Home           closes it, and goes home as always

Other keys (play/pause, Red) still do what they always do. The menu closes by itself after
MENU_TIMEOUT without a key, and whenever the app in front changes.

The volume panel comes up for each `volume_changed` (#11), unless the menu is open: its
Volume card says the same.

Toasts: anything on the bus (`ctx.bus.toast`, the `overlay.toast` command, POST /api/toast)
goes to the app in front too. They go after TOAST_MS. So does the volume panel, after the
last change.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from couchd import overlay_page
from couchd.bus import TOAST_ICONS, TOAST_LEVELS
from couchd.chromium import CdpError, Chromium
from couchd.commands import CommandError, arg
from couchd.kodi import Kodi, KodiError
from couchd.services import Context, Service

log = logging.getLogger(__name__)

Host = Literal["launcher", "chromium", "kodi"]

TOAST_MS = 2400  # ui/js/overlay-view.js has the same
MENU_TIMEOUT = 30.0
VOLUME_STEP = 5
KODI_TITLE = "Couch"
KODI_IMAGES = {"info": "info", "warning": "warning", "error": "error"}

ITEMS = ("home", "sound", "volume", "bluetooth")
MINUS = "\u2212"  # the Volume card's "<minus> 60 +", as in the design
PALETTES = {"youtube": "yt", "plex": "plex"}  # app id -> ui/css/tokens.css palette
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

    kind: Literal["home", "cycle_output", "volume", "mute", "bluetooth"]
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
                case _:
                    self.hide()
                    return True, Action("bluetooth")
        return False, None


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
        now: Callable[[], datetime] = datetime.now,
        menu_timeout: float = MENU_TIMEOUT,
    ) -> None:
        """`pages(app_id)` gives a Chromium app's tab (AppService.page); `go_home()` takes the
        screen back to the launcher (AppService.home)."""
        self.ctx = ctx
        self.chromium = chromium
        self.pages = pages or (lambda app_id: None)
        self.kodi = kodi
        self.go_home = go_home
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
        apps = self.ctx.state.get("apps")
        audio = self.ctx.state.get("audio")
        app = self.menu.app or "launcher"
        title = next((a["name"] for a in apps.get("list", ()) if a["id"] == app), "Couch")
        outputs = {o["id"]: o["name"] for o in audio.get("outputs", ())}
        sound = outputs.get(audio.get("current"), "No sound")
        volume = "Muted" if audio.get("muted") else f"{MINUS} {audio.get('volume', 0)} +"
        values = {
            "home": ("Go to", "Home"),
            "sound": ("Sound", sound),
            "volume": ("Volume", volume),
            "bluetooth": ("Devices", "Bluetooth"),
        }
        return {
            "open": True,
            "app": app,
            "host": self.menu.host,
            "title": "Couch" if app == "launcher" else title,
            "palette": PALETTES.get(app),
            "evening": is_evening(
                self.ctx.settings.get("display.evening"),
                self.ctx.state.get("evening").get("dark"),
                self.now(),
            ),
            "focus": self.menu.focus,
            "items": [{"id": i, "label": values[i][0], "value": values[i][1]} for i in ITEMS],
        }

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
            if isinstance(data.get("audio"), dict) and self.menu.open:
                self._menu_changed()  # its Sound and Volume cards
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
        assert self.kodi is not None
        image = KODI_IMAGES.get(toast.get("level", "info"), "info")
        try:
            await self.kodi.show_notification(
                KODI_TITLE, str(toast.get("text", "")), image, TOAST_MS
            )
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
        """A fresh document in a tab (a reload): draw an open menu again."""
        if message == overlay_page.HELLO and target == self._menu_target and self.menu.open:
            self._push(target, "menu", self.menu_state())

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

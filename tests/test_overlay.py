"""The overlays over every app (#18): the quick menu's keys, where each overlay goes for the
app in front, the injection into Chromium pages, and Kodi's JSON-RPC."""

import asyncio
import json
import re
from datetime import datetime

import pytest

from couchd import overlay_page, youtube
from couchd.bus import EventBus
from couchd.chromium import Chromium, PageSetup
from couchd.commands import CommandError, Commands
from couchd.kodi import FakeKodi, KodiClient, KodiError
from couchd.kodi_rpc import KodiRpc
from couchd.overlay import (
    ITEMS,
    VOLUME_STEP,
    Action,
    OverlayService,
    QuickMenu,
    VolumeChange,
    is_evening,
    local_time,
    volume_from_event,
)
from couchd.services import Context
from couchd.sleep import SleepService
from couchd.state import LiveState, SavedState
from tests.fake_chromium import OVERLAY_CALL, FakeChromium

APPS = [
    {"id": "launcher", "name": "Couch", "kind": "launcher"},
    {"id": "youtube", "name": "YouTube", "kind": "chromium"},
    {"id": "plex", "name": "Plex", "kind": "kodi"},
]
AUDIO = {
    "current": "tv",
    "outputs": [{"id": "tv", "name": "TV speakers"}, {"id": "bt", "name": "Headphones"}],
    "volume": 40,
    "muted": False,
}


async def until(condition, what="condition"):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# --- the menu on its own -------------------------------------------------------------------


def test_a_closed_menu_takes_nothing():
    menu = QuickMenu()
    assert menu.key("left") == (False, None)
    assert menu.key("select") == (False, None)


def test_left_and_right_move_and_stop_at_the_ends():
    menu = QuickMenu()
    menu.show("youtube", "chromium")
    assert menu.focus == "home"
    assert menu.key("left") == (True, None)
    assert menu.focus == "home"
    for want in ITEMS[1:]:
        menu.key("right")
        assert menu.focus == want
    menu.key("right")
    assert menu.focus == "sleep"


def test_up_and_down_change_the_volume_only_on_volume():
    menu = QuickMenu()
    menu.show("launcher", "launcher")
    assert menu.key("up") == (True, None)  # taken, but nothing to do on Home
    menu.focus = "volume"
    assert menu.key("up") == (True, Action("volume", VOLUME_STEP))
    assert menu.key("down") == (True, Action("volume", -VOLUME_STEP))


def test_ok_on_each_card():
    menu = QuickMenu()
    want = {
        "home": (Action("home"), False),
        "sound": (Action("cycle_output"), True),
        "volume": (Action("mute"), True),
        "bluetooth": (Action("bluetooth"), False),
        "sleep": (Action("sleep"), True),  # the next time, and it stays open to show it
    }
    for item, (action, stays_open) in want.items():
        menu.show("launcher", "launcher")
        menu.focus = item
        assert menu.key("select") == (True, action)
        assert menu.open is stays_open, item


def test_back_and_blue_close_it_home_closes_and_goes_through():
    menu = QuickMenu()
    for key in ("back", "blue"):
        menu.show("launcher", "launcher")
        assert menu.key(key) == (True, None)
        assert not menu.open
    menu.show("youtube", "chromium")
    assert menu.key("home") == (False, None)  # Home still goes home
    assert not menu.open
    menu.show("youtube", "chromium")
    assert menu.key("play_pause") == (False, None)  # the app still gets it
    assert menu.open


def test_evening():
    noon, night = datetime(2026, 1, 1, 12), datetime(2026, 1, 1, 22)
    assert is_evening("always", False, noon)
    assert not is_evening("off", True, night)
    assert is_evening("sunset", True, noon)  # the sun is down: evening.dark says so
    assert not is_evening("sunset", False, night)
    assert is_evening("sunset", None, datetime(2026, 1, 1, 19))  # no couchd/evening.py yet
    assert is_evening("sunset", None, datetime(2026, 1, 1, 6))
    assert not is_evening("sunset", None, noon)


# --- the volume adapter ---------------------------------------------------------------------


def test_volume_from_an_event_fills_in_from_the_audio_state():
    assert volume_from_event({"volume": 55}, AUDIO) == VolumeChange("tv", 55, False)
    assert volume_from_event({"volume": 12.6, "muted": True, "output": "bt"}, AUDIO) == (
        VolumeChange("bt", 13, True)
    )
    assert volume_from_event({}, AUDIO) == VolumeChange("tv", 40, False)
    assert volume_from_event({"volume": True}, AUDIO) is None
    assert volume_from_event({"volume": "loud"}, {}) is None
    assert volume_from_event("nope", AUDIO) is None


# --- the service ----------------------------------------------------------------------------


class Pages:
    """A stand-in for Chromium: every tab has the overlay, and records what it was told."""

    def __init__(self):
        self.drawn: list[tuple[str, str, object]] = []
        self.listeners = []
        self.fail = False

    def on_page_message(self, listener):
        self.listeners.append(listener)

    async def evaluate(self, target, expression):
        m = OVERLAY_CALL.match(expression)
        assert m, expression[:80]
        self.drawn.append((target, m[1], json.loads(m[2])))
        return True

    def last(self, method):
        return next((p for _, m, p in reversed(self.drawn) if m == method), "none")


class Couch:
    def __init__(self, ctx, service, pages, kodi):
        self.ctx, self.service, self.pages, self.kodi = ctx, service, pages, kodi
        self.ran: list[tuple[str, dict]] = []
        self.homes = 0

    def front(self, app):
        self.ctx.state.update("apps", {"foreground": app})

    def menu(self):
        return self.ctx.state.get("overlay")["menu"]

    def press(self, *keys):
        return [self.service.take_key(k) for k in keys]


@pytest.fixture
async def couch(tmp_path):
    bus = EventBus()
    ctx = Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())
    ctx.state.update("apps", {"foreground": "launcher", "list": APPS})
    ctx.state.update("audio", dict(AUDIO))
    pages, kodi = Pages(), FakeKodi()
    holder = {}

    async def go_home():
        holder["couch"].homes += 1
        ctx.state.update("apps", {"foreground": "launcher"})

    service = OverlayService(
        ctx,
        chromium=pages,  # type: ignore[arg-type]
        pages=lambda app: "T-yt" if app == "youtube" else None,
        kodi=kodi,
        go_home=go_home,
        now=lambda: datetime(2026, 11, 16, 21),
        menu_timeout=0.2,
    )
    c = holder["couch"] = Couch(ctx, service, pages, kodi)

    def record(name):
        async def run(args):
            c.ran.append((name, args))
            audio = ctx.state.get("audio")
            if name == "audio.step_volume":
                ctx.state.update("audio", {"volume": audio["volume"] + args["by"]})
            if name == "audio.toggle_muted":
                ctx.state.update("audio", {"muted": not audio["muted"]})

        return run

    for name in (
        "apps.home",
        "audio.cycle_output",
        "audio.step_volume",
        "audio.toggle_muted",
        "sleep.cycle",
    ):
        ctx.commands.add(name, record(name))
    await service.start()
    yield c
    await service.stop()


def watch(ctx, kind):
    got = []
    sub = ctx.bus.subscribe()

    async def pump():
        async for m in sub:
            if m["type"] == kind:
                got.append(m["data"])

    task = asyncio.ensure_future(pump())
    return got, task, sub


async def test_blue_on_the_launcher(couch):
    assert couch.menu() is None
    assert couch.press("blue") == [True]
    menu = couch.menu()
    assert (menu["host"], menu["title"], menu["focus"], menu["palette"]) == (
        "launcher",
        "Couch",
        "home",
        None,
    )
    assert menu["evening"] is True
    assert [(i["id"], i["value"]) for i in menu["items"]] == [
        ("home", "Home"),
        ("sound", "TV speakers"),
        ("volume", "\u2212 40 +"),
        ("bluetooth", "Bluetooth"),
        ("sleep", "Off"),
    ]
    assert couch.press("right", "right") == [True, True]
    assert couch.menu()["focus"] == "volume"
    assert couch.press("back") == [True]
    assert couch.menu() is None
    assert couch.pages.drawn == []  # nothing for a tab
    assert couch.kodi.calls == []


async def test_keys_go_through_while_its_closed(couch):
    assert couch.press("left", "select", "back") == [False, False, False]
    assert not couch.service.take_key("blue", repeat=True)  # a held Blue doesn't open it
    assert couch.menu() is None


async def test_blue_over_youtube_draws_in_its_tab(couch):
    couch.front("youtube")
    couch.press("blue")
    assert couch.menu()["host"] == "chromium"
    await until(lambda: couch.pages.last("menu") != "none")
    drawn = couch.pages.last("menu")
    assert (drawn["title"], drawn["palette"]) == ("YouTube", "yt")
    couch.press("right")
    await until(lambda: couch.pages.last("menu")["focus"] == "sound")
    couch.press("blue")  # Blue again closes it
    await until(lambda: couch.pages.last("menu") is None)
    assert {t for t, _, _ in couch.pages.drawn} == {"T-yt"}


async def test_a_reload_draws_the_open_menu_again(couch):
    couch.front("youtube")
    couch.press("blue")
    await until(lambda: couch.pages.last("menu") != "none")
    n = len(couch.pages.drawn)
    for listener in couch.pages.listeners:
        listener("T-yt", overlay_page.HELLO)
        listener("T-other", overlay_page.HELLO)
    await until(lambda: len(couch.pages.drawn) == n + 1)
    assert couch.pages.drawn[-1][:2] == ("T-yt", "menu")


async def test_blue_over_plex_asks_kodi_to_draw_it(couch):
    couch.front("plex")
    assert couch.press("blue") == [True]
    await until(lambda: couch.menu() is not None)
    assert couch.menu()["host"] == "kodi"
    await until(lambda: couch.kodi.sent("JSONRPC.NotifyAll"))
    sent = couch.kodi.sent("JSONRPC.NotifyAll")[-1]
    assert (sent["sender"], sent["message"]) == ("couch", "overlay")
    assert sent["data"]["menu"]["title"] == "Plex"
    assert sent["data"]["menu"]["palette"] == "plex"
    couch.press("back")
    await until(lambda: couch.kodi.sent("JSONRPC.NotifyAll")[-1]["data"]["menu"] is None)
    assert couch.homes == 0


async def test_without_service_couch_the_menu_opens_on_the_launcher(couch):
    couch.kodi.hosting = False
    couch.front("plex")
    couch.press("blue")
    await until(lambda: couch.menu() is not None)
    assert couch.homes == 1
    assert couch.menu()["host"] == "launcher"
    assert couch.ctx.state.get("apps")["foreground"] == "launcher"
    assert couch.kodi.sent("JSONRPC.NotifyAll") == []


async def test_volume_card(couch):
    couch.press("blue", "right", "right", "up", "up")
    await until(lambda: len(couch.ran) == 2)
    assert couch.ran == [("audio.step_volume", {"by": 5}), ("audio.step_volume", {"by": 5})]
    await until(lambda: couch.menu()["items"][2]["value"] == "\u2212 50 +")
    couch.press("select")
    await until(lambda: couch.ran[-1] == ("audio.toggle_muted", {}))
    await until(lambda: couch.menu()["items"][2]["value"] == "Muted")


async def test_home_and_bluetooth_go_to_the_launcher(couch):
    screens, task, sub = watch(couch.ctx, "open_screen")
    try:
        couch.front("youtube")
        couch.press("blue", "select")
        await until(lambda: screens, "open_screen")
        assert screens == [{"screen": "home"}]
        assert couch.ran == [("apps.home", {})]
        assert couch.menu() is None
        couch.press("blue", "right", "right", "right", "select")
        await until(lambda: len(screens) == 2)
        assert screens[-1] == {"screen": "bluetooth"}
    finally:
        task.cancel()
        sub.close()


async def test_sound_card_cycles(couch):
    couch.press("blue", "right", "select")
    await until(lambda: couch.ran)
    assert couch.ran == [("audio.cycle_output", {})]
    assert couch.menu() is not None


async def test_sleep_card(couch):
    def sleep_card():
        return couch.menu()["items"][-1]

    couch.press("blue", "right", "right", "right", "right")
    card = sleep_card()
    assert (card["id"], card["label"], card["value"]) == ("sleep", "Sleep", "Off")
    assert card["hint"] == "OK: Off \u00b7 15 \u00b7 30 \u00b7 60 \u00b7 90"
    assert "detail" not in card  # nothing to say while it's off
    couch.press("select")
    await until(lambda: couch.ran)
    assert couch.ran == [("sleep.cycle", {})]
    assert couch.menu() is not None  # it stays open: the card shows the new time
    # couchd/sleep.py's state redraws the open menu.
    couch.ctx.state.update("sleep", {"minutes": 30, "ends_at": "2026-11-16T21:20:00+01:00"})
    await until(lambda: sleep_card()["value"] == "30 min")
    assert sleep_card()["detail"] == "TV off at 21:20"
    couch.ctx.state.update("sleep", {"minutes": None, "ends_at": None})
    await until(lambda: sleep_card()["value"] == "Off")
    assert "detail" not in sleep_card()


async def test_in_the_sleep_timers_last_minute_a_key_only_keeps_the_tv_on(couch):
    ran = []

    async def standby(args):
        ran.append("tv.standby")

    couch.ctx.commands.add("tv.standby", standby)
    couch.ctx.commands.remove_prefix("sleep.")  # the fixture's stand-in: the real one now
    sleep = couch.service.sleep = SleepService(couch.ctx, minute=0.2)
    await sleep.start()
    try:
        sleep.set(2)
        await until(lambda: sleep.warning, "the warning")
        assert couch.press("blue") == [True]
        assert couch.menu() is None  # it didn't open the menu
        assert sleep.minutes is None
        assert couch.press("blue") == [True]  # the next one does
        assert couch.menu() is not None
        await asyncio.sleep(0.3)
        assert ran == []
    finally:
        await sleep.stop()


def test_local_time():
    assert local_time("2026-11-16T21:20:00+01:00") == "21:20"
    assert local_time("nonsense") is None
    assert local_time(None) is None


async def test_the_menu_closes_by_itself(couch):
    couch.press("blue")
    await asyncio.sleep(0.12)
    couch.press("right")  # a key starts the clock again
    await asyncio.sleep(0.12)
    assert couch.menu() is not None
    await until(lambda: couch.menu() is None, "the timeout")


async def test_the_menu_closes_when_the_app_changes(couch):
    couch.front("youtube")
    couch.press("blue")
    couch.front("launcher")
    await until(lambda: couch.menu() is None)


async def test_toasts_go_to_the_app_in_front(couch):
    couch.ctx.bus.toast("Sound now on TV speakers", icon="speaker")  # launcher: its own
    await asyncio.sleep(0.05)
    assert couch.pages.drawn == [] and couch.kodi.calls == []

    couch.front("youtube")
    couch.ctx.bus.toast("Headphones connected", icon="bluetooth")
    await until(lambda: couch.pages.last("toast") != "none")
    assert couch.pages.last("toast") == {
        "text": "Headphones connected",
        "level": "info",
        "icon": "bluetooth",
    }

    couch.front("plex")
    couch.ctx.bus.toast("Couldn't switch", "error")
    await until(lambda: couch.kodi.sent("GUI.ShowNotification"))
    assert couch.kodi.sent("GUI.ShowNotification") == [
        {"title": "Couch", "message": "Couldn't switch", "image": "error", "displaytime": 2400}
    ]


async def test_a_toast_with_a_detail_and_a_time_of_its_own(couch):
    """The sleep timer's warning: a title over a line, for the minute it has left."""
    warning = {"icon": "moon", "detail": "Press any button to stay on", "ms": 60000, "id": "sleep"}
    couch.front("youtube")
    couch.ctx.bus.toast("Turning off in 1 minute", **warning)
    await until(lambda: couch.pages.last("toast") != "none")
    assert couch.pages.last("toast") == {
        "text": "Turning off in 1 minute",
        "level": "info",
        **warning,
    }

    couch.front("plex")
    couch.ctx.bus.toast("Turning off in 1 minute", **warning)
    await until(lambda: couch.kodi.sent("GUI.ShowNotification"))
    assert couch.kodi.sent("GUI.ShowNotification") == [
        {
            "title": "Turning off in 1 minute",
            "message": "Press any button to stay on",
            "image": "info",
            "displaytime": 60000,
        }
    ]


async def test_a_toast_in_kodi_that_isnt_there_is_dropped(couch):
    couch.kodi.up = False
    couch.front("plex")
    couch.ctx.bus.toast("Hello")
    await asyncio.sleep(0.05)  # logged, nothing raised


def volume_changed(ctx, **data):
    ctx.bus.publish("volume_changed", {"output": "tv", "volume": 40, "muted": False, **data})


async def test_the_volume_panel_follows_the_volume(couch):
    panels, task, sub = watch(couch.ctx, "volume_panel")
    try:
        couch.front("youtube")
        volume_changed(couch.ctx, volume=55, source="tv_remote")
        await until(lambda: panels)
        assert panels == [{"output": "tv", "name": "TV speakers", "volume": 55, "muted": False}]
        await until(lambda: couch.pages.last("volume") != "none")
        assert couch.pages.last("volume")["volume"] == 55

        couch.front("plex")
        volume_changed(couch.ctx, output="bt", muted=True, source="command")
        await until(lambda: couch.kodi.sent("JSONRPC.NotifyAll"))
        data = couch.kodi.sent("JSONRPC.NotifyAll")[-1]["data"]
        assert data == {"volume": panels[-1], "ms": 2400}
        assert (data["volume"]["name"], data["volume"]["muted"]) == ("Headphones", True)
    finally:
        task.cancel()
        sub.close()


async def test_no_panel_under_an_open_menu(couch):
    panels, task, sub = watch(couch.ctx, "volume_panel")
    try:
        couch.press("blue", "right", "right")
        couch.ctx.state.update("audio", {"volume": 70})  # the card says it
        volume_changed(couch.ctx, volume=70, source="command")
        await until(lambda: couch.menu()["items"][2]["value"] == "\u2212 70 +")
        await asyncio.sleep(0.05)
        assert panels == []
        couch.ctx.state.update("audio", {"volume": 40})  # a state patch alone: no panel
        couch.press("back")
        await asyncio.sleep(0.05)
        assert panels == []
    finally:
        task.cancel()
        sub.close()


async def test_volume_over_kodi_without_service_couch_is_a_notification(couch):
    couch.kodi.hosting = False
    couch.front("plex")
    volume_changed(couch.ctx, volume=30, source="tv_remote")
    await until(lambda: couch.kodi.sent("GUI.ShowNotification"))
    assert couch.kodi.sent("GUI.ShowNotification")[0]["message"] == "TV speakers: 30"


async def test_commands(couch):
    toasts, task, sub = watch(couch.ctx, "toast")
    try:
        await couch.ctx.commands.run("overlay.toast", {"text": "Updated to v1.2", "icon": "update"})
        await until(lambda: toasts)
        assert toasts == [{"text": "Updated to v1.2", "level": "info", "icon": "update"}]
        for bad in (
            {"text": " "},
            {"text": "x", "level": "loud"},
            {"text": "x", "icon": "cat"},
            {},
        ):
            with pytest.raises(CommandError):
                await couch.ctx.commands.run("overlay.toast", bad)
    finally:
        task.cancel()
        sub.close()
    await couch.ctx.commands.run("overlay.menu", {"open": True})
    assert couch.menu()["host"] == "launcher"
    await couch.ctx.commands.run("overlay.menu", {"open": False})
    assert couch.menu() is None


async def test_a_stopped_service_takes_no_keys(couch):
    await couch.service.stop()
    assert couch.press("blue") == [False]


# --- the page script, and its injection over DevTools -------------------------------------


def test_the_page_script_is_self_contained():
    script = overlay_page.SCRIPT
    assert not re.search(r"^\s*(import|export)\b", script, re.M)
    assert "class OverlayView" in script
    assert "window.top !== window" in script  # not in YouTube's iframes
    assert 'attachShadow({ mode: "closed" })' in script
    assert ":root" not in script  # all of it rewritten for the shadow root
    assert "Couch Display" in script and "Couch Text" in script
    assert youtube.SCRIPT.endswith(overlay_page.SCRIPT)


def test_the_page_uses_its_own_font_names():
    css = json.loads(re.search(r"const CSS = (.*);\n", overlay_page.SCRIPT)[1])
    assert re.findall(r"--font-display:\s*([^,;]+)", css)[-1] == '"Couch Display"'
    assert re.findall(r"--font-text:\s*([^,;]+)", css)[-1] == '"Couch Text"'


def test_shadow_css():
    css = ':root { --a: 1 } :root[data-evening="on"] [data-palette] { x: y }'
    assert overlay_page.shadow_css(css) == (
        ':host { --a: 1 } :host([data-evening="on"]) [data-palette] { x: y }'
    )


def test_call_quotes_its_payload():
    js = overlay_page.call("toast", {"text": 'a "b" </script>'})
    assert js.startswith("(() => { const o = window.__couchOverlay; if (!o) return false;")
    assert json.dumps({"text": 'a "b" </script>'}) in js


@pytest.fixture
async def browser():
    fake = FakeChromium()
    fake.add_tab("http://127.0.0.1:8765/")
    await fake.start()
    yield fake
    await fake.stop()


async def test_a_page_without_the_overlay_gets_it(couch, browser):
    """A tab from before couchd started has no overlay: couchd injects it, then draws."""
    chromium = Chromium(browser.port)
    await chromium.connect()
    try:
        tab = browser.add_tab("https://www.youtube.com/tv")
        couch.service.chromium = chromium
        couch.service.pages = lambda app: tab if app == "youtube" else None
        couch.front("youtube")
        couch.ctx.bus.toast("Hello")
        await until(lambda: browser.tabs[tab].drawn)
        assert browser.tabs[tab].overlay
        assert browser.tabs[tab].drawn == [("toast", {"text": "Hello", "level": "info"})]
    finally:
        await chromium.close()


async def test_the_overlay_survives_a_reload(couch, browser):
    """It's in the tab's PageSetup, so a new document has it and says hello; couchd draws
    the open menu again."""
    chromium = Chromium(browser.port)
    await chromium.connect()
    try:
        tab = browser.add_tab("https://www.youtube.com/tv")
        await chromium.prepare(tab, PageSetup(user_agent="TV", script=youtube.SCRIPT))
        await browser.reload(tab)
        assert browser.tabs[tab].overlay
        couch.service.chromium = chromium
        chromium.on_page_message(couch.service._page_message)
        couch.service.pages = lambda app: tab if app == "youtube" else None
        couch.front("youtube")
        couch.press("blue")
        await until(lambda: browser.tabs[tab].drawn)
        browser.tabs[tab].overlay = False  # gone with the old document...
        browser.tabs[tab].drawn.clear()
        await browser.reload(tab)  # ...and back with the new one
        await until(lambda: browser.tabs[tab].drawn)
        method, menu = browser.tabs[tab].drawn[-1]
        assert (method, menu["host"], menu["title"]) == ("menu", "chromium", "YouTube")
    finally:
        await chromium.close()


# --- Kodi's JSON-RPC ------------------------------------------------------------------------


@pytest.fixture
async def kodi_server():
    """Answers each call, after a notification and split across writes, as Kodi might."""
    got = []

    async def handle(reader, writer):
        request = json.loads((await reader.read(65536)).decode())
        got.append(request)
        if request["method"] == "Nope":
            reply = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"message": "Method not found."},
            }
        elif request["method"] == "XBMC.GetInfoLabels":
            labels = {label: "1" for label in request["params"]["labels"]}
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": labels}
        else:
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": "OK — fine"}
        note = {"jsonrpc": "2.0", "method": "GUI.OnScreensaverDeactivated", "params": {}}
        data = (json.dumps(note) + json.dumps(reply, ensure_ascii=False)).encode()
        dash = data.find("—".encode())
        cut = dash + 1 if dash >= 0 else len(data) // 2  # inside a multi-byte character
        writer.write(data[:cut])
        await writer.drain()
        await asyncio.sleep(0.01)
        writer.write(data[cut:])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    server.got = got
    server.port = server.sockets[0].getsockname()[1]
    async with server:
        yield server


async def test_kodi_rpc(kodi_server):
    kodi = KodiClient(KodiRpc(port=kodi_server.port))
    await kodi.show_notification("Couch", "Sound now on TV", "info", 2400)
    assert kodi_server.got[-1]["method"] == "GUI.ShowNotification"
    assert kodi_server.got[-1]["params"]["displaytime"] == 2400
    assert await kodi.call("JSONRPC.Ping") == "OK — fine"
    assert await kodi.can_host()
    await kodi.notify("overlay", {"menu": None})
    assert kodi_server.got[-1]["params"] == {
        "sender": "couch",
        "message": "overlay",
        "data": {"menu": None},
    }
    with pytest.raises(KodiError, match="Method not found"):
        await kodi.call("Nope")


async def test_no_kodi_is_a_kodi_error(unused_tcp_port):
    kodi = KodiClient(KodiRpc(port=unused_tcp_port))
    with pytest.raises(KodiError):
        await kodi.call("JSONRPC.Ping")
    assert not await kodi.can_host()

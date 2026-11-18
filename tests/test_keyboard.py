"""The on-screen keyboard (#77): its keys on their own, then couchd's overlay service opening it
for a page's text field and typing into the field over DevTools."""

import asyncio
import json
import re
from datetime import datetime

import pytest

from couchd import overlay_page
from couchd.bus import EventBus
from couchd.chromium import Chromium
from couchd.commands import Commands
from couchd.keyboard import Keyboard, Op, layout, read_field
from couchd.overlay import OverlayService
from couchd.services import Context
from couchd.state import LiveState, SavedState
from tests.fake_chromium import OVERLAY_CALL, FakeChromium


def field(**info):
    return read_field({"id": "f.1", "tag": "input", **info})


def keyboard(**info):
    kb = Keyboard()
    kb.show(field(**info), "ytmusic")
    return kb


def label(kb):
    """The focused key's label (or icon), as drawn."""
    view = kb.view()
    if view["focus"] == "reveal":
        return view["reveal"]
    if view["focus"].startswith("chip:"):
        return view["chips"][int(view["focus"][5:])]
    r, c = map(int, view["focus"].split(":"))
    key = view["rows"][r][c]
    return key["label"] or key["icon"]


# --- reading a field ------------------------------------------------------------------------


def test_the_kind_of_field():
    assert field(type="password").kind == "password"
    assert field(type="email").kind == "email"
    assert field(type="text", inputmode="email").kind == "email"
    assert field(type="text", autocomplete="username email").kind == "email"
    assert field(type="text", label="Email or phone").kind == "email"
    assert field(type="text", name="e-mail").kind == "email"
    assert field(type="text", label="Emailing list").kind == "text"
    assert field(type="search").kind == "search"
    assert field(type="text", search=True).kind == "search"
    assert field(type="text", enterkeyhint="search").kind == "search"
    assert field(type="text").kind == "text"
    assert field(tag="textarea").kind == "text"


def test_the_title_and_the_placeholder():
    f = field(type="search", placeholder="Search songs, albums, artists")
    assert (f.title, f.placeholder) == ("Search", "Songs, albums, artists")
    f = field(type="text", label="Display name", placeholder="Display name")
    assert (f.title, f.placeholder) == ("Display name", "")
    assert field(type="text", placeholder="Your city").title == "Your city"
    assert field(type="text", name="first_name").title == "First name"
    assert field(type="text").title == "Text"
    assert field(type="password").title == "Password"
    assert len(field(type="text", label="x" * 500).title) == 60


def test_the_action_key():
    assert field(type="search").action == "Search"
    assert field(type="email").action == "Next"
    assert field(type="password").action == "Done"
    assert field(type="password", enterkeyhint="go").action == "Go"
    assert field(type="password", enterkeyhint="nonsense").action == "Done"
    assert field(type="text").enter
    assert not field(tag="textarea").enter
    assert not field(tag="editable").enter


def test_a_report_without_an_id_is_no_field():
    assert read_field({"type": "text"}) is None
    assert read_field({"id": ""}) is None
    assert read_field("f.1") is None


def test_numbers_start_on_the_123_page():
    assert keyboard(type="tel").page == "123"
    assert keyboard(type="text", inputmode="numeric").page == "123"
    assert keyboard(type="text").page == "abc"


# --- the keys --------------------------------------------------------------------------------


def test_every_row_is_ten_keys_wide():
    for page in ("abc", "123", "#+="):
        for bottom in ("search", "email", "other"):
            for row in layout(page, bottom):
                assert sum(k.w for k in row) == 10, (page, bottom, row)


def test_the_bottom_row_depends_on_the_field():
    def bottom(**info):
        return [k.get("label") or k.get("icon") for k in keyboard(**info).view()["rows"][3]]

    assert bottom(type="search") == ["123", "-", "space", ".", "Search"]
    assert bottom(type="email") == ["123", "@", ".", "@gmail.com", ".com", "Next"]
    assert bottom(type="password") == ["123", "_", "space", ".", "Done"]
    assert bottom(type="text") == ["123", "_", "space", ".", "Done"]


def test_left_and_right_wrap_around_a_row_and_skip_the_gaps():
    kb = keyboard(type="text")
    assert kb.key("left") == (True, [])
    assert label(kb) == "p"
    kb.key("right")
    assert label(kb) == "q"
    kb.key("down")  # the middle row: a half-key gap at each end
    assert label(kb) == "a"
    kb.key("left")
    assert label(kb) == "l"


def test_up_and_down_keep_to_a_column():
    kb = keyboard(type="text")
    for _ in range(4):
        kb.key("right")
    assert label(kb) == "t"
    kb.key("down")
    assert label(kb) == "g"  # the letter below, half a key to the right
    kb.key("down")
    assert label(kb) == "v"
    kb.key("down")
    assert label(kb) == "space"
    kb.key("down")  # no row below: it stays
    assert label(kb) == "space"
    for want in ("v", "g", "t"):
        kb.key("up")
        assert label(kb) == want  # back up the same column, not drifting


def test_typing():
    kb = keyboard(type="text")
    assert kb.key("select") == (True, [Op("insert", "q")])
    assert kb.key("blue") == (True, [])
    assert label(kb) == "Q"
    assert kb.key("select") == (True, [Op("insert", "Q")])
    assert kb.key("select") == (True, [Op("insert", "q")])  # Shift is for one letter
    assert kb.key("red") == (True, [Op("backspace")])
    assert kb.key("7") == (True, [Op("insert", "7")])
    assert kb.key("green") == (True, [])  # taken, nothing to do


def test_the_pages():
    kb = keyboard(type="email")
    for _ in range(3):
        kb.key("down")
    assert label(kb) == "123"
    kb.key("select")
    assert kb.page == "123"
    assert label(kb) == "ABC"
    kb.key("up")
    assert label(kb) == "#+="
    kb.key("select")
    assert kb.page == "#+="
    kb.key("up")
    assert kb.key("select") == (True, [Op("insert", "~")])
    kb.key("blue")  # Shift: back to the letters, for a capital
    assert (kb.page, kb.shift) == ("abc", True)
    for _ in range(4):
        kb.key("right")
    kb.key("down")
    kb.key("down")
    assert label(kb) == "@gmail.com"
    assert kb.key("select") == (True, [Op("insert", "@gmail.com")])


def test_the_action_key_sends_enter_and_closes():
    kb = keyboard(type="search")
    kb.row, kb.col = 3, 4
    assert label(kb) == "Search"
    assert kb.key("select") == (True, [Op("enter"), Op("blur")])
    assert not kb.open
    kb = keyboard(tag="textarea")
    kb.row, kb.col = 3, 4
    assert kb.key("select") == (True, [Op("blur")])  # Enter there would be a new line


def test_back_and_home_close_it_media_keys_go_through():
    kb = keyboard(type="text")
    assert kb.key("play_pause") == (False, [])
    assert kb.open
    assert kb.key("back") == (True, [Op("blur")])
    assert not kb.open
    assert kb.key("left") == (False, [])  # closed: takes nothing
    kb = keyboard(type="text")
    assert kb.key("home") == (False, [Op("blur")])
    assert not kb.open


def test_suggestions():
    kb = keyboard(type="search")
    assert kb.view()["chips"] is None  # the field has no list
    kb.key("up")
    assert kb.focused() == "0:0"  # nothing above the keys
    suggestions = ["lo-fi", "Lo-fi beats", "lo-fi beats", " ", "jazz", "lo-fi radio", "piano"]
    assert kb.mirror({"value": "lo-fi", "caret": 5, "suggestions": suggestions})
    assert kb.view()["chips"] == ["Lo-fi beats", "jazz", "lo-fi radio", "piano"]
    kb.key("up")
    assert kb.focused() == "chip:0"
    kb.key("left")
    assert label(kb) == "piano"
    assert kb.key("select") == (True, [Op("replace", "piano"), Op("enter"), Op("blur")])
    assert not kb.open


def test_suggestions_that_go_away_take_the_focus_back_to_the_keys():
    kb = keyboard(type="search")
    kb.mirror({"value": "a", "suggestions": ["ab", "ac"]})
    kb.key("up")
    kb.key("right")
    assert kb.focused() == "chip:1"
    kb.mirror({"value": "ab", "suggestions": ["abc"]})
    assert kb.focused() == "chip:0"
    kb.mirror({"value": "abz", "suggestions": []})
    assert kb.focused() == "0:0"
    assert kb.view()["chips"] == []  # a list, empty for now


def test_mirror_says_whether_anything_changed():
    kb = keyboard(type="text")
    assert kb.mirror({"value": "hi", "caret": 2})
    assert not kb.mirror({"value": "hi", "caret": 2})
    assert kb.mirror({"value": "hi", "caret": 1})
    kb.mirror({"value": "hi", "caret": 9})
    assert kb.caret is None  # nonsense: at the end
    kb.mirror({"value": 5})
    assert kb.text == ""


def test_a_password_is_dots_until_shown():
    kb = keyboard(type="password")
    kb.mirror({"value": "sofa-time2", "suggestions": ["sofa-time2"]})
    view = kb.view()
    assert view["text"] == "•" * 10
    assert (view["masked"], view["reveal"], view["chips"]) == (True, "Show", None)
    kb.key("up")
    assert kb.focused() == "reveal"
    assert kb.key("select") == (True, [])
    view = kb.view()
    assert (view["text"], view["masked"], view["reveal"]) == ("sofa-time2", False, "Hide")
    kb.key("down")
    assert kb.focused() == "0:0"


def test_the_view_marks_the_special_keys():
    rows = keyboard(type="text").view()["rows"]
    assert rows[1][0] == {"gap": True, "w": 0.5}
    shift, back = rows[2][0], rows[2][-1]
    assert (shift["icon"], shift["dot"], back["icon"], back["dot"]) == (
        "shift",
        "blue",
        "backspace",
        "red",
    )
    done = rows[3][-1]
    assert (done["label"], done["icon"], done["go"]) == ("Done", "go", True)


# --- the service ----------------------------------------------------------------------------

APPS = [
    {"id": "launcher", "name": "Couch", "kind": "launcher"},
    {"id": "youtube", "name": "YouTube", "kind": "chromium"},
    {"id": "ytmusic", "name": "YouTube Music", "kind": "chromium"},
    {"id": "plex", "name": "Plex", "kind": "kodi"},
]
TABS = {"youtube": "T-yt", "ytmusic": "T-ym"}
FIELD_FN = re.compile(r"o\.fields\.(\w+)\((\"[^\"]*\")\)")


async def until(condition, what="condition"):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class Page:
    """A stand-in for Chromium and the page in each tab: one text field, what's typed in it,
    and what the overlay was told to draw."""

    def __init__(self):
        self.drawn: list[tuple[str, str, object]] = []
        self.done: list[tuple[str, str]] = []  # (tab, what): typing, keys, field calls
        self.value = ""
        self.listeners = []

    def on_page_message(self, listener):
        self.listeners.append(listener)

    def says(self, tab, **info):
        """The page in `tab` reports its focused field."""
        for listener in self.listeners:
            listener(tab, overlay_page.FIELD + json.dumps({"id": "f.1", "tag": "input", **info}))

    async def evaluate(self, target, expression):
        if m := FIELD_FN.search(expression):
            self.done.append((target, f"{m[1]} {json.loads(m[2])}"))
            if m[1] == "state":
                return {"id": json.loads(m[2]), "focused": True, "value": self.value}
            if m[1] == "select":
                self.value = ""
            return True
        m = OVERLAY_CALL.match(expression)
        assert m, expression[:80]
        self.drawn.append((target, m[1], json.loads(m[2])))
        return True

    async def insert_text(self, target, text):
        self.done.append((target, f"type {text}"))
        self.value += text

    async def press_key(self, target, name):
        self.done.append((target, f"press {name}"))
        if name == "Backspace":
            self.value = self.value[:-1]

    def last(self, method="keyboard"):
        return next((p for _, m, p in reversed(self.drawn) if m == method), "none")


class Couch:
    def __init__(self, ctx, service, page):
        self.ctx, self.service, self.page = ctx, service, page

    def front(self, app):
        self.ctx.state.update("apps", {"foreground": app})

    def press(self, *keys):
        return [self.service.take_key(k) for k in keys]

    def state(self):
        return self.ctx.state.get("overlay").get("keyboard")


@pytest.fixture
async def couch(tmp_path):
    bus = EventBus()
    ctx = Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())
    ctx.state.update("apps", {"foreground": "ytmusic", "list": APPS})
    ctx.state.update("audio", {"current": None, "outputs": [], "volume": 40, "muted": False})
    page = Page()
    service = OverlayService(
        ctx,
        chromium=page,  # type: ignore[arg-type]
        pages=TABS.get,
        now=lambda: datetime(2026, 11, 16, 12),
    )
    await service.start()
    yield Couch(ctx, service, page)
    await service.stop()


async def test_a_focused_field_opens_it(couch):
    couch.page.says("T-ym", focused=True, type="search", value="lo", suggestions=["lo-fi"])
    assert couch.state() == {"open": True, "app": "ytmusic", "kind": "search", "title": "Search"}
    await until(lambda: couch.page.last() != "none")
    drawn = couch.page.last()
    assert drawn["kicker"] == "YouTube Music"
    assert (drawn["palette"], drawn["text"], drawn["chips"]) == ("yt", "lo", ["lo-fi"])
    assert couch.page.drawn[-1][0] == "T-ym"


async def test_it_takes_the_keys_and_types(couch):
    couch.page.says("T-ym", focused=True, type="text")
    assert couch.press("select", "blue", "right", "select", "red", "4") == [True] * 6
    await until(lambda: len(couch.page.done) >= 5)
    assert [d for _, d in couch.page.done[:4]] == ["type q", "type W", "press Backspace", "type 4"]
    await until(lambda: couch.page.last()["text"] == "q4")  # read back from the field
    assert ("T-ym", "state f.1") in couch.page.done
    assert couch.press("play_pause", "home") == [False, False]  # still the app's, and home's


async def test_back_closes_it_and_the_field_loses_focus(couch):
    couch.page.says("T-ym", focused=True, type="text")
    assert couch.press("back") == [True]
    assert couch.state() is None
    await until(lambda: couch.page.last() is None)
    await until(lambda: ("T-ym", "blur f.1") in couch.page.done)
    assert couch.press("left") == [False]  # the app's again
    couch.page.says("T-ym", focused=True, type="text")  # its blur hasn't arrived yet
    assert couch.state() is None
    couch.page.says("T-ym", id="f.2", focused=True, type="text")  # focused again: a new id
    assert couch.state() is not None


async def test_the_action_key_presses_enter(couch):
    couch.page.says("T-ym", focused=True, type="search")
    couch.press("down", "down", "down", "left", "select")
    assert couch.state() is None
    await until(lambda: len(couch.page.done) == 2)
    assert [d for _, d in couch.page.done] == ["press Enter", "blur f.1"]


async def test_a_suggestion_replaces_the_text_and_searches(couch):
    couch.page.value = "lo"
    couch.page.says("T-ym", focused=True, type="search", value="lo", suggestions=["lo-fi"])
    couch.press("up", "select")
    await until(lambda: len(couch.page.done) == 4)
    assert [d for _, d in couch.page.done] == [
        "select f.1",
        "type lo-fi",
        "press Enter",
        "blur f.1",
    ]
    assert couch.page.value == "lo-fi"


async def test_the_field_losing_focus_closes_it(couch):
    couch.page.says("T-ym", focused=True, type="text")
    couch.page.says("T-ym", id="f.9", focused=False)  # some other field: no matter
    assert couch.state() is not None
    couch.page.says("T-ym", focused=False)
    assert couch.state() is None
    await until(lambda: couch.page.last() is None)
    assert couch.page.done == []  # it has no focus to take


async def test_the_app_changing_closes_it(couch):
    couch.page.says("T-ym", focused=True, type="text")
    couch.front("launcher")
    await until(lambda: couch.state() is None)
    await until(lambda: ("T-ym", "blur f.1") in couch.page.done)


async def test_a_reload_closes_it(couch):
    couch.page.says("T-ym", focused=True, type="text")
    for listener in couch.page.listeners:
        listener("T-ym", overlay_page.HELLO)
    assert couch.state() is None


async def test_not_for_youtube_nor_a_tab_behind(couch):
    couch.front("youtube")
    couch.page.says("T-yt", focused=True, type="search")
    assert couch.state() is None
    couch.front("ytmusic")
    couch.page.says("T-yt", focused=True, type="search")  # YouTube's tab, behind
    assert couch.state() is None
    couch.front("plex")
    couch.page.says("T-ym", focused=True, type="search")
    assert couch.state() is None
    for listener in couch.page.listeners:
        listener("T-ym", overlay_page.FIELD + "{not json")
        listener("T-ym", overlay_page.FIELD + '"f.1"')
    assert couch.page.drawn == []


async def test_it_closes_the_quick_menu(couch):
    assert couch.press("blue") == [True]
    assert couch.ctx.state.get("overlay")["menu"] is not None
    couch.page.says("T-ym", focused=True, type="text")
    assert couch.ctx.state.get("overlay")["menu"] is None
    assert couch.press("blue") == [True]  # Shift now, not the menu
    assert couch.ctx.state.get("overlay")["menu"] is None


async def test_its_state_never_has_the_text(couch):
    couch.page.says("T-ym", focused=True, type="password", label="PIN", value="1234")
    assert couch.state() == {"open": True, "app": "ytmusic", "kind": "password", "title": "PIN"}
    assert "1234" not in json.dumps(couch.ctx.state.get("overlay"))
    await until(lambda: couch.page.last() != "none")
    assert couch.page.last()["text"] == "•" * 4


async def test_a_held_key_repeats_only_the_arrows(couch):
    couch.page.says("T-ym", focused=True, type="text")
    assert couch.service.take_key("right", repeat=True)
    assert couch.service.take_key("select", repeat=True)
    assert couch.service.keyboard.focused() == "0:1"
    await asyncio.sleep(0.05)
    assert couch.page.done == []


# --- over DevTools -----------------------------------------------------------------------------


@pytest.fixture
async def browser():
    fake = FakeChromium()
    fake.add_tab("http://127.0.0.1:8765/")
    await fake.start()
    yield fake
    await fake.stop()


async def test_typing_reaches_the_page_as_input(couch, browser):
    chromium = Chromium(browser.port)
    await chromium.connect()
    try:
        tab = browser.add_tab("https://music.youtube.com/")
        browser.tabs[tab].overlay = True
        couch.service.chromium = chromium
        couch.service.pages = lambda app: tab if app == "ytmusic" else None
        couch.page.says(tab, focused=True, type="search")
        couch.press("select", "right", "select", "red", "right", "select")
        await until(lambda: browser.tabs[tab].value == "qe", "typing")
        couch.press("down", "down", "down", "right", "right", "select")  # Search
        await until(lambda: ("blur", "f.1") in browser.tabs[tab].fields, "the blur")
        assert browser.tabs[tab].keys == [
            ("rawKeyDown", "Backspace"),
            ("keyUp", "Backspace"),
            ("keyDown", "Enter"),
            ("keyUp", "Enter"),
        ]
        assert ("state", "f.1") in browser.tabs[tab].fields
    finally:
        await chromium.close()

"""The remote: key repeat, hold Back for Home, and routing keys to the app in front."""

import asyncio
import itertools

import pytest

from couchd import remote as remote_module
from couchd import uinput
from couchd.apps import AppService
from couchd.audio import AudioService
from couchd.backends import BackendError
from couchd.bus import EventBus
from couchd.chromium import Chromium
from couchd.commands import CommandError, Commands
from couchd.display import FakeDisplay
from couchd.fakes import FakeAudio, FakeKeyboard, FakeTv
from couchd.keys import REMOTE_KEYS
from couchd.remote import APP_KEYMAPS, KEYMAPS, RemoteService, Timing
from couchd.services import Context
from couchd.state import LiveState, SavedState
from tests.fake_chromium import FakeChromium

# Fast enough for tests, far enough apart that the event loop's jitter doesn't matter.
FAST = Timing(delay=0.08, first=0.04, speedup=0.5, fastest=0.01, hold_after=0.15, max_hold=0.6)

APPS = [
    {"id": "launcher", "name": "Home", "kind": "launcher"},
    {"id": "youtube", "name": "YouTube", "kind": "chromium"},
    {"id": "plex", "name": "Plex", "kind": "kodi"},
    {"id": "web", "name": "Web", "kind": "chromium"},
]


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


@pytest.fixture
async def remote(ctx):
    tv, keyboard = FakeTv(speed=0), FakeKeyboard()
    service = RemoteService(ctx, tv, keyboard, timing=FAST)
    await service.start()
    yield service
    await service.stop()


def in_front(ctx, app_id):
    ctx.state.update("apps", {"list": APPS, "foreground": app_id})


def keys(sub):
    """Key messages published so far, oldest first, with their flags."""
    out = []
    while not sub._queue.empty():
        message = sub._queue.get_nowait()
        if message["type"] == "key":
            d = message["data"]
            flags = tuple(f for f in ("repeat", "hold", "overlay", "raw") if d.get(f))
            out.append((d["key"], d["app"], *flags))
    return out


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --- routing ---------------------------------------------------------------------------------


async def test_the_launcher_gets_keys_over_the_websocket_only(ctx, remote):
    in_front(ctx, "launcher")
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("down")
    remote.tv.key_released()
    assert keys(sub) == [("down", "launcher")]
    assert remote.keyboard.typed == []


async def test_no_apps_service_means_the_launcher(ctx, remote):
    sub = ctx.bus.subscribe()
    remote.tv.key_tapped("select")
    assert keys(sub) == [("select", "launcher")]
    assert remote.keyboard.typed == []


@pytest.mark.parametrize(
    ("app", "key", "code"),
    [
        ("youtube", "down", uinput.KEY_DOWN),
        ("youtube", "select", uinput.KEY_ENTER),
        ("youtube", "back", uinput.KEY_ESC),
        ("web", "back", uinput.KEY_BACK),
        ("youtube", "play_pause", uinput.KEY_PLAYPAUSE),
        ("youtube", "7", uinput.KEY_7),
        ("plex", "back", uinput.KEY_BACKSPACE),
        ("plex", "stop", uinput.KEY_STOPCD),
        ("plex", "0", uinput.KEY_0),
    ],
)
async def test_apps_get_keys_typed(ctx, remote, app, key, code):
    in_front(ctx, app)
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed(key)
    remote.tv.key_released()
    assert remote.keyboard.typed == [code]
    # The message still goes out, for couchd's own services, marked as the app's.
    assert keys(sub) == [(key, app)]


@pytest.mark.parametrize("key", ["home", "red", "green", "yellow", "blue"])
async def test_couchd_keys_are_never_typed_into_apps(ctx, remote, key):
    in_front(ctx, "youtube")
    remote.tv.key_tapped(key)
    assert remote.keyboard.typed == []


class Taker:
    """Stands in for the overlay: takes the keys in `takes`."""

    def __init__(self, *takes):
        self.takes = set(takes)
        self.seen = []

    def take_key(self, key, *, repeat=False, hold=False):
        self.seen.append((key, repeat, hold))
        return key in self.takes


async def test_the_overlay_sees_every_key_first(ctx, remote):
    remote.overlay = taker = Taker("blue", "down")
    in_front(ctx, "youtube")
    sub = ctx.bus.subscribe()
    for key in ("blue", "down", "play_pause"):
        remote.tv.key_tapped(key)
    assert taker.seen == [
        ("blue", False, False),
        ("down", False, False),
        ("play_pause", False, False),
    ]
    # Taken keys go to no app, and say so to the launcher.
    assert remote.keyboard.typed == [uinput.KEY_PLAYPAUSE]
    assert keys(sub) == [
        ("blue", "youtube", "overlay"),
        ("down", "youtube", "overlay"),
        ("play_pause", "youtube"),
    ]


async def test_a_taken_key_isnt_typed_into_kodi_either(ctx, remote):
    remote.overlay = Taker("select")
    in_front(ctx, "plex")
    remote.tv.key_pressed("select")
    remote.tv.key_released()
    assert remote.keyboard.typed == []


# --- a screen claims the raw keys (Settings' remote test) -------------------------------


async def test_a_screen_with_the_raw_keys_gets_blue_instead_of_the_overlay(ctx, remote):
    remote.overlay = taker = Taker("blue")
    in_front(ctx, "launcher")
    sub = ctx.bus.subscribe()
    await ctx.commands.run("remote.raw_keys", {"on": True})
    remote.tv.key_tapped("blue")
    remote.tv.key_tapped("red")
    assert taker.seen == []
    assert keys(sub) == [("blue", "launcher", "raw"), ("red", "launcher", "raw")]
    await ctx.commands.run("remote.raw_keys", {"on": False})
    remote.tv.key_tapped("blue")
    assert taker.seen == [("blue", False, False)]
    assert keys(sub) == [("blue", "launcher", "overlay")]


async def test_the_raw_keys_are_only_the_launchers(ctx, remote):
    remote.overlay = Taker("blue")
    await ctx.commands.run("remote.raw_keys", {"on": True})
    in_front(ctx, "youtube")
    sub = ctx.bus.subscribe()
    remote.tv.key_tapped("blue")
    assert keys(sub) == [("blue", "youtube", "overlay")]


async def test_a_claim_on_the_raw_keys_lapses(ctx, remote, monkeypatch):
    monkeypatch.setattr(remote_module, "RAW_KEYS_FOR", 0.1)
    in_front(ctx, "launcher")
    await ctx.commands.run("remote.raw_keys", {"on": True})
    await asyncio.sleep(0.06)
    remote.tv.key_tapped("red")  # a key keeps it alive
    await asyncio.sleep(0.06)
    assert remote.raw_keys
    await asyncio.sleep(0.06)
    assert not remote.raw_keys


async def test_raw_keys_checks_its_args(ctx, remote):
    with pytest.raises(CommandError):
        await ctx.commands.run("remote.raw_keys", {})
    with pytest.raises(CommandError):
        await ctx.commands.run("remote.raw_keys", {"on": "yes"})


async def test_keyboard_presses_stay_in_the_launcher(ctx, remote):
    # couchd thinks YouTube is in front, but the key was typed into the launcher: typing it
    # into "YouTube" would put it straight back into the launcher, and round again.
    in_front(ctx, "youtube")
    sub = ctx.bus.subscribe()
    await ctx.commands.run("remote.press", {"key": "down", "keyboard": True})
    assert keys(sub) == [("down", "launcher")]
    assert remote.keyboard.typed == []


async def test_remote_press_acts_like_the_remote(ctx, remote):
    in_front(ctx, "youtube")
    await ctx.commands.run("remote.press", {"key": "back"})
    assert remote.keyboard.typed == [uinput.KEY_ESC]


async def test_remote_press_checks_its_args(ctx, remote):
    with pytest.raises(CommandError, match="unknown key"):
        await ctx.commands.run("remote.press", {"key": "self_destruct"})
    with pytest.raises(CommandError, match="keyboard"):
        await ctx.commands.run("remote.press", {"key": "up", "keyboard": "yes"})


async def test_unknown_keys_from_the_tv_are_ignored(ctx, remote):
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("self_destruct")
    assert keys(sub) == []


def test_keymaps_only_use_remote_keys():
    for keymap in (*KEYMAPS.values(), *APP_KEYMAPS.values()):
        assert set(keymap) <= REMOTE_KEYS


async def test_without_a_virtual_keyboard_the_launcher_still_works(ctx):
    class Broken(FakeKeyboard):
        async def start(self):
            raise BackendError("no /dev/uinput")

    service = RemoteService(ctx, FakeTv(speed=0), Broken(), timing=FAST)
    await service.start()
    sub = ctx.bus.subscribe()
    in_front(ctx, "youtube")
    service.tv.key_tapped("select")
    assert keys(sub) == [("select", "youtube")]
    assert service.keyboard.typed == []
    await service.stop()


# --- repeat ----------------------------------------------------------------------------------


async def test_a_quick_press_doesnt_repeat(ctx, remote):
    in_front(ctx, "youtube")
    remote.tv.key_pressed("down")
    await asyncio.sleep(FAST.delay / 2)
    remote.tv.key_released()
    await asyncio.sleep(FAST.delay * 2)
    assert remote.keyboard.typed == [uinput.KEY_DOWN]


async def test_held_arrows_repeat_faster_and_faster(ctx, remote):
    in_front(ctx, "youtube")
    loop = asyncio.get_running_loop()
    times = []
    real_tap = remote.keyboard.tap
    remote.keyboard.tap = lambda code: (times.append(loop.time()), real_tap(code))
    remote.tv.key_pressed("down")
    await asyncio.sleep(FAST.delay + 0.2)
    remote.tv.key_released()
    typed = len(remote.keyboard.typed)
    await asyncio.sleep(0.1)
    assert len(remote.keyboard.typed) == typed  # stopped on release
    assert set(remote.keyboard.typed) == {uinput.KEY_DOWN}
    assert typed >= 6
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert gaps[0] == pytest.approx(FAST.delay, abs=0.03)
    assert gaps[1] == pytest.approx(FAST.first, abs=0.02)
    assert gaps[-1] < gaps[1]


async def test_repeats_are_marked_for_the_launcher(ctx, remote):
    in_front(ctx, "launcher")
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("up")
    await asyncio.sleep(FAST.delay + FAST.first / 2)
    remote.tv.key_released()
    assert keys(sub) == [("up", "launcher"), ("up", "launcher", "repeat")]


async def test_the_tvs_own_repeats_are_ignored(ctx, remote):
    in_front(ctx, "youtube")
    for _ in range(3):
        remote.tv.key_pressed("play")
        remote.tv.key_pressed("left")
    remote.tv.key_released()
    # select, left, select, left...: each new button is a new press. The same one again isn't.
    assert len(remote.keyboard.typed) == 6
    remote.keyboard.typed.clear()
    remote.tv.key_pressed("left")
    remote.tv.key_pressed("left")
    remote.tv.key_pressed("left")
    remote.tv.key_released()
    assert remote.keyboard.typed == [uinput.KEY_LEFT]


async def test_select_doesnt_repeat(ctx, remote):
    in_front(ctx, "youtube")
    remote.tv.key_pressed("select")
    await asyncio.sleep(FAST.max_hold / 2)
    remote.tv.key_released()
    assert remote.keyboard.typed == [uinput.KEY_ENTER]


async def test_ok_acts_on_release(ctx, remote):
    in_front(ctx, "launcher")
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("select")
    await settle()
    assert keys(sub) == []
    remote.tv.key_released()
    assert keys(sub) == [("select", "launcher")]


async def test_holding_ok_is_a_long_press(ctx, remote):
    in_front(ctx, "launcher")
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("select")
    await asyncio.sleep(FAST.hold_after + 0.05)
    assert keys(sub) == [("select", "launcher", "hold")]
    remote.tv.key_released()
    assert keys(sub) == []


async def test_a_long_press_in_an_app_is_one_enter(ctx, remote):
    in_front(ctx, "plex")
    remote.tv.key_pressed("select")
    await asyncio.sleep(FAST.hold_after + 0.05)
    remote.tv.key_released()
    assert remote.keyboard.typed == [uinput.KEY_ENTER]


async def test_remote_press_can_hold(ctx, remote):
    sub = ctx.bus.subscribe()
    await ctx.commands.run("remote.press", {"key": "select", "hold": True, "keyboard": True})
    await ctx.commands.run("remote.press", {"key": "back", "hold": True})
    assert keys(sub) == [("select", "launcher", "hold"), ("home", "launcher")]
    with pytest.raises(CommandError, match="can be held"):
        await ctx.commands.run("remote.press", {"key": "up", "hold": True})


async def test_a_lost_release_stops_the_repeat_eventually(ctx, remote):
    in_front(ctx, "youtube")
    remote.tv.key_pressed("down")
    await asyncio.sleep(FAST.max_hold + 0.1)
    typed = len(remote.keyboard.typed)
    await asyncio.sleep(0.1)
    assert len(remote.keyboard.typed) == typed
    # And the same button works again without a release in between.
    remote.tv.key_pressed("down")
    assert len(remote.keyboard.typed) == typed + 1
    remote.tv.key_released()


# --- Back and Home ---------------------------------------------------------------------------


async def test_back_acts_on_release(ctx, remote):
    in_front(ctx, "plex")
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("back")
    await settle()
    assert keys(sub) == []
    remote.tv.key_released()
    assert keys(sub) == [("back", "plex")]
    assert remote.keyboard.typed == [uinput.KEY_BACKSPACE]


async def test_holding_back_is_home(ctx, remote):
    in_front(ctx, "youtube")
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("back")
    await asyncio.sleep(FAST.hold_after + 0.05)
    assert keys(sub) == [("home", "youtube")]
    remote.tv.key_released()
    assert keys(sub) == []
    assert remote.keyboard.typed == []


async def test_another_button_during_back_ends_it(ctx, remote):
    in_front(ctx, "launcher")
    sub = ctx.bus.subscribe()
    remote.tv.key_pressed("back")
    remote.tv.key_pressed("down")
    remote.tv.key_released()
    assert keys(sub) == [("back", "launcher"), ("down", "launcher")]


async def test_a_tap_ends_a_hold(ctx, remote):
    in_front(ctx, "youtube")
    remote.tv.key_pressed("down")
    remote.tv.key_tapped("pause")  # the TV went to standby
    await asyncio.sleep(FAST.delay * 2)
    assert remote.keyboard.typed == [uinput.KEY_DOWN, uinput.KEY_PAUSECD]


# --- with the app manager and audio ---------------------------------------------------------


async def test_held_back_takes_you_home_and_red_moves_the_sound(ctx, remote):
    browser = FakeChromium()
    browser.add_tab("http://127.0.0.1:8765/")
    await browser.start()
    audio = AudioService(ctx, FakeAudio(speed=0))
    await audio.start()
    apps = AppService(
        ctx,
        FakeDisplay(speed=0),
        Chromium(browser.port),
        launcher_url="http://127.0.0.1:8765/",
        opening_delay=0,
    )
    await apps.start()
    try:
        await until(lambda: ctx.state.get("apps")["browser"] == "connected")
        await apps.launch("plex")
        assert ctx.state.get("apps")["foreground"] == "plex"
        remote.tv.key_pressed("red")
        remote.tv.key_released()
        await until(lambda: ctx.state.get("audio")["current"] == "jack")
        remote.tv.key_pressed("back")
        await asyncio.sleep(FAST.hold_after + 0.05)
        remote.tv.key_released()
        await until(lambda: ctx.state.get("apps")["foreground"] == "launcher")
        assert remote.keyboard.typed == []
    finally:
        await apps.stop()
        await audio.stop()
        await browser.stop()


async def until(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def test_remote_hold_holds_a_button(ctx, remote):
    in_front(ctx, "youtube")
    await ctx.commands.run("remote.hold", {"key": "down", "seconds": FAST.delay + 0.1})
    assert len(remote.keyboard.typed) >= 3
    typed = len(remote.keyboard.typed)
    await asyncio.sleep(0.1)
    assert len(remote.keyboard.typed) == typed  # released
    await ctx.commands.run("remote.hold", {"key": "back", "seconds": FAST.hold_after + 0.05})
    assert ctx.state.get("apps")["foreground"] == "youtube"  # no apps service here
    with pytest.raises(CommandError, match="seconds"):
        await ctx.commands.run("remote.hold", {"key": "down", "seconds": 99})
    with pytest.raises(CommandError, match="seconds"):
        await ctx.commands.run("remote.hold", {"key": "down"})

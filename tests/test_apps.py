"""The app manager against a pretend Chromium (real DevTools over a socket) and pretend Kodi."""

import asyncio

import pytest

from couchd import apps as apps_module
from couchd import youtube
from couchd.apps import AppService, CrashLimiter
from couchd.bus import EventBus
from couchd.chromium import Chromium
from couchd.commands import CommandError, Commands
from couchd.display import FakeDisplay
from couchd.services import Context
from couchd.state import LiveState, SavedState
from tests.fake_chromium import FakeChromium

LAUNCHER = "http://127.0.0.1:8765/"
YOUTUBE = "https://www.youtube.com/"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(apps_module, "RETRY_MIN", 0.01)
    monkeypatch.setattr(apps_module, "RETRY_MAX", 0.05)
    monkeypatch.setattr(apps_module, "BROWSER_WAIT", 2.0)


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


@pytest.fixture
async def browser():
    fake = FakeChromium()
    fake.add_tab(LAUNCHER)
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def display():
    return FakeDisplay(speed=0)


async def make_service(ctx, browser, display, **kwargs):
    service = AppService(
        ctx, display, Chromium(browser.port), launcher_url=LAUNCHER, opening_delay=0, **kwargs
    )
    await service.start()
    await until(lambda: ctx.state.get("apps")["browser"] == "connected")
    return service


@pytest.fixture
async def apps(ctx, browser, display):
    service = await make_service(ctx, browser, display)
    yield service
    await service.stop()


async def until(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


def state(ctx):
    return ctx.state.get("apps")


async def run(ctx, name, **args):
    return await ctx.commands.run(name, args)


# --- the launcher and YouTube --------------------------------------------------------------


async def test_attaches_to_the_launcher(ctx, apps, browser):
    s = state(ctx)
    assert s["foreground"] == "launcher"
    assert s["running"] == ["launcher"]
    assert [a["id"] for a in s["list"]] == ["launcher", "youtube", "plex"]
    assert s["error"] is None and s["opening"] is None
    assert "Target.setDiscoverTargets" in browser.calls


async def test_launch_opens_a_tab_and_brings_it_back_later(ctx, apps, browser):
    await run(ctx, "apps.launch", app="youtube")
    youtube = browser.tab_for(YOUTUBE)
    assert youtube is not None and browser.active == youtube.id
    assert state(ctx)["foreground"] == "youtube"
    assert state(ctx)["running"] == ["launcher", "youtube"]

    await run(ctx, "apps.home")
    assert browser.active == browser.tab_for(LAUNCHER).id
    assert youtube.paused == 1  # its video stops when it leaves the screen
    assert state(ctx)["running"] == ["launcher", "youtube"]

    await run(ctx, "apps.launch", app="youtube")
    assert browser.active == youtube.id
    assert len(browser.tabs) == 2  # same tab, not a new one


async def test_close_returns_to_the_launcher(ctx, apps, browser):
    await run(ctx, "apps.launch", app="youtube")
    await run(ctx, "apps.close", app="youtube")
    assert browser.tab_for(YOUTUBE) is None
    assert browser.active == browser.tab_for(LAUNCHER).id
    assert state(ctx)["foreground"] == "launcher"
    assert state(ctx)["running"] == ["launcher"]


async def test_bad_commands(ctx, apps):
    with pytest.raises(CommandError, match="unknown app"):
        await run(ctx, "apps.launch", app="netflix")
    with pytest.raises(CommandError, match="can't be closed"):
        await run(ctx, "apps.close", app="launcher")
    with pytest.raises(CommandError, match="'app' must be a string"):
        await run(ctx, "apps.launch")


async def test_home_key_goes_home(ctx, apps, browser):
    await run(ctx, "apps.launch", app="youtube")
    ctx.bus.publish("key", {"key": "home"})
    await until(lambda: state(ctx)["foreground"] == "launcher")
    assert browser.active == browser.tab_for(LAUNCHER).id


async def test_pause_key_pauses_the_app_in_front(ctx, apps, browser):
    ctx.bus.publish("key", {"key": "pause"})  # on the launcher: nothing to pause
    await asyncio.sleep(0.05)
    await run(ctx, "apps.launch", app="youtube")
    ctx.bus.publish("key", {"key": "pause"})
    youtube = browser.tab_for(YOUTUBE)
    await until(lambda: youtube.paused == 1)
    assert state(ctx)["foreground"] == "youtube"  # it stays on screen
    assert browser.tab_for(LAUNCHER).paused == 0


async def test_youtube_gets_a_tv_user_agent_and_its_script(ctx, apps, browser):
    await run(ctx, "apps.launch", app="youtube")
    tab = browser.tab_for(YOUTUBE)
    assert tab.url == youtube.URL
    assert tab.user_agent == youtube.USER_AGENT and "Tizen" in tab.user_agent
    assert tab.scripts == [youtube.SCRIPT]
    assert "isTypeSupported" in youtube.SCRIPT and "window.close" in youtube.SCRIPT


async def test_leaving_youtube_goes_home_and_keeps_the_tab(ctx, apps, browser):
    await run(ctx, "apps.launch", app="youtube")
    tab = browser.tab_for(YOUTUBE)
    await browser.page_calls(tab.id, "something else")  # ignored
    await asyncio.sleep(0.05)
    assert state(ctx)["foreground"] == "youtube"

    await browser.page_calls(tab.id, "home")
    await until(lambda: state(ctx)["foreground"] == "launcher")
    assert browser.active == browser.tab_for(LAUNCHER).id
    assert state(ctx)["running"] == ["launcher", "youtube"]

    # From the background it can't take anyone home.
    await browser.page_calls(tab.id, "home")
    await asyncio.sleep(0.05)
    assert state(ctx)["foreground"] == "launcher"


async def test_a_restarted_couchd_sets_youtube_up_again(ctx, browser, display):
    tab_id = browser.add_tab(YOUTUBE + "tv#/watch")
    service = await make_service(ctx, browser, display)
    try:
        assert browser.tabs[tab_id].user_agent == youtube.USER_AGENT
        await browser.page_calls(tab_id, "home")
        await until(lambda: state(ctx)["foreground"] == "launcher")
    finally:
        await service.stop()


async def test_a_crashed_tab_goes_home_with_an_error(ctx, apps, browser):
    await run(ctx, "apps.launch", app="youtube")
    with ctx.bus.subscribe() as sub:
        await browser.crash(browser.tab_for(YOUTUBE).id)
        await until(lambda: state(ctx)["foreground"] == "launcher")
        toasts = [m async for m in _drain(sub) if m["type"] == "toast"]
    assert state(ctx)["error"] == {"app": "youtube", "message": "YouTube stopped unexpectedly"}
    assert state(ctx)["running"] == ["launcher"]
    assert browser.tab_for(YOUTUBE) is None
    assert toasts == [
        {"type": "toast", "data": {"text": "YouTube stopped unexpectedly", "level": "error"}}
    ]

    await run(ctx, "apps.clear_error")
    assert state(ctx)["error"] is None


async def test_crash_loops_are_capped(ctx, apps, browser):
    for _ in range(3):
        await run(ctx, "apps.launch", app="youtube")
        await browser.crash(browser.tab_for(YOUTUBE).id)
        await until(lambda: "youtube" not in state(ctx)["running"])
    with pytest.raises(CommandError, match="keeps stopping"):
        await run(ctx, "apps.launch", app="youtube")


async def test_a_crashed_launcher_is_reopened(ctx, apps, browser):
    await browser.crash(browser.tab_for(LAUNCHER).id)
    await until(lambda: browser.tab_for(LAUNCHER) is not None and len(browser.calls) > 0)
    await until(lambda: state(ctx)["running"] == ["launcher"])
    assert browser.active == browser.tab_for(LAUNCHER).id


async def test_losing_the_browser_under_an_app(ctx, apps, browser):
    await run(ctx, "apps.launch", app="youtube")
    await browser.kill()
    await until(lambda: state(ctx)["browser"] == "disconnected")
    assert state(ctx)["foreground"] == "launcher"
    assert state(ctx)["error"]["app"] == "youtube"

    # systemd restarts the session, which opens the launcher again
    browser.add_tab(LAUNCHER)
    browser.up = True
    await until(lambda: state(ctx)["browser"] == "connected")
    assert state(ctx)["running"] == ["launcher"]
    await run(ctx, "apps.launch", app="youtube")
    assert state(ctx)["foreground"] == "youtube"


async def test_adopts_the_tab_already_in_front(ctx, browser, display):
    # couchd restarted while YouTube was on screen: stay there.
    browser.add_tab(YOUTUBE + "tv#/watch")
    service = await make_service(ctx, browser, display)
    try:
        assert state(ctx)["foreground"] == "youtube"
        assert state(ctx)["running"] == ["launcher", "youtube"]
    finally:
        await service.stop()


async def test_opens_the_launcher_if_its_tab_is_missing(ctx, browser, display):
    browser.tabs.clear()
    service = await make_service(ctx, browser, display)
    try:
        assert browser.tab_for(LAUNCHER) is not None
        assert state(ctx)["running"] == ["launcher"]
    finally:
        await service.stop()


async def test_waits_for_a_browser_that_starts_later(ctx, browser, display):
    browser.up = False
    service = AppService(ctx, display, Chromium(browser.port), launcher_url=LAUNCHER)
    await service.start()
    try:
        await asyncio.sleep(0.05)
        assert state(ctx)["browser"] == "disconnected"
        browser.up = True
        await until(lambda: state(ctx)["browser"] == "connected")
    finally:
        await service.stop()


# --- Plex (Kodi) ---------------------------------------------------------------------------


async def test_plex_takes_the_screen_and_gives_it_back(ctx, apps, browser, display):
    seen = []
    with ctx.bus.subscribe() as sub:
        await run(ctx, "apps.launch", app="plex")
        seen = [m["data"]["apps"] async for m in _drain(sub) if "apps" in (m["data"] or {})]
    assert any(s.get("opening") == "plex" for s in seen)  # the Opening screen showed
    assert display.kodi_running
    assert state(ctx)["foreground"] == "plex"
    assert "plex" in state(ctx)["running"]
    assert state(ctx)["opening"] is None

    await run(ctx, "apps.launch", app="plex")  # already up: nothing happens
    assert display.kodi_starts == 1

    await run(ctx, "apps.dev.kodi_exit")
    await until(lambda: state(ctx)["foreground"] == "launcher")
    assert state(ctx)["error"] is None
    assert "plex" not in state(ctx)["running"]


async def test_kodi_crashing_shows_an_error(ctx, apps, display):
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.dev.kodi_exit", crashed=True)
    await until(lambda: state(ctx)["foreground"] == "launcher")
    assert state(ctx)["error"] == {"app": "plex", "message": "Plex stopped unexpectedly"}


async def test_kodi_failing_to_start(ctx, apps, display):
    display.fail_next_start = True
    with pytest.raises(CommandError, match="Plex didn't start"):
        await run(ctx, "apps.launch", app="plex")
    assert state(ctx)["foreground"] == "launcher"
    assert state(ctx)["error"]["app"] == "plex"
    assert state(ctx)["opening"] is None


async def test_home_and_youtube_from_plex(ctx, apps, browser, display):
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.home")
    assert not display.kodi_running
    assert state(ctx)["foreground"] == "launcher"
    assert state(ctx)["error"] is None  # we stopped it; that's no crash

    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.launch", app="youtube")
    assert not display.kodi_running
    assert state(ctx)["foreground"] == "youtube"
    assert browser.active == browser.tab_for(YOUTUBE).id


async def test_the_done_when_loop(ctx, apps, browser, display):
    """YouTube → Home → Plex → exit Plex → launcher, repeatedly."""
    for _ in range(5):
        await run(ctx, "apps.launch", app="youtube")
        assert browser.active == browser.tab_for(YOUTUBE).id
        await run(ctx, "apps.home")
        assert browser.active == browser.tab_for(LAUNCHER).id
        await run(ctx, "apps.launch", app="plex")
        assert state(ctx)["foreground"] == "plex"
        await run(ctx, "apps.dev.kodi_exit")
        await until(lambda: state(ctx)["foreground"] == "launcher")
    assert state(ctx)["error"] is None
    assert display.kodi_starts == 5
    assert len(browser.tabs) == 2


async def test_dev_kodi_exit_needs_kodi(ctx, apps):
    with pytest.raises(CommandError, match="isn't running"):
        await run(ctx, "apps.dev.kodi_exit")


async def test_adopts_a_running_kodi(ctx, browser, display):
    display.kodi_running = True
    service = AppService(ctx, display, Chromium(browser.port), launcher_url=LAUNCHER)
    await service.start()
    try:
        assert state(ctx)["foreground"] == "plex"
        assert state(ctx)["running"] == ["plex"]
        await asyncio.sleep(0.05)
        assert state(ctx)["browser"] == "disconnected"  # Kodi has the screen; no browser
        display.exit_kodi(crashed=False)
        await until(lambda: state(ctx)["browser"] == "connected")
        assert state(ctx)["foreground"] == "launcher"
    finally:
        await service.stop()


async def test_commands_go_away_on_stop(ctx, browser, display):
    service = await make_service(ctx, browser, display)
    await service.stop()
    assert not [n for n in ctx.commands.names() if n.startswith("apps.")]


def test_crash_limiter_forgets_old_crashes():
    now = [0.0]
    limiter = CrashLimiter(limit=2, window=10, clock=lambda: now[0])
    limiter.record("x")
    limiter.record("x")
    assert limiter.too_many("x")
    assert not limiter.too_many("y")
    now[0] = 11
    assert not limiter.too_many("x")


async def _drain(sub):
    """Messages already queued on a subscription."""
    await asyncio.sleep(0.05)
    while not sub._queue.empty():
        message = await sub.get()
        if message is None:
            return
        yield message

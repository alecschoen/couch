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
from couchd.kodi_rpc import KodiRpc
from couchd.network import FakeConnectivity
from couchd.services import Context
from couchd.state import LiveState, SavedState
from tests.fake_chromium import FakeChromium
from tests.fake_kodi import FakeKodi

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


async def test_pause_key_pauses_kodi_over_json_rpc(ctx, browser, display):
    kodi = FakeKodi()
    await kodi.start()
    service = await make_service(ctx, browser, display, kodi_rpc=KodiRpc(port=kodi.port))
    try:
        ctx.bus.publish("key", {"key": "pause"})  # on the launcher: Kodi isn't asked
        await asyncio.sleep(0.05)
        assert kodi.calls == []
        await run(ctx, "apps.launch", app="plex")
        ctx.bus.publish("key", {"key": "pause"})
        await until(lambda: kodi.paused == [1])
        assert state(ctx)["foreground"] == "plex"
    finally:
        await service.stop()
        await kodi.stop()


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
    assert state(ctx)["error"] == {
        "app": "youtube",
        "kind": "crashed",
        "message": "YouTube stopped unexpectedly",
    }
    assert state(ctx)["running"] == ["launcher"]
    assert browser.tab_for(YOUTUBE) is None
    assert toasts == []  # the launcher's error screen says it

    await run(ctx, "apps.clear_error")
    assert state(ctx)["error"] is None


async def test_crash_loops_are_capped(ctx, apps, browser):
    for _ in range(3):
        await run(ctx, "apps.launch", app="youtube")
        await browser.crash(browser.tab_for(YOUTUBE).id)
        await until(lambda: "youtube" not in state(ctx)["running"])
    with pytest.raises(CommandError, match="keeps stopping"):
        await run(ctx, "apps.launch", app="youtube")
    assert state(ctx)["error"]["kind"] == "repeated"


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


async def test_leaving_plex_puts_the_waiting_launcher_in_front(ctx, apps, browser, display):
    """The browser session stays up behind Kodi, so coming back is just the launcher's tab."""
    await run(ctx, "apps.launch", app="youtube")
    await run(ctx, "apps.home")
    await run(ctx, "apps.launch", app="plex")
    browser.active = browser.tab_for(YOUTUBE).id  # whatever Chromium had in front meanwhile
    await run(ctx, "apps.dev.kodi_exit")
    await until(lambda: browser.active == browser.tab_for(LAUNCHER).id)
    assert state(ctx)["foreground"] == "launcher"
    assert len(browser.tabs) == 2  # nothing reopened


async def test_a_launcher_that_doesnt_draw_after_plex_is_restarted(
    ctx, apps, browser, display, monkeypatch
):
    monkeypatch.setattr(apps_module, "DRAW_WAIT", 0.1)
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.dev.kodi_exit")
    await until(lambda: state(ctx)["foreground"] == "launcher")
    await asyncio.sleep(0.2)
    assert display.browser_restarts == 0  # it drew

    browser.drawing = False  # cage didn't take the screen back
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.dev.kodi_exit")
    await until(lambda: display.browser_restarts == 1)


async def test_plex_opens_even_if_the_launcher_that_asked_goes_away(ctx, browser):
    # On the Pi, Kodi starting stops the browser, which closes the launcher's WebSocket,
    # which cancels the apps.launch it sent. Kodi came up anyway, with couchd still sending
    # the remote to the launcher that was gone.
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowDisplay(FakeDisplay):
        async def start_kodi(self, on_exit):
            started.set()
            await release.wait()
            await super().start_kodi(on_exit)

    display = SlowDisplay(speed=0)
    service = await make_service(ctx, browser, display)
    try:
        launch = asyncio.create_task(run(ctx, "apps.launch", app="plex"))
        await started.wait()
        launch.cancel()  # the launcher's socket closed
        with pytest.raises(asyncio.CancelledError):
            await launch
        release.set()
        await until(lambda: state(ctx)["foreground"] == "plex")
        assert display.kodi_running
        assert "plex" in state(ctx)["running"]
        assert state(ctx)["opening"] is None
    finally:
        await service.stop()


async def test_kodi_crashing_shows_an_error(ctx, apps, display):
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.dev.kodi_exit", crashed=True)
    await until(lambda: state(ctx)["foreground"] == "launcher")
    assert state(ctx)["error"] == {
        "app": "plex",
        "kind": "crashed",
        "message": "Plex stopped unexpectedly",
    }


async def test_kodi_failing_to_start(ctx, apps, display):
    display.fail_next_start = True
    with pytest.raises(CommandError, match="Plex didn't start"):
        await run(ctx, "apps.launch", app="plex")
    assert state(ctx)["foreground"] == "launcher"
    assert state(ctx)["error"]["app"] == "plex"
    assert state(ctx)["error"]["kind"] == "failed"
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


# --- opening, and the checks before it (#19) ------------------------------------------------


@pytest.fixture
def net():
    return FakeConnectivity(speed=0)


@pytest.fixture
async def checked(ctx, browser, display, net):
    service = await make_service(ctx, browser, display, connectivity=net)
    yield service
    await service.stop()


async def apps_states(ctx, coro):
    """Run `coro`, and return each `apps` patch published meanwhile."""
    with ctx.bus.subscribe() as sub:
        await coro
        return [m["data"]["apps"] async for m in _drain(sub) if "apps" in (m["data"] or {})]


async def test_youtube_loads_behind_the_opening_screen(ctx, checked, browser):
    browser.load_new_tabs = False
    task = asyncio.create_task(run(ctx, "apps.launch", app="youtube"))
    await until(lambda: browser.tab_for(YOUTUBE) is not None)
    await asyncio.sleep(0.3)
    # Opened in the background: the launcher (showing Opening) stays in front while it loads.
    assert browser.active == browser.tab_for(LAUNCHER).id
    assert state(ctx)["opening"] == "youtube"
    browser.tab_for(YOUTUBE).loaded = True
    await task
    assert browser.active == browser.tab_for(YOUTUBE).id
    assert state(ctx)["opening"] is None
    assert state(ctx)["foreground"] == "youtube"


async def test_a_tab_that_never_loads_is_shown_anyway(ctx, checked, browser, monkeypatch):
    monkeypatch.setattr(apps_module, "PAGE_LOAD_WAIT", 0.3)
    browser.load_new_tabs = False
    await run(ctx, "apps.launch", app="youtube")
    assert browser.active == browser.tab_for(YOUTUBE).id


async def test_home_gives_up_on_an_app_that_is_opening(ctx, checked, browser):
    browser.load_new_tabs = False
    task = asyncio.create_task(run(ctx, "apps.launch", app="youtube"))
    await until(lambda: browser.tab_for(YOUTUBE) is not None)
    await run(ctx, "apps.home")
    await task  # returns quietly
    assert state(ctx)["opening"] is None
    assert state(ctx)["foreground"] == "launcher"
    assert browser.active == browser.tab_for(LAUNCHER).id
    assert state(ctx)["error"] is None
    # The tab stays open behind the launcher, so the next launch is instant.
    assert "youtube" in state(ctx)["running"]


async def test_no_internet_shows_an_error_instead_of_youtube(ctx, checked, browser, net):
    net.online = False
    with pytest.raises(CommandError, match="needs the internet"):
        await run(ctx, "apps.launch", app="youtube")
    assert state(ctx)["error"] == {
        "app": "youtube",
        "kind": "offline",
        "message": "YouTube needs the internet",
    }
    assert state(ctx)["opening"] is None
    assert browser.tab_for(YOUTUBE) is None  # no tab with an error page in it
    assert browser.active == browser.tab_for(LAUNCHER).id

    net.online = True  # Try again
    seen = await apps_states(ctx, run(ctx, "apps.launch", app="youtube"))
    assert any(s.get("opening") == "youtube" for s in seen)
    assert any(s.get("error", "unset") is None for s in seen)
    assert state(ctx)["foreground"] == "youtube"


async def test_an_open_youtube_needs_no_check(ctx, checked, net):
    await run(ctx, "apps.launch", app="youtube")
    await run(ctx, "apps.home")
    net.online = False
    await run(ctx, "apps.launch", app="youtube")  # just back to the front
    assert state(ctx)["foreground"] == "youtube"


async def test_plex_server_unreachable(ctx, checked, display, net):
    net.plex_reach = "unreachable"
    with pytest.raises(CommandError, match="Plex server"):
        await run(ctx, "apps.launch", app="plex")
    assert state(ctx)["error"]["kind"] == "server"
    assert not display.kodi_running
    assert state(ctx)["foreground"] == "launcher"
    assert state(ctx)["opening"] is None

    net.online = False  # the whole network is down: say that instead
    with pytest.raises(CommandError, match="needs the network"):
        await run(ctx, "apps.launch", app="plex")
    assert state(ctx)["error"]["kind"] == "offline"


async def test_plex_opens_when_its_server_is_unknown(ctx, checked, display, net):
    net.plex_reach = "unknown"  # never seen one: let Plex sort it out
    await run(ctx, "apps.launch", app="plex")
    assert display.kodi_running


async def test_home_gives_up_on_plex_before_the_handoff(ctx, checked, display, net):
    net.speed = 1.0
    net.plex_reach = "unreachable"  # slow to answer
    task = asyncio.create_task(run(ctx, "apps.launch", app="plex"))
    await until(lambda: state(ctx)["opening"] == "plex")
    await run(ctx, "apps.home")
    await task
    assert not display.kodi_running
    assert state(ctx)["opening"] is None and state(ctx)["error"] is None


async def test_errors_clear_on_home_and_after_a_while(ctx, apps, browser, monkeypatch):
    await run(ctx, "apps.launch", app="youtube")
    await browser.crash(browser.tab_for(YOUTUBE).id)
    await until(lambda: state(ctx)["error"] is not None)
    await run(ctx, "apps.home")
    assert state(ctx)["error"] is None

    monkeypatch.setattr(apps_module, "ERROR_TTL", 0.05)
    await run(ctx, "apps.launch", app="youtube")
    await browser.crash(browser.tab_for(YOUTUBE).id)
    await until(lambda: state(ctx)["error"] is not None)
    await until(lambda: state(ctx)["error"] is None)


async def test_dev_network_switch(ctx, checked, net):
    await run(ctx, "apps.dev.network", internet=False, plex="unreachable")
    assert not net.online and net.plex_reach == "unreachable"
    with pytest.raises(CommandError, match="must be ok"):
        await run(ctx, "apps.dev.network", plex="maybe")


async def test_dev_crash(ctx, apps, browser, display):
    await run(ctx, "apps.launch", app="youtube")
    await run(ctx, "apps.dev.crash", app="youtube")
    await until(lambda: state(ctx)["error"] is not None)
    assert state(ctx)["error"]["kind"] == "crashed"
    assert state(ctx)["foreground"] == "launcher"
    with pytest.raises(CommandError, match="isn't open"):
        await run(ctx, "apps.dev.crash", app="youtube")
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.dev.crash", app="plex")
    await until(lambda: (state(ctx)["error"] or {}).get("app") == "plex")

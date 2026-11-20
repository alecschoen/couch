"""The app manager against a pretend Chromium (real DevTools over a socket) and pretend Kodi.
The launcher is native: Chromium runs only while YouTube is open, started and stopped through
the display (couch-chromium.service on the Pi)."""

import asyncio

import pytest

from couchd import apps as apps_module
from couchd import youtube, ytmusic
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

YOUTUBE = "https://www.youtube.com/"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(apps_module, "RETRY_MIN", 0.01)
    monkeypatch.setattr(apps_module, "RETRY_MAX", 0.05)
    monkeypatch.setattr(apps_module, "BROWSER_WAIT", 2.0)
    monkeypatch.setattr(apps_module, "SESSION_POLL", 0.02)
    monkeypatch.setattr(apps_module, "DRAW_WAIT", 0.2)


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


@pytest.fixture
async def browser():
    """Not running until the display starts it, as on the Pi."""
    fake = FakeChromium()
    fake.up = False
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def display(browser):
    async def start():
        browser.up = True
        browser.add_tab("about:blank")

    async def stop():
        await browser.kill()

    return FakeDisplay(speed=0, chromium=(start, stop))


async def make_service(ctx, browser, display, **kwargs):
    service = AppService(ctx, display, Chromium(browser.port), opening_delay=0, **kwargs)
    await service.start()
    return service


@pytest.fixture
async def apps(ctx, browser, display):
    service = await make_service(ctx, browser, display)
    yield service
    await service.stop()


async def until(condition):
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


def state(ctx):
    return ctx.state.get("apps")


async def run(ctx, name, **args):
    return await ctx.commands.run(name, args)


# --- Chromium only while YouTube is open ------------------------------------------------


async def test_starts_without_chromium(ctx, apps, display):
    s = state(ctx)
    assert s["foreground"] == "launcher"
    assert s["running"] == ["launcher"]
    assert [a["id"] for a in s["list"]] == ["launcher", "youtube", "ytmusic", "plex"]
    assert s["error"] is None and s["opening"] is None
    await asyncio.sleep(0.05)
    assert display.chromium_starts == 0


async def test_youtube_starts_chromium_and_home_stops_it(ctx, apps, browser, display):
    await run(ctx, "apps.launch", app="youtube")
    assert display.chromium_starts == 1 and display.chromium_up
    youtube = browser.tab_for(YOUTUBE)
    assert youtube is not None and browser.active == youtube.id
    assert browser.focused == youtube.id  # its page has the keyboard, so the remote works
    assert state(ctx)["foreground"] == "youtube"
    assert state(ctx)["running"] == ["launcher", "youtube"]

    await run(ctx, "apps.home")
    assert not display.chromium_up  # its window going shows the launcher
    assert state(ctx)["foreground"] == "launcher"
    assert state(ctx)["running"] == ["launcher"]

    await run(ctx, "apps.launch", app="youtube")  # and again, from scratch
    assert display.chromium_starts == 2
    assert state(ctx)["foreground"] == "youtube"


async def test_youtube_loads_in_the_tab_chromium_starts_with(ctx, apps, browser, display):
    """A new tab costs a renderer: YouTube loads in the blank one Chromium starts on."""
    await run(ctx, "apps.launch", app="youtube")
    assert "Target.createTarget" not in browser.calls
    assert len(browser.tabs) == 1
    youtube = browser.tab_for(YOUTUBE)
    assert youtube is not None and youtube.user_agent  # set up before it loaded


async def test_home_says_launcher_before_chromium_stops(ctx, browser):
    """The native launcher maps over Chromium as soon as it hears, so Home shows at once
    instead of after Chromium's exit."""
    seen = []

    async def start():
        browser.up = True
        browser.add_tab("about:blank")

    async def stop():
        seen.append(state(ctx)["foreground"])
        await browser.kill()

    display = FakeDisplay(speed=0, chromium=(start, stop))
    service = await make_service(ctx, browser, display)
    try:
        await run(ctx, "apps.launch", app="youtube")
        await run(ctx, "apps.home")
        assert seen == ["launcher"]
    finally:
        await service.stop()


async def test_close_stops_chromium(ctx, apps, display):
    await run(ctx, "apps.launch", app="youtube")
    await run(ctx, "apps.close", app="youtube")
    assert not display.chromium_up
    assert state(ctx)["foreground"] == "launcher"


async def test_home_while_opening_stops_chromium(ctx, apps, browser, display):
    browser.load_new_tabs = False  # YouTube never finishes loading
    launch = asyncio.create_task(run(ctx, "apps.launch", app="youtube"))
    await until(lambda: display.chromium_up and browser.tab_for(YOUTUBE) is not None)
    await run(ctx, "apps.home")
    await launch
    assert not display.chromium_up
    assert state(ctx)["foreground"] == "launcher" and state(ctx)["opening"] is None


async def test_chromium_that_never_answers_is_an_error(ctx, apps, browser, display, monkeypatch):
    monkeypatch.setattr(apps_module, "BROWSER_WAIT", 0.2)

    async def start():
        pass  # "starts", but DevTools never answers

    display._chromium = (start, display._chromium[1])
    with pytest.raises(CommandError, match="Couldn't open YouTube"):
        await run(ctx, "apps.launch", app="youtube")
    assert state(ctx)["error"]["kind"] == "failed"
    assert not display.chromium_up


async def test_chromium_dying_under_youtube(ctx, apps, browser, display):
    await run(ctx, "apps.launch", app="youtube")
    await browser.kill()
    await until(lambda: state(ctx)["foreground"] == "launcher")
    assert state(ctx)["error"]["kind"] == "crashed"
    assert not display.chromium_up


async def test_plex_from_youtube_stops_chromium_first(ctx, apps, display):
    await run(ctx, "apps.launch", app="youtube")
    await run(ctx, "apps.launch", app="plex")
    assert not display.chromium_up
    assert display.kodi_running and state(ctx)["foreground"] == "plex"


async def test_back_from_plex_checks_the_launcher_draws(ctx, apps, display):
    drawn = []

    async def launcher():
        # The native launcher: answers a draw check after its next frame.
        with ctx.bus.subscribe() as sub:
            async for message in sub:
                if message["type"] == "draw_check":
                    drawn.append(message["data"]["id"])
                    await run(ctx, "ui.drawn", id=message["data"]["id"])

    task = asyncio.create_task(launcher())
    await asyncio.sleep(0)
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.home")
    assert drawn and display.session_restarts == 0
    task.cancel()


async def test_a_launcher_that_doesnt_draw_after_plex_is_restarted(ctx, apps, display):
    await run(ctx, "apps.launch", app="plex")
    await run(ctx, "apps.home")  # nobody answers the draw check
    assert display.session_restarts == 1


async def test_adopts_youtube_left_running(ctx, browser, display):
    # couchd restarted while YouTube was up: stay there, and set it up again.
    browser.up = True
    tab_id = browser.add_tab(YOUTUBE + "tv#/watch")
    display.chromium_up = True
    service = await make_service(ctx, browser, display)
    try:
        await until(lambda: state(ctx)["foreground"] == "youtube")
        assert display.chromium_up
        assert state(ctx)["running"] == ["launcher", "youtube"]
        assert browser.tabs[tab_id].user_agent == youtube.USER_AGENT
        await browser.page_calls(tab_id, "home")
        await until(lambda: state(ctx)["foreground"] == "launcher")
    finally:
        await service.stop()


# --- the launcher and YouTube --------------------------------------------------------------


async def test_bad_commands(ctx, apps):
    with pytest.raises(CommandError, match="unknown app"):
        await run(ctx, "apps.launch", app="netflix")
    with pytest.raises(CommandError, match="can't be closed"):
        await run(ctx, "apps.close", app="launcher")
    with pytest.raises(CommandError, match="'app' must be a string"):
        await run(ctx, "apps.launch")


async def test_home_key_goes_home(ctx, apps, display):
    await run(ctx, "apps.launch", app="youtube")
    ctx.bus.publish("key", {"key": "home"})
    await until(lambda: state(ctx)["foreground"] == "launcher")
    await until(lambda: not display.chromium_up)


async def test_pause_key_pauses_the_app_in_front(ctx, apps, browser):
    ctx.bus.publish("key", {"key": "pause"})  # on the launcher: nothing to pause
    await asyncio.sleep(0.05)
    await run(ctx, "apps.launch", app="youtube")
    ctx.bus.publish("key", {"key": "pause"})
    youtube = browser.tab_for(YOUTUBE)
    await until(lambda: youtube.paused == 1)
    assert state(ctx)["foreground"] == "youtube"  # it stays on screen


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


async def test_youtube_music_is_laid_out_small_and_drawn_big(ctx, apps, browser):
    await run(ctx, "apps.launch", app="ytmusic")
    tab = browser.tab_for(ytmusic.MATCH)
    assert tab.url == ytmusic.URL
    assert tab.user_agent == ""  # the desktop site, as Chromium itself
    assert tab.viewport == (1280, 720, 1.5)
    assert tab.scripts == [ytmusic.SCRIPT]
    assert "MediaPlayPause" in ytmusic.SCRIPT and "couch-focus" in ytmusic.SCRIPT
    assert state(ctx)["foreground"] == "ytmusic"


async def test_youtube_asking_to_leave_goes_home(ctx, apps, browser, display):
    await run(ctx, "apps.launch", app="youtube")
    tab = browser.tab_for(YOUTUBE)
    await browser.page_calls(tab.id, "something else")  # ignored
    await asyncio.sleep(0.05)
    assert state(ctx)["foreground"] == "youtube"

    await browser.page_calls(tab.id, "home")
    await until(lambda: state(ctx)["foreground"] == "launcher")
    await until(lambda: not display.chromium_up)
    assert state(ctx)["running"] == ["launcher"]


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
        assert not display.chromium_up
        await run(ctx, "apps.launch", app="plex")
        assert state(ctx)["foreground"] == "plex"
        await run(ctx, "apps.dev.kodi_exit")
        await until(lambda: state(ctx)["foreground"] == "launcher")
    assert state(ctx)["error"] is None
    assert display.kodi_starts == 5
    assert display.chromium_starts == 5


async def test_dev_kodi_exit_needs_kodi(ctx, apps):
    with pytest.raises(CommandError, match="isn't running"):
        await run(ctx, "apps.dev.kodi_exit")


async def test_adopts_a_running_kodi(ctx, browser, display):
    display.kodi_running = True
    service = await make_service(ctx, browser, display)
    try:
        assert state(ctx)["foreground"] == "plex"
        assert state(ctx)["running"] == ["launcher", "plex"]
        display.exit_kodi(crashed=False)
        await until(lambda: state(ctx)["foreground"] == "launcher")
        assert not display.chromium_up
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
    # The launcher (showing Opening) stays in front while it loads.
    assert state(ctx)["foreground"] == "launcher"
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


async def test_no_internet_shows_an_error_instead_of_youtube(ctx, checked, display, net):
    net.online = False
    with pytest.raises(CommandError, match="needs the internet"):
        await run(ctx, "apps.launch", app="youtube")
    assert state(ctx)["error"] == {
        "app": "youtube",
        "kind": "offline",
        "message": "YouTube needs the internet",
    }
    assert state(ctx)["opening"] is None
    assert display.chromium_starts == 0  # no Chromium with an error page in it

    net.online = True  # Try again
    seen = await apps_states(ctx, run(ctx, "apps.launch", app="youtube"))
    assert any(s.get("opening") == "youtube" for s in seen)
    assert any(s.get("error", "unset") is None for s in seen)
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

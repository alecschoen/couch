"""The DevTools client on its own."""

import asyncio
import socket

import pytest

from couchd.chromium import BINDING, CdpError, Chromium, PageSetup
from tests.fake_chromium import FakeChromium


@pytest.fixture
async def browser():
    fake = FakeChromium()
    fake.add_tab("http://127.0.0.1:8765/")
    await fake.start()
    yield fake
    await fake.stop()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_no_browser_is_a_cdp_error():
    chromium = Chromium(free_port())
    with pytest.raises(CdpError, match="no browser"):
        await chromium.connect()
    assert not chromium.connected
    with pytest.raises(CdpError, match="not connected"):
        await chromium.pages()


async def test_tabs(browser):
    chromium = Chromium(browser.port)
    await chromium.connect()
    try:
        pages = await chromium.pages()
        assert [p.url for p in pages] == ["http://127.0.0.1:8765/"]  # no service workers
        tab = await chromium.open("https://www.youtube.com/tv")
        assert browser.active == tab
        await chromium.activate(pages[0].id)
        assert browser.active == pages[0].id
        assert await chromium.evaluate(tab, "document.visibilityState") == "hidden"
        with pytest.raises(CdpError, match="ReferenceError"):
            await chromium.evaluate(tab, "nope()")
        with pytest.raises(CdpError, match="no such target"):
            await chromium.activate("T999")
        await chromium.close_page(tab)
        assert tab not in browser.tabs
    finally:
        await chromium.close()


async def test_reports_tabs_that_go(browser):
    chromium = Chromium(browser.port)
    gone: list[tuple[str, bool]] = []
    chromium.on_page_gone(lambda target, crashed: gone.append((target, crashed)))
    await chromium.connect()
    try:
        tab = await chromium.open("https://www.youtube.com/tv")
        await browser.crash(tab)
        await chromium.close_page(tab)
        await asyncio.sleep(0.05)
        assert gone == [(tab, True), (tab, False)]
    finally:
        await chromium.close()


async def test_a_tab_set_up_before_it_loads(browser):
    chromium = Chromium(browser.port)
    messages: list[tuple[str, str]] = []
    chromium.on_page_message(lambda target, message: messages.append((target, message)))
    await chromium.connect()
    try:
        setup = PageSetup(user_agent="TV/1.0", script="window.x = 1")
        tab_id = await chromium.open("https://www.youtube.com/tv", setup)
        tab = browser.tabs[tab_id]
        assert tab.url == "https://www.youtube.com/tv"
        assert tab.user_agent == "TV/1.0"
        assert tab.scripts == ["window.x = 1"]
        assert tab.bindings == {BINDING}
        # It went to about:blank first and was set up before navigating.
        calls = browser.calls
        assert calls.index("Emulation.setUserAgentOverride") < calls.index("Page.navigate")

        # Short-lived sessions (evaluate) come and go without undoing the setup.
        assert await chromium.evaluate(tab_id, "document.visibilityState") == "visible"
        assert tab.user_agent == "TV/1.0"

        await browser.page_calls(tab_id, "home")
        await asyncio.sleep(0.05)
        assert messages == [(tab_id, "home")]

        await chromium.prepare(tab_id, setup)  # already set up: nothing more
        assert tab.scripts == ["window.x = 1"]
    finally:
        await chromium.close()


async def test_setting_up_again_after_reconnecting(browser):
    tab_id = browser.add_tab("https://www.youtube.com/tv")
    tab = browser.tabs[tab_id]
    setup = PageSetup(user_agent="TV/1.0", script="s")
    first = Chromium(browser.port)
    await first.connect()
    await first.prepare(tab_id, setup)
    await first.close()
    await asyncio.sleep(0.05)
    assert tab.user_agent == ""  # the setup went with the session

    second = Chromium(browser.port)
    messages: list[tuple[str, str]] = []
    second.on_page_message(lambda target, message: messages.append((target, message)))
    await second.connect()
    try:
        await second.prepare(tab_id, setup)
        assert tab.user_agent == "TV/1.0"
        await browser.page_calls(tab_id, "home")
        await asyncio.sleep(0.05)
        assert messages == [(tab_id, "home")]
    finally:
        await second.close()


async def test_a_tab_that_cant_be_set_up_is_closed(browser):
    chromium = Chromium(browser.port)
    await chromium.connect()
    try:
        original = browser._handle

        async def refuse_user_agents(method, params, raw, ws=None):
            if method == "Emulation.setUserAgentOverride":
                return {"error": {"code": -32000, "message": "nope"}}
            return await original(method, params, raw, ws)

        browser._handle = refuse_user_agents
        with pytest.raises(CdpError, match="nope"):
            await chromium.open("https://www.youtube.com/tv", PageSetup(user_agent="TV/1.0"))
        assert [t.url for t in browser.tabs.values()] == ["http://127.0.0.1:8765/"]
    finally:
        await chromium.close()


async def test_notices_the_browser_going_away(browser):
    chromium = Chromium(browser.port)
    await chromium.connect()
    await browser.kill()
    async with asyncio.timeout(2):
        await chromium.wait_closed()
    assert not chromium.connected
    await chromium.close()

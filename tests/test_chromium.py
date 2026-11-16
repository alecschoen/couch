"""The DevTools client on its own."""

import asyncio
import socket

import pytest

from couchd.chromium import CdpError, Chromium
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


async def test_notices_the_browser_going_away(browser):
    chromium = Chromium(browser.port)
    await chromium.connect()
    await browser.kill()
    async with asyncio.timeout(2):
        await chromium.wait_closed()
    assert not chromium.connected
    await chromium.close()

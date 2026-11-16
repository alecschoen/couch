"""Plex's navigation sounds follow the UI sounds setting (couchd/kodi_sounds.py, #49)."""

import asyncio

import pytest

from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.kodi import FakeKodi
from couchd.kodi_sounds import KODI_SETTING, PACK, SETTING, KodiSoundsService
from couchd.services import Context
from couchd.state import LiveState, SavedState

APPS = [
    {"id": "launcher", "name": "Home", "kind": "launcher"},
    {"id": "youtube", "name": "YouTube", "kind": "chromium"},
    {"id": "plex", "name": "Plex", "kind": "kodi"},
]


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    ctx = Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())
    ctx.state.update("apps", {"list": APPS, "foreground": "launcher"})
    ctx.state.update("audio", {"current": "tv"})
    return ctx


@pytest.fixture
async def kodi_sounds(ctx):
    kodi = FakeKodi()
    service = KodiSoundsService(ctx, kodi, retry=0.01)
    await service.start()
    yield kodi
    await service.stop()


def sets(kodi):
    sent = kodi.sent("Settings.SetSettingValue")
    return [p["value"] for p in sent if p["setting"] == KODI_SETTING]


async def until(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_nothing_is_sent_while_kodi_is_not_in_front(ctx, kodi_sounds):
    ctx.settings.set(SETTING, "off")
    ctx.state.update("audio", {"current": "jack"})
    await settle()
    assert kodi_sounds.calls == []


async def test_plex_gets_the_sounds_when_it_comes_to_the_front(ctx, kodi_sounds):
    ctx.state.update("apps", {"foreground": "plex"})
    await until(lambda: sets(kodi_sounds) == [PACK])
    ctx.state.update("audio", {"volume": 30})  # nothing that matters changed
    await settle()
    assert sets(kodi_sounds) == [PACK]


async def test_the_setting_turns_them_off_and_on(ctx, kodi_sounds):
    ctx.state.update("apps", {"foreground": "plex"})
    await until(lambda: sets(kodi_sounds) == [PACK])
    ctx.settings.set(SETTING, "off")
    await until(lambda: sets(kodi_sounds) == [PACK, ""])
    ctx.settings.set(SETTING, "on")
    await until(lambda: sets(kodi_sounds) == [PACK, "", PACK])


async def test_tv_only_follows_the_output(ctx, kodi_sounds):
    ctx.settings.set(SETTING, "tv")
    ctx.state.update("apps", {"foreground": "plex"})
    await until(lambda: sets(kodi_sounds) == [PACK])
    ctx.state.update("audio", {"current": "jack"})
    await until(lambda: sets(kodi_sounds) == [PACK, ""])
    ctx.state.update("audio", {"current": "tv"})
    await until(lambda: sets(kodi_sounds) == [PACK, "", PACK])


async def test_kodi_still_starting_is_tried_again(ctx, kodi_sounds):
    kodi_sounds.up = False  # JSON-RPC isn't up yet
    ctx.state.update("apps", {"foreground": "plex"})
    await asyncio.sleep(0.05)
    assert sets(kodi_sounds) == []
    kodi_sounds.up = True
    await until(lambda: sets(kodi_sounds) == [PACK])


async def test_each_kodi_run_is_set_again(ctx, kodi_sounds):
    ctx.state.update("apps", {"foreground": "plex"})
    await until(lambda: sets(kodi_sounds) == [PACK])
    ctx.state.update("apps", {"foreground": "launcher"})  # Kodi quit
    await settle()
    ctx.state.update("apps", {"foreground": "plex"})  # and started again
    await until(lambda: sets(kodi_sounds) == [PACK, PACK])

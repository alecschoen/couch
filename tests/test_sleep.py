"""The sleep timer (#79): the Sleep card's times, the warning a minute before the end, a key
that keeps the TV on, and the TV going to standby at the end (with fake commands: nothing
here turns a TV off)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.services import Context
from couchd.sleep import CANCELLED, MAX_MINUTES, TOAST_ID, WARNING, SleepService, next_choice
from couchd.state import LiveState, SavedState

NOW = datetime(2026, 11, 19, 20, 50, tzinfo=timezone(timedelta(hours=1)))
MINUTE = 0.2  # seconds: a 2 minute timer warns after 0.2 s and ends after 0.4 s


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


async def until(condition, what="condition"):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class Couch:
    """The sleep timer with stand-ins for the commands it runs, and the toasts it sent."""

    def __init__(self, ctx, service):
        self.ctx, self.service = ctx, service
        self.ran: list[str] = []
        self.toasts: list[dict] = []

    def state(self):
        return self.ctx.state.get("sleep")

    def standbys(self):
        return self.ran.count("tv.standby")


@pytest.fixture
async def couch(ctx):
    service = SleepService(ctx, now=lambda: NOW, minute=MINUTE)
    c = Couch(ctx, service)

    def record(name):
        async def run(args):
            c.ran.append(name)

        return run

    for name in ("tv.standby", "idle.activity"):
        ctx.commands.add(name, record(name))
    sub = ctx.bus.subscribe()

    async def pump():
        async for m in sub:
            if m["type"] == "toast":
                c.toasts.append(m["data"])

    task = asyncio.ensure_future(pump())
    await service.start()
    yield c
    await service.stop()
    task.cancel()
    sub.close()


def test_the_cards_times_go_round():
    seen, minutes = [], None
    for _ in range(5):
        minutes = next_choice(minutes)
        seen.append(minutes)
    assert seen == [15, 30, 60, 90, None]
    assert next_choice(20) == 30  # the phone's 20 goes on to the next one up
    assert next_choice(MAX_MINUTES) is None


async def test_off_to_start_with(couch):
    assert couch.state() == {"minutes": None, "ends_at": None, "warning": False}


async def test_the_card_cycles_and_says_when_the_tv_goes_off(couch):
    for want in (15, 30):
        got = await couch.ctx.commands.run("sleep.cycle")
        assert got["minutes"] == want
    assert couch.state() == {
        "minutes": 30,
        "ends_at": "2026-11-19T21:20:00+01:00",  # from now, local time
        "warning": False,
    }
    for want in (60, 90, None):
        await couch.ctx.commands.run("sleep.cycle")
        assert couch.state()["minutes"] == want
    assert couch.state()["ends_at"] is None
    await asyncio.sleep(0.05)
    assert couch.toasts == []  # the card's value is enough


async def test_the_set_command(couch):
    got = await couch.ctx.commands.run("sleep.set", {"minutes": 45})
    assert got == {"minutes": 45, "ends_at": "2026-11-19T21:35:00+01:00", "warning": False}
    for bad in (-1, MAX_MINUTES + 1, "30", True, None):
        with pytest.raises(CommandError):
            await couch.ctx.commands.run("sleep.set", {"minutes": bad})
    assert couch.state()["minutes"] == 45
    await couch.ctx.commands.run("sleep.set", {"minutes": 0})
    assert couch.state() == {"minutes": None, "ends_at": None, "warning": False}


async def test_a_minute_before_the_end_it_warns_then_the_tv_goes_off(couch):
    couch.service.set(2)
    await asyncio.sleep(MINUTE / 2)
    assert not couch.state()["warning"] and couch.toasts == []
    await until(lambda: couch.state()["warning"], "the warning")
    await until(lambda: couch.toasts, "the toast")
    assert couch.toasts == [
        {
            "text": WARNING[0],
            "level": "info",
            "icon": "moon",
            "detail": WARNING[1],
            "ms": round(MINUTE * 1000),  # up for the minute that's left
            "id": TOAST_ID,
        }
    ]
    await until(lambda: "idle.activity" in couch.ran, "the idle clock woken")
    assert couch.standbys() == 0
    await until(lambda: couch.standbys(), "standby")
    assert couch.state() == {"minutes": None, "ends_at": None, "warning": False}
    await asyncio.sleep(MINUTE)
    assert couch.standbys() == 1
    assert len(couch.toasts) == 1  # nothing more to say


async def test_a_key_in_the_last_minute_keeps_the_tv_on(couch):
    couch.service.set(2)
    assert not couch.service.take_key("select")  # not yet: the key goes on as usual
    await until(lambda: couch.state()["warning"], "the warning")
    assert couch.service.take_key("select")  # taken: it does nothing else
    assert couch.state() == {"minutes": None, "ends_at": None, "warning": False}
    await until(lambda: len(couch.toasts) == 2)
    assert couch.toasts[-1] == {"text": CANCELLED, "level": "info", "icon": "moon", "id": TOAST_ID}
    assert not couch.service.take_key("select")  # and the next one goes through
    await asyncio.sleep(MINUTE * 1.5)
    assert couch.standbys() == 0


async def test_home_in_the_last_minute_still_goes_home(couch):
    couch.service.set(2)
    await until(lambda: couch.state()["warning"], "the warning")
    assert not couch.service.take_key("home")
    assert couch.state()["minutes"] is None
    await asyncio.sleep(MINUTE * 1.5)
    assert couch.standbys() == 0


@pytest.mark.parametrize(
    "kind, data",
    [
        ("key", {"key": "down", "raw": True}),  # Settings' remote test has the keys
        ("volume_changed", {"output": "tv", "volume": 30, "muted": False}),
    ],
)
async def test_raw_keys_and_the_volume_keep_it_on_too(couch, kind, data):
    couch.service.set(2)
    await until(lambda: couch.state()["warning"], "the warning")
    couch.ctx.bus.publish(kind, data)
    await until(lambda: couch.state()["minutes"] is None)
    await asyncio.sleep(MINUTE * 1.5)
    assert couch.standbys() == 0
    assert couch.toasts[-1]["text"] == CANCELLED


async def test_keys_before_the_last_minute_leave_it_alone(couch):
    couch.service.set(30)
    couch.ctx.bus.publish("key", {"key": "down", "raw": True})
    couch.ctx.bus.publish("volume_changed", {"output": "tv", "volume": 30, "muted": False})
    await asyncio.sleep(0.05)
    assert couch.state()["minutes"] == 30


async def test_setting_it_again_in_the_last_minute_replaces_the_warning(couch):
    couch.service.set(2)
    await until(lambda: couch.state()["warning"], "the warning")
    await couch.ctx.commands.run("sleep.set", {"minutes": 30})  # from the phone
    assert couch.state() == {
        "minutes": 30,
        "ends_at": "2026-11-19T21:20:00+01:00",
        "warning": False,
    }
    await until(lambda: len(couch.toasts) == 2)
    assert couch.toasts[-1] == {
        "text": "TV off at 21:20",
        "level": "info",
        "icon": "moon",
        "id": TOAST_ID,
    }
    await asyncio.sleep(MINUTE * 1.5)
    assert couch.standbys() == 0


async def test_the_tv_going_to_standby_turns_it_off(couch):
    couch.service.set(2)
    couch.ctx.state.update("tv", {"power": "standby"})  # its own remote
    await until(lambda: couch.state()["minutes"] is None)
    await asyncio.sleep(MINUTE * 2.5)
    assert couch.standbys() == 0
    assert couch.toasts == []


async def test_a_standby_that_fails_is_only_logged(couch):
    async def fail(args):
        raise CommandError("no TV")

    couch.ctx.commands.remove_prefix("tv.")
    couch.ctx.commands.add("tv.standby", fail)
    couch.ctx.commands.remove_prefix("idle.")  # no idle clock either: still warns
    couch.service.set(2)
    await until(lambda: couch.state()["warning"], "the warning")
    await until(lambda: couch.state()["minutes"] is None, "the end")
    assert couch.toasts[0]["text"] == WARNING[0]


async def test_stopping_couchd_stops_it(ctx):
    ran = []

    async def standby(args):
        ran.append("tv.standby")

    ctx.commands.add("tv.standby", standby)
    service = SleepService(ctx, now=lambda: NOW, minute=0.05)
    await service.start()
    service.set(2)
    await service.stop()
    await asyncio.sleep(0.15)
    assert ran == []
    with pytest.raises(CommandError):
        await ctx.commands.run("sleep.set", {"minutes": 15})

import asyncio

import pytest

from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.idle import IdleService
from couchd.services import Context
from couchd.state import LiveState, SavedState


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


async def until(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


@pytest.fixture
async def idle(ctx):
    ctx.state.update("apps", {"foreground": "launcher"})
    service = IdleService(ctx, idle_after=0.1)
    await service.start()
    yield service
    await service.stop()


async def test_goes_idle_on_the_launcher_and_wakes_on_a_key(ctx, idle):
    changes: list[bool] = []
    idle.on_change(changes.append)
    assert ctx.state.get("idle") == {"idle": False, "after": 0.1, "tv_off_after": 0.0}
    await until(lambda: ctx.state.get("idle")["idle"])
    ctx.bus.publish("key", {"key": "down"})
    await until(lambda: not ctx.state.get("idle")["idle"])
    assert changes == [True, False]


async def test_never_idle_while_an_app_is_up(ctx, idle):
    ctx.state.update("apps", {"foreground": "youtube"})
    await asyncio.sleep(0.25)
    assert not ctx.state.get("idle")["idle"]
    ctx.state.update("apps", {"foreground": "launcher"})  # back home: the clock starts now
    await asyncio.sleep(0.05)
    assert not ctx.state.get("idle")["idle"]
    await until(lambda: ctx.state.get("idle")["idle"])


async def test_the_ui_can_report_activity(ctx, idle):
    await until(lambda: ctx.state.get("idle")["idle"])
    await ctx.commands.run("idle.activity")
    assert not ctx.state.get("idle")["idle"]


async def test_the_wake_key_reaches_subscribers_before_idle_turns_false(ctx, idle):
    # The UI drops the key that wakes the clock; for that it must see the key while `idle`
    # is still true, so the key message has to come first on the bus.
    await until(lambda: ctx.state.get("idle")["idle"])
    with ctx.bus.subscribe() as sub:
        ctx.bus.publish("key", {"key": "select"})
        first = await sub.get()
        second = await sub.get()
    assert first["type"] == "key"
    assert second == {"type": "state", "data": {"idle": {"idle": False}}, "full": False}


async def test_idle_now_goes_idle_at_once_but_only_on_the_launcher(ctx):
    ctx.state.update("apps", {"foreground": "launcher"})
    service = IdleService(ctx)  # 5 minutes
    await service.start()
    try:
        await ctx.commands.run("idle.now")
        assert ctx.state.get("idle")["idle"]
        await asyncio.sleep(0.05)
        assert ctx.state.get("idle")["idle"]  # stays idle until something happens
        ctx.bus.publish("key", {"key": "up"})
        await until(lambda: not ctx.state.get("idle")["idle"])
        ctx.state.update("apps", {"foreground": "youtube"})
        await asyncio.sleep(0.02)
        with pytest.raises(CommandError):
            await ctx.commands.run("idle.now")
        assert not ctx.state.get("idle")["idle"]
    finally:
        await service.stop()


async def test_an_app_coming_up_ends_idle(ctx, idle):
    await until(lambda: ctx.state.get("idle")["idle"])
    ctx.state.update("apps", {"foreground": "youtube"})
    await until(lambda: not ctx.state.get("idle")["idle"])


# --- the TV turning itself off -------------------------------------------------------------


@pytest.fixture
def standby_calls(ctx):
    calls = []

    async def standby(args):
        calls.append(args)
        ctx.state.update("tv", {"power": "standby"})

    ctx.commands.add("tv.standby", standby)
    return calls


@pytest.fixture
async def tv_idle(ctx, standby_calls):
    ctx.state.update("apps", {"foreground": "launcher"})
    ctx.state.update("tv", {"power": "on"})
    ctx.settings.set("tv.off_when_idle", True)
    service = IdleService(ctx, idle_after=0.05, tv_off_after=0.2)
    await service.start()
    yield service
    await service.stop()


async def test_tv_goes_off_after_a_long_idle_once(ctx, tv_idle, standby_calls):
    assert ctx.state.get("idle")["tv_off_after"] == 0.2
    await until(lambda: ctx.state.get("idle")["idle"])
    assert standby_calls == []  # the clock first
    await until(lambda: standby_calls)
    await asyncio.sleep(0.3)
    assert len(standby_calls) == 1


async def test_turning_the_tv_back_on_wakes_and_restarts_the_count(ctx, tv_idle, standby_calls):
    await until(lambda: standby_calls)
    assert ctx.state.get("idle")["idle"]
    ctx.state.update("tv", {"power": "on"})  # someone's back
    await until(lambda: not ctx.state.get("idle")["idle"])
    await until(lambda: len(standby_calls) == 2)


async def test_tv_off_is_a_setting_and_off_by_default(ctx, standby_calls):
    ctx.state.update("apps", {"foreground": "launcher"})
    ctx.state.update("tv", {"power": "on"})
    service = IdleService(ctx, idle_after=0.05, tv_off_after=0.1)
    await service.start()
    try:
        assert ctx.state.get("idle")["tv_off_after"] == 0
        await asyncio.sleep(0.3)
        assert standby_calls == []
        ctx.settings.set("tv.off_when_idle", True)  # counts from the last key, long ago
        await until(lambda: standby_calls)
    finally:
        await service.stop()


async def test_tv_stays_on_while_an_app_is_up(ctx, tv_idle, standby_calls):
    ctx.state.update("apps", {"foreground": "plex"})
    await asyncio.sleep(0.4)
    assert standby_calls == []


async def test_tv_already_in_standby_is_left_alone(ctx, tv_idle, standby_calls):
    ctx.state.update("tv", {"power": "standby"})
    await asyncio.sleep(0.4)
    assert standby_calls == []


async def test_no_tv_service_is_only_a_warning(ctx):
    ctx.state.update("apps", {"foreground": "launcher"})
    ctx.settings.set("tv.off_when_idle", True)
    service = IdleService(ctx, idle_after=0.05, tv_off_after=0.1)
    await service.start()
    try:
        await asyncio.sleep(0.3)  # tv.standby isn't a command: logged, nothing breaks
        assert ctx.state.get("idle")["idle"]
    finally:
        await service.stop()

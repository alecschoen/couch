import asyncio

import pytest

from couchd.bus import EventBus
from couchd.commands import Commands
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
    assert ctx.state.get("idle") == {"idle": False, "after": 0.1}
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

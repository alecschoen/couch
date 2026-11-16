"""Startup progress: services starting, the TV waking, and the limits on both."""

import asyncio

import pytest

from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.services import Context, Service, ServiceRegistry
from couchd.startup import StartupService
from couchd.state import LiveState, SavedState


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


class Slow(Service):
    def __init__(self, name, gate):
        self.name = name
        self.gate = gate

    async def start(self):
        await self.gate.wait()


async def until(condition, tries=200):
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


def startup(ctx):
    return ctx.state.get("startup")


async def test_waits_for_services_and_the_tv(ctx):
    gate = asyncio.Event()
    registry = ServiceRegistry(ctx.state)
    registry.add(StartupService(ctx))
    registry.add(Slow("tv", asyncio.Event()))  # started below by hand
    registry.add(Slow("audio", gate))
    assert startup(ctx) == {"done": False, "progress": 0.0, "step": "services"}

    ctx.state.update("tv", {"power": "standby"})
    starting = asyncio.create_task(registry.start_all())
    await until(lambda: startup(ctx)["step"] == "tv")
    registry.get("tv").gate.set()
    await until(lambda: ctx.state.get("services")["audio"] == "starting")
    assert startup(ctx)["progress"] == pytest.approx(1 / 3, abs=0.01)  # tv started

    ctx.state.update("tv", {"power": "on"})
    await until(lambda: startup(ctx)["step"] == "services")
    assert not startup(ctx)["done"]
    gate.set()
    await starting
    await until(lambda: startup(ctx)["done"])
    assert startup(ctx) == {"done": True, "progress": 1.0, "step": None}
    await registry.stop_all()


async def test_a_tv_that_never_says_on_doesnt_hold_up_startup(ctx):
    registry = ServiceRegistry(ctx.state)
    registry.add(StartupService(ctx, tv_wait=0.1))
    gate = asyncio.Event()
    gate.set()
    registry.add(Slow("tv", gate))
    ctx.state.update("tv", {"power": "unknown"})
    await registry.start_all()
    await asyncio.sleep(0.05)
    assert startup(ctx)["step"] == "tv"
    await until(lambda: startup(ctx)["done"])
    await registry.stop_all()


async def test_a_failed_tv_service_isnt_waited_for(ctx):
    class Broken(Service):
        name = "tv"

        async def start(self):
            raise RuntimeError("no /dev/cec0")

    registry = ServiceRegistry(ctx.state)
    registry.add(StartupService(ctx))
    registry.add(Broken())
    await registry.start_all()
    await until(lambda: startup(ctx)["done"])
    await registry.stop_all()


async def test_a_stuck_service_is_given_up_on(ctx):
    registry = ServiceRegistry(ctx.state)
    registry.add(StartupService(ctx, limit=0.1))
    registry.add(Slow("bluetooth", asyncio.Event()))  # never finishes starting
    starting = asyncio.create_task(registry.start_all())
    await until(lambda: startup(ctx)["done"])
    starting.cancel()
    await registry.stop_all()

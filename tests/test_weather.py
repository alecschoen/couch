"""The weather (#82): Open-Meteo's answer, the codes, the request, and the service's timing.
Nothing here reaches the real Open-Meteo: a local aiohttp server stands in for it."""

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp import web

from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.services import Context
from couchd.state import LiveState, SavedState
from couchd.weather import (
    CODES,
    EMPTY,
    ICONS,
    WeatherService,
    degrees,
    describe,
    fake_forecast,
    open_meteo,
    parse,
    query,
)

BRUSSELS = ZoneInfo("Europe/Brussels")
NOW = datetime(2026, 11, 16, 20, 51, tzinfo=BRUSSELS).timestamp()
HOUR = int(datetime(2026, 11, 16, 20, 0, tzinfo=BRUSSELS).timestamp())
PLACE = (50.8333, 4.3333)


def answer(*, temp: float = 12.4, code: int = 3) -> dict:
    """What Open-Meteo sends for `query()`: from 19:00, hourly, cloudy then rain."""
    times = [HOUR + i * 3600 for i in range(-1, 47)]
    return {
        "latitude": 50.84,
        "longitude": 4.34,
        "current": {"time": int(NOW), "temperature_2m": temp, "weather_code": code, "is_day": 0},
        "hourly": {
            "time": times,
            "temperature_2m": [12.0 - 0.6 * i for i in range(48)],
            "weather_code": [3] * 4 + [61] * 44,
            "is_day": [0] * 48,
        },
        "daily": {
            "time": [HOUR - 20 * 3600, HOUR + 4 * 3600],
            "temperature_2m_max": [15.2, 11.0],
            "temperature_2m_min": [6.6, 4.0],
        },
    }


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


# --- the codes ---------------------------------------------------------------------------


def test_codes_map_to_words_and_a_few_icons():
    assert describe(0) == ("Clear", "clear")
    assert describe(0, day=False) == ("Clear", "clear-night")
    assert describe(1, day=False) == ("Mostly clear", "clear-night")
    assert describe(2, day=False) == ("Partly cloudy", "partly-cloudy")
    assert describe(3) == ("Cloudy", "cloudy")
    assert describe(45) == ("Fog", "fog")
    assert describe(55) == ("Drizzle", "rain")
    assert describe(63) == ("Rain", "rain")
    assert describe(82) == ("Heavy showers", "rain")
    assert describe(75) == ("Heavy snow", "snow")
    assert describe(86) == ("Snow showers", "snow")
    assert describe(99) == ("Thunderstorm", "thunder")
    # One we don't know: no words, the plain cloud.
    assert describe(42) == ("", "cloudy")
    assert describe(None) == ("", "cloudy")
    assert {icon for _, icon in CODES.values()} | {"clear-night"} == set(ICONS)
    assert all(len(words) <= 16 for words, _ in CODES.values())  # fits "… · 15° / 7°"


def test_whole_degrees():
    assert degrees(12.4) == 12
    assert degrees(12.5) == 13
    assert degrees(-2.5) == -3
    assert degrees(-0.4) == 0
    assert str(degrees(-0.4)) == "0"
    for bad in (None, "12", True, float("nan")):
        with pytest.raises(ValueError):
            degrees(bad)


# --- the answer --------------------------------------------------------------------------


def test_parse_takes_now_today_and_the_hours_to_come():
    w = parse(answer(), NOW, BRUSSELS)
    assert w["temp"] == 12
    assert (w["condition"], w["icon"]) == ("Cloudy", "cloudy")
    assert (w["high"], w["low"]) == (15, 7)
    assert w["updated_at"] == int(NOW)
    hourly = w["hourly"]
    # 19:00 and 20:00 have been; from 21:00, twelve hours, past midnight.
    assert [h["time"] for h in hourly[:4]] == ["21:00", "22:00", "23:00", "00:00"]
    assert len(hourly) == 12
    assert hourly[0] == {"at": HOUR + 3600, "time": "21:00", "temp": 11, "icon": "cloudy"}
    assert hourly[3]["icon"] == "rain"


def test_parse_at_night_draws_the_moon():
    w = parse(answer(code=0), NOW, BRUSSELS)
    assert (w["condition"], w["icon"]) == ("Clear", "clear-night")


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"current": {}, "hourly": {}, "daily": {}},
        {**answer(), "current": {"temperature_2m": None, "weather_code": 3}},
        {**answer(), "daily": {"temperature_2m_max": [], "temperature_2m_min": []}},
    ],
)
def test_parse_refuses_an_answer_that_isnt_one(broken):
    with pytest.raises((KeyError, ValueError, TypeError, IndexError)):
        parse(broken, NOW, BRUSSELS)


def test_the_request_says_where_rounded_and_nothing_else():
    q = query(PLACE)
    assert (q["latitude"], q["longitude"]) == ("50.83", "4.33")
    assert set(q) == {
        "latitude",
        "longitude",
        "current",
        "hourly",
        "daily",
        "timezone",
        "timeformat",
        "forecast_days",
    }
    assert q["timeformat"] == "unixtime"


async def test_open_meteo_over_http():
    seen = []

    async def forecast(request: web.Request) -> web.Response:
        seen.append(dict(request.query))
        return web.json_response(answer())

    app = web.Application()
    app.router.add_get("/v1/forecast", forecast)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        fetch = open_meteo(f"http://127.0.0.1:{port}/v1/forecast")
        data = await fetch(PLACE)
        assert parse(data, NOW, BRUSSELS)["temp"] == 12
        assert seen == [query(PLACE)]
        # An error status is an error, not a forecast.
        down = open_meteo(f"http://127.0.0.1:{port}/nope")
        with pytest.raises(aiohttp.ClientResponseError, match="404"):
            await down(PLACE)
    finally:
        await runner.cleanup()


async def test_the_dev_forecast_parses():
    data = await fake_forecast(NOW)(PLACE)
    w = parse(data, NOW, UTC)
    assert w["temp"] == 12 and w["condition"] == "Cloudy" and len(w["hourly"]) == 12


# --- the service -------------------------------------------------------------------------


class Clock:
    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class Sleeps:
    """Stands in for the waits between fetches: notes each one, moves the clock on by it,
    and lets the loop go round `rounds` times before holding it."""

    def __init__(self, clock: Clock, rounds: int) -> None:
        self.clock = clock
        self.rounds = rounds
        self.waits: list[float] = []
        self.done = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.at += seconds
        if len(self.waits) >= self.rounds:
            self.done.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)


class Forecasts:
    """A fetch that answers from a script: a dict is a forecast, an exception is raised."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.calls = 0

    async def __call__(self, location) -> dict:
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


async def run(ctx, fetch, clock, rounds, **kw) -> tuple[WeatherService, Sleeps]:
    sleeps = Sleeps(clock, rounds)
    service = WeatherService(
        ctx, fetch=fetch, location=PLACE, clock=clock, sleep=sleeps, refresh=1800, **kw
    )
    await service.start()
    await asyncio.wait_for(sleeps.done.wait(), 2)
    return service, sleeps


async def test_publishes_and_refreshes_every_half_hour(ctx):
    clock = Clock(NOW)
    service, sleeps = await run(ctx, Forecasts(answer()), clock, rounds=3)
    try:
        assert sleeps.waits == [1800, 1800, 1800]
        w = ctx.state.get("weather")
        assert (w["temp"], w["condition"], w["high"], w["low"]) == (12, "Cloudy", 15, 7)
    finally:
        await service.stop()


async def test_backs_off_on_errors_and_keeps_the_last_forecast(ctx):
    clock = Clock(NOW)
    fetch = Forecasts(answer(), *[OSError("no network")] * 6, answer(temp=9.0))
    service, sleeps = await run(ctx, fetch, clock, rounds=8, retry_min=60)
    try:
        # Good, then six failures: 1, 2, 4, 8, 16 minutes, then no longer than the refresh.
        assert sleeps.waits == [1800, 60, 120, 240, 480, 960, 1800, 1800]
        # Kept through the failures while it wasn't stale yet; then the new one.
        assert ctx.state.get("weather")["temp"] == 9
    finally:
        await service.stop()


async def test_a_stale_forecast_goes(ctx):
    clock = Clock(NOW)
    fetch = Forecasts(answer(), *[TimeoutError()] * 20)
    service, _ = await run(ctx, fetch, clock, rounds=4, stale_after=3 * 3600)
    try:
        # 30 min, then 1, 2, 4 minutes of failures: still fresh enough.
        assert ctx.state.get("weather")["temp"] == 12
    finally:
        await service.stop()
    clock = Clock(NOW)
    service, _ = await run(ctx, Forecasts(answer(), *[TimeoutError()] * 20), clock, rounds=12)
    try:
        assert ctx.state.get("weather") == EMPTY
    finally:
        await service.stop()


async def test_a_broken_answer_counts_as_a_failure(ctx):
    clock = Clock(NOW)
    service, sleeps = await run(ctx, Forecasts({"oops": True}), clock, rounds=2)
    try:
        assert sleeps.waits == [60, 120]
        assert ctx.state.get("weather") == EMPTY
    finally:
        await service.stop()


async def test_turned_off_fetches_nothing_and_shows_nothing(ctx):
    ctx.settings.set("display.weather", False)
    fetch = Forecasts(answer())
    service = WeatherService(ctx, fetch=fetch, location=PLACE, clock=Clock(NOW))
    await service.start()
    try:
        await asyncio.sleep(0.01)
        assert fetch.calls == 0
        assert ctx.state.get("weather") == EMPTY
        ctx.settings.set("display.weather", True)
        for _ in range(100):
            if ctx.state.get("weather")["temp"] is not None:
                break
            await asyncio.sleep(0.005)
        assert fetch.calls == 1
        assert ctx.state.get("weather")["temp"] == 12
        ctx.settings.set("display.weather", False)
        assert ctx.state.get("weather") == EMPTY
        await asyncio.sleep(0.01)
        assert fetch.calls == 1
    finally:
        await service.stop()


async def test_nowhere_no_weather(ctx, monkeypatch):
    monkeypatch.setattr("couchd.weather.system_timezone", lambda: None)
    fetch = Forecasts(answer())
    service = WeatherService(ctx, fetch=fetch, clock=Clock(NOW))
    await service.start()
    await service.stop()
    assert fetch.calls == 0
    assert ctx.state.get("weather") == EMPTY

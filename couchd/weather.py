"""The weather (#82), for Home and the idle clock: now, today's high and low, and the next hours.

From Open-Meteo (api.open-meteo.com, free, no key). The place is the one the evening colours
use (couchd/evening.py): tzdata's coordinates for the Pi's timezone, rounded to 2 decimals
(about a kilometre). The request carries those and nothing else about the house: no name, no
address, no key. Celsius, as the screens show it.

It's fetched every `REFRESH` seconds. A failed fetch (no network yet at boot, Open-Meteo down)
is tried again after `RETRY_MIN`, doubling up to `REFRESH`, and the last good forecast stays
up meanwhile, until it's `STALE_AFTER` old: a temperature from yesterday is worse than none.
With the `display.weather` setting off nothing is fetched at all, and the section is empty.

Live state section `weather`:

    {"temp": 12, "condition": "Cloudy", "icon": "cloudy", "high": 15, "low": 7,
     "hourly": [{"at": 1795377600, "time": "22:00", "temp": 11, "icon": "cloudy"}, ...],
     "updated_at": 1795373460}

Temperatures are whole °C; `time` is local `HH:MM` and `at` the same hour in Unix seconds, so
the UI can pick the hours still to come. `icon` is one of `ICONS`, which the launcher draws
(launcher/ui/weather.slint). Everything is null (and `hourly` empty) while there's no
forecast: the UI then shows no weather at all.
"""

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import aiohttp

from couchd.evening import Location, system_timezone, zone_location
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SECTION = "weather"
SETTING = "display.weather"
URL = "https://api.open-meteo.com/v1/forecast"
FETCH_TIMEOUT = 15.0
REFRESH = 30 * 60.0
RETRY_MIN = 60.0
STALE_AFTER = 3 * 3600.0
HOURS = 12  # how many hours ahead `hourly` reaches

ICONS = ("clear", "clear-night", "partly-cloudy", "cloudy", "fog", "rain", "snow", "thunder")

# WMO weather interpretation codes (Open-Meteo's `weather_code`) → a few words, and an icon.
CODES: dict[int, tuple[str, str]] = {
    0: ("Clear", "clear"),
    1: ("Mostly clear", "clear"),
    2: ("Partly cloudy", "partly-cloudy"),
    3: ("Cloudy", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Fog", "fog"),
    51: ("Drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Drizzle", "rain"),
    56: ("Freezing drizzle", "rain"),
    57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow", "snow"),
    80: ("Showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "thunder"),
    96: ("Thunderstorm", "thunder"),
    99: ("Thunderstorm", "thunder"),
}

EMPTY: dict[str, Any] = {
    "temp": None,
    "condition": None,
    "icon": None,
    "high": None,
    "low": None,
    "hourly": [],
    "updated_at": None,
}

Fetch = Callable[[Location], Awaitable[dict]]


def describe(code: Any, day: bool = True) -> tuple[str, str]:
    """A WMO code's words and icon. A code we don't know gets no words and the cloud."""
    condition, icon = CODES.get(code, ("", "cloudy")) if isinstance(code, int) else ("", "cloudy")
    if icon == "clear" and not day:
        icon = "clear-night"
    return condition, icon


def degrees(value: Any) -> int:
    """Whole degrees, halves away from zero (so -0.4 is 0, never "-0")."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"not a temperature: {value!r}")
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def parse(data: dict, now: float, tz: Any = None) -> dict[str, Any]:
    """Open-Meteo's answer (`timeformat=unixtime`) as the `weather` section. `tz` is the zone
    for the hours' `HH:MM` (None: the system's). Raises ValueError, KeyError or TypeError on an
    answer that isn't what we asked for."""
    current = data["current"]
    daily = data["daily"]
    hourly = data["hourly"]
    condition, icon = describe(current["weather_code"], bool(current.get("is_day", 1)))
    hours = []
    times = hourly["time"]
    temps = hourly["temperature_2m"]
    codes = hourly["weather_code"]
    days = hourly.get("is_day") or [1] * len(times)
    for at, temp, code, day in zip(times, temps, codes, days, strict=False):
        if at <= now or temp is None:
            continue
        if at > now + HOURS * 3600:
            break
        hours.append(
            {
                "at": int(at),
                "time": datetime.fromtimestamp(at, tz).strftime("%H:%M"),
                "temp": degrees(temp),
                "icon": describe(code, bool(day))[1],
            }
        )
    return {
        "temp": degrees(current["temperature_2m"]),
        "condition": condition,
        "icon": icon,
        "high": degrees(daily["temperature_2m_max"][0]),
        "low": degrees(daily["temperature_2m_min"][0]),
        "hourly": hours,
        "updated_at": int(now),
    }


def query(location: Location) -> dict[str, str]:
    """The request's parameters: the place, rounded, and what we want to know. Nothing else."""
    lat, lon = location
    return {
        "latitude": f"{lat:.2f}",
        "longitude": f"{lon:.2f}",
        "current": "temperature_2m,weather_code,is_day",
        "hourly": "temperature_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto",  # "today" for the high and low is the place's own day
        "timeformat": "unixtime",
        "forecast_days": "2",  # the next hours run past midnight
    }


def open_meteo(url: str = URL, timeout: float = FETCH_TIMEOUT) -> Fetch:
    """A fetch from Open-Meteo (or `url`, for tests)."""

    async def fetch(location: Location) -> dict:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http,
            http.get(url, params=query(location)) as resp,
        ):
            resp.raise_for_status()
            return await resp.json()

    return fetch


def fake_forecast(now: float | None = None) -> Fetch:
    """Dev mode's weather: a cloudy evening turning to rain, shaped like Open-Meteo's answer,
    so it goes through `parse` like the real one. Nothing is fetched."""

    async def fetch(location: Location) -> dict:
        t = time.time() if now is None else now
        hour = int(t // 3600) * 3600
        times = [hour + i * 3600 for i in range(-2, 30)]
        temps = [12.4 - 0.5 * max(i, 0) + (1.5 if i > 16 else 0) for i in range(-2, 30)]
        codes = [3 if i < 3 else 61 if i < 8 else 2 for i in range(-2, 30)]
        days = [int(7 <= datetime.fromtimestamp(x).hour < 17) for x in times]
        return {
            "current": {"temperature_2m": 12.4, "weather_code": 3, "is_day": days[2]},
            "hourly": {
                "time": times,
                "temperature_2m": temps,
                "weather_code": codes,
                "is_day": days,
            },
            "daily": {"temperature_2m_max": [15.2, 13.0], "temperature_2m_min": [6.8, 5.1]},
        }

    return fetch


class WeatherService(Service):
    name = "weather"

    def __init__(
        self,
        ctx: Context,
        *,
        fetch: Fetch | None = None,
        location: Location | None = None,
        clock: Callable[[], float] = time.time,
        refresh: float = REFRESH,
        retry_min: float = RETRY_MIN,
        stale_after: float = STALE_AFTER,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Left out, `fetch` is Open-Meteo and `location` the system timezone's coordinates.
        `sleep` waits between fetches (tests): by default until then, or the setting changes."""
        self.ctx = ctx
        self._fetch = fetch or open_meteo()
        self._location = location
        self._clock = clock
        self._refresh = refresh
        self._retry_min = retry_min
        self._stale_after = stale_after
        self._sleep = sleep or self._pause
        self._last: dict[str, Any] | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._unsubscribe: Callable[[], None] | None = None

    async def start(self) -> None:
        self.ctx.state.update(SECTION, EMPTY)
        zone = None
        if self._location is None:
            zone = system_timezone()
            self._location = zone_location(zone) if zone else None
        if self._location is None:
            log.warning("no weather: don't know where we are", extra={"timezone": zone})
            return
        self._unsubscribe = self.ctx.settings.on_change(self._setting_changed)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _on(self) -> bool:
        return bool(self.ctx.settings.get(SETTING))

    def _setting_changed(self, key: str, value: Any) -> None:
        if key != SETTING:
            return
        if not value:
            self._last = None
            self.ctx.state.update(SECTION, EMPTY)
        self._wake.set()  # turned on: fetch now; off: stop waiting for the next fetch

    async def refresh(self) -> bool:
        """Fetch once. True if it worked; on failure the last forecast stays, unless stale."""
        assert self._location is not None
        try:
            data = await self._fetch(self._location)
            weather = parse(data, self._clock())
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError, KeyError, TypeError) as e:
            log.info("no weather", extra={"error": str(e) or type(e).__name__})
            if self._last and self._clock() - self._last["updated_at"] > self._stale_after:
                self._last = None
                self.ctx.state.update(SECTION, EMPTY)
            return False
        if not self._on():
            return True  # turned off while it was on its way
        self._last = weather
        self.ctx.state.update(SECTION, weather)
        return True

    async def _run(self) -> None:
        retry = self._retry_min
        while True:
            self._wake.clear()  # before the fetch: a change during it still wakes the wait
            if not self._on():
                await self._wake.wait()
                continue
            if await self.refresh():
                wait, retry = self._refresh, self._retry_min
            else:
                wait, retry = retry, min(retry * 2, self._refresh)
            await self._sleep(wait)

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()

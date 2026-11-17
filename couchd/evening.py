"""Evening colours (#20): is the sun down where the couch is?

The place comes from the Pi's own timezone (`timedatectl set-timezone Europe/Brussels`):
tzdata's `zone.tab` gives each zone the coordinates of its main city, which is close enough
for a sunset; a few minutes either way doesn't matter for a colour change. No network.
Within a zone, the city in zone.tab can be a few hundred km off; the setting is still "after
sunset", give or take a quarter of an hour at worst.

Live state section `evening`:

    {"dark": true, "sunrise": "07:59", "sunset": "16:54", "place": "Europe/Brussels"}

`dark` is whether the sun is down now; `sunrise` and `sunset` are today's, in local time,
for the Settings screen (None in a polar day or night). If the timezone is unknown or has
no coordinates (UTC, say), `place` is None and `dark` falls back to 18:00-07:00.

The UI combines it with the `display.evening` setting: "sunset" follows `dark`, "always"
and "off" ignore it (launcher/src/app.rs). It's rechecked every minute, so a clock that jumps
at boot (the Pi has no real-time clock until NTP answers) is caught up within a minute.
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from couchd.services import Context, Service
from couchd.sun import sun_times

log = logging.getLogger(__name__)

ZONEINFO = Path("/usr/share/zoneinfo")
ZONE_TABLES = ("zone.tab", "zone1970.tab")  # zone.tab has one zone per country, so more cities
FIXED_EVENING = (18, 7)  # dark from 18:00 to 07:00 when we don't know where we are
CHECK_EVERY = 60.0

Location = tuple[float, float]  # latitude, longitude in degrees, north and east positive


def system_timezone(*, env: dict[str, str] | None = None, etc: Path = Path("/etc")) -> str | None:
    """The system's timezone name, e.g. "Europe/Brussels", or None."""
    env = os.environ if env is None else env
    tz = env.get("TZ", "").lstrip(":")
    if tz and "/" in tz and not tz.startswith("/"):
        return tz
    with contextlib.suppress(OSError):
        target = os.readlink(etc / "localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    with contextlib.suppress(OSError):
        name = (etc / "timezone").read_text().strip()
        if name:
            return name
    return None


def zone_location(zone: str, zoneinfo: Path = ZONEINFO) -> Location | None:
    """The coordinates tzdata gives `zone`, or None."""
    for table in ZONE_TABLES:
        with contextlib.suppress(OSError):
            for line in (zoneinfo / table).read_text().splitlines():
                if line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 3 and cols[2] == zone:
                    return parse_iso6709(cols[1])
    return None


def parse_iso6709(coords: str) -> Location | None:
    """ "+5050+00420" or "+404251-0740023" (±DDMM[SS]±DDDMM[SS]) to degrees."""
    split = max(coords.rfind("+"), coords.rfind("-"))
    if split <= 0:
        return None
    try:
        return _dms(coords[:split], 2), _dms(coords[split:], 3)
    except ValueError:
        return None


def _dms(part: str, degree_digits: int) -> float:
    sign = -1 if part[0] == "-" else 1
    digits = part[1:]
    if not digits.isdigit() or len(digits) not in (degree_digits + 2, degree_digits + 4):
        raise ValueError(part)
    d = int(digits[:degree_digits])
    m = int(digits[degree_digits : degree_digits + 2])
    s = int(digits[degree_digits + 2 :] or 0)
    return sign * (d + m / 60 + s / 3600)


class EveningService(Service):
    name = "evening"

    def __init__(
        self,
        ctx: Context,
        *,
        place: str | None = None,
        location: Location | None = None,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        check_every: float = CHECK_EVERY,
    ) -> None:
        """Left out, `place` is the system timezone and `location` its coordinates."""
        self.ctx = ctx
        self._place = place
        self._location = location
        self._now = now
        self._check_every = check_every
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._location is None:
            if self._place is None:
                self._place = system_timezone()
            if self._place is not None:
                self._location = zone_location(self._place)
        if self._location is None:
            log.warning(
                "don't know where we are; evening colours from 18:00 to 07:00",
                extra={"timezone": self._place},
            )
        else:
            log.info(
                "evening colours follow the sun",
                extra={"timezone": self._place, "location": self._location},
            )
        self.check()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def check(self) -> None:
        self.ctx.state.update("evening", self.describe(self._now()))

    def describe(self, now: datetime) -> dict:
        if self._location is None:
            start, end = FIXED_EVENING
            return {
                "dark": now.hour >= start or now.hour < end,
                "sunrise": None,
                "sunset": None,
                "place": None,
            }
        times = sun_times(now.date(), *self._location)
        local = lambda t: t.astimezone(now.tzinfo).strftime("%H:%M") if t else None  # noqa: E731
        return {
            "dark": times.dark(now),
            "sunrise": local(times.sunrise),
            "sunset": local(times.sunset),
            "place": self._place or "{:.2f}, {:.2f}".format(*self._location),
        }

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._check_every)
            self.check()

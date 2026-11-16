"""Sunrise and sunset, worked out offline (#20), for the evening colours.

NOAA's solar calculator (after Meeus): the sun's position from the Julian century gives the
equation of time and the declination, and from those the hour angle at which the sun's
centre is 0.833° below the horizon (refraction plus the sun's radius). Each event is worked
out twice, the second time at the first answer, which keeps it within a minute of the
almanac anywhere people live.

    times = sun_times(date(2026, 12, 21), 51.51, -0.13)   # London
    times.sunrise, times.sunset                           # aware datetimes, in UTC

Latitude is north positive, longitude east positive. Inside the polar circles the sun can
stay up or down all day: `sunrise` and `sunset` are then None and `polar` says which.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

ZENITH = 90.833  # degrees: the sun's centre this far from straight up at sunrise and sunset


@dataclass(frozen=True)
class SunTimes:
    sunrise: datetime | None
    sunset: datetime | None
    polar: Literal["day", "night"] | None = None  # sun up, or down, all day

    def dark(self, at: datetime) -> bool:
        """Is the sun down at `at` (an aware datetime on the same day)?"""
        if self.polar is not None:
            return self.polar == "night"
        assert self.sunrise is not None and self.sunset is not None
        return not self.sunrise <= at < self.sunset


def sun_times(day: date, latitude: float, longitude: float) -> SunTimes:
    """Sunrise and sunset on the local calendar day `day` at the given place, in UTC."""
    rise = _event(day, latitude, longitude, rising=True)
    set_ = _event(day, latitude, longitude, rising=False)
    if isinstance(rise, str) or isinstance(set_, str):
        polar = rise if isinstance(rise, str) else set_
        return SunTimes(None, None, polar)  # type: ignore[arg-type]
    midnight = datetime.combine(day, time(), UTC)
    return SunTimes(midnight + timedelta(minutes=rise), midnight + timedelta(minutes=set_))


def _event(day: date, latitude: float, longitude: float, *, rising: bool) -> float | str:
    """Minutes after midnight UTC on `day` (can be below 0 or past 1440), or "day"/"night"."""
    minutes = 720.0 - 4 * longitude  # first guess: solar noon
    for _ in range(2):
        eqtime, decl = _sun(day, minutes)
        lat = math.radians(latitude)
        cos_ha = math.cos(math.radians(ZENITH)) / (math.cos(lat) * math.cos(decl)) - math.tan(
            lat
        ) * math.tan(decl)
        if cos_ha > 1:
            return "night"
        if cos_ha < -1:
            return "day"
        ha = math.degrees(math.acos(cos_ha))
        minutes = 720 - 4 * (longitude + (ha if rising else -ha)) - eqtime
    return minutes


def _sun(day: date, minutes: float) -> tuple[float, float]:
    """Equation of time (minutes) and declination (radians) at `minutes` UTC on `day`."""
    jd = day.toordinal() + 1721424.5 + minutes / 1440  # Julian day
    t = (jd - 2451545.0) / 36525  # Julian centuries since J2000
    l0 = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360)  # mean longitude
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))  # mean anomaly
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)  # orbit eccentricity
    centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * t)
    longitude = math.radians(math.degrees(l0) + centre - 0.00569 - 0.00478 * math.sin(omega))
    seconds = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))
    obliquity = math.radians(23 + (26 + seconds / 60) / 60 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliquity) * math.sin(longitude))
    y = math.tan(obliquity / 2) ** 2
    eqtime = 4 * math.degrees(
        y * math.sin(2 * l0)
        - 2 * e * math.sin(m)
        + 4 * e * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * e * e * math.sin(2 * m)
    )
    return eqtime, decl

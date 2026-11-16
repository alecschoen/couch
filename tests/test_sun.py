"""Sunrise and sunset against almanac times (timeanddate.com), to within two minutes."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from couchd.sun import sun_times

# place, day, latitude, longitude, timezone, sunrise and sunset (local)
CASES = [
    ("London, winter solstice", date(2026, 12, 21), 51.5074, -0.1278, "Europe/London",
     "08:04", "15:53"),
    ("Amsterdam, summer solstice", date(2026, 6, 21), 52.3676, 4.9041, "Europe/Amsterdam",
     "05:18", "22:06"),
    ("Brussels, today", date(2026, 11, 16), 50.8503, 4.3517, "Europe/Brussels",
     "07:59", "16:54"),
    ("Sydney, new year", date(2026, 1, 1), -33.8688, 151.2093, "Australia/Sydney",
     "05:47", "20:09"),
    ("New York, equinox", date(2026, 3, 20), 40.7128, -74.0060, "America/New_York",
     "06:59", "19:08"),
    ("Reykjavik, sets after midnight", date(2026, 6, 21), 64.1466, -21.9426, "Atlantic/Reykjavik",
     "02:55", "00:04"),
]  # fmt: skip


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def off_by(actual: datetime, expected: str) -> int:
    got = actual.hour * 60 + actual.minute
    diff = abs(got - minutes(expected))
    return min(diff, 1440 - diff)


@pytest.mark.parametrize(("place", "day", "lat", "lon", "tz", "sunrise", "sunset"), CASES)
def test_known_cities(place, day, lat, lon, tz, sunrise, sunset):
    times = sun_times(day, lat, lon)
    zone = ZoneInfo(tz)
    assert times.polar is None
    assert off_by(times.sunrise.astimezone(zone), sunrise) <= 2, place
    assert off_by(times.sunset.astimezone(zone), sunset) <= 2, place


def test_polar_day_and_night():
    tromso = (69.6496, 18.9560)
    assert sun_times(date(2026, 6, 21), *tromso).polar == "day"
    assert sun_times(date(2026, 12, 21), *tromso).polar == "night"
    assert sun_times(date(2026, 6, 21), *tromso).dark(datetime.now().astimezone()) is False
    assert sun_times(date(2026, 12, 21), *tromso).dark(datetime.now().astimezone()) is True


def test_dark_is_before_sunrise_and_from_sunset():
    zone = ZoneInfo("Europe/Brussels")
    times = sun_times(date(2026, 11, 16), 50.85, 4.35)
    sunset = times.sunset.astimezone(zone)
    assert times.dark(datetime(2026, 11, 16, 6, 0, tzinfo=zone))
    assert not times.dark(datetime(2026, 11, 16, 12, 0, tzinfo=zone))
    assert not times.dark(sunset - timedelta(minutes=1))
    assert times.dark(sunset)
    assert times.dark(datetime(2026, 11, 16, 23, 0, tzinfo=zone))

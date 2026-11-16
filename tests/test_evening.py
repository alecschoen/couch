from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.evening import EveningService, parse_iso6709, system_timezone, zone_location
from couchd.services import Context
from couchd.state import LiveState, SavedState

BRUSSELS = ZoneInfo("Europe/Brussels")


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


def test_parse_iso6709():
    assert parse_iso6709("+5050+00420") == pytest.approx((50.8333, 4.3333), abs=1e-3)
    assert parse_iso6709("+404251-0740023") == pytest.approx((40.7142, -74.0064), abs=1e-3)
    assert parse_iso6709("-3352+15113") == pytest.approx((-33.8667, 151.2167), abs=1e-3)
    assert parse_iso6709("nonsense") is None
    assert parse_iso6709("+50x0+00420") is None


def test_zone_location_reads_tzdata(tmp_path):
    (tmp_path / "zone.tab").write_text(
        "# comment\nBE\t+5050+00420\tEurope/Brussels\nNL\t+5222+00454\tEurope/Amsterdam\n"
    )
    (tmp_path / "zone1970.tab").write_text("AU\t-3352+15113\tAustralia/Sydney\tNew South Wales\n")
    assert zone_location("Europe/Amsterdam", tmp_path) == pytest.approx((52.3667, 4.9), abs=1e-3)
    assert zone_location("Australia/Sydney", tmp_path)[0] < 0
    assert zone_location("Etc/UTC", tmp_path) is None
    assert zone_location("Europe/Brussels", tmp_path / "missing") is None


def test_system_timezone(tmp_path):
    assert system_timezone(env={"TZ": ":Europe/Paris"}, etc=tmp_path) == "Europe/Paris"
    (tmp_path / "localtime").symlink_to("/usr/share/zoneinfo/Europe/Brussels")
    assert system_timezone(env={}, etc=tmp_path) == "Europe/Brussels"
    (tmp_path / "localtime").unlink()
    (tmp_path / "timezone").write_text("Europe/Lisbon\n")
    assert system_timezone(env={}, etc=tmp_path) == "Europe/Lisbon"
    (tmp_path / "timezone").unlink()
    assert system_timezone(env={}, etc=tmp_path) is None


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


async def test_follows_the_sun_where_the_timezone_is(ctx):
    clock = Clock(datetime(2026, 11, 16, 16, 50, tzinfo=BRUSSELS))
    service = EveningService(
        ctx, place="Europe/Brussels", location=(50.8333, 4.3333), now=clock, check_every=3600
    )
    await service.start()
    try:
        assert ctx.state.get("evening") == {
            "dark": False,
            "sunrise": "07:59",
            "sunset": "16:54",
            "place": "Europe/Brussels",
        }
        clock.at = datetime(2026, 11, 16, 16, 55, tzinfo=BRUSSELS)  # just after sunset
        service.check()
        assert ctx.state.get("evening")["dark"] is True
        clock.at = datetime(2026, 11, 17, 8, 5, tzinfo=BRUSSELS)  # the next morning
        service.check()
        assert ctx.state.get("evening")["dark"] is False
    finally:
        await service.stop()


async def test_fixed_hours_when_we_dont_know_where_we_are(ctx, monkeypatch):
    monkeypatch.setattr("couchd.evening.zone_location", lambda zone: None)
    clock = Clock(datetime(2026, 6, 21, 18, 30, tzinfo=BRUSSELS))
    service = EveningService(ctx, place="Etc/UTC", now=clock, check_every=3600)
    await service.start()
    try:
        assert ctx.state.get("evening") == {
            "dark": True,  # 18:00-07:00, though the sun is still up in June
            "sunrise": None,
            "sunset": None,
            "place": None,
        }
    finally:
        await service.stop()


async def test_polar_night(ctx):
    tromso = ZoneInfo("Europe/Oslo")
    clock = Clock(datetime(2026, 12, 21, 12, 0, tzinfo=tromso))
    service = EveningService(ctx, place="Arctic/Longyearbyen", location=(78.2, 15.6), now=clock)
    await service.start()
    try:
        assert ctx.state.get("evening")["dark"] is True
        assert ctx.state.get("evening")["sunset"] is None
    finally:
        await service.stop()

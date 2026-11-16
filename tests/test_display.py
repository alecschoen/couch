"""SystemdDisplay against a pretend `systemctl` that keeps unit states in files."""

import asyncio
from pathlib import Path

import pytest

from couchd import display as display_module
from couchd.backends import BackendError
from couchd.display import KODI_UNIT, SESSION_UNIT, SystemdDisplay

# Units are files holding their state. Starting one of the two stops the other, like the
# Conflicts= in the real units. `start --wait` blocks until the unit is no longer active,
# then exits with the code in <unit>.rc.
SYSTEMCTL = """#!/bin/sh
dir="$FAKE_SYSTEMD"
echo "$*" >> "$dir/log"
state() { cat "$dir/$1" 2>/dev/null || echo inactive; }
case "$1" in
is-active) s=$(state "$2"); echo "$s"; [ "$s" = active ] ;;
reset-failed) exit 0 ;;
start)
    if [ "$2" = --wait ]; then unit=$3; else unit=$2; fi
    [ -e "$dir/$unit.broken" ] && { echo "Job for $unit failed."; exit 1; }
    case "$unit" in
    couch-kodi.service) echo inactive > "$dir/couch-session.service" ;;
    couch-session.service) echo inactive > "$dir/couch-kodi.service" ;;
    esac
    echo active > "$dir/$unit"
    if [ "$2" = --wait ]; then
        while [ "$(state "$unit")" = active ]; do sleep 0.02; done
        exit "$(cat "$dir/$unit.rc" 2>/dev/null || echo 0)"
    fi ;;
esac
"""


@pytest.fixture
def systemd(tmp_path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    units = tmp_path / "units"
    units.mkdir()
    (units / SESSION_UNIT).write_text("active\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_SYSTEMD", str(units))
    monkeypatch.setattr(display_module, "KODI_START_TIMEOUT", 2.0)
    return units


async def until(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


def unit(systemd: Path, name: str) -> str:
    path = systemd / name
    return path.read_text().strip() if path.exists() else "inactive"


async def test_hands_the_screen_to_kodi_and_back(systemd):
    display = SystemdDisplay()
    assert await display.owner() == "browser"
    exits: list[bool] = []

    await display.start_kodi(exits.append)
    assert await display.owner() == "kodi"
    assert unit(systemd, SESSION_UNIT) == "inactive"

    await display.show_browser()
    assert await display.owner() == "browser"
    await until(lambda: exits)
    assert exits == [False]
    log = (systemd / "log").read_text()
    assert f"reset-failed {SESSION_UNIT}" in log
    await display.stop()


async def test_kodi_crashing_is_reported(systemd):
    display = SystemdDisplay()
    exits: list[bool] = []
    (systemd / f"{KODI_UNIT}.rc").write_text("3")
    await display.start_kodi(exits.append)
    (systemd / KODI_UNIT).write_text("failed\n")
    await until(lambda: exits)
    assert exits == [True]
    assert await display.owner() == "none"  # the stuck screen couchd must fix
    await display.stop()


async def test_kodi_that_wont_start(systemd):
    (systemd / f"{KODI_UNIT}.broken").touch()
    display = SystemdDisplay()
    with pytest.raises(BackendError, match="didn't start"):
        await display.start_kodi(lambda failed: None)
    await display.stop()


async def test_a_session_that_wont_start(systemd):
    (systemd / f"{SESSION_UNIT}.broken").touch()
    display = SystemdDisplay()
    with pytest.raises(BackendError, match="couldn't start the launcher"):
        await display.show_browser()


async def test_stopping_couchd_leaves_kodi_running(systemd):
    display = SystemdDisplay()
    await display.start_kodi(lambda failed: None)
    await display.stop()
    assert unit(systemd, KODI_UNIT) == "active"

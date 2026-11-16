"""install.sh can't run in CI (it needs a Pi), but it must parse and find its files."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_install_sh_parses():
    subprocess.run(["bash", "-n", str(INSTALL)], check=True)


def test_every_system_file_it_installs_exists():
    script = INSTALL.read_text()
    named = set(re.findall(r'\$SRC/system/([\w.-]+)"', script))
    units = re.search(r"^UNITS=\((.*)\)$", script, re.M)
    assert units, "UNITS=(...) not found"
    named |= set(units.group(1).split())
    static = re.search(r"^STATIC_UNITS=\((.*)\)$", script, re.M)
    assert static, "STATIC_UNITS=(...) not found"
    named |= set(static.group(1).split())
    assert {
        "couchd.service",
        "couch-session.service",
        "couch-browser",
        "couch-session-start",
        "couch-swayimg.conf",
        "couch-kodi.service",
        "couch-kodi",
        "couch.rules",
        "kodi",
        "60-couch-uinput.rules",
        "couch-uinput.conf",
    } <= named
    for name in named:
        path = ROOT / "system" / name
        assert path.is_dir() if name == "kodi" else path.is_file(), name


def test_session_runs_the_installed_browser_wrapper():
    unit = (ROOT / "system" / "couch-session.service").read_text()
    assert "ExecStart=/usr/bin/cage -s -- /opt/couch/bin/couch-session-start" in unit
    for name in ("couch-session-start", "couch-browser"):
        wrapper = rf'"\$SRC/system/{name}" "\$PREFIX/bin/{name}" 755'
        assert re.search(wrapper, INSTALL.read_text()), name
    start = (ROOT / "system" / "couch-session-start").read_text()
    assert "exec /opt/couch/bin/couch-browser" in start


def test_the_session_splash_is_the_startup_frame_with_no_keys():
    start = (ROOT / "system" / "couch-session-start").read_text()
    assert "SPLASH=/opt/couch/ui/brand/splash.png" in start
    assert (ROOT / "ui" / "brand" / "splash.png").is_file()
    assert "couch-splash.pid" in start
    conf_install = r'"\$SRC/system/couch-swayimg.conf" "\$PREFIX/share/swayimg/config"'
    assert re.search(conf_install, INSTALL.read_text())
    conf = (ROOT / "system" / "couch-swayimg.conf").read_text()
    keys = conf.split("[keys.viewer]")[1]
    bindings = [line for line in keys.splitlines() if "=" in line and not line.startswith("#")]
    assert {"Escape", "q", "Return", "Left", "Right", "Up", "Down", "BackSpace"} <= {
        b.split("=")[0].strip() for b in bindings
    }
    assert all(b.split("=", 1)[1].strip() == "none" for b in bindings)
    assert "show = no" in conf


def test_kodi_and_the_session_take_turns():
    kodi = (ROOT / "system" / "couch-kodi.service").read_text()
    assert re.search(r"^Conflicts=.*couch-session\.service", kodi, re.M)
    assert re.search(r"^After=.*couch-session\.service", kodi, re.M)
    # the launcher comes back however Kodi ends, couchd or not
    assert "OnSuccess=couch-session.service" in kodi
    assert "OnFailure=couch-session.service" in kodi
    assert not re.search(r"^\[Install\]", kodi, re.M)  # only couchd starts it
    assert "ExecStart=/opt/couch/bin/couch-kodi" in kodi
    # stopping Kodi ends its shell wrapper with 143; that's a clean stop, not a crash
    assert re.search(r"^SuccessExitStatus=.*\b143\b", kodi, re.M)


def test_polkit_rule_covers_only_our_units():
    rule = (ROOT / "system" / "couch.rules").read_text()
    assert set(re.findall(r'"([\w-]+\.service)"', rule)) == {
        "couch-session.service",
        "couch-kodi.service",
    }
    assert 'subject.user !== "couch"' in rule


def test_polkit_rule_allows_only_reboot_and_power_off():
    rule = (ROOT / "system" / "couch.rules").read_text()
    assert set(re.findall(r'"(org\.freedesktop\.login1\.[\w-]+)"', rule)) == {
        "org.freedesktop.login1.reboot",
        "org.freedesktop.login1.reboot-multiple-sessions",
        "org.freedesktop.login1.power-off",
        "org.freedesktop.login1.power-off-multiple-sessions",
    }


def test_browser_listens_for_devtools_on_loopback_only():
    wrapper = (ROOT / "system" / "couch-browser").read_text()
    assert '--remote-debugging-port="${COUCH_CDP_PORT:-9222}"' in wrapper
    assert "--remote-debugging-address=127.0.0.1" in wrapper
    assert "--remote-allow-origins" not in wrapper  # pages must not reach DevTools


def test_browser_turns_off_the_forced_screen_reader_tree():
    wrapper = (ROOT / "system" / "couch-browser").read_text()
    # Pi OS's /etc/chromium.d forces renderer accessibility on; this turns it back off.
    assert "--disable-renderer-accessibility" in wrapper
    assert "--enable-gpu-rasterization" in wrapper
    # YouTube's user agent is set per tab (couchd/youtube.py), not for the whole browser.
    assert not re.search(r"^\s*--user-agent", wrapper, re.MULTILINE)


def test_uinput_is_for_group_input_only():
    rule = (ROOT / "system" / "60-couch-uinput.rules").read_text()
    assert 'KERNEL=="uinput"' in rule
    assert 'GROUP="input"' in rule
    assert 'MODE="0660"' in rule
    script = INSTALL.read_text()
    assert "python3-evdev" in script
    assert re.search(r"^COUCH_GROUPS=\(.*\binput\b.*\)$", script, re.M)

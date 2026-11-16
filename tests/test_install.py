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
        "couch-kodi.service",
        "couch-kodi",
        "couch.rules",
        "kodi",
    } <= named
    for name in named:
        path = ROOT / "system" / name
        assert path.is_dir() if name == "kodi" else path.is_file(), name


def test_session_runs_the_installed_browser_wrapper():
    unit = (ROOT / "system" / "couch-session.service").read_text()
    assert "ExecStart=/usr/bin/cage -s -- /opt/couch/bin/couch-browser" in unit
    wrapper = r'"\$SRC/system/couch-browser" "\$PREFIX/bin/couch-browser" 755'
    assert re.search(wrapper, INSTALL.read_text())


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


def test_browser_listens_for_devtools_on_loopback_only():
    wrapper = (ROOT / "system" / "couch-browser").read_text()
    assert '--remote-debugging-port="${COUCH_CDP_PORT:-9222}"' in wrapper
    assert "--remote-debugging-address=127.0.0.1" in wrapper
    assert "--remote-allow-origins" not in wrapper  # pages must not reach DevTools

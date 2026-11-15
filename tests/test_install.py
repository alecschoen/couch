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
    assert {"couchd.service", "couch-session.service", "couch-browser"} <= named
    for name in named:
        assert (ROOT / "system" / name).is_file(), name


def test_session_runs_the_installed_browser_wrapper():
    unit = (ROOT / "system" / "couch-session.service").read_text()
    assert "ExecStart=/usr/bin/cage -s -- /opt/couch/bin/couch-browser" in unit
    wrapper = r'"\$SRC/system/couch-browser" "\$PREFIX/bin/couch-browser" 755'
    assert re.search(wrapper, INSTALL.read_text())

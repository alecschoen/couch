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
        "couch-splash",
        "couch-kodi.service",
        "couch-kodi",
        "couch-kodi-vt",
        "couch.rules",
        "kodi",
        "60-couch-uinput.rules",
        "70-couch-no-pointer.rules",
        "couch-uinput.conf",
        "couch-wifi-boot",
        "couch-wifi-boot.service",
        "70-couch-wifi.rules",
        "systemd-rfkill-couch.conf",
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


def test_the_session_splash_is_installed_next_to_what_it_draws_with():
    script = INSTALL.read_text()
    assert re.search(r'"\$SRC/system/couch-splash" "\$PREFIX/bin/couch-splash" 755', script)
    # couch-splash borrows couch-fbsplash's drawing code from the same directory
    assert re.search(r'"\$SRC/system/couch-fbsplash" "\$PREFIX/bin/couch-fbsplash" 755', script)
    assert "swayimg" not in script
    start = (ROOT / "system" / "couch-session-start").read_text()
    assert "couch-splash.pid" in start
    assert "/opt/couch/bin/couch-splash" in start


def test_kodi_runs_in_front_of_the_waiting_session():
    kodi = (ROOT / "system" / "couch-kodi.service").read_text()
    # its own VT, so the session (tty1) keeps running behind it
    assert "TTYPath=/dev/tty2" in kodi
    assert not re.search(r"^Conflicts=.*couch-session\.service", kodi, re.M)
    assert re.search(r"^Conflicts=.*autovt@tty2\.service", kodi, re.M)  # no login prompt
    # the console goes to tty2 before Kodi opens the screen, and back to tty1 however it ends
    # (couch-kodi-vt, which waits for Kodi to let go of the screen first)
    assert "ExecStartPre=+/opt/couch/bin/couch-kodi-vt enter" in kodi
    assert "ExecStopPost=+/opt/couch/bin/couch-kodi-vt leave" in kodi
    # nothing puts tty2 back in text mode under Kodi: the black console would show
    assert not re.search(r"^TTY(Reset|VTDisallocate)=yes", kodi, re.M)
    # and no login prompt on tty2 to fight Kodi for it
    script = (ROOT / "install.sh").read_text()
    assert "KODI_VT_GETTYS=(getty@tty2.service autovt@tty2.service)" in script
    assert 'ln -sfn /dev/null "$UNIT_DIR/$u"' in script
    unmask = 'if [[ $(readlink "$UNIT_DIR/$u") == /dev/null ]]; then rm -f "$UNIT_DIR/$u"; fi'
    assert unmask in script
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
        "couch-update-now.service",  # Check now (#24): start only
    }
    assert 'subject.user !== "couch"' in rule
    assert 'unit === "couch-update-now.service" && verb === "start"' in rule


def test_updates_are_installed():
    """#24: couch-update's units, the apt drop-in, and a copy of the source to go back to."""
    script = INSTALL.read_text()
    units = re.search(r"^UPDATE_UNITS=\((.*)\)$", script, re.M).group(1).split()
    assert units == ["couch-update.service", "couch-update-now.service", "couch-update.timer"]
    for name in [*units, "apt-couch.conf", "update.example.toml"]:
        assert (ROOT / "system" / name).is_file(), name
    files = re.search(r"^SOURCE_FILES=\((.*)\)$", script, re.M).group(1).split()
    for name in files:
        assert (ROOT / name).exists(), name
    # unattended-upgrades and the timer only once the night runs are turned on
    assert "ensure_packages unattended-upgrades" in script
    assert not re.search(r"^\s+unattended-upgrades\s", script, re.M)
    assert '/usr/bin/python3 -I "$SRC/couchd/updater.py" enabled' in script
    assert re.search(r"^install_updates$", script, re.M)
    assert re.search(r"^    uninstall_updates$", script, re.M)
    for unit in ("couch-update.service", "couch-update-now.service"):
        text = (ROOT / "system" / unit).read_text()
        # the system Python: install.sh rebuilds couchd's venv while it runs
        assert "ExecStart=/usr/bin/python3 -I /opt/couch/couchd/updater.py" in text
        assert "StateDirectory=couch-update" in text
    assert not re.search(
        r"^\[Install\]", (ROOT / "system" / "couch-update-now.service").read_text(), re.M
    )
    apt = (ROOT / "system" / "apt-couch.conf").read_text()
    assert 'APT::Periodic::Unattended-Upgrade "0";' in apt  # couch-update runs it, when idle


def test_updater_uses_the_standard_library_only():
    import ast

    tree = ast.parse((ROOT / "couchd" / "updater.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    import sys

    assert names - {"__future__"} <= sys.stdlib_module_names, names - sys.stdlib_module_names


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


def test_the_cec_inputs_are_not_pointers():
    # rc-core's CEC inputs have mouse axes, so cage drew a cursor (#4).
    rule = (ROOT / "system" / "70-couch-no-pointer.rules").read_text()
    assert 'ATTRS{name}=="vc4-hdmi-[0-9]"' in rule
    assert 'ENV{LIBINPUT_IGNORE_DEVICE}="1"' in rule
    assert '"$NO_POINTER_RULE"' in INSTALL.read_text().split("uninstall() {")[1]


def _source_version(src: Path) -> str:
    """Run install.sh's source_version on its own."""
    fn = re.search(r"^source_version\(\) \{\n.*?^\}\n", INSTALL.read_text(), re.M | re.S).group(0)
    out = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", fn + 'source_version "$1"', "_", str(src)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_installed_version_is_never_a_leftover(tmp_path):
    """#24: Settings shows /opt/couch/VERSION; install.sh writes it from what it installs."""

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
            check=True, capture_output=True,
        )  # fmt: skip

    repo = tmp_path / "checkout"
    repo.mkdir()
    git("init", "-q")
    (repo / "a").write_text("1")
    git("add", "a")
    git("commit", "-qm", "one")
    # a VERSION left in a checkout (say, by hand) doesn't count: git does
    (repo / "VERSION").write_text("v0.0.4\n")
    assert re.fullmatch(r"dev-[0-9a-f]{7,}-dirty|dev-[0-9a-f]{7,}", _source_version(repo))
    (repo / "VERSION").unlink()
    assert re.fullmatch(r"dev-[0-9a-f]{7,}", _source_version(repo))
    git("tag", "v0.3.0")
    assert _source_version(repo) == "v0.3.0"
    (repo / "a").write_text("2")
    assert _source_version(repo) == "v0.3.0-dirty"
    git("commit", "-qam", "two")
    assert re.fullmatch(r"v0\.3\.0-1-g[0-9a-f]{7,}", _source_version(repo))
    # a release unpacked by couch-update: its VERSION file
    release = tmp_path / "release"
    release.mkdir()
    (release / "VERSION").write_text("v0.4.0\n")
    assert _source_version(release) == "v0.4.0"
    # neither: nothing, and install_updates removes VERSION
    assert _source_version(tmp_path / "bare") == ""
    script = INSTALL.read_text()
    assert 'version=$(source_version "$SRC")' in script
    assert re.search(r'if \[\[ -z \$version \]\]; then\n\s+rm -f "\$f"', script)

"""SD card protection against power cuts (#25): the system files and install.sh's wiring."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SYSTEM = ROOT / "system"
INSTALL = (ROOT / "install.sh").read_text()


def function(name: str) -> str:
    return re.search(rf"^{name}\(\) \{{(.*?)^\}}", INSTALL, re.M | re.S).group(1)


def test_journal_is_in_ram():
    conf = (SYSTEM / "journald-couch.conf").read_text()
    assert re.search(r"^\[Journal\]$", conf, re.M)
    assert re.search(r"^Storage=volatile$", conf, re.M)
    assert re.search(r"^RuntimeMaxUse=\d+M$", conf, re.M)


def test_cache_mount_is_a_bounded_tmpfs_named_after_its_path():
    unit = (SYSTEM / "var-cache-couch.mount").read_text()
    assert "Where=/var/cache/couch\n" in unit  # systemd wants the name to match the path
    assert "Type=tmpfs\n" in unit
    options = re.search(r"^Options=(.*)$", unit, re.M).group(1).split(",")
    assert {"nosuid", "nodev"} <= set(options)
    assert any(re.fullmatch(r"size=\d+[MG]", o) for o in options)
    assert "WantedBy=local-fs.target" in unit


def test_cache_directories_match_what_the_wrappers_look_for():
    tmpfiles = (SYSTEM / "couch-cache.conf").read_text()
    made = set(re.findall(r"^d (\S+)\s+0700 couch couch -$", tmpfiles, re.M))
    assert made == {"/var/cache/couch/chromium", "/var/cache/couch/kodi", "/var/cache/couch/plex"}
    assert "cache=/var/cache/couch/chromium" in (SYSTEM / "couch-browser").read_text()
    kodi = (SYSTEM / "couch-kodi").read_text()
    assert "export KODI_TEMP=/var/cache/couch/kodi" in kodi
    # The sign-ins stay where they are.
    assert "--user-data-dir=/var/lib/couch/chromium" in (SYSTEM / "couch-browser").read_text()
    assert "export KODI_DATA=/var/lib/couch/kodi" in kodi
    from couchd.plex import DEVICE_CACHE

    assert str(DEVICE_CACHE) == "/var/cache/couch/plex"


def browser_args(tmp_path: Path, cache_exists: bool) -> list[str]:
    fake = tmp_path / "chromium"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    fake.chmod(0o755)
    cache = tmp_path / "cache"
    if cache_exists:
        cache.mkdir()
    script = (SYSTEM / "couch-browser").read_text()
    script = script.replace("/usr/bin/chromium", str(fake))
    script = script.replace("cache=/var/cache/couch/chromium", f"cache={cache}")
    wrapper = tmp_path / "couch-browser"
    wrapper.write_text(script)
    out = subprocess.run(["sh", str(wrapper)], capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_browser_puts_its_cache_in_ram_when_it_can(tmp_path):
    args = browser_args(tmp_path, cache_exists=True)
    assert f"--disk-cache-dir={tmp_path / 'cache'}" in args
    assert "--disk-cache-size=104857600" in args
    assert args[-1] == "about:blank"  # the URL stays last; couchd loads YouTube
    assert "--hide-crash-restore-bubble" in args


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_browser_keeps_the_cache_in_its_profile_without_the_tmpfs(tmp_path):
    args = browser_args(tmp_path, cache_exists=False)
    assert not [a for a in args if a.startswith("--disk-cache")]
    assert args[-1] == "about:blank"


def test_install_sets_it_up_before_the_session_starts():
    main = INSTALL[INSTALL.index('say "Packages"') :]
    assert main.index("install_sd_card") < main.index("install_couch")
    assert main.index("ensure_user") < main.index("install_sd_card")  # tmpfiles needs couch
    body = function("install_sd_card")
    for path in ("journald-couch.conf", "var-cache-couch.mount", "couch-cache.conf"):
        assert f'"$SRC/system/{path}"' in body
    assert '"$FSTAB_TOOL" apply sd-card' in body
    assert '"$BOOTFILES" apply sd-card' in body
    assert re.search(r"^SD_NOATIME=\(/ /boot/firmware\)", INSTALL, re.M)
    assert re.search(r"^SD_CMDLINE=\(fsck\.repair=yes\)", INSTALL, re.M)


def test_revert_undoes_each_piece():
    assert re.search(r"^--revert-sd\) uninstall_sd_card;", INSTALL, re.M)
    body = function("uninstall_sd_card")
    assert "revert sd-card" in body
    assert '"$bootfiles" revert sd-card' in body
    for var in ("SD_JOURNALD_CONF", "SD_CACHE_MOUNT", "SD_CACHE_TMPFILES", "SD_TMP_CONF"):
        assert f'"${var}"' in body
    # --uninstall reverts it too, while the tools are still installed.
    uninstall = function("uninstall")
    assert uninstall.index("uninstall_sd_card") < uninstall.index('rm -rf "$PREFIX"')


def test_usage_shows_the_whole_header():
    header_end = INSTALL.splitlines().index("set -euo pipefail") + 1  # 1-based line number
    assert f"sed -n '2,{header_end - 1}p'" in INSTALL
    assert "--revert-sd" in "\n".join(INSTALL.splitlines()[: header_end - 1])

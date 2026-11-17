"""Couch's cage (cage/): the Pi's cage, patched to draw the boot logo from its first frame,
to hand the keyboard on when a window unmaps, and to keep a keyboard while devices are away."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAGE = ROOT / "cage"


def test_it_is_built_from_the_pis_own_cage_source():
    dockerfile = (CAGE / "Dockerfile").read_text()
    build = (CAGE / "build.sh").read_text()
    version = re.search(r"^ARG CAGE_VERSION=(\S+)$", dockerfile, re.M).group(1)
    assert version == "0.3.1-1~bpo13+1+rpt2"  # Raspberry Pi OS trixie's, as on the Pi
    assert f"DSC=cage_{version}.dsc" in build
    assert re.search(r"^DSC_SHA256=[0-9a-f]{64}$", build, re.M)
    assert "sha256sum -c" in build
    # from the Raspberry Pi archive, checked against its key, with the Pi's wlroots
    assert "Signed-By: /usr/share/keyrings/raspberrypi-archive-keyring.pgp" in dockerfile
    assert "libwlroots-0.20-dev:$ARCH" in dockerfile
    assert (CAGE / "raspberrypi-archive-keyring.pgp").stat().st_size > 1000


def test_the_patch_adds_a_background_that_goes_when_the_first_window_maps():
    patch = (CAGE / "couch-background.patch").read_text()
    files = set(re.findall(r"^\+\+\+ b/(\S+)$", patch, re.M))
    assert files == {
        "background.c", "background.h", "cage.c", "meson.build", "output.c", "server.h", "view.c"
    }  # fmt: skip
    assert '"b:dDhm:sv"' in patch
    assert "+\tbackground_remove(view->server);" in patch  # in view_map
    assert "wlr_scene_output_build_state(output->scene_output, &state, NULL)" in patch


def test_the_keyboard_patches_are_applied_after_the_background():
    build = (CAGE / "build.sh").read_text()
    applied = re.findall(r"^patch -p1 --quiet </couch-cage/(\S+)$", build, re.M)
    assert applied == ["couch-background.patch", "couch-focus.patch", "couch-keyboard.patch"]
    focus = (CAGE / "couch-focus.patch").read_text()
    assert set(re.findall(r"^\+\+\+ b/(\S+)$", focus, re.M)) == {"xdg_shell.c"}
    assert "+\t\tseat_set_focus(server->seat, next);" in focus  # in the unmap handler
    keyboard = (CAGE / "couch-keyboard.patch").read_text()
    assert set(re.findall(r"^\+\+\+ b/(\S+)$", keyboard, re.M)) == {"seat.c"}
    assert "-\twlr_seat_set_keyboard(seat->seat, keyboard);" in keyboard
    assert "&keyboard->group->keyboard" in keyboard  # the group's, which outlives the device


def test_install_installs_it_for_the_session():
    install = (ROOT / "install.sh").read_text()
    assert '"$SRC/cage/dist/cage-$(uname -m)" "$PREFIX/bin/cage" 755' in install
    start = (ROOT / "system/couch-session-start").read_text()
    assert 'exec /opt/couch/bin/cage -b "$tga" -s -- "$0"' in start
    assert 'exec /usr/bin/cage -s -- "$0"' in start  # without it


def test_its_builds_are_not_committed():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "cage/dist/cage-aarch64"], cwd=ROOT, check=False
    )
    assert ignored.returncode == 0

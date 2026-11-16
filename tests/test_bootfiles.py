"""system/couch-bootfiles: edits cmdline.txt and config.txt, and undoes exactly that."""

import importlib.machinery
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("bootfiles", str(ROOT / "system/couch-bootfiles"))
_spec = importlib.util.spec_from_loader("bootfiles", _loader)
bf = importlib.util.module_from_spec(_spec)
_loader.exec_module(bf)

STOCK_CMDLINE = (
    "console=serial0,115200 console=tty1 root=PARTUUID=21f4d5e7-02 rootfstype=ext4 "
    "fsck.repair=yes rootwait\n"
)
STOCK_CONFIG = "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\n[cm4]\notg_mode=1\n[all]\n"
SETS = ["quiet", "loglevel=0", "console=tty3", "fullscreen_logo=1"]
DROPS = ["console=tty1"]


@pytest.fixture
def boot(tmp_path):
    (tmp_path / "cmdline.txt").write_text(STOCK_CMDLINE)
    (tmp_path / "config.txt").write_text(STOCK_CONFIG)
    (tmp_path / "extra.txt").write_text("# a comment\n[all]\ndisable_splash=1\n\n")
    return tmp_path


def run(boot, *args):
    assert bf.main(["--boot", str(boot), *args]) == 0


def apply(boot, owner="boot", sets=SETS, drops=DROPS, config="extra.txt"):
    args = ["apply", owner]
    for t in sets:
        args += ["--set", t]
    for t in drops:
        args += ["--drop", t]
    if config:
        args += ["--config", str(boot / config)]
    run(boot, *args)


def test_apply_edits_only_our_tokens(boot):
    apply(boot)
    assert (boot / "cmdline.txt").read_text() == (
        "console=serial0,115200 root=PARTUUID=21f4d5e7-02 rootfstype=ext4 fsck.repair=yes "
        "rootwait quiet loglevel=0 console=tty3 fullscreen_logo=1\n"
    )
    config = (boot / "config.txt").read_text()
    assert config.startswith(STOCK_CONFIG)
    assert config.endswith("# a comment\n[all]\ndisable_splash=1\n# <<< couch:boot\n")
    assert (boot / "cmdline.txt.couch-orig").read_text() == STOCK_CMDLINE
    assert (boot / "config.txt.couch-orig").read_text() == STOCK_CONFIG


def test_apply_twice_changes_nothing(boot, capsys):
    apply(boot)
    capsys.readouterr()
    before = {p.name: p.read_bytes() for p in boot.iterdir()}
    apply(boot)
    assert capsys.readouterr().out == ""
    assert {p.name: p.read_bytes() for p in boot.iterdir()} == before


def test_revert_restores_the_originals_and_cleans_up(boot):
    apply(boot)
    run(boot, "revert", "boot")
    assert (boot / "cmdline.txt").read_text() == STOCK_CMDLINE
    assert (boot / "config.txt").read_text() == STOCK_CONFIG
    assert sorted(p.name for p in boot.iterdir()) == ["cmdline.txt", "config.txt", "extra.txt"]


def test_revert_without_apply_does_nothing(boot, capsys):
    run(boot, "revert", "boot")
    assert capsys.readouterr().out == ""
    assert (boot / "cmdline.txt").read_text() == STOCK_CMDLINE


def test_tokens_that_were_there_already_stay_on_revert(boot):
    (boot / "cmdline.txt").write_text("root=/dev/sda2 quiet loglevel=3 rootwait\n")
    apply(boot)
    assert (boot / "cmdline.txt").read_text() == (
        "root=/dev/sda2 quiet loglevel=0 rootwait console=tty3 fullscreen_logo=1\n"
    )
    run(boot, "revert", "boot")
    assert (boot / "cmdline.txt").read_text() == "root=/dev/sda2 quiet loglevel=3 rootwait\n"


def test_reapply_with_fewer_tokens_undoes_the_ones_dropped(boot):
    apply(boot)
    apply(boot, sets=["quiet"], drops=[])
    assert (boot / "cmdline.txt").read_text() == STOCK_CMDLINE.replace("\n", " quiet\n")


def test_changes_made_by_others_survive_our_revert(boot):
    apply(boot)
    line = (boot / "cmdline.txt").read_text().replace("rootwait", "rootwait splash")
    (boot / "cmdline.txt").write_text(line)
    with open(boot / "config.txt", "a") as f:
        f.write("dtoverlay=foo\n")
    run(boot, "revert", "boot")
    assert (boot / "cmdline.txt").read_text() == STOCK_CMDLINE.replace(
        "rootwait", "rootwait splash"
    )
    assert (boot / "config.txt").read_text() == STOCK_CONFIG + "dtoverlay=foo\n"
    # the originals stay for a person to look at, since the files differ from them now
    assert (boot / "cmdline.txt.couch-orig").exists()


def test_two_owners_undo_independently(boot):
    apply(boot)
    (boot / "other.txt").write_text("dtparam=sd_poll_once\n")
    apply(boot, owner="sdcard", sets=["fsck.mode=skip"], drops=[], config="other.txt")
    run(boot, "revert", "boot")
    assert (boot / "cmdline.txt").read_text() == STOCK_CMDLINE.replace("\n", " fsck.mode=skip\n")
    config = (boot / "config.txt").read_text()
    assert "couch:boot" not in config and "# >>> couch:sdcard" in config
    state = json.loads((boot / "couch-boot.json").read_text())
    assert list(state["cmdline"]) == ["sdcard"] and state["config"] == ["sdcard"]
    run(boot, "revert", "sdcard")
    assert (boot / "cmdline.txt").read_text() == STOCK_CMDLINE
    assert (boot / "config.txt").read_text() == STOCK_CONFIG
    assert not (boot / "couch-boot.json").exists()


def test_never_writes_a_cmdline_without_root(boot):
    with pytest.raises(SystemExit):
        apply(boot, sets=[], drops=["root=PARTUUID=21f4d5e7-02"])
    assert (boot / "cmdline.txt").read_text() == STOCK_CMDLINE


@pytest.mark.parametrize(
    ("token", "key"),
    [
        ("quiet", "quiet"),
        ("loglevel=0", "loglevel"),
        ("console=tty3", "console=tty3"),
        ("video=HDMI-A-1:1920x1080@60", "video=HDMI-A-1"),
    ],
)
def test_keys(token, key):
    assert bf.key(token) == key


def test_install_uses_it_for_the_silent_boot():
    script = (ROOT / "install.sh").read_text()
    assert '"$BOOTFILES" apply boot' in script
    assert '"$tool" revert boot' in script
    for token in ("quiet", "loglevel=0", "vt.global_cursor_default=0", "consoleblank=0"):
        assert token in script
    assert "fullscreen_logo_name=couch-splash.tga" in script
    tokens = re.search(r"^BOOT_CMDLINE=\((.*?)^\)", script, re.M | re.S).group(1)
    assert "logo.nologo" not in tokens  # it would hide the fullscreen logo too
    config = (ROOT / "system/boot-config.txt").read_text()
    assert "disable_splash=1" in config and "disable_overscan=1" in config
    assert "cec" not in config.lower().replace("hdmi-cec stays on", "").replace("/dev/cec0", "")


def test_splash_tga_is_what_the_kernel_reads():
    tga = (ROOT / "ui/brand/splash.tga").read_bytes()
    w, h = int.from_bytes(tga[12:14], "little"), int.from_bytes(tga[14:16], "little")
    assert tga[2] == 2 and tga[16] == 24  # uncompressed true colour, 24 bpp
    assert w <= 1920 and h <= 1080 and (1920 - w) % 2 == 0 and (1080 - h) % 2 == 0
    pixels = tga[18 + tga[0] :]
    assert len(pixels) == w * h * 3
    colours = {pixels[i : i + 3] for i in range(0, len(pixels), 3)}
    assert len(colours) <= 224
    assert pixels[:3] == bytes((0x14, 0x12, 0x12))  # night (BGR): the kernel fills with it

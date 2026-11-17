"""system/couch-bootfiles: edits cmdline.txt and config.txt, and undoes exactly that."""

import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import sys
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


def test_boot_can_be_reverted_on_its_own():
    script = (ROOT / "install.sh").read_text()
    assert re.search(r"^--revert-boot\) uninstall_boot;", script, re.M)
    body = re.search(r"^uninstall_boot\(\) \{(.*?)^\}", script, re.M | re.S).group(1)
    assert "update-initramfs" in body and "$INITRAMFS_HOOK" in body and "$SPLASH_TGA" in body


def test_session_keeps_the_splash_on_tty1():
    unit = (ROOT / "system/couch-session.service").read_text()
    settings = [ln for ln in unit.splitlines() if not ln.startswith("#")]
    assert not any(ln.startswith(("TTYReset=", "TTYVTDisallocate=")) for ln in settings)
    assert "TTYPath=/dev/tty1" in settings


def test_cloud_init_comes_back_only_if_couch_turned_it_off():
    script = (ROOT / "install.sh").read_text()
    assert 'echo "$CLOUD_INIT_MARK" >"$CLOUD_INIT_OFF"' in script
    body = re.search(r"^uninstall_boot\(\) \{(.*?)^\}", script, re.M | re.S).group(1)
    assert '== "$CLOUD_INIT_MARK"' in body and 'rm -f "$CLOUD_INIT_OFF"' in body


# --- the real thing: the Pi's own boot files, install.sh's own tokens, the script as a CLI --

PI_BOOT = ROOT / "tests/data/pi-boot"  # copied from the Pi (Pi OS Lite Trixie, 16 Nov 2026)


def install_tokens(name):
    """An array from install.sh, as bash itself reads it (comments and all)."""
    script = (ROOT / "install.sh").read_text()
    # one line, or many up to a lone ")" (comments in it may hold parentheses)
    block = re.search(rf"^{name}=\((?:[^\n]*\)$|.*?^\)$)", script, re.M | re.S).group(0)
    out = subprocess.run(
        ["bash", "-c", f'{block}\nprintf "%s\\n" "${{{name}[@]}}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def cli(boot, *args):
    cmd = [sys.executable, str(ROOT / "system/couch-bootfiles"), "--boot", str(boot), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_install_sh_edits_on_the_real_pi_files_revert_byte_for_byte(tmp_path):
    for f in PI_BOOT.iterdir():
        shutil.copy(f, tmp_path / f.name)
    original = {f.name: f.read_bytes() for f in PI_BOOT.iterdir()}
    sets, drops = install_tokens("BOOT_CMDLINE"), install_tokens("BOOT_CMDLINE_DROP")
    assert "fullscreen_logo=1" in sets and drops == ["console=tty1"]
    args = ["apply", "boot", "--config", str(ROOT / "system/boot-config.txt")]
    args += [a for t in sets for a in ("--set", t)] + [a for t in drops for a in ("--drop", t)]

    assert cli(tmp_path, *args)  # it says what it changed
    cmdline = (tmp_path / "cmdline.txt").read_text()
    assert cmdline.count("\n") == 1 and cmdline.endswith("\n")
    tokens = cmdline.split()
    assert "console=tty1" not in tokens and "root=PARTUUID=21f4d5e7-02" in tokens
    assert tokens[-len(sets) :] == sets
    config = (tmp_path / "config.txt").read_text()
    assert config.startswith(original["config.txt"].decode())
    assert "\ndisable_splash=1\n" in config and "dtoverlay=vc4-kms-v3d" in config

    assert cli(tmp_path, *args) == ""  # a second install changes nothing
    cli(tmp_path, "revert", "boot")
    assert {f.name: f.read_bytes() for f in tmp_path.iterdir()} == original


_fb_loader = importlib.machinery.SourceFileLoader("fbsplash", str(ROOT / "system/couch-fbsplash"))
fbsplash = importlib.util.module_from_spec(importlib.util.spec_from_loader("fbsplash", _fb_loader))
_fb_loader.exec_module(fbsplash)

RGB565 = (16, (11, 5), (5, 6), (0, 5))  # vc4drmfb on the Pi: bpp, then (offset, length) of r, g, b
XRGB8888 = (32, (16, 8), (8, 8), (0, 8))


@pytest.mark.parametrize("layout", [RGB565, XRGB8888], ids=["rgb565", "xrgb8888"])
def test_fbsplash_draws_the_tga_centred_like_the_kernel(layout):
    bpp, *rgb = layout
    w, h, rows = fbsplash.read_tga((ROOT / "ui/brand/splash.tga").read_bytes())
    xres, yres, size = 1920, 1080, bpp // 8
    stride = xres * size + 64  # padding at the end of each line stays untouched
    pack = fbsplash.packer(bpp, *rgb)
    frame = fbsplash.render(w, h, rows, xres, yres, stride, pack)
    assert len(frame) == stride * yres

    def px(x, y):
        return frame[y * stride + x * size : y * stride + (x + 1) * size]

    fill = pack(*rows[0][0])
    for x, y in [(0, 0), (xres - 1, yres - 1), (759, 540), (1160, 540), (960, 379), (960, 700)]:
        assert px(x, y) == fill, (x, y)
    dx, dy = (xres - w) // 2, (yres - h) // 2
    for iy in range(0, h, 7):
        for ix in range(0, w, 11):
            assert px(dx + ix, dy + iy) == pack(*rows[iy][ix])
    # Not all fill: the mark is in there.
    assert sum(px(dx + ix, dy + iy) != fill for iy in range(h) for ix in range(w)) > 1000


def test_fbsplash_packs_rgb565():
    pack = fbsplash.packer(*RGB565)
    assert pack(255, 255, 255) == b"\xff\xff"
    assert pack(255, 0, 0) == (0xF800).to_bytes(2, "little")
    assert pack(0, 255, 0) == (0x07E0).to_bytes(2, "little")
    assert pack(18, 18, 20) == ((18 >> 3) << 11 | (18 >> 2) << 5 | 20 >> 3).to_bytes(2, "little")


def test_fbsplash_runs_as_vc4s_framebuffer_appears():
    rule = (ROOT / "system/couch-fbsplash.rules").read_text()
    assert 'ATTR{name}=="vc4drmfb"' in rule and "couch-fbsplash.service" in rule
    unit = (ROOT / "system/couch-fbsplash.service").read_text()
    assert "DefaultDependencies=no" in unit
    assert "Before=shutdown.target couch-session.service" in unit
    install = (ROOT / "install.sh").read_text()
    assert '"$SRC/system/couch-fbsplash.rules" "$FBSPLASH_RULE"' in install
    assert 'rm -f "$FBSPLASH_RULE" "$FBSPLASH_UNIT"' in install


def test_cloud_init_done_is_read_from_its_output_not_its_exit_status():
    # Pi OS's first boot ends "degraded done", and `cloud-init status` then exits 2, which
    # pipefail turned into "not done".
    install = (ROOT / "install.sh").read_text()
    assert "status=$(cloud-init status 2>/dev/null) || true" in install
    assert "cloud-init status 2>/dev/null |" not in install


def test_revert_removes_the_udev_rule_before_rebuilding_the_initramfs():
    install = (ROOT / "install.sh").read_text()
    body = install[install.index("uninstall_boot() {") :]
    assert body.index('rm -f "$FBSPLASH_RULE"') < body.index("update-initramfs")


def test_fbsplash_draws_again_at_shutdown_once_cage_has_gone():
    unit = (ROOT / "system/couch-fbsplash.service").read_text()
    assert "RemainAfterExit=yes" in unit  # so it gets stopped, after couch-session
    assert "ExecStop=-/opt/couch/bin/couch-fbsplash fb0 --after-text tty1" in unit


def test_fbsplash_reuses_the_boot_frame(tmp_path, monkeypatch):
    """The shutdown redraw copies the frame drawn at boot instead of rendering it again."""
    monkeypatch.setattr(fbsplash, "CACHE", tmp_path / "cache")
    sysfs = tmp_path / "sys"
    (sysfs / "fb0").mkdir(parents=True)
    (sysfs / "fb0/stride").write_text("3840\n")
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "fb0").write_bytes(bytes(3840 * 1080))
    real_path, real_open = fbsplash.Path, open

    def path(p, *rest):
        p = str(p).replace("/sys/class/graphics", str(sysfs))
        return real_path(p, *rest)

    def fake_open(p, *a, **k):
        return real_open(str(p).replace("/dev/", str(dev) + "/"), *a, **k)

    monkeypatch.setattr(fbsplash, "Path", path)
    monkeypatch.setattr(fbsplash, "open", fake_open, raising=False)
    monkeypatch.setattr(fbsplash, "TGA", ROOT / "ui/brand/splash.tga")
    info = (1920, 1080, 0, 0, 16, (11, 5), (5, 6), (0, 5))
    monkeypatch.setattr(fbsplash, "screen_info", lambda fd: info)
    assert fbsplash.main(["couch-fbsplash", "fb0"]) == 0
    drawn = (dev / "fb0").read_bytes()
    (cached,) = (tmp_path / "cache").iterdir()
    assert cached.name == "fb0-1920x1080-16-3840-11-5-5-6-0-5"
    assert cached.read_bytes() == drawn
    (dev / "fb0").write_bytes(bytes(3840 * 1080))
    monkeypatch.setattr(fbsplash, "render", lambda *a: pytest.fail("rendered again"))
    assert fbsplash.main(["couch-fbsplash", "fb0"]) == 0
    assert (dev / "fb0").read_bytes() == drawn


def test_no_white_frame_under_youtube():
    browser = (ROOT / "system/couch-browser").read_text()
    assert "--force-dark-mode" in browser
    assert "--system-deep: #121214;" in (ROOT / "ui/css/tokens.css").read_text()

"""The boot animation (ui/brand/boot-animation.bin) and couch-fbsplash's player for it."""

import importlib.machinery
import importlib.util
import re
import struct
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("fbsplash_anim", str(ROOT / "system/couch-fbsplash"))
_spec = importlib.util.spec_from_loader("fbsplash_anim", _loader)
fbsplash = importlib.util.module_from_spec(_spec)
_loader.exec_module(fbsplash)

W, H = 1920, 1080
STRIDE = W * 2
RGB565 = ((11, 5), (5, 6), (0, 5))
DATA = (ROOT / "ui/brand/boot-animation.bin").read_bytes()


@pytest.fixture(scope="module")
def anim():
    return fbsplash.Animation(DATA)


class Screen:
    """A framebuffer in memory, written the way the player writes /dev/fb0."""

    def __init__(self, stride=STRIDE, base=0):
        self.buf = bytearray(base + stride * H)
        self.stride, self.base = stride, base

    def pwrite(self, fd, data, offset):
        self.buf[offset : offset + len(data)] = data
        return len(data)

    def visible(self):
        rows = (self.buf[self.base + y * self.stride :][: W * 2] for y in range(H))
        return b"".join(bytes(r) for r in rows)


@pytest.fixture
def screen(monkeypatch):
    s = Screen()
    monkeypatch.setattr(fbsplash.os, "pwrite", s.pwrite)
    return s


def still_logo():
    w, h, rows = fbsplash.read_tga((ROOT / "ui/brand/splash.tga").read_bytes())
    return fbsplash.render(w, h, rows, W, H, STRIDE, fbsplash.packer(16, *RGB565))


def test_the_animation_is_for_vc4s_boot_framebuffer(anim):
    assert (anim.width, anim.height, anim.fps) == (W, H, 30)
    assert anim.fits(W, H, 16, *RGB565)
    assert not anim.fits(W, H, 32, (16, 8), (8, 8), (0, 8))
    assert not anim.fits(3840, 2160, 16, *RGB565)
    # The intro fits between couch-fbsplash starting (about 7 s) and couch-session (11.5 s)
    # plus cage's start (about 3 s more); the outro is short.
    assert 4 <= len(anim.intro) / anim.fps <= 6
    assert len(anim.outro) / anim.fps <= 1.5
    assert len(DATA) < 4_000_000


def test_its_still_frame_is_the_kernels_logo(anim, screen):
    """The first frame is splash.tga drawn exactly as couch-fbsplash draws it, so the boot goes
    from the kernel's logo to the animation without a jump."""
    anim.draw(0, anim.still, 0, STRIDE)
    assert screen.visible() == still_logo()


def test_it_ends_on_the_still_logo_from_the_intro_and_from_the_idle_loop(anim, screen):
    anim.draw(0, anim.still, 0, STRIDE)
    still = screen.visible()
    for frame in anim.intro:
        anim.draw(0, frame, 0, STRIDE)
    after_intro = screen.visible()
    assert after_intro != still
    for frame in anim.idle:
        anim.draw(0, frame, 0, STRIDE)
    assert screen.visible() == after_intro  # the loop comes back to where it started
    for frame in anim.outro:
        anim.draw(0, frame, 0, STRIDE)
    assert screen.visible() == still


def test_it_draws_on_a_padded_offset_framebuffer(anim, monkeypatch):
    s = Screen(stride=STRIDE + 64, base=STRIDE + 64)
    monkeypatch.setattr(fbsplash.os, "pwrite", s.pwrite)
    anim.draw(0, anim.still, s.base, s.stride)
    assert s.visible() == still_logo()
    assert not any(s.buf[: s.base])  # nothing above the visible area


def test_a_broken_file_is_refused():
    with pytest.raises(ValueError):
        fbsplash.Animation(b"nope")
    with pytest.raises((ValueError, struct.error)):
        fbsplash.Animation(DATA[:-10])
    with pytest.raises(ValueError):
        fbsplash.Animation(DATA + b"\0")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def run(anim, cue_at=None, limit=None, monkeypatch=None):
    clock = Clock()
    shown = []
    if limit is not None:
        monkeypatch.setattr(fbsplash, "IDLE_LIMIT", limit)

    def show(frame):
        shown.append(frame)
        clock.now += 0.001

    fbsplash.play(
        anim, show, lambda: cue_at is not None and clock.now >= cue_at, clock, clock.sleep
    )
    return shown, clock.now


def test_cued_during_the_intro_it_goes_straight_to_the_outro(anim):
    shown, ended = run(anim, cue_at=1.0)
    assert shown == anim.intro + anim.outro
    assert ended == pytest.approx((len(anim.intro) + len(anim.outro)) / anim.fps, abs=0.01)


def test_it_loops_the_idle_until_cued_and_finishes_the_loop(anim):
    intro = len(anim.intro) / anim.fps
    shown, _ = run(anim, cue_at=intro + 0.5)
    assert shown == anim.intro + anim.idle + anim.outro


def test_it_finishes_on_its_own_if_couch_session_never_starts(anim, monkeypatch):
    shown, ended = run(anim, limit=12.0, monkeypatch=monkeypatch)
    loops = (len(shown) - len(anim.intro) - len(anim.outro)) / len(anim.idle)
    assert loops == int(loops) and loops >= 1
    assert shown[-len(anim.outro) :] == anim.outro
    assert ended < 12.0 + (len(anim.idle) + len(anim.outro)) / anim.fps + 0.5


def test_a_slow_frame_slows_the_animation_but_skips_nothing(anim):
    clock = Clock()
    shown = []

    def show(frame):
        shown.append(frame)
        clock.now += 0.2  # far slower than 30 fps

    fbsplash.play(anim, show, lambda: True, clock, clock.sleep)
    assert shown == anim.intro + anim.outro


def test_session_start_cues_the_ending():
    unit = (ROOT / "system/couch-session.service").read_text()
    pre = re.search(r"^ExecStartPre=(.*)$", unit, re.M).group(1)
    assert pre.startswith("-+")  # as root, and never fails the session
    assert "--kill-whom=main --signal=SIGUSR1 couch-fbsplash.service" in pre
    assert unit.index("ExecStartPre=") < unit.index("ExecStart=")


def test_the_unit_plays_it_without_holding_up_the_session():
    unit = (ROOT / "system/couch-fbsplash.service").read_text()
    assert "Type=simple" in unit and "Type=oneshot" not in unit
    assert "ExecStart=/opt/couch/bin/couch-fbsplash fb0 --animate" in unit
    assert "RemainAfterExit=yes" in unit
    install = (ROOT / "install.sh").read_text()
    assert '"$SRC/ui/brand/boot-animation.bin" "$PREFIX/share/boot-animation.bin" 644' in install
    assert '"$PREFIX/share/boot-animation.bin"' in install[install.index("uninstall_boot() {") :]
    assert Path("/opt/couch/share/boot-animation.bin") == fbsplash.ANIMATION


@pytest.fixture
def fake_fb(tmp_path, monkeypatch):
    """/sys and /dev for fb0 under tmp_path, RGB565 1920x1080 like vc4's at boot."""
    monkeypatch.setattr(fbsplash, "CACHE", tmp_path / "cache")
    sysfs = tmp_path / "sys"
    (sysfs / "fb0").mkdir(parents=True)
    (sysfs / "fb0/stride").write_text(f"{STRIDE}\n")
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "fb0").write_bytes(bytes(STRIDE * H))
    real_path, real_open = fbsplash.Path, open

    def path(p, *rest):
        return real_path(str(p).replace("/sys/class/graphics", str(sysfs)), *rest)

    def fake_open(p, *a, **k):
        return real_open(str(p).replace("/dev/", str(dev) + "/"), *a, **k)

    monkeypatch.setattr(fbsplash, "Path", path)
    monkeypatch.setattr(fbsplash, "open", fake_open, raising=False)
    monkeypatch.setattr(fbsplash, "TGA", ROOT / "ui/brand/splash.tga")
    monkeypatch.setattr(fbsplash, "ANIMATION", ROOT / "ui/brand/boot-animation.bin")
    info = (W, H, 0, 0, 16, *RGB565)
    monkeypatch.setattr(fbsplash, "screen_info", lambda fd: info)
    monkeypatch.setattr(fbsplash.signal, "signal", lambda *a: None)
    return dev / "fb0"


def test_main_plays_it_and_leaves_the_still_logo(fake_fb, tmp_path, monkeypatch):
    seen = []

    def fake_play(anim, show, cued, *a):
        for frame in anim.intro[:60]:  # 2 s in
            show(frame)
        seen.append(fake_fb.read_bytes())
        for frame in anim.intro[60:] + anim.outro:
            show(frame)

    monkeypatch.setattr(fbsplash, "play", fake_play)
    assert fbsplash.main(["couch-fbsplash", "fb0", "--animate"]) == 0
    still = still_logo()
    assert seen[0] != still  # it did animate
    assert fake_fb.read_bytes() == still
    # The still frame is kept for the shutdown redraw and couch-splash, as without --animate.
    (cached,) = [p for p in (tmp_path / "cache").iterdir() if p.name.startswith("fb0-")]
    assert cached.read_bytes() == still
    assert not (tmp_path / "cache/player.pid").exists()


def test_without_the_file_it_draws_the_still_logo_as_before(fake_fb, monkeypatch):
    monkeypatch.setattr(fbsplash, "ANIMATION", Path("/nonexistent/boot-animation.bin"))
    monkeypatch.setattr(fbsplash, "play", lambda *a: pytest.fail("played"))
    assert fbsplash.main(["couch-fbsplash", "fb0", "--animate"]) == 0
    assert fake_fb.read_bytes() == still_logo()


def test_on_another_screen_it_draws_the_still_logo_as_before(fake_fb, monkeypatch):
    info = (W, H, 0, 0, 16, (0, 5), (5, 6), (11, 5))  # BGR565
    monkeypatch.setattr(fbsplash, "screen_info", lambda fd: info)
    monkeypatch.setattr(fbsplash, "play", lambda *a: pytest.fail("played"))
    assert fbsplash.main(["couch-fbsplash", "fb0", "--animate"]) == 0
    w, h, rows = fbsplash.read_tga((ROOT / "ui/brand/splash.tga").read_bytes())
    pack = fbsplash.packer(16, *info[5:])
    assert fake_fb.read_bytes() == fbsplash.render(w, h, rows, W, H, STRIDE, pack)


def test_the_shutdown_redraw_stops_a_player_first(fake_fb, monkeypatch):
    stopped = []
    monkeypatch.setattr(fbsplash, "stop_player", lambda: stopped.append(True))
    monkeypatch.setattr(fbsplash, "wait_for_text_mode", lambda tty: None)
    assert fbsplash.main(["couch-fbsplash", "fb0", "--after-text", "tty1"]) == 0
    assert stopped == [True]
    assert fake_fb.read_bytes() == still_logo()


def test_stop_player_only_signals_couch_fbsplash(tmp_path, monkeypatch):
    monkeypatch.setattr(fbsplash, "CACHE", tmp_path)
    (tmp_path / "player.pid").write_text("1\n")  # pid 1 is init, not a player
    killed = []
    monkeypatch.setattr(fbsplash.os, "kill", lambda *a: killed.append(a))
    fbsplash.stop_player()
    assert killed == []


def test_frames_hold_only_what_changed():
    """Small rectangles mostly: the whole screen changes only while the lights go down and up."""
    anim = fbsplash.Animation(DATA)
    full = [
        f
        for f in anim.intro + anim.idle + anim.outro
        if any(w * h > W * H // 2 for _, _, w, h, _, _ in f)
    ]
    assert len(full) < 0.3 * (len(anim.intro) + len(anim.outro))
    assert not any(any(w * h > W * H // 2 for _, _, w, h, _, _ in f) for f in anim.idle)
    for f in anim.intro[:3]:
        for *_, at, length in f:
            zlib.decompress(DATA[at : at + length])

"""couch-splash, the Wayland client that holds the splash over cage (#23).

The real thing is tried in cage itself (scripts/perf/cage-splash-check.sh). These tests cover the
wire format, what it does on each event, and that it draws exactly the boot splash's pixels."""

import importlib.machinery
import importlib.util
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    loader = importlib.machinery.SourceFileLoader(
        name.replace("-", "_"), str(ROOT / "system" / name)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cs = load("couch-splash")
fbsplash = load("couch-fbsplash")


def event(obj: int, opcode: int, *args) -> tuple[int, int, bytes]:
    msg = cs.message(obj, opcode, *args)
    (parsed,), rest = cs.split_messages(msg)
    assert rest == b""
    return parsed


def array(*values: int) -> bytes:
    data = struct.pack(f"<{len(values)}I", *values)
    return struct.pack("<I", len(data)) + data


def sent(splash) -> list[tuple[int, int, bytes, int | None]]:
    out = []
    for msg, fd in splash.out:
        (m,), _ = cs.split_messages(msg)
        out.append((*m, fd))
    splash.out.clear()
    return out


def test_wire_format():
    msg = cs.message(5, 3, "couch-splash", 7, -1)
    obj, word, length = struct.unpack_from("<III", msg)
    assert (obj, word & 0xFFFF, word >> 16) == (5, 3, len(msg))
    assert length == len(b"couch-splash\0") and len(msg) % 4 == 0
    assert cs.read_string(msg[8:], 0) == ("couch-splash", 4 + 16)
    two = cs.message(1, 0, 9) + cs.message(2, 1)
    messages, rest = cs.split_messages(two + two[:6])  # and half of the next one
    assert [(o, op) for o, op, _ in messages] == [(1, 0), (2, 1)]
    assert rest == two[:6]


def started(size=(1920, 1080), presentation=False):
    pixels = []
    splash = cs.Splash(pixels_for=lambda w, h: pixels.append((w, h)) or b"\0" * (w * h * 4))
    first = sent(splash)
    assert [(o, op) for o, op, *_ in first] == [(1, 1), (1, 0)]  # get_registry, sync
    names = ["wl_compositor", "wl_shm", "xdg_wm_base", "wl_seat"]
    if presentation:
        names.append("wp_presentation")
    for i, iface in enumerate(names, 1):
        splash.feed(*event(2, 0, i, iface, 5))
    splash.feed(*event(splash.sync_id, 0, 0))
    binds = [m for m in sent(splash) if m[0] == 2]
    assert {cs.read_string(m[2], 4)[0] for m in binds} == set(names) - {"wl_seat"}
    return splash, pixels


def configure(splash, window, *, activated, serial=1, size=(1920, 1080)):
    states = array(cs.XDG_TOPLEVEL_ACTIVATED) if activated else array()
    splash.feed(window.toplevel, 0, struct.pack("<ii", *size) + states)
    splash.feed(*event(window.xdg_surface, 0, serial))


def test_maps_once_configured():
    splash, pixels = started()
    top = splash.top
    configure(splash, top, activated=True)
    reqs = sent(splash)
    assert (top.xdg_surface, 4) in [(o, op) for o, op, *_ in reqs]  # ack_configure
    pool = [m for m in reqs if m[0] == splash.shm and m[1] == 0]
    assert len(pool) == 1 and pool[0][3] is not None  # the pixels go as an fd
    buffer = next(m for m in reqs if m[0] == splash.pool and m[1] == 0)
    _, _, w, h, stride, fmt = struct.unpack("<6i", buffer[2])
    assert (w, h, stride, fmt) == (1920, 1080, 1920 * 4, cs.ARGB8888)
    assert (top.surface, 1) in [(o, op) for o, op, *_ in reqs]  # attach
    assert top.mapped and pixels == [(1920, 1080)]
    assert len(splash.windows) == 1  # just the one window


def test_goes_once_the_launcher_is_over_it():
    """The launcher's first frame is the same picture, so the splash doesn't go back on top:
    it leaves."""
    splash, _ = started()
    configure(splash, splash.top, activated=True)
    sent(splash)
    assert not splash.covered_by_another
    configure(splash, splash.top, activated=False, serial=2)  # the launcher mapped over it
    assert splash.covered_by_another
    assert not [m for m in sent(splash) if m[1] == 1]  # nothing attached again


def test_focus_it_never_had_is_not_a_loss():
    splash, _ = started()
    configure(splash, splash.top, activated=False)
    configure(splash, splash.top, activated=False, serial=2)
    assert not splash.covered_by_another


def test_answers_pings_and_reports_errors():
    splash, _ = started()
    splash.feed(*event(splash.wm_base, 0, 77))
    assert sent(splash)[-1][:3] == (splash.wm_base, 3, struct.pack("<I", 77))
    splash.feed(*event(1, 0, 9, 2, "invalid size"))
    assert "invalid size" in splash.error
    missing = cs.Splash(pixels_for=lambda w, h: b"")
    missing.feed(*event(missing.sync_id, 0, 0))
    assert "wl_compositor" in missing.error


def test_logs_when_it_reaches_the_screen(capsys):
    splash, _ = started(presentation=True)
    configure(splash, splash.top, activated=True)
    feedback = next(iter(splash.feedback))
    splash.feed(*event(feedback, 1, 0, 100, 5_000_000, 16_666_666, 0, 1, 0))
    assert splash.top.presented and not splash.feedback
    assert "splash on screen" in capsys.readouterr().err


# --- the picture --------------------------------------------------------------------------


def png_rgb(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB/RGBA, non-interlaced PNG with the standard library."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(data):
        (n,) = struct.unpack_from(">I", data, pos)
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and color in (2, 6) and not interlace
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    bpp = 3 if color == 2 else 4
    raw, stride = zlib.decompress(idat), w * bpp
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        f, line = (
            raw[y * (stride + 1)],
            bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]),
        )
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        out += line if bpp == 3 else bytes(v for i, v in enumerate(line) if i % 4 != 3)
    return w, h, bytes(out)


@pytest.fixture(scope="module")
def drawn():
    tga = ROOT / "ui" / "brand" / "splash.tga"
    return cs.splash_pixels(1920, 1080, cache=Path("/nonexistent"), tgas=(tga,), fbsplash=fbsplash)


def test_draws_the_startup_frame_on_the_same_pixels(drawn):
    """The kernel and couch-fbsplash draw splash.tga centred on its corner colour; the Startup
    screen (#19) is splash.png. This draws the TGA the same way, so the logo stays put from
    power-on to the launcher. Outside the TGA's box every pixel equals splash.png. Inside it,
    the TGA's grey ramp has 128 steps (the kernel allows 224 colours), so the lockup's
    anti-aliased edges may be up to 2 levels off."""
    w, h, rgb = png_rgb(ROOT / "ui" / "brand" / "splash.png")
    assert (w, h) == (1920, 1080)
    assert len(drawn) == w * h * 4
    assert set(drawn[3::4]) == {0xFF}  # every pixel opaque, though the format has alpha
    bgr = bytearray(len(rgb))
    bgr[0::3], bgr[1::3], bgr[2::3] = drawn[2::4], drawn[1::4], drawn[0::4]
    bw, bh = 400, 320  # make_brand.py's TGA_BOX
    x0, y0 = (w - bw) // 2, (h - bh) // 2
    worst = 0
    for y in range(h):
        row = slice(y * w * 3, (y + 1) * w * 3)
        if not y0 <= y < y0 + bh:
            assert bgr[row] == rgb[row], f"row {y} differs outside the logo"
            continue
        left, box = (
            slice(y * w * 3, (y * w + x0) * 3),
            slice((y * w + x0) * 3, (y * w + x0 + bw) * 3),
        )
        right = slice((y * w + x0 + bw) * 3, (y + 1) * w * 3)
        assert bgr[left] == rgb[left] and bgr[right] == rgb[right], f"row {y} beside the logo"
        worst = max(worst, max(abs(a - b) for a, b in zip(bgr[box], rgb[box], strict=True)))
    assert worst <= 2


def test_uses_couch_fbsplash_s_own_frame_when_there_is_one(tmp_path):
    frame = bytes([1, 2, 3, 0]) * (4 * 2)
    (tmp_path / "fb0-4x2-32-16-16-8-8-8-0-8").write_bytes(frame)
    assert cs.splash_pixels(4, 2, cache=tmp_path) == bytes([1, 2, 3, 255]) * 8
    # a frame of another size isn't used
    got = cs.splash_pixels(2, 2, cache=tmp_path, tgas=(tmp_path / "none.tga",))
    assert got == cs.GROUND * 4  # and with no TGA either, it's the ground colour


def test_couch_session_start_runs_it_before_the_launcher():
    start = (ROOT / "system" / "couch-session-start").read_text()
    assert start.index("couch-splash") < start.index("exec /opt/couch/bin/couch-launcher")
    assert "swayimg" not in start


def test_the_splash_is_not_left_a_child_of_the_launcher(tmp_path):
    """couch-session-start execs the launcher, which never reaps children it didn't start.
    Started from a subshell, the splash goes to a reaper."""
    stub = tmp_path / "start"
    stub.write_text(
        (ROOT / "system" / "couch-session-start")
        .read_text()
        .replace("[ -x /opt/couch/bin/couch-splash ]", "true")
        .replace('/opt/couch/bin/couch-splash "$@"', "sleep 2")
        .replace("/opt/couch/bin/cage", str(tmp_path / "none"))
        .replace('exec /usr/bin/cage -s -- "$0"', "exec sleep 1")
    )
    proc = subprocess.Popen(
        ["sh", str(stub), "--start-cage"], env={"XDG_RUNTIME_DIR": str(tmp_path)}
    )
    try:
        for _ in range(50):
            pidfile = tmp_path / "couch-splash.pid"
            if pidfile.exists() and pidfile.read_text().strip():
                break
            subprocess.run(["sleep", "0.02"], check=True)
        pid = int(pidfile.read_text())
        ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        assert ppid != proc.pid  # not the shell that becomes cage
    finally:
        proc.wait(timeout=5)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_it_waits_for_cages_socket(tmp_path, monkeypatch):
    """Started before cage: no socket yet, then one nobody listens on (the last cage's), then
    cage's."""
    import socket

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    path = tmp_path / "wayland-0"
    clock = FakeClock()
    server = None

    def sleep(s):
        nonlocal server
        clock.sleep(s)
        if clock.now > 0.05 and not path.exists():
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))  # bound, never listening: refused
            stale.close()
        elif clock.now > 0.1 and server is None:
            path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen()

    sock = cs.connect(5.0, sleep=sleep, clock=clock)
    try:
        assert clock.now > 0.1
        assert sock.getpeername() == str(path)
    finally:
        sock.close()
        server.close()
    with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
        path.unlink()
        cs.connect(1.0, sleep=clock.sleep, clock=clock)  # cage never came: it gives up


def test_its_picture_is_ready_before_cage(monkeypatch):
    made = []
    monkeypatch.setattr(cs, "splash_pixels", lambda w, h: made.append((w, h)) or b"p")
    pixels_for = cs.ready_pixels()
    assert made == [(1920, 1080)]  # before connecting
    assert pixels_for(1920, 1080) == b"p" and made == [(1920, 1080)]
    pixels_for(1280, 720)  # another screen: made then
    assert made == [(1920, 1080), (1280, 720)]


def session_start(tmp_path, couch_cage=False, tgas=()):
    """couch-session-start with stubs: the cages and the launcher log what they were started
    as, and the splash is a script that sleeps. Couch's cage is there with couch_cage; tgas
    are the splash TGAs there ("firmware", "ui")."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    splash = bin_ / "couch-splash"
    splash.write_text('#!/bin/sh\necho "splash $*" >>"$XDG_RUNTIME_DIR/log"\nsleep 3\n')
    launcher = bin_ / "couch-launcher"
    launcher.write_text('#!/bin/sh\necho "launcher" >>"$XDG_RUNTIME_DIR/log"\n')
    stubs = [splash, launcher]
    for name in ("cage", "couch-cage"):
        cage = bin_ / name
        cage.write_text(
            f'#!/bin/sh\necho "{name} $*" >>"$XDG_RUNTIME_DIR/log"\n'
            'while [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'
        )
        stubs.append(cage)
    for f in stubs:
        f.chmod(0o755)
    for tga in tgas:
        (tmp_path / f"{tga}.tga").write_bytes(b"TGA")
    stub = tmp_path / "couch-session-start"
    stub.write_text(
        (ROOT / "system" / "couch-session-start")
        .read_text()
        .replace("/opt/couch/bin/couch-launcher", str(launcher))
        .replace("/opt/couch/bin/couch-splash", str(splash))
        .replace("/opt/couch/bin/couch-chromium-warm", str(bin_ / "none"))
        .replace("/usr/bin/cage", str(bin_ / "cage"))
        .replace("/opt/couch/bin/cage", str(bin_ / ("couch-cage" if couch_cage else "none")))
        .replace("/lib/firmware/couch-splash.tga", str(tmp_path / "firmware.tga"))
        .replace("/opt/couch/ui/brand/splash.tga", str(tmp_path / "ui.tga"))
    )
    stub.chmod(0o755)
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run([str(stub), "--start-cage"], env=env, check=True, timeout=10)
    return (tmp_path / "log").read_text().splitlines()


def test_the_session_starts_the_splash_before_cage(tmp_path):
    log = session_start(tmp_path)
    assert log[0] == "splash --wait" or log[1] == "splash --wait"
    assert [line for line in log if not line.startswith("splash")] == [
        f"cage -s -- {tmp_path}/couch-session-start",
        "launcher",
    ]
    assert log.count("splash --wait") == 1  # cage's client doesn't start a second one
    assert (tmp_path / "couch-splash.pid").exists()


def test_couchs_cage_draws_the_boot_splash_from_its_first_frame(tmp_path):
    log = session_start(tmp_path, couch_cage=True, tgas=("firmware", "ui"))
    assert [line for line in log if not line.startswith("splash")] == [
        f"couch-cage -b {tmp_path}/firmware.tga -s -- {tmp_path}/couch-session-start",
        "launcher",
    ]
    assert "splash --wait" in log


def test_couchs_cage_falls_back_to_the_ui_copy_of_the_splash(tmp_path):
    log = session_start(tmp_path, couch_cage=True, tgas=("ui",))
    start = f"{tmp_path}/couch-session-start"
    assert f"couch-cage -b {tmp_path}/ui.tga -s -- {start}" in log


def test_without_a_picture_it_runs_the_systems_cage(tmp_path):
    log = session_start(tmp_path, couch_cage=True)
    assert f"cage -s -- {tmp_path}/couch-session-start" in log
    assert not any(line.startswith("couch-cage") for line in log)

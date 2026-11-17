#!/usr/bin/env python3
"""Render the boot animation (N1, "lights down") for couch-fbsplash: ui/brand/boot-animation.bin

    scripts/render-boot-animation.py [--out FILE] [--preview preview.mp4]

Run on a development machine, not the Pi: it needs numpy and pycairo (python3-numpy,
python3-cairo), and ffmpeg for --preview. The output is committed, so the Pi only plays it.

The story, from the kernel's still logo:
  intro   the room dims to navy and the letters fade with it; the couch takes the colours of a
          TV in front of it; the back cushion jumps and plops down; a popcorn bucket drops in
          beside the couch; the letters rise one by one out of the top of the bucket, from
          behind it, and arc over into their places.
  idle    while the Pi is still booting: the TV light and popcorn popping out of the bucket.
          It loops; it starts and ends on the intro's last frame.
  outro   the TV goes off, the lights come back up and the bucket goes: the still logo again.
couch-fbsplash plays the intro, loops the idle until couch-session starts (or a minute has
passed), then plays the outro, so the boot always ends on the still logo, before cage takes
the screen.

The frames are RGB565 at 1920x1080, vc4's framebuffer at boot. Each frame is stored as the
rectangles that changed since the frame before, zlib-compressed. Pixels that are the same as
in the still logo are the still logo's own pixels exactly (splash.tga drawn the way
couch-fbsplash draws it), so the first frame and the last match the kernel's logo; the rest
are dithered, so the navy and the TV glow don't band in 16 bits.

Format (little-endian):
  b"COUCHANIM\\x01", u16 width, u16 height, u8 fps, u8 0, u16 intro, u16 idle, u16 outro
  then base (the still logo, one full-screen rect), then intro+idle+outro frames; a frame is
  u16 count, then per rect u16 x, y, w, h, u32 length, and that many bytes of zlib data
  (w*h RGB565 pixels, row by row).
"""

import argparse
import importlib.machinery
import importlib.util
import itertools
import math
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import cairo
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
W, H, FPS = 1920, 1080, 30
MAGIC = b"COUCHANIM\x01"

DARK = (0x12, 0x12, 0x14)  # the night colour, splash.svg's ground
NAVY = (0x10, 0x14, 0x2A)
WHITE = (255, 255, 255)
BUCKET_BODY = (0xF4, 0xEB, 0xDD)
BUCKET_STRIPE = (0xFF, 0x5E, 0x5E)
POPCORN = (0xFF, 0xF3, 0xD6)
# The TV's colours on the couch: a 2 s loop, (seconds, colour).
TV = [
    (0, "8fa2ff"),
    (0.28, "8fa2ff"),
    (0.32, "ff8fd0"),
    (0.55, "ff8fd0"),
    (0.6, "8fa2ff"),
    (0.95, "b8f0ff"),
    (1.25, "8fa2ff"),
    (1.3, "ffffff"),
    (1.36, "8fa2ff"),
    (1.7, "ffd37a"),
    (2.0, "8fa2ff"),
]
TV_PERIOD = 2.0

# The lockup, in splash.svg's own units: the group is at (789.96, 412.32), scale 0.12.
ORIGIN, SCALE = (789.96, 412.32), 0.12
CUSHION = (1148.88, 0, 536.29, 423.39, 112.9)
ARMS_SEAT = [
    (852.51, 225.81, 239.92, 649.19, 119.96),
    (1741.62, 225.81, 239.92, 649.19, 119.96),
    (852.51, 550.4, 1129.03, 324.6, 119.96),
]
WORDMARK_AT = (-52.45, 1414)
BUCKET_X, INSIDE_Y, MOUTH_Y = 2262, -580, -830  # in the wordmark's units
GLOW = (1416, 920, 900, 120)

# Timing, in seconds from the first animated frame.
INTRO, IDLE, OUTRO = 5.6, TV_PERIOD, 1.0
TV_ON = (1.4, 1.8)
LETTER_RISE = [3.0 + i * 0.32 for i in range(5)]


# ---- easing and keyframes -------------------------------------------------------------


def bezier(x1, y1, x2, y2):
    def f(t):
        lo, hi = 0.0, 1.0
        for _ in range(40):  # solve x(u) = t
            u = (lo + hi) / 2
            x = 3 * (1 - u) ** 2 * u * x1 + 3 * (1 - u) * u * u * x2 + u**3
            lo, hi = (u, hi) if x < t else (lo, u)
        u = (lo + hi) / 2
        return 3 * (1 - u) ** 2 * u * y1 + 3 * (1 - u) * u * u * y2 + u**3

    return f


SPRING = bezier(0.3, 0, 0.4, 1)
SMOOTH = bezier(0.45, 0, 0.55, 1)
LINEAR = lambda t: t  # noqa: E731
IDENTITY = dict(tx=0.0, ty=0.0, sx=1.0, sy=1.0, rot=0.0, op=1.0)


def mix(a, b, f):
    if isinstance(a, dict):
        return {k: a[k] + (b[k] - a[k]) * f for k in a}
    if isinstance(a, tuple):
        return tuple(x + (y - x) * f for x, y in zip(a, b, strict=True))
    return a + (b - a) * f


def keyframes(t, keys, ease=SPRING):
    """keys: [(time, value)], sorted; holds the ends."""
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, v0), (t1, v1) in itertools.pairwise(keys):
        if t <= t1:
            return mix(v0, v1, ease((t - t0) / (t1 - t0)) if t1 > t0 else 1)
    return keys[-1][1]


def tf(**kw):
    return {**IDENTITY, **kw}


def hexrgb(h):
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def tv_colour(t):
    u = round((t - TV_ON[0]) % TV_PERIOD, 6)  # the same colour each time round the loop
    return keyframes(u, [(s, hexrgb(c)) for s, c in TV], LINEAR)


# ---- the scene at time t ------------------------------------------------------------------


def scene(phase, t):
    """Everything that moves, at t seconds into the phase."""
    if phase == "idle":
        t += INTRO
    s = {}
    if phase == "outro":
        f = SMOOTH(min(t / OUTRO, 1))
        s["room"] = mix(NAVY, DARK, f)
        s["tv"] = 1 - f
        s["tv_t"] = INTRO  # the TV's colour where the idle loop starts and ends
        s["bucket"] = tf(op=max(0.0, 1 - min(t / (OUTRO * 0.6), 1)))
        s["letters"] = [IDENTITY] * 5
        s["cushion"] = IDENTITY
        s["corn"] = []
        return s
    s["room"] = keyframes(t, [(0.3, DARK), (1.5, NAVY)], SMOOTH)
    s["tv"] = keyframes(t, [(TV_ON[0], 0.0), (TV_ON[1], 1.0)], SMOOTH)
    s["tv_t"] = t
    s["cushion"] = keyframes(
        t,
        [
            (1.5, IDENTITY),
            (1.9, tf(ty=-600, sx=0.92, sy=1.08)),
            (2.25, tf(sx=1.18, sy=0.72)),
            (2.45, tf(ty=-120, sx=0.95, sy=1.06)),
            (2.7, IDENTITY),
        ],
    )
    bucket = keyframes(
        t,
        [
            (2.2, tf(ty=-1500, op=0)),
            (2.3, tf(ty=-1100, op=1)),
            (2.55, tf(sx=1.2, sy=0.78)),
            (2.7, tf(ty=-80, sx=0.95, sy=1.06)),
            (2.85, IDENTITY),
        ],
    )
    # A rattle while the letters come out, fading in and out.
    env = keyframes(t, [(2.95, 0.0), (3.1, 1.0), (4.8, 1.0), (5.1, 0.0)], SMOOTH)
    bucket = {**bucket, "rot": 4 * env * math.sin(2 * math.pi * t / 0.14)}
    s["bucket"] = bucket
    s["letters"] = [letter(i, t) for i in range(5)]
    s["corn"] = corn(t - INTRO) if phase == "idle" else []
    return s


def letter(i, t):
    dx = BUCKET_X - LETTER_CX[i]
    start = tf(tx=dx, ty=INSIDE_Y - LETTER_BOTTOM, sx=0.3, sy=0.3, op=0)
    s = LETTER_RISE[i]
    if t < s:  # fade out with the lights, then wait inside the bucket
        fade = keyframes(t, [(0.3, 1.0), (1.4, 0.0)], SMOOTH)
        return tf(op=fade) if fade > 0 else start
    up = MOUTH_Y - LETTER_BOTTOM
    return keyframes(
        t,
        [
            (s, {**start, "op": 1}),
            (s + 0.24, tf(tx=dx, ty=up, sx=0.42, sy=0.42)),
            (s + 0.56, tf(tx=dx * 0.45, ty=up - 380, sx=0.85, sy=0.85, rot=-15)),
            (s + 0.88, tf(sx=1.25, sy=0.78)),
            (s + 1.04, tf(ty=-40, sx=0.92, sy=1.1)),
            (s + 1.28, IDENTITY),
        ],
    )


def corn(u):
    """Popcorn kernels in the idle loop: (transform) for each one in the air."""
    out = []
    for start, dx in ((0.05, -260), (0.55, 200)):
        k = u - start
        if not 0 <= k <= 1.4:
            continue
        p = keyframes(
            k,
            [
                (0, tf(op=0)),
                (0.07, tf(tx=dx * 0.17, ty=-150, rot=35, op=1)),
                (0.42, tf(tx=dx, ty=-620, rot=200)),
                (0.91, tf(tx=dx * 1.8, ty=160, rot=400)),
                (0.98, tf(tx=dx * 1.8, ty=180, rot=400, op=0)),
                (1.4, tf(op=0)),
            ],
            bezier(0.2, 0.7, 0.4, 1),
        )
        out.append(p)
    return out


# ---- drawing ------------------------------------------------------------------------------


def parse_path(d):
    """Subpaths of an SVG path using M, L, H, V, Q and Z (absolute), as cairo commands."""
    import re

    tokens = re.findall(r"[MLHVQZ]|-?[\d.]+", d)
    cmds, i, cur, sub = [], 0, (0, 0), []
    while i < len(tokens):
        c = tokens[i]
        i += 1
        nums = []
        while i < len(tokens) and tokens[i] not in "MLHVQZ":
            nums.append(float(tokens[i]))
            i += 1
        if c == "M":
            if sub:
                cmds.append(sub)
            cur = (nums[0], nums[1])
            sub = [("M", cur)]
            for j in range(2, len(nums), 2):
                cur = (nums[j], nums[j + 1])
                sub.append(("L", cur))
        elif c == "L":
            for j in range(0, len(nums), 2):
                cur = (nums[j], nums[j + 1])
                sub.append(("L", cur))
        elif c == "H":
            for x in nums:
                cur = (x, cur[1])
                sub.append(("L", cur))
        elif c == "V":
            for y in nums:
                cur = (cur[0], y)
                sub.append(("L", cur))
        elif c == "Q":
            for j in range(0, len(nums), 4):
                qx, qy, x, y = nums[j : j + 4]
                c1 = (cur[0] + 2 / 3 * (qx - cur[0]), cur[1] + 2 / 3 * (qy - cur[1]))
                c2 = (x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y))
                sub.append(("C", (*c1, *c2, x, y)))
                cur = (x, y)
        elif c == "Z":
            sub.append(("Z", None))
    if sub:
        cmds.append(sub)
    return cmds


def wordmark_letters():
    import re

    svg = (ROOT / "ui/brand/splash.svg").read_text()
    d = re.search(r'<path[^>]*d="([^"]+)"', svg).group(1)
    subs = parse_path(d)
    assert len(subs) == 6, len(subs)
    return [
        [subs[0]],
        [subs[1], subs[2]],
        [subs[3]],
        [subs[4]],
        [subs[5]],
    ]  # C, o (two rings), u, c, h


LETTERS = wordmark_letters()


def bbox(subpaths):
    pts = [p for sub in subpaths for op, p in sub if p for p in [p[-2:]]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


LETTER_BOX = [bbox(s) for s in LETTERS]
LETTER_CX = [(b[0] + b[2]) / 2 for b in LETTER_BOX]
LETTER_BOTTOM = 714


def trace(ctx, subpaths):
    for sub in subpaths:
        for op, p in sub:
            if op == "M":
                ctx.move_to(*p)
            elif op == "L":
                ctx.line_to(*p)
            elif op == "C":
                ctx.curve_to(*p)
            else:
                ctx.close_path()


def rgb(ctx, c, a=1.0):
    ctx.set_source_rgba(c[0] / 255, c[1] / 255, c[2] / 255, a)


def rounded(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def apply(ctx, t, origin):
    """A CSS transform (translate, scale, rotate) about origin."""
    ctx.translate(*origin)
    ctx.translate(t["tx"], t["ty"])
    ctx.scale(t["sx"], t["sy"])
    ctx.rotate(math.radians(t["rot"]))
    ctx.translate(-origin[0], -origin[1])


def bucket(ctx, colour_scale=1.0):
    ctx.move_to(2080, 600), ctx.line_to(2340, 600), ctx.line_to(2300, 875), ctx.line_to(2120, 875)
    ctx.close_path()
    rgb(ctx, BUCKET_BODY)
    ctx.fill()
    for x0, x1, x2, x3 in ((2132, 2172, 2168, 2144), (2230, 2270, 2258, 2232)):
        ctx.move_to(x0, 600), ctx.line_to(x1, 600), ctx.line_to(x2, 875), ctx.line_to(x3, 875)
        ctx.close_path()
    rgb(ctx, BUCKET_STRIPE)
    ctx.fill()
    rgb(ctx, POPCORN)
    for cx, cy, r in ((2120, 590, 48), (2200, 565, 56), (2290, 590, 48)):
        ctx.arc(cx, cy, r, 0, 2 * math.pi)
        ctx.fill()


def kernel(ctx):
    rgb(ctx, POPCORN)
    for cx, cy, r in ((2190, 560, 44), (2235, 540, 36), (2215, 590, 34)):
        ctx.arc(cx, cy, r, 0, 2 * math.pi)
        ctx.fill()


def draw(s):
    """The frame for scene s, as an (H, W, 3) uint8 array."""
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
    ctx = cairo.Context(surf)
    rgb(ctx, s["room"])
    ctx.paint()
    ctx.translate(*ORIGIN)
    ctx.scale(SCALE, SCALE)
    couch = mix(WHITE, tv_colour(s["tv_t"]), s["tv"]) if s["tv"] > 0 else WHITE
    if s["tv"] > 0:  # the TV's glow on the floor
        cx, cy, rx, ry = GLOW
        ctx.save()
        ctx.translate(cx, cy)
        ctx.scale(rx + 120, ry + 120)
        g = cairo.RadialGradient(0, 0, 0, 0, 0, 1)
        a = 0.3 * s["tv"]
        for stop, k in ((0, 1), (0.5, 0.85), (0.75, 0.45), (0.9, 0.15), (1, 0)):
            g.add_color_stop_rgba(stop, *(c / 255 for c in couch), a * k)
        ctx.set_source(g)
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
        ctx.fill()
        ctx.restore()
    rgb(ctx, couch)
    for r in ARMS_SEAT:
        rounded(ctx, *r)
    ctx.fill()
    ctx.save()
    x, y, w, h, r = CUSHION
    apply(ctx, s["cushion"], (x + w / 2, y + h))
    rounded(ctx, *CUSHION)
    ctx.fill()
    ctx.restore()
    # Letters and popcorn, then the bucket in front of them: they come out from behind it.
    ctx.save()
    ctx.translate(*WORDMARK_AT)
    for i, t in enumerate(s["letters"]):
        if t["op"] <= 0:
            continue
        b = LETTER_BOX[i]
        ctx.save()
        apply(ctx, t, ((b[0] + b[2]) / 2, b[3]))
        trace(ctx, LETTERS[i])
        ctx.restore()
        rgb(ctx, WHITE, t["op"])
        ctx.fill()
    ctx.restore()
    for t in s["corn"]:
        if t["op"] <= 0:
            continue
        ctx.save()
        apply(ctx, t, (2212, 562))
        ctx.push_group()
        kernel(ctx)
        ctx.pop_group_to_source()
        ctx.paint_with_alpha(t["op"])
        ctx.restore()
    b = s["bucket"]
    if b["op"] > 0:
        ctx.save()
        apply(ctx, b, (2210, 875))
        ctx.push_group()
        bucket(ctx)
        ctx.pop_group_to_source()
        ctx.paint_with_alpha(b["op"])
        ctx.restore()
    surf.flush()
    a = np.frombuffer(surf.get_data(), np.uint8).reshape(H, surf.get_stride() // 4, 4)[:, :W]
    return a[:, :, 2::-1].copy()  # BGRX -> RGB


# ---- RGB565 ---------------------------------------------------------------------------------

BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16
DITHER = np.tile(BAYER, (H // 4 + 1, W // 4 + 1))[:H, :W]


def to565(a):
    """RGB to RGB565, with ordered dithering."""
    a = a.astype(np.float64)
    r = np.clip(np.floor(a[..., 0] * 31 / 255 + DITHER), 0, 31).astype(np.uint16)
    g = np.clip(np.floor(a[..., 1] * 63 / 255 + DITHER), 0, 63).astype(np.uint16)
    b = np.clip(np.floor(a[..., 2] * 31 / 255 + DITHER), 0, 31).astype(np.uint16)
    return (r << 11) | (g << 5) | b


def load_fbsplash():
    path = ROOT / "system/couch-fbsplash"
    loader = importlib.machinery.SourceFileLoader("couch_fbsplash", str(path))
    spec = importlib.util.spec_from_loader("couch_fbsplash", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def still_logo_565():
    """splash.tga drawn exactly as couch-fbsplash draws it on a 1920x1080 RGB565 screen."""
    fb = load_fbsplash()
    w, h, rows = fb.read_tga((ROOT / "ui/brand/splash.tga").read_bytes())
    raw = fb.render(w, h, rows, W, H, W * 2, fb.packer(16, (11, 5), (5, 6), (0, 5)))
    return np.frombuffer(raw, "<u2").reshape(H, W)


def rects(prev, cur):
    """The changed areas: runs of changed rows, each with its changed columns' span."""
    changed = prev != cur
    rows = np.flatnonzero(changed.any(axis=1))
    out = []
    if not len(rows):
        return out
    runs = np.split(rows, np.flatnonzero(np.diff(rows) > 8) + 1)  # merge near rows
    for run in runs:
        y0, y1 = run[0], run[-1] + 1
        cols = np.flatnonzero(changed[y0:y1].any(axis=0))
        out.append((int(cols[0]), int(y0), int(cols[-1] + 1 - cols[0]), int(y1 - y0)))
    return out


def encode(prev, cur):
    rs = rects(prev, cur)
    out = bytearray(struct.pack("<H", len(rs)))
    for x, y, w, h in rs:
        data = zlib.compress(cur[y : y + h, x : x + w].astype("<u2").tobytes(), 9)
        out += struct.pack("<HHHHI", x, y, w, h, len(data)) + data
    return bytes(out)


def frames():
    """(phase, frame index, scene) for every frame after the still logo."""
    for phase, length in (("intro", INTRO), ("idle", IDLE), ("outro", OUTRO)):
        n = round(length * FPS)
        for k in range(1, n + 1):
            yield phase, scene(phase, k / FPS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "ui/brand/boot-animation.bin")
    ap.add_argument("--preview", type=Path, help="also write an MP4 of intro, idle and outro")
    args = ap.parse_args()

    base = still_logo_565()
    still = draw(scene("intro", 0))
    end = draw(scene("outro", OUTRO))
    assert (still == end).all(), "the outro must end on the intro's first frame"
    counts = {p: round(n * FPS) for p, n in (("intro", INTRO), ("idle", IDLE), ("outro", OUTRO))}
    out = bytearray(
        MAGIC
        + struct.pack("<HHBBHHH", W, H, FPS, 0, counts["intro"], counts["idle"], counts["outro"])
    )
    out += struct.pack("<H", 1)
    data = zlib.compress(base.astype("<u2").tobytes(), 9)
    out += struct.pack("<HHHHI", 0, 0, W, H, len(data)) + data

    shown = []  # for the preview
    prev = base
    intro_last = None
    for n, (phase, s) in enumerate(frames()):
        img = draw(s)
        same = (img == still).all(axis=2)
        cur = np.where(same, base, to565(img))
        out += encode(prev, cur)
        prev = cur
        if phase == "intro":
            intro_last = cur
        if phase == "idle" and n == counts["intro"] + counts["idle"] - 1:
            assert (cur == intro_last).all(), "the idle loop must end where it starts"
        if phase == "outro" and n == sum(counts.values()) - 1:
            assert (cur == base).all(), "the outro must end on the still logo"
        if args.preview:
            shown.append(cur)
    args.out.write_bytes(out)
    print(f"{args.out}: {len(out) / 1e6:.1f} MB, {sum(counts.values())} frames {counts}")

    if args.preview:
        # The still logo for a second, the intro, two idle loops, the outro, a second still.
        idle = shown[counts["intro"] : counts["intro"] + counts["idle"]]
        seq = (
            [base] * FPS
            + shown[: counts["intro"]]
            + idle * 2
            + shown[-counts["outro"] :]
            + [base] * FPS
        )
        ff = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb565le",
                "-s",
                f"{W}x{H}",
                "-r",
                str(FPS),
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "18",
                str(args.preview),
            ],
            stdin=subprocess.PIPE,
        )
        for f in seq:
            ff.stdin.write(f.astype("<u2").tobytes())
        ff.stdin.close()
        ff.wait()
        print(f"{args.preview}: {len(seq) / FPS:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

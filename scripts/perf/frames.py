#!/usr/bin/env python3
"""What a screen recording showed, frame by frame (#23): frames.py RECORDING [--json OUT]

Each frame is one of:
  black   nothing drawn (cage before any client)
  white   a bright, mostly white frame (a browser window before its page paints)
  ground  only the splash's ground colour, #121214, with nothing on it
  splash  the ground with something bright only inside the boot splash's box (the lockup)
  other   anything else: the Startup screen's bar and text, Home, an app

It prints the runs in order ("black x3 → splash x280 → other x120") and a verdict: any black
or white frame after the first splash frame and before the first "other" frame is a gap in
the splash. Needs ffmpeg and ffprobe. Frames are read as recorded (wf-recorder records a new
frame only when the screen changes, unless it's given -D).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

W, H = 192, 108  # frames are scaled to this before they're looked at
GROUND = (0x12, 0x12, 0x14)
TGA = (400, 320)  # the splash TGA's box in screen pixels, centred (ui/brand/make_brand.py)


def classify(rgb: bytes, box: tuple[float, float], w: int = W, h: int = H) -> str:
    """box: the TGA's share of the screen's width and height."""
    n = w * h
    total = sum(rgb[0::3]) + sum(rgb[1::3]) + sum(rgb[2::3])
    mean = total / (3 * n)
    if mean < 4:
        return "black"
    if mean > 180:
        return "white"
    bw, bh = round(w * box[0]) + 2, round(h * box[1]) + 2  # a pixel of slack for the scaling
    x0, y0 = (w - bw) // 2, (h - bh) // 2
    inside = outside = 0
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if abs(r - GROUND[0]) + abs(g - GROUND[1]) + abs(b - GROUND[2]) <= 12:
                continue
            if x0 <= x < x0 + bw and y0 <= y < y0 + bh:
                inside += 1
            else:
                outside += 1
    if outside > n * 0.002:
        return "other"
    return "splash" if inside > 0 else "ground"


def size_of(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")  # fmt: skip
    return int(out[0]), int(out[1])


def frames(path: Path):
    """(seconds into the recording, rgb24 bytes at W x H) for every recorded frame."""
    pts = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "frame=pts_time",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-fps_mode", "passthrough",
         "-vf", f"scale={W}:{H}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    size = W * H * 3
    for i in range(len(raw) // size):
        t = float(pts[i]) if i < len(pts) and pts[i] not in ("", "N/A") else None
        yield t, raw[i * size : (i + 1) * size]


def runs_of(kinds: list[tuple[float | None, str]]) -> list[dict]:
    runs: list[dict] = []
    for t, kind in kinds:
        if runs and runs[-1]["kind"] == kind:
            runs[-1]["frames"] += 1
            runs[-1]["end_s"] = t
        else:
            runs.append({"kind": kind, "frames": 1, "start_s": t, "end_s": t})
    return runs


def verdict(runs: list[dict]) -> dict:
    """Gaps: black or white runs between the first splash frame and the first other frame."""
    kinds = [r["kind"] for r in runs]
    first = kinds.index("splash") if "splash" in kinds else None
    if first is None:
        return {"splash_seen": False, "gaps": [], "ok": False}
    end = kinds.index("other", first) if "other" in kinds[first:] else len(runs)
    gaps = [r for r in runs[first:end] if r["kind"] in ("black", "white")]
    before = [r for r in runs[:first] if r["kind"] == "white"]
    return {
        "splash_seen": True,
        "black_before_splash_frames": sum(
            r["frames"] for r in runs[:first] if r["kind"] == "black"
        ),
        "white_before_splash": before,
        "gaps": gaps,
        "reached_other": end < len(runs),
        "ok": not gaps and not before,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("recording", type=Path)
    p.add_argument("--json", type=Path, help="also write the result here")
    p.add_argument(
        "--screen",
        help="the screen's size, WxH, when the recording was scaled down (record-boot.sh "
        "records at 480x270); default: the recording's own size, or 1920x1080 below 1000 wide",
    )
    args = p.parse_args(argv)
    rw, rh = size_of(args.recording)
    if args.screen:
        sw, sh = map(int, args.screen.lower().split("x"))
    else:
        sw, sh = (rw, rh) if rw >= 1000 else (1920, 1080)
    box = (TGA[0] / sw, TGA[1] / sh)
    kinds = [(t, classify(rgb, box)) for t, rgb in frames(args.recording)]
    runs = runs_of(kinds)
    result = {"recording": str(args.recording), "frames": len(kinds), "runs": runs, **verdict(runs)}
    print(" → ".join(f"{r['kind']} x{r['frames']}" for r in runs))
    if result["ok"]:
        print("ok: no black or white frame from the splash's first frame to the hand-over")
    elif not result["splash_seen"]:
        print("FAIL: the splash never showed")
    else:
        print(f"FAIL: {result['gaps'] or result['white_before_splash']}")
    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

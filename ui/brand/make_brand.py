"""Regenerate the Couch brand assets in this folder.

    uv run --no-project --with fonttools --with brotli --with uharfbuzz --with pillow \
        python ui/brand/make_brand.py [path/to/space-grotesk.woff2]

Needs `rsvg-convert` (librsvg) on PATH for the PNGs. The font defaults to the
UI's own copy in ui/fonts/. The wordmark is outlined from it, so the SVGs and
PNGs don't depend on any installed font.
"""

import io
import subprocess
import sys
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image

HERE = Path(__file__).resolve().parent
NAME = "Couch"
WEIGHT = 600
TRACKING = -0.03  # em, as in the launcher's headings

NIGHT = "#121214"  # startup ground, from the evening B3 design
WHITE = "#FFFFFF"

# The Seat mark: four rounded bars on a 100 x 100 grid. Its ink spans x 10..90, y 22..84.
SEAT = [
    (31, 22, 38, 30, 8),  # back cushion
    (10, 38, 17, 46, 8.5),  # left arm
    (73, 38, 17, 46, 8.5),  # right arm
    (10, 61, 80, 23, 8.5),  # seat
]
SEAT_BOX = (10, 22, 90, 84)


def seat(scale=1.0, dx=0.0, dy=0.0):
    """The mark as SVG <rect>s, moved so its ink's top-left lands on (dx, dy)."""
    x0, y0 = SEAT_BOX[0], SEAT_BOX[1]
    out = []
    for x, y, w, h, r in SEAT:
        out.append(
            f'<rect x="{fmt((x - x0) * scale + dx)}" y="{fmt((y - y0) * scale + dy)}" '
            f'width="{fmt(w * scale)}" height="{fmt(h * scale)}" rx="{fmt(r * scale)}"/>'
        )
    return "".join(out)


def fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def wordmark_path(font_path):
    """Outline NAME and return (svg path d, width, cap height), y down, baseline at cap height."""
    tt = TTFont(font_path)
    tt.flavor = None  # HarfBuzz reads TTF, not WOFF2
    raw = io.BytesIO()
    tt.save(raw)
    data = raw.getvalue()
    face = hb.Face(data)
    hbfont = hb.Font(face)
    hbfont.set_variations({"wght": WEIGHT})
    buf = hb.Buffer()
    buf.add_str(NAME)
    buf.guess_segment_properties()
    hb.shape(hbfont, buf, {"kern": True, "liga": True})

    tt = TTFont(io.BytesIO(data))
    upem = tt["head"].unitsPerEm
    cap = tt["OS/2"].sCapHeight
    inst = instantiateVariableFont(tt, {"wght": WEIGHT})
    glyphs = inst.getGlyphSet()
    order = inst.getGlyphOrder()
    track = TRACKING * upem

    pen = SVGPathPen(glyphs, ntos=fmt)
    bounds = BoundsPen(glyphs)
    x = 0.0
    infos, positions = buf.glyph_infos, buf.glyph_positions
    for i, (info, pos) in enumerate(zip(infos, positions, strict=True)):
        name = order[info.codepoint]
        # Flip y and sit the baseline at y = cap, so capitals touch y = 0.
        t = (1, 0, 0, -1, x + pos.x_offset, cap - pos.y_offset)
        glyphs[name].draw(TransformPen(pen, t))
        glyphs[name].draw(TransformPen(bounds, t))
        x += pos.x_advance + (track if i < len(infos) - 1 else 0)
    xmin, ymin, xmax, ymax = bounds.bounds
    return pen.getCommands(), (xmin, ymin, xmax, ymax), cap


def svg(w, h, body, fill, vb=None, size=0.1):
    """Drawn in font units; the intrinsic size is `size` px per unit (0.1 = a 100 px em)."""
    vb = vb or f"0 0 {fmt(w)} {fmt(h)}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'width="{fmt(w * size)}" height="{fmt(h * size)}" fill="{fill}" '
        f'role="img" aria-label="{NAME}">{body}</svg>\n'
    )


def write_variants(stem, w, h, body, vb=None, size=0.1):
    """currentColor (for inlining in the UI), plus white and night for <img> and docs."""
    for suffix, fill in (("", "currentColor"), ("-white", WHITE), ("-night", NIGHT)):
        (HERE / "svg" / f"{stem}{suffix}.svg").write_text(svg(w, h, body, fill, vb, size))


def png(svg_text, out, w, h):
    subprocess.run(
        ["rsvg-convert", "-w", str(w), "-h", str(h), "-o", str(out)],
        input=svg_text.encode(),
        check=True,
    )


def main():
    default = HERE.parent / "fonts" / "space-grotesk-latin.woff2"
    font = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    (HERE / "svg").mkdir(exist_ok=True)
    d, (wx0, wy0, wx1, wy1), cap = wordmark_path(font)
    ww, wh = wx1 - wx0, wy1 - wy0
    word = f'<path transform="translate({fmt(-wx0)} {fmt(-wy0)})" d="{d}"/>'

    # Mark: square, ink centred.
    mw, mh = SEAT_BOX[2] - SEAT_BOX[0], SEAT_BOX[3] - SEAT_BOX[1]
    write_variants("mark", 100, 100, seat(1, 10, 10 + (mw - mh) / 2), vb="0 0 100 100", size=1)

    # Wordmark: tight to the ink.
    write_variants("wordmark", ww, wh, word)

    # Horizontal lockup (launcher header): the mark is cap height and sits on the baseline.
    s = cap / mh
    gap = 0.55 * cap
    lw = mw * s + gap + ww
    lh = max(cap, wy1) - min(0, wy0)
    top = -min(0, wy0)
    body = (
        seat(s, 0, top)
        + f'<path transform="translate({fmt(mw * s + gap - wx0)} {fmt(top)})" d="{d}"/>'
    )
    write_variants("lockup", lw, lh, body)

    # Stacked lockup (splash, startup): a bigger mark over the word.
    s2 = 1.25 * cap / mh
    gap2 = 0.75 * cap
    sw = max(mw * s2, ww)
    sh = mh * s2 + gap2 + wh
    tx, ty = (sw - ww) / 2 - wx0, mh * s2 + gap2 - wy0
    body2 = seat(s2, (sw - mw * s2) / 2, 0) + (
        f'<path transform="translate({fmt(tx)} {fmt(ty)})" d="{d}"/>'
    )
    write_variants("lockup-stacked", sw, sh, body2)

    # Splash: 1920 x 1080, stacked lockup centred, word at 120 px as in the design.
    px = 120 / 1000
    W, H = 1920, 1080
    ox, oy = (W - sw * px) / 2, (H - sh * px) / 2
    splash = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
        f'<rect width="{W}" height="{H}" fill="{NIGHT}"/>'
        f'<g fill="{WHITE}" transform="translate({fmt(ox)} {fmt(oy)}) scale({px})">{body2}</g>'
        "</svg>\n"
    )
    (HERE / "splash.svg").write_text(splash)
    png(splash, HERE / "splash.png", W, H)

    # App icon / favicon: night tile, white mark.
    k = 0.76
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
        f'<rect width="100" height="100" rx="22" fill="{NIGHT}"/>'
        f'<g fill="{WHITE}">{seat(k, (100 - mw * k) / 2, (100 - mh * k) / 2)}</g>'
        "</svg>\n"
    )
    (HERE / "favicon.svg").write_text(icon)
    for size in (180, 192, 512):
        png(icon, HERE / f"icon-{size}.png", size, size)
    frames = []
    for size in (16, 32, 48):
        out = HERE / f".icon-{size}.png"
        png(icon, out, size, size)
        frames.append(Image.open(out).copy())
        out.unlink()
    sizes = [(f.width, f.height) for f in frames]
    frames[-1].save(HERE / "favicon.ico", sizes=sizes, append_images=frames[:-1])


if __name__ == "__main__":
    main()

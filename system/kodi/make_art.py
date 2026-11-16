"""Regenerate the images Kodi shows while Plex opens and closes.

    uv run --no-project --with fonttools --with brotli --with uharfbuzz \
        python system/kodi/make_art.py

Needs `rsvg-convert` (librsvg) on PATH. The text is outlined from the UI's own fonts, so
Kodi needs neither font.

They copy the "Opening Plex" state of the Opening artboard in the evening B3 design (1280x720
there, x1.5 here): the Plex amber flood, "Plex" big at the bottom left, and a small bar with
"Opening" under it. Kodi's splash is the first frame; the skin's Home draws the same picture
from three layers so the bar can move, and drops the bar while Kodi quits.

- splash.png             Kodi's splash, shown while it loads its skin
- skin/media/plex.png    the flood and the word
- skin/media/opening.png the bar's track and "Opening", on transparent
- skin/media/bar.png     the bar itself

And the white shapes skin.couch tints for couchd's overlays (Custom_1190_CouchOverlay.xml):

- skin/media/couch-white.png  a plain square, for the sheet
- skin/media/couch-round.png  a card: 30 px corners, drawn with border="30"
- skin/media/couch-ring.png   a card's 3 px outline, the same corners
- skin/media/couch-pill.png   the volume bar: 6 px corners, border="6"
"""

import io
import subprocess
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

HERE = Path(__file__).resolve().parent
FONTS = HERE.parent.parent / "ui" / "fonts"
DISPLAY = FONTS / "space-grotesk-latin.woff2"
TEXT = FONTS / "inter-latin.woff2"

W, H = 1920, 1080
S = 1.5  # the artboard is 1280 x 720
GROUND = "#B57D09"  # --plex-deep: evening colours are on by default
INK = "#1F1600"  # --plex-ink
TRACK_ALPHA = 0.2
LABEL_ALPHA = 0.85

MARGIN = 72 * S
WORD_SIZE, WORD_LH, WORD_WEIGHT, WORD_TRACK = 140 * S, 0.9, 600, -0.035
GAP = 24 * S
BAR_W, BAR_H, BAR_PART = 180 * S, 4 * S, 0.4
ROW_GAP = 18 * S
LABEL_SIZE, LABEL_WEIGHT = 20 * S, 400


def fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def outline(font_path, text, size, weight, tracking=0.0):
    """Outline `text`; returns (svg path d at `size` px with the baseline at y = 0, metrics)."""
    tt = TTFont(font_path)
    tt.flavor = None  # HarfBuzz reads TTF, not WOFF2
    raw = io.BytesIO()
    tt.save(raw)
    data = raw.getvalue()
    hbfont = hb.Font(hb.Face(data))
    hbfont.set_variations({"wght": weight})
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hbfont, buf, {"kern": True, "liga": True})

    tt = TTFont(io.BytesIO(data))
    upem = tt["head"].unitsPerEm
    ascent, descent = tt["hhea"].ascent / upem, -tt["hhea"].descent / upem
    inst = instantiateVariableFont(tt, {"wght": weight})
    glyphs, order = inst.getGlyphSet(), inst.getGlyphOrder()
    k = size / upem
    pen = SVGPathPen(glyphs, ntos=fmt)
    x = 0.0
    infos, positions = buf.glyph_infos, buf.glyph_positions
    for i, (info, pos) in enumerate(zip(infos, positions, strict=True)):
        t = (k, 0, 0, -k, (x + pos.x_offset) * k, -pos.y_offset * k)
        glyphs[order[info.codepoint]].draw(TransformPen(pen, t))
        x += pos.x_advance + (tracking * upem if i < len(infos) - 1 else 0)
    return pen.getCommands(), {"ascent": ascent * size, "descent": descent * size}


def layout():
    """Where everything goes, as CSS would put it (flex column at the bottom left)."""
    word, wm = outline(DISPLAY, "Plex", WORD_SIZE, WORD_WEIGHT, WORD_TRACK)
    label, lm = outline(TEXT, "Opening", LABEL_SIZE, LABEL_WEIGHT)
    row_h = max(BAR_H, lm["ascent"] + lm["descent"])  # line-height: normal
    row_top = H - MARGIN - row_h
    row_mid = row_top + row_h / 2
    word_box = WORD_SIZE * WORD_LH
    word_top = row_top - GAP - word_box
    half_leading = (word_box - (wm["ascent"] + wm["descent"])) / 2
    return {
        "word": word,
        "word_base": word_top + half_leading + wm["ascent"],
        "label": label,
        "label_x": MARGIN + BAR_W + ROW_GAP,
        "label_base": row_mid - (lm["ascent"] + lm["descent"]) / 2 + lm["ascent"],
        "bar_y": row_mid - BAR_H / 2,
    }


def bar(x, y, w):
    return (
        f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(w)}" height="{fmt(BAR_H)}" '
        f'rx="{fmt(BAR_H / 2)}"/>'
    )


def svg(body, w=W, h=H):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">{body}</svg>\n'
    )


def png(text, out):
    subprocess.run(["rsvg-convert", "-o", str(out)], input=text.encode(), check=True)


def main():
    p = layout()
    ground = f'<rect width="{W}" height="{H}" fill="{GROUND}"/>'
    word = (
        f'<path fill="{INK}" transform="translate({fmt(MARGIN)} {fmt(p["word_base"])})" '
        f'd="{p["word"]}"/>'
    )
    opening = (
        f'<g fill="{INK}" fill-opacity="{TRACK_ALPHA}">{bar(MARGIN, p["bar_y"], BAR_W)}</g>'
        f'<path fill="{INK}" fill-opacity="{LABEL_ALPHA}" '
        f'transform="translate({fmt(p["label_x"])} {fmt(p["label_base"])})" d="{p["label"]}"/>'
    )
    moving = f'<g fill="{INK}">{bar(MARGIN, p["bar_y"], BAR_W * BAR_PART)}</g>'

    media = HERE / "skin" / "media"
    media.mkdir(parents=True, exist_ok=True)
    png(svg(ground + word + opening + moving), HERE / "splash.png")
    png(svg(ground + word), media / "plex.png")
    png(svg(opening), media / "opening.png")
    bw = BAR_W * BAR_PART
    png(svg(f'<g fill="{INK}">{bar(0, 0, bw)}</g>', fmt(bw), fmt(BAR_H)), media / "bar.png")
    shapes(media)
    # Where Home.xml puts the bar, and how far it slides.
    print(f"bar: left {fmt(MARGIN)} top {fmt(p['bar_y'])} size {fmt(bw)}x{fmt(BAR_H)}")
    print(f"slide: {fmt(BAR_W - bw)} px")


def shapes(media):
    """White nine-slice shapes; the skin gives them colour with colordiffuse."""
    card, r, line = 64, 30, 3  # --radius-card and --border, x1.5 already
    png(svg('<rect width="8" height="8" fill="#fff"/>', 8, 8), media / "couch-white.png")
    png(
        svg(f'<rect width="{card}" height="{card}" rx="{r}" fill="#fff"/>', card, card),
        media / "couch-round.png",
    )
    half = line / 2
    ring = (
        f'<rect x="{half}" y="{half}" width="{card - line}" height="{card - line}" '
        f'rx="{r - half}" fill="none" stroke="#fff" stroke-width="{line}"/>'
    )
    png(svg(ring, card, card), media / "couch-ring.png")
    png(svg('<rect width="24" height="12" rx="6" fill="#fff"/>', 24, 12), media / "couch-pill.png")


if __name__ == "__main__":
    main()

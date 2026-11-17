# Make "Space Grotesk Tabular": the digits mapped to their tabular (.tf) forms, since Slint
# can't turn on the tnum feature. Used for the clocks only.
import sys

from fontTools.ttLib import TTFont

src, dst = sys.argv[1], sys.argv[2]
f = TTFont(src)
names = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
order = set(f.getGlyphOrder())
for table in f["cmap"].tables:
    if table.isUnicode():
        for i, n in enumerate(names):
            if f"{n}.tf" in order and 0x30 + i in table.cmap:
                table.cmap[0x30 + i] = f"{n}.tf"
        if "colon.tf" in order and 0x3A in table.cmap:
            table.cmap[0x3A] = "colon.tf"
for rec in f["name"].names:
    if rec.nameID in (1, 16):
        rec.string = "Space Grotesk Tabular"
    elif rec.nameID in (4,):
        rec.string = str(rec.toUnicode()).replace("Space Grotesk", "Space Grotesk Tabular")
    elif rec.nameID == 6:
        rec.string = str(rec.toUnicode()).replace("SpaceGrotesk", "SpaceGroteskTabular")
f.save(dst)

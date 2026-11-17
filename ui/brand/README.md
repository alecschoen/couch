# Couch brand

The name is **Couch**. The mark is **Seat**: a sofa drawn from four rounded bars, with the same soft corners as the launcher's cards. Both are white on the evening B3 colours.

## Files

| File | Use |
| --- | --- |
| `splash.png` | Boot splash (#4), 1920×1080: the stacked lockup on the night colour |
| `splash.svg` | The same splash as a vector |
| `splash.tga` | What the Pi's kernel draws at boot (#4): the middle 400×320 of `splash.png`, 128 greys. The kernel centres it and fills the rest of the screen with night, so it lands on the same pixels as `splash.png` |
| `boot-animation.bin` | The boot animation couch-fbsplash plays from the still logo and back (see `system/README.md`). Built by `scripts/render-boot-animation.py` |
| `svg/lockup-stacked*.svg` | Mark over the word: the startup screen (#19) |
| `svg/lockup*.svg` | Mark beside the word: the launcher header and About |
| `svg/wordmark*.svg` | The word alone |
| `svg/mark*.svg` | The mark alone, square, 100×100 |
| `favicon.svg`, `favicon.ico` | Browser tab (16, 32 and 48 px in the .ico) |
| `icon-180.png`, `icon-192.png`, `icon-512.png` | App icon: the mark on a rounded night tile |

Each SVG comes in three colours:

- `name.svg` uses `currentColor`. Inline it in the UI and it takes the text colour, so it follows the flood: white on most apps, and `#1F1600` on Plex amber.
- `name-white.svg` is white, for `<img>` on dark grounds.
- `name-night.svg` is `#121214`, for light grounds such as docs and GitHub.

The wordmark is outlined Space Grotesk 600 with −0.03 em tracking, the same as the launcher's headings, so the files don't need the font.

## Sizes on a 1080p TV

- Splash and startup screen: the word is 120 px tall (its em), and the mark is 1.25 × its cap height.
- Launcher header: 40 px, with the mark at cap height sitting on the baseline.
- Keep at least half the mark's height clear around any lockup.

## Colours

| Token | Hex | Where |
| --- | --- | --- |
| Night | `#121214` | Splash and startup ground, icon tile |
| White | `#FFFFFF` | The mark and word on every deep colour |
| Amber ink | `#1F1600` | The mark and word on Plex amber (`#B57D09`) |

## CEC name

The TV's input list shows the OSD name the Pi sends over HDMI-CEC. Use `Couch`, which is 5 of the 14 characters allowed.

## Regenerating

`make_brand.py` builds every file here from the mark's geometry and the UI's Space Grotesk:

```bash
uv run --no-project --with fonttools --with brotli --with uharfbuzz --with pillow \
    python ui/brand/make_brand.py
```

It needs `rsvg-convert` (librsvg) for the PNGs. It reads `ui/fonts/space-grotesk-latin.woff2` by default, or you can pass a font path.

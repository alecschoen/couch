# ui/

What's left of the web launcher, now that the launcher is native (`launcher/`):

```
brand/                the logo, the boot splash and boot animation, icons (brand/README.md)
js/overlay-view.js    the overlays couchd draws over YouTube: quick menu, toasts, volume panel
css/tokens.css        palettes, type sizes, spacing, radii and durations, for the overlays
css/components.css    the base pieces the overlays use (pills, sheet, toasts)
css/overlay.css       the quick menu, the volume panel and their corner
fonts/                Space Grotesk and Inter (woff2, SIL OFL), embedded in the overlays
```

couchd builds one script from these (`couchd/overlay_page.py`) and injects it into YouTube's
page over DevTools, inside a closed shadow root: so `overlay-view.js` has no imports, only
touches the DOM, and never takes focus or listens for keys. couchd routes the remote's keys
while the quick menu is open. Nothing loads from the network
(`tests/test_launcher_sources.py` checks).

The launcher draws the same overlays itself (`launcher/ui/overlay.slint`), and its design
tokens (`launcher/ui/tokens.slint`) are these, by hand: change one, change the other.

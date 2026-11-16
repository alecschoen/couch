"""Static checks on the launcher UI: it must work with no internet, from local files only."""

import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "ui"
SOURCES = sorted(p for p in UI.rglob("*") if p.suffix in {".html", ".css", ".js"})

# url(...) in CSS, and href/src/import specifiers in HTML and JS.
CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""")
HTML_REF = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""")
JS_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(?\s*)["']([^"']+)["']""")


COMMENT = re.compile(r"/\*.*?\*/|^\s*//.*$", re.DOTALL | re.MULTILINE)


def references(path: Path) -> list[str]:
    text = path.read_text()
    if path.suffix in {".css", ".js"}:
        text = COMMENT.sub("", text)  # usage examples in comments aren't real references
    patterns = {".css": [CSS_URL], ".html": [HTML_REF, JS_IMPORT], ".js": [JS_IMPORT]}[path.suffix]
    return [m for p in patterns for m in p.findall(text)]


def test_found_ui_sources():
    assert UI / "index.html" in SOURCES
    assert any(p.suffix == ".css" for p in SOURCES)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(UI)))
def test_nothing_loads_from_the_network(path: Path):
    remote = [ref for ref in references(path) if re.match(r"(?:https?:)?//", ref)]
    assert not remote, f"{path.name} loads from the network: {remote}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(UI)))
def test_local_references_exist(path: Path):
    missing = []
    for ref in references(path):
        if ref.startswith(("data:", "#", "ws:", "wss:")):
            continue
        target = (UI / ref.lstrip("/")) if ref.startswith("/") else (path.parent / ref)
        if not target.resolve().is_file():
            missing.append(ref)
    assert not missing, f"{path.name} refers to missing files: {missing}"


def test_fonts_are_bundled_with_their_licences():
    fonts = {p.name for p in (UI / "fonts").glob("*.woff2")}
    assert {"inter-latin.woff2", "space-grotesk-latin.woff2"} <= fonts
    assert (UI / "fonts" / "OFL-Inter.txt").is_file()
    assert (UI / "fonts" / "OFL-SpaceGrotesk.txt").is_file()

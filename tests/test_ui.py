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


# Block comments, and whole-line // comments (not `//` inside a URL).
COMMENT = re.compile(r"/\*[\s\S]*?\*/|^\s*//[^\n]*", re.MULTILINE)


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


def test_inline_brand_matches_the_brand_files():
    """ui/js/brand.js carries copies of the brand SVGs; they must not drift."""
    text = (UI / "js" / "brand.js").read_text()
    svg = (UI / "brand" / "svg" / "lockup-stacked.svg").read_text().strip()
    assert f"export const LOCKUP_STACKED = `{svg}`;" in text


# ---- Screens, routes and commands: the names the UI uses must exist on the other side.

ROOT = UI.parent
JS = sorted(p for p in UI.rglob("*.js"))


def code(path: Path) -> str:
    return COMMENT.sub("", path.read_text())


def registered_screens() -> set[str]:
    return set(re.findall(r"""\.register\(\s*["']([\w-]+)["']""", (UI / "index.html").read_text()))


def couchd_commands() -> set[str]:
    source = "\n".join(p.read_text() for p in (ROOT / "couchd").glob("*.py"))
    return set(re.findall(r"""commands\.add\(\s*["']([\w.]+)["']""", source))


def test_home_is_the_first_screen():
    page = (UI / "index.html").read_text()
    assert re.search(r"""app\.start\(\s*["']home["']\s*\)""", page)
    assert "home" in registered_screens()


def test_screens_only_go_to_registered_screens():
    screens = registered_screens()
    gone_to = {
        name
        for path in JS
        for name in re.findall(r"""(?:\.go\(|\broute:\s*)["']([\w-]+)["']""", code(path))
    }
    assert {"bluetooth", "settings"} <= gone_to  # Home's cards
    assert gone_to <= screens, f"not registered in index.html: {gone_to - screens}"


def test_ui_only_sends_commands_couchd_has():
    commands = couchd_commands()
    assert "audio.set_output" in commands  # the pattern still finds them
    pattern = r"""command\(\s*["']([\w.]+)["']"""
    sent = {name for path in JS for name in re.findall(pattern, code(path))}
    assert {"apps.launch", "audio.set_output"} <= sent
    assert sent <= commands, f"couchd has no such command: {sent - commands}"


def test_home_cards_match_the_design_and_launch_real_apps():
    home = code(UI / "js" / "screens" / "home.js")
    ids = re.findall(r"""\{\s*id:\s*["']([\w-]+)["']""", home)
    assert ids == ["youtube", "plex", "audio", "bluetooth", "settings"]
    apps_py = (ROOT / "couchd" / "apps.py").read_text()
    apps = set(re.findall(r"""App\(\s*["']([\w-]+)["']""", apps_py))
    launched = set(re.findall(r"""\bapp:\s*["']([\w-]+)["']""", home))
    assert launched == {"youtube", "plex"}
    assert launched <= apps


def test_the_app_binds_settings_to_couchd():
    # Without this, ui/js/settings.js only changes its local copy and nothing is saved.
    assert re.search(r"settings\.bind\(app\.couch\)", (UI / "js" / "app.js").read_text())

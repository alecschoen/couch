"""Static checks across the native launcher (launcher/, Rust) and couchd: the names one side
uses must exist on the other, and what couchd puts in YouTube's pages must work offline."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher" / "src"
UI = ROOT / "ui"  # the overlays' sources and the brand, all that's left of the web launcher

COMMAND = re.compile(r"""(?:command(?:_or_toast)?|run)\(\s*(?:app,\s*)?"([a-z_]+\.[a-z_.]+)\"""")
CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""")


def rust() -> str:
    return "\n".join(p.read_text() for p in sorted(LAUNCHER.rglob("*.rs")))


def couchd_commands() -> set[str]:
    source = "\n".join(p.read_text() for p in (ROOT / "couchd").glob("*.py"))
    return set(re.findall(r"""commands\.add\(\s*["']([\w.]+)["']""", source))


def test_launcher_only_sends_commands_couchd_has():
    commands = couchd_commands()
    assert "audio.set_output" in commands  # the pattern still finds them
    sent = set(COMMAND.findall(rust()))
    assert {"apps.launch", "apps.home", "settings.set", "ui.first_frame", "ui.drawn"} <= sent
    assert sent <= commands, f"couchd has no such command: {sent - commands}"


def test_launcher_setting_defaults_match_couchd():
    # The launcher paints from its defaults before couchd's snapshot arrives; they must be
    # couchd's, or the first frame shows the wrong thing.
    from couchd.settings import SCHEMA

    state = (LAUNCHER / "state.rs").read_text()
    block = re.search(r"serde_json::json!\(\{([\s\S]*?)\}\)", state).group(1)
    defaults = {k: json.loads(v) for k, v in re.findall(r'"([\w.]+)":\s*([^,\n]+),', block)}
    assert defaults == {k: s.default for k, s in SCHEMA.items()}


def test_launcher_ui_sound_modes_are_couchds():
    from couchd.settings import SCHEMA

    text = (LAUNCHER / "screens" / "settings" / "text.rs").read_text()
    line = next(x for x in text.splitlines() if '"audio.ui_sounds" =>' in x)
    assert re.findall(r's\("(\w+)"\)', line) == list(SCHEMA["audio.ui_sounds"].choices)


def test_home_launches_real_apps():
    home = (LAUNCHER / "screens" / "home.rs").read_text()
    launched = set(re.findall(r'app: Some\("([\w-]+)"\)', home))
    assert launched == {"youtube", "plex"}
    apps_py = (ROOT / "couchd" / "apps.py").read_text()
    assert launched <= set(re.findall(r"""App\(\s*["']([\w-]+)["']""", apps_py))


def test_overlay_sources_load_nothing_from_the_network():
    for path in sorted((UI / "css").glob("*.css")):
        remote = [u for u in CSS_URL.findall(path.read_text()) if re.match(r"(?:https?:)?//", u)]
        assert not remote, f"{path.name} loads from the network: {remote}"
    view = (UI / "js" / "overlay-view.js").read_text()
    assert not re.search(r"""\bimport\b[^;]*["']""", view)  # injected as one script


def test_fonts_are_bundled_with_their_licences():
    fonts = {p.name for p in (UI / "fonts").glob("*.woff2")}
    assert {"inter-latin.woff2", "space-grotesk-latin.woff2"} <= fonts
    assert (UI / "fonts" / "OFL-Inter.txt").is_file()
    assert (UI / "fonts" / "OFL-SpaceGrotesk.txt").is_file()

"""Kodi's profile for Plex (#22): pinned addons, enabled without Kodi's UI, settings merged."""

import hashlib
import importlib.util
import io
import sqlite3
import tomllib
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from couchd import kodi_profile
from couchd.kodi_profile import GUI_SETTINGS, Profile, ProfileError

ROOT = Path(__file__).resolve().parent.parent
KODI_SRC = ROOT / "system" / "kodi"


def addon_zip(addon_id, version, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{addon_id}/addon.xml", f'<addon id="{addon_id}" version="{version}"/>')
        z.writestr(f"{addon_id}/lib/x.py", "x = 1\n")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def world(tmp_path):
    """A source tree with two pinned addons, a fake Estuary, and an empty Kodi home."""
    src = tmp_path / "src"
    (src / "service.couch").mkdir(parents=True)
    (src / "service.couch" / "addon.xml").write_text('<addon id="service.couch" version="1"/>')
    (src / "service.couch" / "service.py").write_text("print('hi')\n")
    (src / "skin" / "xml").mkdir(parents=True)
    (src / "skin" / "xml" / "Home.xml").write_text("<window/>")
    (src / "skin" / "media").mkdir()
    (src / "skin" / "media" / "plex.png").write_bytes(b"png")
    (src / "splash.png").write_bytes(b"splash")
    zips = {
        "https://example/a.zip": addon_zip("script.plexmod", "1.3.19"),
        "https://example/b.zip": addon_zip("script.module.six", "1.16.0+matrix.1"),
    }
    pins = [("script.plexmod", "1.3.19", "a"), ("script.module.six", "1.16.0+matrix.1", "b")]
    (src / "addons.toml").write_text(
        "".join(
            f'[[addon]]\nid = "{i}"\nversion = "{v}"\nurl = "https://example/{u}.zip"\n'
            f'sha256 = "{hashlib.sha256(zips[f"https://example/{u}.zip"]).hexdigest()}"\n'
            for i, v, u in pins
        )
    )
    est = tmp_path / "estuary"
    (est / "xml").mkdir(parents=True)
    (est / "media").mkdir()
    (est / "fonts").mkdir()
    (est / "addon.xml").write_text(
        '<addon id="skin.estuary" version="4.0.0" name="Estuary" provider-name="phil65">'
        '<extension point="xbmc.gui.skin"><res width="1920" height="1080" folder="xml"/>'
        "</extension></addon>"
    )
    for f in ("Home.xml", "Startup.xml", "VideoFullScreen.xml"):
        (est / "xml" / f).write_text("<window/>")
    (est / "media" / "Textures.xbt").write_bytes(b"xbt")
    fetched = []

    def fetch(url):
        fetched.append(url)
        return zips[url]

    def make(**kw):
        return Profile(tmp_path / "home", src, est, fetch=fetch, **kw)

    return {"make": make, "home": tmp_path / "home", "src": src, "est": est, "fetched": fetched}


def installed(home):
    (db,) = (home / "userdata" / "Database").glob("Addons*.db")
    with sqlite3.connect(db) as c:
        return dict(c.execute("SELECT addonID, enabled FROM installed"))


def settings(path):
    return {s.get("id"): s.text for s in ET.parse(path).getroot().iter("setting")}


def test_fresh_profile_then_nothing_to_do(world):
    changes = world["make"]().apply()
    assert changes and all(changes)
    home = world["home"]
    assert (home / "addons" / "script.plexmod" / "lib" / "x.py").is_file()
    assert (home / "addons" / "service.couch" / "service.py").is_file()
    assert (home / "media" / "splash.png").read_bytes() == b"splash"
    assert installed(home) == {
        "script.plexmod": 1,
        "script.module.six": 1,
        "service.couch": 1,
        "skin.couch": 1,
        "repository.xbmc.org": 0,
    }
    gui = settings(home / "userdata" / "guisettings.xml")
    assert gui["lookandfeel.skin"] == "skin.couch"
    assert gui["videoplayer.adjustrefreshrate"] == "2"
    assert gui["videoplayer.useprimedecoder"] == "true"
    assert gui["audiooutput.audiodevice"] == "PULSE:Default"
    assert gui["general.addonupdates"] == "2"
    plex = settings(home / "userdata" / "addon_data" / "script.plexmod" / "settings.xml")
    assert plex["auto_update_check"] == "false"
    cec = ET.parse(home / "userdata" / "peripheral_data" / "cec_CEC_Adapter.xml").getroot()
    assert [(s.get("id"), s.get("value")) for s in cec] == [("enabled", "0")]

    # A second run changes nothing and downloads nothing.
    world["fetched"].clear()
    assert world["make"]().apply() == []
    assert world["fetched"] == []


def test_dry_run_writes_nothing(world):
    changes = world["make"](dry_run=True).apply()
    assert any("script.plexmod" in c for c in changes)
    assert not world["home"].exists()
    assert world["fetched"] == []


def test_database_matches_what_kodi_would_create(world):
    world["make"]().apply()
    db = world["home"] / "userdata" / "Database" / "Addons33.db"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT idVersion, iCompressCount FROM version").fetchall() == [(33, 0)]
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"version", "addons", "repo", "addonlinkrepo", "update_rules", "package",
                      "installed"}  # fmt: skip


def test_existing_database_rows_are_flipped_not_duplicated(world):
    db_dir = world["home"] / "userdata" / "Database"
    db_dir.mkdir(parents=True)
    with sqlite3.connect(db_dir / "Addons33.db") as c:
        c.executescript(kodi_profile.ADDONS_DB_SCHEMA)
        c.execute("INSERT INTO version VALUES (33, 0)")
        # What Kodi does with an addon it finds by itself: a disabled row.
        c.execute(
            "INSERT INTO installed (addonID, enabled, installDate) VALUES ('skin.couch', 0, 'x')"
        )
        c.execute(
            "INSERT INTO installed (addonID, enabled, installDate, origin) "
            "VALUES ('repository.xbmc.org', 1, 'x', 'b6a50484')"
        )
    world["make"]().apply()
    assert installed(world["home"])["skin.couch"] == 1
    assert installed(world["home"])["repository.xbmc.org"] == 0
    with sqlite3.connect(db_dir / "Addons33.db") as c:
        rows = c.execute("SELECT COUNT(*) FROM installed WHERE addonID='skin.couch'")
        assert rows.fetchone() == (1,)


def test_settings_merge_keeps_kodis_own(world):
    gui = world["home"] / "userdata" / "guisettings.xml"
    gui.parent.mkdir(parents=True)
    gui.write_text(
        '<settings version="2">'
        '<setting id="locale.language" default="true">resource.language.en_gb</setting>'
        '<setting id="lookandfeel.skin" default="true">skin.estuary</setting>'
        # Kodi expands the device name once it has seen it; that still counts.
        '<setting id="audiooutput.audiodevice">PULSE:Default|Default Output Device</setting>'
        "</settings>"
    )
    world["make"]().apply()
    got = settings(gui)
    assert got["locale.language"] == "resource.language.en_gb"
    assert got["lookandfeel.skin"] == "skin.couch"
    assert got["audiooutput.audiodevice"] == "PULSE:Default|Default Output Device"
    skin = next(s for s in ET.parse(gui).getroot() if s.get("id") == "lookandfeel.skin")
    assert "default" not in skin.attrib  # or Kodi would put its default back


def test_plex_sign_in_survives(world):
    path = world["home"] / "userdata" / "addon_data" / "script.plexmod" / "settings.xml"
    path.parent.mkdir(parents=True)
    path.write_text('<settings version="2"><setting id="auth.token">secret</setting></settings>')
    world["make"]().apply()
    assert settings(path)["auth.token"] == "secret"


def test_skin_is_estuary_with_our_home(world):
    world["make"]().apply()
    skin = world["home"] / "addons" / "skin.couch"
    est = world["est"]
    root = ET.parse(skin / "addon.xml").getroot()
    assert (root.get("id"), root.get("version")) == ("skin.couch", "4.0.0+couch.1")
    assert root.find("extension/res") is not None
    assert (skin / "xml" / "Home.xml").read_text() == "<window/>"
    assert not (skin / "xml" / "Home.xml").is_symlink()
    assert (skin / "xml" / "VideoFullScreen.xml").readlink() == est / "xml" / "VideoFullScreen.xml"
    assert (skin / "media" / "Textures.xbt").readlink() == est / "media" / "Textures.xbt"
    assert (skin / "media" / "plex.png").read_bytes() == b"png"
    assert (skin / "fonts").readlink() == est / "fonts"

    # Estuary changes with a Kodi upgrade: the skin follows, stale links go.
    (est / "xml" / "Startup.xml").unlink()
    (est / "xml" / "New.xml").write_text("<window/>")
    changes = world["make"]().apply()
    assert changes == ["updated skin.couch (2 files)"]
    assert not (skin / "xml" / "Startup.xml").exists()
    assert (skin / "xml" / "New.xml").is_symlink()


def test_new_pin_replaces_the_old_version(world):
    world["make"]().apply()
    old = world["home"] / "addons" / "script.plexmod"
    (old / "stale.py").write_text("")
    src = world["src"]
    new = addon_zip("script.plexmod", "1.4.0")
    url = "https://example/a.zip"
    pins = tomllib.loads((src / "addons.toml").read_text())["addon"]
    pins[0].update(version="1.4.0", sha256=hashlib.sha256(new).hexdigest())
    (src / "addons.toml").write_text(
        "".join(
            f'[[addon]]\nid = "{p["id"]}"\nversion = "{p["version"]}"\nurl = "{p["url"]}"\n'
            f'sha256 = "{p["sha256"]}"\n'
            for p in pins
        )
    )
    profile = world["make"]()
    profile.fetch = lambda u: new if u == url else pytest.fail(u)
    assert profile.apply() == ["installed script.plexmod 1.4.0"]
    assert kodi_profile.addon_version(old) == "1.4.0"
    assert not (old / "stale.py").exists()
    assert not [p for p in old.parent.iterdir() if p.name.startswith(".")]


def test_a_bad_download_is_refused(world):
    profile = world["make"]()
    profile.fetch = lambda url: addon_zip("script.plexmod", "1.3.19", {"script.plexmod/evil": "x"})
    with pytest.raises(ProfileError, match="sha256"):
        profile.apply()
    assert not (world["home"] / "addons" / "script.plexmod").exists()


def test_a_zip_cant_write_outside_its_addon():
    data = addon_zip("script.plexmod", "1", {"script.plexmod/../../etc/x": "x"})
    with pytest.raises(ProfileError, match="unexpected path"):
        kodi_profile._unzip_addon(data, "script.plexmod", Path("/nonexistent"))


def test_cli_prints_changes(world, capsys, monkeypatch):
    monkeypatch.setattr(kodi_profile, "fetch_url", world["make"]().fetch)
    args = ["--home", str(world["home"]), "--src", str(world["src"]),
            "--estuary", str(world["est"])]  # fmt: skip
    assert kodi_profile.main(args) == 0
    assert "installed script.plexmod 1.3.19" in capsys.readouterr().out
    assert kodi_profile.main(args) == 0
    assert capsys.readouterr().out == ""
    (world["est"] / "addon.xml").unlink()
    assert kodi_profile.main(args) == 1
    assert "no Estuary skin" in capsys.readouterr().err


# --- the real files in system/kodi ---------------------------------------------------------


def test_pins_are_complete_and_pinned():
    pins = kodi_profile.load_pins(KODI_SRC / "addons.toml")
    ids = {p.id for p in pins}
    assert "script.plexmod" in ids
    for p in pins:
        assert len(p.sha256) == 64 and p.url.startswith("https://"), p.id
        assert p.url.endswith(f"/{p.id}-{p.version}.zip"), p.id
        assert "/master/" not in p.url, f"{p.id}: pin a commit, not a branch"
    # PM4K's dependencies and theirs (Kodi's own xbmc.* are built in).
    assert ids >= {
        "script.module.requests", "script.module.six", "script.module.kodi-six",
        "script.module.urllib3", "script.module.idna", "script.module.chardet",
        "script.module.certifi",
    }  # fmt: skip


def test_service_addon_needs_plex():
    root = ET.parse(KODI_SRC / "service.couch" / "addon.xml").getroot()
    assert root.get("id") == "service.couch"
    assert {i.get("addon") for i in root.iter("import")} == {"xbmc.python", "script.plexmod"}
    assert root.find("extension[@point='xbmc.service']").get("library") == "service.py"


def test_home_and_splash_exist():
    home = ET.parse(KODI_SRC / "skin" / "xml" / "Home.xml").getroot()
    textures = {t.text for t in home.iter("texture")}
    for t in textures:
        assert (KODI_SRC / "skin" / "media" / t).is_file(), t
    assert (KODI_SRC / "splash.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_gui_settings_cover_the_issue():
    for key in (
        "videoplayer.adjustrefreshrate",
        "videoplayer.useprimedecoder",
        "audiooutput.audiodevice",
        "lookandfeel.skin",
    ):
        assert key in GUI_SETTINGS


def test_kodi_runs_on_gbm_with_pulseaudio():
    wrapper = (ROOT / "system" / "couch-kodi").read_text()
    assert "KODI_DATA=/var/lib/couch/kodi" in wrapper
    assert "--windowing=gbm" in wrapper
    assert "--audio-backend=pulseaudio" in wrapper


# --- service.couch's decisions -------------------------------------------------------------


def load_service():
    spec = importlib.util.spec_from_file_location(
        "couch_service", KODI_SRC / "service.couch" / "service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # imports xbmc only when it runs for real
    return module


class FakeKodi:
    def __init__(self):
        self.started = self.active = False
        self.home = True

    def plex_started(self):
        return self.started

    def plex_active(self):
        return self.active

    def home_in_front(self):
        return self.home


@pytest.fixture
def svc():
    service = load_service()
    kodi = FakeKodi()
    now = [0.0]
    watch = service.Watch(kodi, clock=lambda: now[0])

    def at(t):
        now[0] = t
        return watch.tick()

    return service, kodi, at


def test_plex_exit_quits_kodi(svc):
    _, kodi, at = svc
    assert at(1) is None  # PM4K loading; Home (the Opening screen) is in front
    kodi.started = True
    assert at(5) is None  # rendering templates, no window yet: not "minimised"
    kodi.active, kodi.home = True, False
    assert at(8) is None
    kodi.started = kodi.active = False  # Exit
    kodi.home = True
    assert at(100) is None
    assert at(101) is None
    assert at(102) == "Plex exited"


def test_plex_restarting_itself_is_not_an_exit(svc):
    _, kodi, at = svc
    kodi.started = kodi.active = True
    kodi.home = False
    at(1)
    kodi.started = False  # signing out restarts PM4K
    assert at(2) is None
    kodi.started = True
    assert at(3) is None
    assert at(10) is None


def test_minimise_quits_kodi(svc):
    _, kodi, at = svc
    kodi.started = kodi.active = True
    kodi.home = False
    assert at(1) is None
    kodi.active, kodi.home = False, True  # Minimize: Kodi's Home with PM4K loaded
    assert at(2) is None
    assert at(3) == "Plex was minimised"


def test_plex_that_never_starts_quits_kodi(svc):
    service, _, at = svc
    assert at(service.START_TIMEOUT - 1) is None
    assert at(service.START_TIMEOUT + 1) == "Plex didn't start"

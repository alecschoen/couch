"""Kodi's profile for Plex (#22): set up /var/lib/couch/kodi so Kodi opens straight into Plex.

install.sh runs this as the couch user, who owns the profile:

    python -m couchd.kodi_profile --home /var/lib/couch/kodi --src /opt/couch/kodi

It prints one line per change and changes nothing that already matches, so it is safe to
re-run. `--dry-run` only prints what it would change. Kodi rewrites its settings files when
it exits, so install.sh stops Kodi before a real run that would change something.

What it sets up, and why:

- Addons: PM4K (script.plexmod) and its script.module.* dependencies, at the versions and
  sha256s in system/kodi/addons.toml, unpacked into addons/. Plus Couch's own two:
  service.couch (opens Plex, quits Kodi after) and skin.couch.
- The addons database. Kodi 21 enters an addon it finds in addons/ as *disabled* unless the
  database already has a row for it, and enabling one normally takes Kodi's UI. Writing the
  rows first (enabled) means Kodi's UI never shows. repository.xbmc.org is disabled, so
  nothing updates behind our back.
- skin.couch: Estuary, linked file by file from the system's copy, with its Home replaced by
  the Opening screen. Kodi shows Home before it starts any addon, and for a moment while it
  quits; PM4K draws everything else with its own windows.
- media/splash.png: Kodi's splash, the same Opening screen.
- guisettings.xml: that skin, DRM PRIME decoding, refresh rate matched on start/stop, sound
  through PulseAudio (so it follows couchd's PipeWire routing), no UI sounds, no screensaver,
  no addon updates.
- Kodi's CEC adapter off: couchd owns CEC (#7).
- PM4K's own settings: no update check, no kiosk auto-start (service.couch starts it).
"""

import argparse
import contextlib
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ESTUARY = Path("/usr/share/kodi/addons/skin.estuary")
SKIN = "skin.couch"
SERVICE = "service.couch"
OFFICIAL_REPO = "repository.xbmc.org"
FETCH_TIMEOUT = 120

# Kodi 21's addon database schema (xbmc/addons/AddonDatabase.cpp, CreateTables and
# CreateAnalytics; version 33). Only used when Kodi hasn't made the database yet.
REASON_NONE, REASON_USER = 0, 1  # Kodi's AddonDisabledReason
ADDONS_DB_VERSION = 33
ADDONS_DB_SCHEMA = """
CREATE TABLE version (idVersion integer, iCompressCount integer);
CREATE TABLE addons (id INTEGER PRIMARY KEY,metadata BLOB,addonID TEXT NOT NULL,version TEXT NOT NULL,name TEXT NOT NULL,summary TEXT NOT NULL,news TEXT NOT NULL,description TEXT NOT NULL);
CREATE TABLE repo (id integer primary key, addonID text,checksum text, lastcheck text, version text, nextcheck TEXT);
CREATE TABLE addonlinkrepo (idRepo integer, idAddon integer);
CREATE TABLE update_rules (id integer primary key, addonID TEXT, updateRule INTEGER);
CREATE TABLE package (id integer primary key, addonID text, filename text, hash text);
CREATE TABLE installed (id INTEGER PRIMARY KEY, addonID TEXT UNIQUE, enabled BOOLEAN, installDate TEXT, lastUpdated TEXT, lastUsed TEXT, origin TEXT NOT NULL DEFAULT '', disabledReason INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idxAddons ON addons(addonID);
CREATE UNIQUE INDEX ix_addonlinkrepo_1 ON addonlinkrepo ( idAddon, idRepo );
CREATE UNIQUE INDEX ix_addonlinkrepo_2 ON addonlinkrepo ( idRepo, idAddon );
CREATE UNIQUE INDEX idxUpdate_rules ON update_rules(addonID, updateRule);
CREATE UNIQUE INDEX idxPackage ON package(filename);
"""  # noqa: E501


def _always(value: str) -> Callable[[str | None], bool]:
    return lambda current: current == value


# guisettings.xml: id -> (value to write, does the current value already do?)
GUI_SETTINGS: dict[str, tuple[str, Callable[[str | None], bool]]] = {
    "lookandfeel.skin": (SKIN, _always(SKIN)),
    "lookandfeel.soundskin": ("", lambda v: not v),  # no navigation clicks
    "lookandfeel.startupwindow": ("10000", _always("10000")),  # Home: the Opening screen
    "screensaver.mode": ("", lambda v: not v),  # couchd does idle
    "input.enablemouse": ("false", _always("false")),  # no pointer
    "videoplayer.useprimedecoder": ("true", _always("true")),
    "videoplayer.useprimedecoderforhw": ("true", _always("true")),
    "videoplayer.useprimerenderer": ("0", _always("0")),  # direct to plane
    "videoplayer.adjustrefreshrate": ("2", _always("2")),  # on start/stop
    # Kodi turns "PULSE:Default" into "PULSE:Default|<description>" when it starts.
    "audiooutput.audiodevice": ("PULSE:Default", lambda v: (v or "").startswith("PULSE:Default")),
    "general.addonupdates": ("2", _always("2")),  # never
    "general.addonnotifications": ("false", _always("false")),
    # Nothing to announce on the network. WS-Discovery alone takes ~2 s of Kodi's quit.
    "services.wsdiscovery": ("false", _always("false")),
    "services.zeroconf": ("false", _always("false")),
    # JSON-RPC on 127.0.0.1:9090, for couchd's overlays and toasts (#18), and nothing more.
    "services.esenabled": ("true", _always("true")),
    "services.esallinterfaces": ("false", _always("false")),
}

# PM4K's settings (it keeps them in Kodi's addon settings, most of them undeclared).
PLEX_SETTINGS = {
    "auto_update_check": "false",
    "kiosk.mode": "false",
    # On exit PM4K runs its plugin endpoint so Kodi lists it as recently used, which opens
    # Kodi's Programs window just before Couch quits Kodi.
    "ensure_lastused": "false",
}


class ProfileError(Exception):
    pass


@dataclass
class Pin:
    id: str
    version: str
    url: str
    sha256: str


def load_pins(path: Path) -> list[Pin]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return [Pin(a["id"], a["version"], a["url"], a["sha256"].lower()) for a in data["addon"]]


def fetch_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "couch-install"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read()


def addon_version(addon_dir: Path) -> str | None:
    try:
        return ET.parse(addon_dir / "addon.xml").getroot().get("version")
    except (OSError, ET.ParseError):
        return None


@dataclass
class Profile:
    home: Path
    src: Path
    estuary: Path = ESTUARY
    dry_run: bool = False
    fetch: Callable[[str], bytes] = fetch_url
    changes: list[str] = field(default_factory=list)

    @property
    def addons(self) -> Path:
        return self.home / "addons"

    @property
    def userdata(self) -> Path:
        return self.home / "userdata"

    def change(self, what: str) -> bool:
        """Note a change; True if it should be made (not a dry run)."""
        self.changes.append(what)
        return not self.dry_run

    def apply(self) -> list[str]:
        pins = load_pins(self.src / "addons.toml")
        for pin in pins:
            self.install_pinned(pin)
        self.sync(self.addons / SERVICE, self.local_tree(self.src / SERVICE), SERVICE)
        self.sync(self.addons / SKIN, self.skin_tree(), SKIN)
        self.sync_file(self.src / "splash.png", self.home / "media" / "splash.png")
        self.enable_addons([p.id for p in pins] + [SERVICE, SKIN], disable=[OFFICIAL_REPO])
        self.merge_settings(self.userdata / "guisettings.xml", GUI_SETTINGS)
        self.merge_settings(
            self.userdata / "addon_data" / "script.plexmod" / "settings.xml",
            {k: (v, _always(v)) for k, v in PLEX_SETTINGS.items()},
        )
        self.disable_cec()
        return self.changes

    # --- addons ---------------------------------------------------------------------------

    def install_pinned(self, pin: Pin) -> None:
        dest = self.addons / pin.id
        if addon_version(dest) == pin.version:
            return
        if not self.change(f"installed {pin.id} {pin.version}"):
            return
        data = self.fetch(pin.url)
        digest = hashlib.sha256(data).hexdigest()
        if digest != pin.sha256:
            raise ProfileError(f"{pin.id} {pin.version}: sha256 is {digest}, expected {pin.sha256}")
        self.addons.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.addons, prefix=".new-") as tmp:
            _unzip_addon(data, pin.id, Path(tmp))
            got = addon_version(Path(tmp) / pin.id)
            if got != pin.version:
                raise ProfileError(f"{pin.id}: the zip holds version {got}, not {pin.version}")
            old = self.addons / f".old-{pin.id}"
            shutil.rmtree(old, ignore_errors=True)
            if dest.exists():
                dest.rename(old)
            (Path(tmp) / pin.id).rename(dest)
            shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def local_tree(src: Path) -> dict[str, bytes | str]:
        return {
            str(p.relative_to(src)): p.read_bytes()
            for p in sorted(src.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }

    def skin_tree(self) -> dict[str, bytes | str]:
        """Estuary, linked entry by entry, with Couch's files in place of a few of its own.

        Links rather than a copy: the skin keeps matching the Kodi it came with."""
        est = self.estuary
        if not (est / "addon.xml").is_file():
            raise ProfileError(f"no Estuary skin at {est}")
        ours = self.src / "skin"
        tree: dict[str, bytes | str] = {"addon.xml": _skin_addon_xml(est / "addon.xml")}
        for entry in sorted(est.iterdir()):
            if entry.name == "addon.xml":
                continue
            if entry.name in ("xml", "media") and entry.is_dir():
                for f in sorted(entry.iterdir()):
                    tree[f"{entry.name}/{f.name}"] = str(f)
            else:
                tree[entry.name] = str(entry)
        for f in sorted(ours.rglob("*")):
            if f.is_file():
                tree[str(f.relative_to(ours))] = f.read_bytes()
        return tree

    def sync(self, dest: Path, tree: dict[str, bytes | str], name: str) -> None:
        """Make `dest` hold exactly `tree`: bytes are files, strs are symlink targets."""
        stale = []
        if dest.is_dir():
            for root, dirs, files in os.walk(dest):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for n in files + [d for d in dirs if (Path(root) / d).is_symlink()]:
                    rel = str((Path(root) / n).relative_to(dest))
                    if rel not in tree:
                        stale.append(rel)
        todo = [rel for rel, want in tree.items() if not _matches(dest / rel, want)]
        if not (stale or todo):
            return
        if not self.change(f"updated {name} ({len(stale) + len(todo)} files)"):
            return
        for rel in stale:
            (dest / rel).unlink()
        for rel in todo:
            path, want = dest / rel, tree[rel]
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            if isinstance(want, str):
                path.symlink_to(want)
            else:
                path.write_bytes(want)
        for root, dirs, files in os.walk(dest, topdown=False):
            if root != str(dest) and not dirs and not files:
                os.rmdir(root)

    def sync_file(self, src: Path, dest: Path) -> None:
        want = src.read_bytes()
        if _matches(dest, want) or not self.change(f"wrote {dest}"):
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(want)

    def enable_addons(self, enable: list[str], disable: list[str]) -> None:
        db_dir = self.userdata / "Database"
        dbs = sorted(db_dir.glob("Addons*.db"), key=_db_version) if db_dir.is_dir() else []
        if not dbs:
            if not self.change("created Kodi's addons database"):
                return
            db_dir.mkdir(parents=True, exist_ok=True)
            path = db_dir / f"Addons{ADDONS_DB_VERSION}.db"
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.executescript(ADDONS_DB_SCHEMA)
                db.execute(
                    "INSERT INTO version (idVersion, iCompressCount) VALUES (?, 0)",
                    (ADDONS_DB_VERSION,),
                )
            dbs = [path]
        path = dbs[-1]
        # (enabled, disabledReason). On Linux, Kodi asks at every start whether to enable each
        # addon that's disabled with reason NONE (CApplication::ConfigureAndEnableAddons), so
        # what we disable, and anything Kodi left disabled that way, is disabled as USER.
        want = {i: (1, REASON_NONE) for i in enable} | {i: (0, REASON_USER) for i in disable}
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            have = {
                i: (int(e), int(r))
                for i, e, r in db.execute("SELECT addonID, enabled, disabledReason FROM installed")
            }
            for i, state in have.items():
                if i not in want and state == (0, REASON_NONE):
                    want[i] = (0, REASON_USER)
            todo = {i: v for i, v in want.items() if have.get(i) != v}
            if not todo:
                return
            on = [i for i, (e, _) in todo.items() if e]
            off = [i for i, (e, _) in todo.items() if not e]
            parts = [f"enabled {', '.join(on)}"] if on else []
            parts += [f"disabled {', '.join(off)}"] if off else []
            what = "; ".join(parts)
            if not self.change(f"addons database: {what}"):
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for i, (e, r) in todo.items():
                if i in have:
                    db.execute(
                        "UPDATE installed SET enabled=?, disabledReason=? WHERE addonID=?",
                        (e, r, i),
                    )
                else:
                    db.execute(
                        "INSERT INTO installed (addonID, enabled, installDate, disabledReason)"
                        " VALUES (?, ?, ?, ?)",
                        (i, e, now, r),
                    )

    # --- settings -------------------------------------------------------------------------

    def merge_settings(
        self, path: Path, want: dict[str, tuple[str, Callable[[str | None], bool]]]
    ) -> None:
        """Kodi's settings file format 2: <setting id="...">value</setting>."""
        root = _load_xml(path, "settings", {"version": "2"})
        by_id = {s.get("id"): s for s in root.iter("setting")}
        todo = [k for k, (_, ok) in want.items() if k not in by_id or not ok(by_id[k].text or "")]
        if not todo or not self.change(f"set {', '.join(todo)} in {path}"):
            return
        for key in todo:
            el = by_id.get(key)
            if el is None:
                el = ET.SubElement(root, "setting", {"id": key})
            el.attrib.pop("default", None)
            el.text = want[key][0] or None
        _write_xml(path, root)

    def disable_cec(self) -> None:
        """Kodi's CEC adapter settings (format: <setting id="..." value="..."/>)."""
        path = self.userdata / "peripheral_data" / "cec_CEC_Adapter.xml"
        root = _load_xml(path, "settings", {})
        el = next((s for s in root.iter("setting") if s.get("id") == "enabled"), None)
        if el is not None and el.get("value") == "0":
            return
        if not self.change(f"turned off Kodi's CEC in {path}"):
            return
        if el is None:
            el = ET.SubElement(root, "setting", {"id": "enabled"})
        el.set("value", "0")
        _write_xml(path, root)


def _matches(path: Path, want: bytes | str) -> bool:
    if isinstance(want, str):
        return path.is_symlink() and os.readlink(path) == want
    return path.is_file() and not path.is_symlink() and path.read_bytes() == want


def _db_version(path: Path) -> int:
    digits = path.stem.removeprefix("Addons")
    return int(digits) if digits.isdigit() else -1


def _unzip_addon(data: bytes, addon_id: str, dest: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or not parts or parts[0] != addon_id:
                raise ProfileError(f"{addon_id}: unexpected path in zip: {name!r}")
        z.extractall(dest)


def _skin_addon_xml(estuary_addon: Path) -> bytes:
    root = ET.parse(estuary_addon).getroot()
    version = root.get("version", "0")
    root.set("id", SKIN)
    root.set("name", "Couch")
    root.set("version", f"{version}+couch.1")
    root.set("provider-name", f"Couch, from Estuary by {root.get('provider-name', 'Team Kodi')}")
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n"


def _load_xml(path: Path, tag: str, attrib: dict[str, str]) -> ET.Element:
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError:
        return ET.Element(tag, attrib)
    except ET.ParseError as e:
        raise ProfileError(f"{path}: {e}") from None
    if root.tag != tag:
        raise ProfileError(f"{path}: expected <{tag}>, found <{root.tag}>")
    return root


def _write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, "    ")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(ET.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m couchd.kodi_profile", description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="Kodi's data dir (KODI_DATA)")
    parser.add_argument("--src", type=Path, required=True, help="Couch's system/kodi files")
    parser.add_argument("--estuary", type=Path, default=ESTUARY, help="the system's Estuary skin")
    parser.add_argument("--dry-run", action="store_true", help="only print what would change")
    args = parser.parse_args(argv)
    profile = Profile(args.home, args.src, args.estuary, dry_run=args.dry_run, fetch=fetch_url)
    try:
        for line in profile.apply():
            print(line)
    except (ProfileError, OSError, sqlite3.Error) as e:
        print(f"error: Kodi profile: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

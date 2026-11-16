"""couch-update (couchd/updater.py), end to end against a fake release source.

No GitHub release or tag is ever made: releases here are tarballs built in tmp_path (and one
with `git archive` of this checkout), served by a local HTTP server as GitHub-shaped JSON,
or read from file:// URLs. The machine (systemd, couchd's API, install.sh's effects) is a
FakeSystem; the Pi test runs the real thing.
"""

import datetime as dt
import functools
import http.server
import io
import json
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from couchd import updater
from couchd.updater import (
    Config,
    Release,
    UpdateError,
    Updater,
    busy_reason,
    in_window,
    is_newer,
    parse_version,
    pick_release,
    read_only_root,
    release_from_json,
)

ROOT = Path(__file__).resolve().parent.parent
IDLE = {
    "apps": {"foreground": "launcher", "running": [], "list": [{"id": "plex", "kind": "kodi"}]},
    "tv": {"power": "standby"},
    "idle": {"idle": True},
    "settings": {"updates.overnight": True},
}

PLAYING_YOUTUBE = {
    **IDLE,
    "apps": {**IDLE["apps"], "foreground": "youtube"},
    "tv": {"power": "on"},
}


# --- pure bits --------------------------------------------------------------------------


def test_parse_version():
    assert parse_version("v1.2.3") == (1, 2, 3, 0)
    assert parse_version("0.4") == (0, 4, 0, 0)
    assert parse_version("v0.2.0-5-gabc1234") == (0, 2, 0, 5)
    assert parse_version("v0.2.0-5-gabc1234-dirty") == (0, 2, 0, 5)
    assert parse_version("ac760f4") is None
    assert parse_version(None) is None
    assert parse_version("v1.2.3-rc1") is None


def test_is_newer():
    assert is_newer("v0.2.0", "v0.1.9")
    assert not is_newer("v0.2.0", "v0.2.0")
    assert not is_newer("v0.2.0", "v0.2.0-3-gabc1234")  # a checkout ahead of the tag
    assert is_newer("v0.2.1", "v0.2.0-3-gabc1234")
    assert is_newer("v0.1.0", None)  # not from a release: any release will do
    assert is_newer("v0.1.0", "ac760f4")
    assert not is_newer("nightly", "v0.1.0")


def test_busy_reason():
    assert busy_reason(IDLE, nightly=True) is None
    playing = {**IDLE, "apps": {**IDLE["apps"], "foreground": "youtube"}, "tv": {"power": "on"}}
    assert busy_reason(playing, nightly=False) == "something is playing"
    assert busy_reason(playing, nightly=True) == "something is playing"
    # TV off: couchd paused it, so YouTube in front at night is fine
    paused = {**playing, "tv": {"power": "standby"}}
    assert busy_reason(paused, nightly=True) is None
    # Plex (Kodi) open: never, even with the TV off
    kodi = {**IDLE, "apps": {**IDLE["apps"], "foreground": "plex", "running": ["plex"]}}
    assert busy_reason(kodi, nightly=False) == "Plex is open"
    # unknown TV power counts as on
    unknown = {**playing, "tv": {"power": "unknown"}}
    assert busy_reason(unknown, nightly=True) == "something is playing"
    # TV on, launcher in front: Check now may, the nightly run waits for idle
    in_use = {**IDLE, "tv": {"power": "on"}, "idle": {"idle": False}}
    assert busy_reason(in_use, nightly=False) is None
    assert busy_reason(in_use, nightly=True) == "Couch is in use"
    assert busy_reason({**in_use, "idle": {"idle": True}}, nightly=True) is None
    off = {**IDLE, "settings": {"updates.overnight": False}}
    assert busy_reason(off, nightly=True) == "overnight updates are off"
    assert busy_reason(off, nightly=False) is None


def test_in_window():
    at = lambda h, m=0: dt.datetime(2026, 11, 16, h, m)  # noqa: E731
    assert in_window(at(3), "01:00", "05:45")
    assert not in_window(at(5, 45), "01:00", "05:45")
    assert not in_window(at(14), "01:00", "05:45")
    assert in_window(at(23, 30), "23:00", "04:00")
    assert in_window(at(1), "23:00", "04:00")
    assert not in_window(at(12), "23:00", "04:00")


def test_pick_release_skips_drafts_and_prereleases():
    releases = [
        {"tag_name": "v0.3.0", "draft": True},
        {"tag_name": "v0.2.5", "prerelease": True},
        {"tag_name": "v0.2.0"},
        {"tag_name": "v0.10.0"},
        {"tag_name": "weird"},
    ]
    assert pick_release(releases)["tag_name"] == "v0.10.0"
    assert pick_release({"tag_name": "v1.0.0"})["tag_name"] == "v1.0.0"
    assert pick_release({"message": "Not Found"}) is None


def test_release_prefers_an_uploaded_tarball():
    data = {
        "tag_name": "v0.2.0",
        "tarball_url": "https://api.github.com/repos/o/r/tarball/v0.2.0",
        "assets": [
            {"name": "notes.txt", "url": "https://api.github.com/a/1"},
            {"name": "couch-v0.2.0.tar.gz", "url": "https://api.github.com/a/2"},
        ],
    }
    assert release_from_json(data) == Release("v0.2.0", "https://api.github.com/a/2")
    data["assets"] = []
    release = release_from_json(data)
    assert release.url.endswith("/tarball/v0.2.0")
    assert release.accept == "application/vnd.github+json"


def test_read_only_root(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n")
    assert not read_only_root(mounts)
    mounts.write_text("overlayroot / overlay rw,relatime 0 0\n")
    assert read_only_root(mounts)
    mounts.write_text("/dev/mmcblk0p2 / ext4 ro,noatime 0 0\n")
    assert read_only_root(mounts)


def test_token_must_be_root_only(tmp_path):
    token = tmp_path / "token"
    token.write_text("ghp_test\n")
    token.chmod(0o644)
    cfg = Config(token_file=str(token))
    with pytest.raises(UpdateError, match="chmod 600"):
        updater.read_token(cfg)
    token.chmod(0o600)
    assert updater.read_token(cfg) == "ghp_test"
    assert updater.read_token(Config(token_file=str(tmp_path / "none"))) is None


def test_token_only_goes_to_github(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("ghp_test")
    token.chmod(0o600)
    cfg = Config(token_file=str(token))
    sent = []
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout: sent.append(req))
    updater._open("https://api.github.com/repos/o/r/releases/latest", cfg, accept="x")
    updater._open("http://127.0.0.1:9/latest.json", cfg, accept="x")
    updater._open("https://example.com/latest.json", cfg, accept="x")
    auth = [r.unredirected_hdrs.get("Authorization") for r in sent]
    assert auth == ["Bearer ghp_test", None, None]
    assert all("Authorization" not in r.headers for r in sent)  # not copied to redirects


def test_load_config(tmp_path):
    path = tmp_path / "update.toml"
    assert updater.load_config(path) == Config()
    path.write_text('source = "file:///tmp/x.json"\nsettle = 5\nbogus = 1\n')
    cfg = updater.load_config(path)
    assert cfg.source == "file:///tmp/x.json" and cfg.settle == 5
    path.write_text('settle = "5"\n')
    with pytest.raises(UpdateError, match="settle must be int"):
        updater.load_config(path)


def test_example_config_is_the_defaults():
    text = (ROOT / "system" / "update.example.toml").read_text()
    lines = [line.removeprefix("# ") for line in text.splitlines() if " = " in line]
    import tomllib

    assert updater.replace(Config(), **tomllib.loads("\n".join(lines))) == Config()


# --- fake releases ------------------------------------------------------------------------


def make_release(tmp: Path, tag: str, *, broken: bool = False, top: str | None = None) -> Path:
    """A tarball shaped like GitHub's: one top directory, Couch's files. Its install.sh is a
    stand-in that records what a real one would leave behind, for FakeSystem."""
    tree = tmp / f"build-{tag}" / (top or f"alecschoen-couch-{tag}")
    (tree / "couchd").mkdir(parents=True, exist_ok=True)
    (tree / "pyproject.toml").write_text("[project]\nname='couchd'\n")
    (tree / "install.sh").write_text(f"# Couch {tag}\n{'BROKEN' if broken else 'OK'}\n")
    out = tmp / f"{tag}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(tree, arcname=tree.name)
    return out


class FakeSystem(updater.System):
    """Runs nothing. `install.sh` 'installs' by copying the release tree into prefix/source
    and writing prefix/VERSION, the way the real one does; a tree whose install.sh says
    BROKEN leaves couchd crash-looping, so the health check fails."""

    def __init__(self, cfg: Config, *, snapshot=IDLE, install_exit: int = 0):
        super().__init__(cfg)
        self.prefix = Path(cfg.prefix)
        self.state = snapshot
        self.install_exit = install_exit
        self.t = 1_000_000.0
        self.ran: list[list[str]] = []
        self.broken = False
        self.restarts = 0
        self.reboots = 0
        self.quiet_seen: list[bool] = []

    def snapshot(self):
        return self.state

    def health(self):
        if self.broken:
            return None
        return {"status": "ok", "services": {"tv": "running", "bluetooth": "running"}}

    def unit(self, unit):
        if self.broken and unit == "couchd.service":
            self.restarts += 1
            return {"ActiveState": "activating", "MainPID": "0", "NRestarts": str(self.restarts)}
        return {"ActiveState": "active", "MainPID": "42", "NRestarts": "0"}

    def run(self, argv, *, timeout, log):
        self.ran.append(argv)
        if argv[0] == "bash":
            script = Path(argv[1])
            src = script.parent
            self.quiet_seen.append((Path(self.cfg.work) / "quiet-until").exists())
            dest = self.prefix / "source"
            if src != dest:
                shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(src, dest)
            version = (src / "VERSION").read_text() if (src / "VERSION").exists() else ""
            (self.prefix / "VERSION").write_text(version)
            self.broken = "BROKEN" in script.read_text()
            return self.install_exit if self.broken else 0
        return 0

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t

    def reboot(self):
        self.reboots += 1


class ReleaseServer:
    """A local GitHub: /repos/o/r/releases/latest and the tarballs, from a directory."""

    def __init__(self, root: Path):
        self.root = root
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        handler.log_message = lambda *a: None
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def publish(self, tag: str, tarball: Path) -> None:
        shutil.copy(tarball, self.root / tarball.name)
        latest = {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "tarball_url": f"{self.url}/{tarball.name}",
            "assets": [],
        }
        (self.root / "latest.json").write_text(json.dumps(latest))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(tmp_path):
    (tmp_path / "www").mkdir()
    s = ReleaseServer(tmp_path / "www")
    yield s
    s.close()


@pytest.fixture
def box(tmp_path, server):
    """An installed Couch v0.1.0 in tmp_path/opt, with a release source on localhost."""
    cfg = Config(
        source=f"{server.url}/latest.json",
        token_file=str(tmp_path / "no-token"),
        prefix=str(tmp_path / "opt"),
        work=str(tmp_path / "work"),
        window_start="00:00",
        window_end="23:59",
        health_timeout=60,
        settle=10,
    )
    first = make_release(tmp_path, "v0.1.0")
    with tarfile.open(first) as tar:
        tar.extractall(tmp_path / "first", filter="data")
    source = tmp_path / "opt" / "source"
    shutil.copytree(tmp_path / "first" / "alecschoen-couch-v0.1.0", source)
    (source / "VERSION").write_text("v0.1.0\n")
    (tmp_path / "opt" / "VERSION").write_text("v0.1.0\n")
    system = FakeSystem(cfg)
    return Updater(cfg, system), system


def version(u: Updater) -> str | None:
    return u.installed_version()


def test_installs_a_newer_release(tmp_path, server, box):
    u, system = box
    server.publish("v0.2.0", make_release(tmp_path, "v0.2.0"))
    assert u.run(nightly=True) == "installed"
    assert version(u) == "v0.2.0"
    status = json.loads((tmp_path / "work" / "status.json").read_text())
    assert status["result"] == "installed"
    assert status["current"] == "v0.2.0" and status["previous"] == "v0.1.0"
    assert status["event"]["kind"] == "installed"
    assert status["state"] == "idle"
    assert system.quiet_seen == [True]  # couchd was told not to wake the TV
    assert not u.quiet_file.exists()  # and that's over
    # the system updates ran after it, at night
    assert ["unattended-upgrade"] in system.ran
    assert any("full-upgrade" in argv for argv in system.ran)
    # once a night
    assert u.run(nightly=True) == "skipped"


def test_up_to_date(tmp_path, server, box):
    u, system = box
    server.publish("v0.1.0", make_release(tmp_path, "v0.1.0"))
    assert u.run(nightly=False) == "up-to-date"
    assert not any(argv[0] == "bash" for argv in system.ran)
    assert ["unattended-upgrade"] not in system.ran  # Check now is Couch only


def test_a_broken_release_rolls_back(tmp_path, server, box):
    u, system = box
    server.publish("v0.2.0", make_release(tmp_path, "v0.2.0", broken=True))
    assert u.run(nightly=False) == "rolled-back"
    assert version(u) == "v0.1.0"
    assert "OK" in (tmp_path / "opt" / "source" / "install.sh").read_text()
    status = json.loads((tmp_path / "work" / "status.json").read_text())
    assert status["result"] == "rolled-back"
    assert status["bad"] == ["v0.2.0"]
    assert status["event"] == {
        "kind": "rolled-back",
        "version": "v0.2.0",
        "kept": "v0.1.0",
        "at": status["event"]["at"],
    }
    assert "stayed on v0.1.0" in status["message"]
    reset = ["systemctl", "reset-failed", "couchd.service", "couch-session.service"]
    assert system.ran.index(reset) < len(system.ran) - 1  # before the old install.sh
    assert system.quiet_seen == [False, False]  # Check now: the TV is on anyway
    # it isn't tried again, but a newer one is
    assert u.run(nightly=False) == "up-to-date"
    server.publish("v0.2.1", make_release(tmp_path, "v0.2.1"))
    assert u.run(nightly=False) == "installed"
    assert version(u) == "v0.2.1"


def test_install_sh_failing_rolls_back(tmp_path, server, box):
    u, system = box
    server.publish("v0.2.0", make_release(tmp_path, "v0.2.0", broken=True))
    system.install_exit = 1
    assert u.run(nightly=False) == "rolled-back"
    assert version(u) == "v0.1.0"
    assert "install.sh exited 1" in u.status.data["message"]


def test_never_while_playing(tmp_path, server, box):
    u, system = box
    server.publish("v0.2.0", make_release(tmp_path, "v0.2.0"))
    system.state = PLAYING_YOUTUBE
    assert u.run(nightly=True) == "waiting"
    assert u.run(nightly=False) == "waiting"
    assert system.ran == []
    assert u.status.data["waiting"] == "something is playing"
    assert version(u) == "v0.1.0"
    # couchd not answering counts as busy too
    system.state = None
    assert u.run(nightly=True) == "waiting"
    assert u.status.data["waiting"] == "couchd isn't answering"
    # the next half hour, it's quiet
    system.state = IDLE
    assert u.run(nightly=True) == "installed"


def test_playback_starting_during_the_download_stops_it(tmp_path, server, box, monkeypatch):
    u, system = box
    server.publish("v0.2.0", make_release(tmp_path, "v0.2.0"))
    real = updater.download

    def download_then_play(*a, **kw):
        out = real(*a, **kw)
        system.state = PLAYING_YOUTUBE
        return out

    monkeypatch.setattr(updater, "download", download_then_play)
    assert u.run(nightly=False) == "waiting"
    assert not any(argv[0] == "bash" for argv in system.ran)


def test_system_updates_wait_for_quiet_too(tmp_path, server, box):
    u, system = box
    server.publish("v0.1.0", make_release(tmp_path, "v0.1.0"))
    calls = iter([IDLE])  # the run's own check; by the system updates someone is watching

    def snapshot():
        return next(calls, {**IDLE, "tv": {"power": "on"}, "idle": {"idle": False}})

    system.snapshot = snapshot
    u.run(nightly=True)
    assert ["unattended-upgrade"] not in system.ran


def test_full_upgrade_weekly(tmp_path, server, box):
    u, system = box
    server.publish("v0.1.0", make_release(tmp_path, "v0.1.0"))
    u.run(nightly=True)
    fulls = lambda: sum("full-upgrade" in argv for argv in system.ran)  # noqa: E731
    assert fulls() == 1
    for _ in range(6):
        system.t += 86400
        u.status.data["night"] = None
        u.run(nightly=True)
    assert fulls() == 1
    assert sum(argv == ["unattended-upgrade"] for argv in system.ran) == 7
    system.t += 86400
    u.status.data["night"] = None
    u.run(nightly=True)
    assert fulls() == 2


def test_outside_the_window(tmp_path, server, box):
    u, system = box
    u.cfg = updater.replace(u.cfg, window_start="00:00", window_end="00:00")
    assert u.run(nightly=True) == "skipped"
    assert system.ran == []


def test_no_access(tmp_path, server, box):
    u, _ = box  # nothing published: 404, like a private repo without a token
    assert u.run(nightly=False) == "failed"
    assert "no-token" in u.status.data["message"]


def test_file_source(tmp_path, box):
    u, _ = box
    tarball = make_release(tmp_path, "v0.3.0")
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"tag_name": "v0.3.0", "tarball_url": tarball.as_uri()}))
    u.cfg = updater.replace(u.cfg, source=latest.as_uri())
    assert u.run(nightly=False) == "installed"
    assert version(u) == "v0.3.0"


def test_not_couch(tmp_path, server, box):
    u, _ = box
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("x/README")
        tar.addfile(info, io.BytesIO(b""))
    server.publish("v0.2.0", bad)
    assert u.run(nightly=False) == "failed"
    assert "doesn't look like Couch" in u.status.data["message"]
    assert version(u) == "v0.1.0"


def test_needs_a_copy_to_roll_back_to(tmp_path, server, box):
    u, system = box
    shutil.rmtree(tmp_path / "opt" / "source")
    server.publish("v0.2.0", make_release(tmp_path, "v0.2.0"))
    assert u.run(nightly=False) == "failed"
    assert system.ran == []


def test_one_at_a_time(tmp_path, box):
    u, _ = box
    import fcntl

    (tmp_path / "work").mkdir(exist_ok=True)
    with (tmp_path / "work" / "lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        assert u.run(nightly=False) == "busy"


def test_reboot_when_needed(tmp_path, server, box, monkeypatch):
    u, system = box
    server.publish("v0.1.0", make_release(tmp_path, "v0.1.0"))
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: True if str(p) == "/run/reboot-required" else real_exists(p)
    )
    u.run(nightly=True)
    assert system.reboots == 1
    assert u.quiet_file.exists()  # kept for couchd's start after the reboot


def test_a_git_archive_of_this_checkout_is_a_release(tmp_path):
    """What a real release tarball looks like, without making a tag: `git archive HEAD`."""
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("needs a git checkout")
    out = tmp_path / "couch.tar.gz"
    subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "archive",
            "--format=tar.gz",
            "--prefix=alecschoen-couch-0abc/",
            "-o",
            str(out),
            "HEAD",
        ],
        check=True,
    )
    with tarfile.open(out) as tar:
        tar.extractall(tmp_path / "x", filter="data")
    root = updater.source_root(tmp_path / "x", "v9.9.9")
    assert (root / "VERSION").read_text() == "v9.9.9\n"
    for name in ("install.sh", "couchd/updater.py", "system/couch-update.service"):
        assert (root / name).exists(), name

"""couch-update: installs new Couch releases and system updates, overnight (#24). Runs as root.

Not part of couchd: couch-update.service runs this file with the system Python, as root, so
it uses the standard library only and never imports couchd. install.sh replaces the files
under /opt/couch while this runs; Python has already read this one.

    couch-update nightly   # the timer, every half hour at night: once a night, when idle
    couch-update now       # Settings > Updates > Check now: right away, unless something plays
    couch-update status    # print the status file

One run:
The night runs only happen with `nightly = true` in /etc/couch/update.toml (off by default).

1. Asks couchd (GET /api/state) whether it may: nothing playing, and for the nightly run the
   `updates.overnight` setting on and nobody using Couch. couchd not answering = no.
2. Reads the latest release from the release source (GitHub's API by default, or any URL
   or file:// path that serves the same JSON: see `fetch_release`), and, if it is newer than
   /opt/couch/VERSION, downloads its tarball, unpacks it into /var/lib/couch-update/releases
   and runs its install.sh.
3. Health check: couchd and couch-session active, /api/health answering with no service
   failed that wasn't failed before, and couchd not restarting, for `settle` seconds, all
   within `health_timeout`. If install.sh fails or the check does, the install.sh of the
   previous install (the copy install.sh keeps in /opt/couch/source) runs again, and that
   release is skipped from then on.
4. Nightly only: security updates (unattended-upgrade), a full upgrade once a week, and a
   reboot if one is needed. Each asks couchd again first.

During a nightly run couchd is told not to wake the TV when it restarts (QUIET_FILE).
The status file (/var/lib/couch-update/status.json) is how couchd and Settings see all this.

Config: /etc/couch/update.toml, every key optional (see DEFAULTS and
system/update.example.toml). The release source is a key there, so a fake one plugs in.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

GITHUB_LATEST = "https://api.github.com/repos/alecschoen/couch/releases/latest"
# Hosts the token is sent to. Never to a redirect target (see _open).
TOKEN_HOSTS = {"api.github.com", "github.com", "codeload.github.com"}


@dataclass(frozen=True)
class Config:
    source: str = GITHUB_LATEST
    token_file: str = "/etc/couch/github-token"
    api: str = "http://127.0.0.1:8765"
    prefix: str = "/opt/couch"
    work: str = "/var/lib/couch-update"
    settings_file: str = "/var/lib/couch/settings.json"
    # The night runs (timer, security updates, reboots) are opt-in: off until someone sets
    # `nightly = true` and re-runs install.sh, which then enables the timer and installs
    # unattended-upgrades. Check now works either way.
    nightly: bool = False
    # The nightly run only does anything between these local times (24 h, "HH:MM").
    window_start: str = "01:00"
    window_end: str = "05:45"
    health_timeout: int = 180
    settle: int = 20
    install_timeout: int = 1800
    system_updates: bool = True
    full_upgrade_days: int = 7
    reboot: bool = True


DEFAULTS = Config()
CONFIG_FILE = Path("/etc/couch/update.toml")
UNITS = ("couchd.service", "couch-session.service")
KEEP_RELEASES = 2


class UpdateError(RuntimeError):
    """Something an update can't get past; the message is for people (Settings shows it)."""


class NoAccess(UpdateError):
    """The Pi can't read the releases: no token for a private fork, or a token GitHub
    refuses. Settings says "Not set up"; see the release-access options in system/README.md."""


def load_config(path: Path = CONFIG_FILE) -> Config:
    if not path.exists():
        return DEFAULTS
    with path.open("rb") as f:
        raw = tomllib.load(f)
    known = {f.name: f.type for f in fields(Config)}
    values = {}
    for key, value in raw.items():
        if key not in known:
            print(f"warning: {path}: unknown key {key!r} ignored", file=sys.stderr)
            continue
        expected = type(getattr(DEFAULTS, key))
        if type(value) is not expected:
            raise UpdateError(f"{path}: {key} must be {expected.__name__}")
        values[key] = value
    return replace(DEFAULTS, **values)


# --- versions -----------------------------------------------------------------------------

_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-(\d+)-g[0-9a-f]+)?(?:-dirty)?$")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """'v1.2.3' → (1, 2, 3, 0); git describe's 'v1.2.3-4-gabc' → (1, 2, 3, 4). Else None."""
    m = _VERSION.match((text or "").strip())
    if not m:
        return None
    major, minor, patch, ahead = m.groups()
    return (int(major), int(minor), int(patch or 0), int(ahead or 0))


def is_newer(candidate: str, current: str | None) -> bool:
    """Whether release `candidate` should replace `current`. An install that isn't from a
    release (no VERSION, or a bare commit) takes any release."""
    new = parse_version(candidate)
    if new is None:
        return False
    old = parse_version(current)
    return old is None or new > old


# --- may we? --------------------------------------------------------------------------------


def busy_reason(snapshot: dict[str, Any], *, nightly: bool) -> str | None:
    """Why not to update now, from couchd's /api/state; None when it may.

    Playing means an app (YouTube, Plex) is in front while the TV is on, or music playing to
    Couch from a phone (Spotify, AirPlay), TV on or not. With the TV in standby couchd has
    paused the app. The nightly run also waits for nobody to be using Couch: the TV off, or
    the launcher gone idle."""
    apps = snapshot.get("apps") or {}
    tv = snapshot.get("tv") or {}
    idle = snapshot.get("idle") or {}
    settings = snapshot.get("settings") or {}
    foreground = apps.get("foreground") or "launcher"
    tv_off = tv.get("power") == "standby"
    kodi = {a.get("id") for a in apps.get("list") or [] if a.get("kind") == "kodi"}
    if kodi & set(apps.get("running") or []):
        # Kodi keeps the screen until it exits, and install.sh may stop it.
        return "Plex is open"
    if foreground != "launcher" and not tv_off:
        return "something is playing"
    if (snapshot.get("speaker") or {}).get("playing"):
        return "music is playing"
    if nightly:
        if settings.get("updates.overnight") is False:
            return "overnight updates are off"
        if not tv_off and not idle.get("idle"):
            return "Couch is in use"
    return None


def in_window(now: dt.datetime, start: str, end: str) -> bool:
    def minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    t, a, b = now.hour * 60 + now.minute, minutes(start), minutes(end)
    return a <= t < b if a <= b else t >= a or t < b


# --- releases -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    tag: str
    url: str  # the tarball
    accept: str = "application/octet-stream"


def _open(url: str, cfg: Config, *, accept: str, timeout: float = 30.0):
    # GitHub redirects tarballs and assets to codeload or a signed storage URL. urllib copies
    # ordinary headers to a redirect, but not unredirected ones, so the token stays here.
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "couch-update"})
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname in TOKEN_HOSTS:
        token = read_token(cfg)
        if token:
            req.add_unredirected_header("Authorization", f"Bearer {token}")
            req.add_unredirected_header("X-GitHub-Api-Version", "2022-11-28")
    return urllib.request.urlopen(req, timeout=timeout)


def read_token(cfg: Config) -> str | None:
    path = Path(cfg.token_file)
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if st.st_mode & 0o077:
        raise UpdateError(f"{path} must be readable by root only (chmod 600)")
    return path.read_text().strip() or None


def pick_release(data: Any) -> dict[str, Any] | None:
    """The release to consider from GitHub-shaped JSON: one release object (releases/latest)
    or a list (releases), where the highest version that isn't a draft or prerelease wins."""
    candidates = data if isinstance(data, list) else [data]
    good = [
        r
        for r in candidates
        if isinstance(r, dict)
        and not r.get("draft")
        and not r.get("prerelease")
        and parse_version(r.get("tag_name"))
    ]
    return max(good, key=lambda r: parse_version(r["tag_name"]), default=None)


def release_from_json(data: Any) -> Release | None:
    """A Release from GitHub's release JSON. An uploaded asset named couch-*.tar.gz is used
    if there is one (through its API url, which works for private repos); otherwise the
    source tarball GitHub makes for the tag."""
    r = pick_release(data)
    if r is None:
        return None
    for asset in r.get("assets") or []:
        name = asset.get("name", "")
        if name.startswith("couch-") and name.endswith(".tar.gz") and asset.get("url"):
            return Release(r["tag_name"], asset["url"])
    if r.get("tarball_url"):
        return Release(r["tag_name"], r["tarball_url"], accept="application/vnd.github+json")
    return None


def fetch_release(cfg: Config) -> Release | None:
    """The latest release at `cfg.source`: an https/http URL or a file:// path."""
    try:
        with _open(cfg.source, cfg, accept="application/vnd.github+json") as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # GitHub says 404 both for "no releases yet" and "you can't see this repo".
            if not _can_see_repo(cfg):
                if read_token(cfg):
                    raise NoAccess(f"The token in {cfg.token_file} can't see the repo") from None
                raise NoAccess(
                    f"Couch's releases need a GitHub token in {cfg.token_file}"
                ) from None
            return None  # the repo is there, with no release yet
        if e.code in (401, 403):
            raise NoAccess(f"GitHub didn't accept the token in {cfg.token_file}") from None
        raise UpdateError(f"The release source answered {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"can't reach the release source: {e}") from None
    return release_from_json(data)


def _can_see_repo(cfg: Config) -> bool:
    """For a GitHub releases URL: whether the repo itself answers. Other sources: True."""
    m = re.match(r"^(https://api\.github\.com/repos/[^/]+/[^/]+)/releases", cfg.source)
    if not m:
        return True
    try:
        with _open(m.group(1), cfg, accept="application/vnd.github+json"):
            return True
    except (urllib.error.URLError, OSError):
        return False


def download(release: Release, cfg: Config, dest: Path) -> Path:
    """Download and unpack the release into dest/<tag>; returns the source tree in it."""
    tag = release.tag
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
        raise UpdateError(f"odd tag name {tag!r}")
    target = dest / tag
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    tarball = target / "release.tar.gz"
    try:
        with (
            _open(release.url, cfg, accept=release.accept, timeout=120) as resp,
            tarball.open("wb") as f,
        ):
            shutil.copyfileobj(resp, f)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"download of {tag} failed: {e}") from None
    unpacked = target / "src"
    try:
        with tarfile.open(tarball) as tar:
            tar.extractall(unpacked, filter="data")
    except (tarfile.TarError, OSError) as e:
        raise UpdateError(f"{tag}'s tarball is broken: {e}") from None
    tarball.unlink()
    return source_root(unpacked, tag)


def source_root(unpacked: Path, tag: str) -> Path:
    """GitHub's tarballs (and `git archive --prefix`) have one top directory."""
    tops = [p for p in unpacked.iterdir() if p.name != "pax_global_header"]
    root = tops[0] if len(tops) == 1 and tops[0].is_dir() else unpacked
    for needed in ("install.sh", "pyproject.toml", "couchd"):
        if not (root / needed).exists():
            raise UpdateError(f"{tag} doesn't look like Couch (no {needed})")
    (root / "VERSION").write_text(tag + "\n")
    return root


# --- the box: systemd, couchd -------------------------------------------------------------


class System:
    """Everything that touches the machine, so tests can stand in for it."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def snapshot(self) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(self.cfg.api + "/api/state", timeout=5) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def health(self) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(self.cfg.api + "/api/health", timeout=5) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def unit(self, unit: str) -> dict[str, str]:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState,MainPID,NRestarts"],
            capture_output=True,
            text=True,
        ).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def run(self, argv: list[str], *, timeout: float, log: Path) -> int:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", COUCH_UPDATER="1")
        with log.open("a") as out:
            out.write(f"\n$ {' '.join(argv)}\n")
            out.flush()
            try:
                return subprocess.run(
                    argv, stdout=out, stderr=subprocess.STDOUT, env=env, timeout=timeout
                ).returncode
            except subprocess.TimeoutExpired:
                out.write("timed out\n")
                return 124
            except OSError as e:
                out.write(f"{e}\n")
                return 127

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()

    def reboot(self) -> None:
        subprocess.run(["systemctl", "reboot"], check=False)


# --- status -------------------------------------------------------------------------------


class Status:
    """status.json: what couchd shows. Written atomically, world-readable."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def set(self, **values: Any) -> None:
        self.data.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".status.")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)


# --- the updater ----------------------------------------------------------------------------


class Updater:
    def __init__(self, cfg: Config, system: System | None = None) -> None:
        self.cfg = cfg
        self.sys = system or System(cfg)
        self.prefix = Path(cfg.prefix)
        self.work = Path(cfg.work)
        self.status = Status(self.work / "status.json")
        self.log = self.work / "last-run.log"
        self._keep_quiet = False

    # Paths
    @property
    def quiet_file(self) -> Path:
        return self.work / "quiet-until"

    def installed_version(self) -> str | None:
        try:
            return (self.prefix / "VERSION").read_text().strip() or None
        except OSError:
            return None

    def say(self, message: str) -> None:
        print(message, flush=True)
        with self.log.open("a") as f:
            f.write(message + "\n")

    # The run
    def run(self, *, nightly: bool, force: bool = False) -> str:
        """One run; returns the result word that also goes into the status file."""
        self.work.mkdir(parents=True, exist_ok=True)
        with self._lock() as locked:
            if not locked:
                self.say("another update is running")
                return "busy"
            self.log.write_text("")
            try:
                return self._run(nightly=nightly, force=force)
            finally:
                if not self._keep_quiet:
                    self.quiet_file.unlink(missing_ok=True)
                self.status.set(state="idle")

    def _run(self, *, nightly: bool, force: bool) -> str:
        now = self.sys.now()
        if nightly:
            if not self.cfg.nightly:
                self.say("nightly updates aren't turned on (nightly = true in update.toml)")
                return "skipped"
            local = dt.datetime.fromtimestamp(now)
            if not in_window(local, self.cfg.window_start, self.cfg.window_end):
                self.say("outside the night window")
                return "skipped"
            if self.status.data.get("night") == local.date().isoformat():
                self.say("already done tonight")
                return "skipped"
        if read_only_root():
            self.say("the root filesystem is read-only; nothing to do")
            self.status.set(last_check=now, result="failed", message="The SD card is read-only")
            return "failed"
        if reason := self._busy(nightly):
            self.say(f"not now: {reason}")
            if nightly:
                self.status.set(waiting=reason)
            else:  # Check now: say so in Settings, instead of the last run's result
                self.status.set(waiting=reason, result="waiting", message=None, last_check=now)
            return "waiting"
        self.status.set(waiting=None, state="checking", last_check=now)
        result = self.update_couch(nightly=nightly, force=force)
        if nightly:
            if self.cfg.system_updates and result != "failed":
                self.update_system()
            self.status.set(night=dt.datetime.fromtimestamp(now).date().isoformat())
            if self.cfg.reboot and Path("/run/reboot-required").exists():
                self.reboot_if_idle()
        return result

    def _busy(self, nightly: bool) -> str | None:
        snapshot = self.sys.snapshot()
        if snapshot is None:
            return "couchd isn't answering"
        return busy_reason(snapshot, nightly=nightly)

    @contextlib.contextmanager
    def _lock(self):
        with (self.work / "lock").open("w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True

    # Couch
    def update_couch(self, *, nightly: bool, force: bool = False) -> str:
        current = self.installed_version()
        try:
            release = fetch_release(self.cfg)
        except NoAccess as e:
            self.say(str(e))
            self.status.set(result="no-access", message=str(e), latest=None)
            return "no-access"
        except UpdateError as e:
            self.say(str(e))
            self.status.set(result="failed", message=str(e))
            return "failed"
        if release is None:
            self.status.set(result="up-to-date", message="No releases yet", latest=None)
            return "up-to-date"
        self.status.set(latest=release.tag)
        bad = self.status.data.get("bad") or []
        if not force and release.tag in bad:
            message = f"{release.tag} didn't work here; waiting for a newer one"
            self.say(message)
            self.status.set(result="up-to-date", message=message)
            return "up-to-date"
        if not (force or is_newer(release.tag, current)):
            self.say(f"up to date ({current})")
            self.status.set(result="up-to-date", message=None)
            return "up-to-date"
        return self.install(release, current, nightly=nightly)

    def install(self, release: Release, current: str | None, *, nightly: bool) -> str:
        tag = release.tag
        rollback = self.work / "rollback"
        if not (self.prefix / "source" / "install.sh").exists():
            message = "no copy of the current install to go back to; not updating"
            self.say(message)
            self.status.set(result="failed", message=message)
            return "failed"
        self.status.set(state="downloading", message=f"Downloading {tag}")
        try:
            src = download(release, self.cfg, self.work / "releases")
        except UpdateError as e:
            self.say(str(e))
            self.status.set(result="failed", message=str(e))
            return "failed"
        # Things may have changed while it downloaded.
        if reason := self._busy(nightly):
            self.say(f"not now: {reason}")
            self.status.set(waiting=reason, result="waiting", message=None)
            return "waiting"
        shutil.rmtree(rollback, ignore_errors=True)
        shutil.copytree(self.prefix / "source", rollback, symlinks=True)
        before = self._baseline()
        if nightly:
            self.quiet_file.write_text(str(int(self.sys.now() + self.cfg.health_timeout + 900)))
            self.quiet_file.chmod(0o644)
        self.status.set(state="installing", message=f"Installing {tag}")
        self.say(f"installing {tag} over {current}")
        code = self.sys.run(
            ["bash", str(src / "install.sh")], timeout=self.cfg.install_timeout, log=self.log
        )
        healthy = code == 0 and self.healthy(before)
        if healthy:
            self.say(f"installed {tag}")
            self.status.set(
                result="installed",
                message=None,
                current=tag,
                previous=current,
                event={"kind": "installed", "version": tag, "at": self.sys.now()},
            )
            self._prune(keep={tag, current})
            return "installed"
        why = f"install.sh exited {code}" if code else "the health check failed"
        self.say(f"{tag} failed ({why}); going back to {current}")
        self.status.set(state="rolling-back", message=f"{tag} didn't work; going back")
        # A crash-looping couchd may have hit systemd's start limit; restart would refuse.
        self.sys.run(["systemctl", "reset-failed", *UNITS], timeout=30, log=self.log)
        back = self.sys.run(
            ["bash", str(rollback / "install.sh")], timeout=self.cfg.install_timeout, log=self.log
        )
        ok = back == 0 and self.healthy(before)
        bad = sorted(set(self.status.data.get("bad") or []) | {tag})
        message = f"{tag} didn't work ({why}), so Couch stayed on {current or 'this version'}"
        if not ok:
            message = f"{tag} didn't work, and going back didn't either. Check the logs"
        self.say(message)
        self.status.set(
            result="rolled-back" if ok else "broken",
            message=message,
            bad=bad,
            event={"kind": "rolled-back", "version": tag, "kept": current, "at": self.sys.now()},
        )
        shutil.rmtree(self.work / "releases" / tag, ignore_errors=True)
        return "rolled-back" if ok else "broken"

    def _baseline(self) -> set[str]:
        """Services already failed before the update; a new couchd isn't blamed for them."""
        health = self.sys.health() or {}
        return {k for k, v in (health.get("services") or {}).items() if v == "failed"}

    def healthy(self, failed_before: set[str]) -> bool:
        """couchd and the session up, the API answering, nothing newly failed, and couchd
        not restarting for `settle` seconds, within `health_timeout`."""
        deadline = self.sys.now() + self.cfg.health_timeout
        steady_since: float | None = None
        pid = None
        last_why = ""
        while self.sys.now() < deadline:
            ok, why = self._check(failed_before)
            unit = self.sys.unit("couchd.service")
            mark = (unit.get("MainPID"), unit.get("NRestarts"))
            if ok and mark == pid and steady_since is not None:
                if self.sys.now() - steady_since >= self.cfg.settle:
                    return True
            elif ok:
                steady_since, pid = self.sys.now(), mark
            else:
                steady_since, pid = None, None
                if why != last_why:
                    self.say(f"health: {why}")
                last_why = why
            self.sys.sleep(2)
        return False

    def _check(self, failed_before: set[str]) -> tuple[bool, str]:
        for unit in UNITS:
            state = self.sys.unit(unit).get("ActiveState")
            if state != "active":
                return False, f"{unit} is {state}"
        health = self.sys.health()
        if health is None:
            return False, "couchd's API isn't answering"
        failed = {k for k, v in (health.get("services") or {}).items() if v == "failed"}
        if failed - failed_before:
            return False, "failed: " + ", ".join(sorted(failed - failed_before))
        return True, ""

    def _prune(self, keep: set[str | None]) -> None:
        releases = self.work / "releases"
        if not releases.is_dir():
            return
        for d in releases.iterdir():
            if d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)

    # The system
    def update_system(self) -> None:
        """Security updates every night, a full upgrade every `full_upgrade_days`."""
        apt = ["-o", "DPkg::Lock::Timeout=600", "-o", "Dpkg::Options::=--force-confold"]
        if reason := self._busy(True):
            self.say(f"system updates: not now: {reason}")
            return
        couch_message = self.status.data.get("message")  # the Couch result's; put back after
        self.status.set(state="system", message="Installing security updates")
        code = self.sys.run(["unattended-upgrade"], timeout=3600, log=self.log)
        result: dict[str, Any] = {"security": code == 0, "at": self.sys.now()}
        last_full = (self.status.data.get("system") or {}).get("full_at") or 0
        if self.sys.now() - last_full >= self.cfg.full_upgrade_days * 86400:
            if reason := self._busy(True):
                self.say(f"full upgrade: not now: {reason}")
            else:
                self.status.set(message="Installing system updates")
                code = self.sys.run(["apt-get", *apt, "update", "-q"], timeout=1800, log=self.log)
                if code == 0:
                    code = self.sys.run(
                        ["apt-get", *apt, "-y", "-q", "full-upgrade"], timeout=3600, log=self.log
                    )
                result["full"] = code == 0
                result["full_at"] = self.sys.now() if code == 0 else last_full
                # Chromium may have been replaced under the running browser.
                if code == 0:
                    self.sys.run(
                        ["systemctl", "try-restart", "couch-session.service"],
                        timeout=60,
                        log=self.log,
                    )
        result.setdefault("full_at", last_full)
        self.status.set(system=result, message=couch_message)

    def reboot_if_idle(self) -> None:
        if reason := self._busy(True):
            self.say(f"a reboot is needed, not now: {reason}")
            self.status.set(reboot_required=True)
            return
        self.say("rebooting for the system updates")
        self.quiet_file.write_text(str(int(self.sys.now() + 900)))
        self.quiet_file.chmod(0o644)
        self.status.set(reboot_required=False, state="idle")
        # Keep the quiet file: couchd reads it when it starts after the reboot.
        self._keep_quiet = True
        self.sys.reboot()


def read_only_root(mounts: Path = Path("/proc/mounts")) -> bool:
    """True for an overlay root (raspi-config's overlay file system) or a read-only /."""
    try:
        for line in mounts.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "/":
                return parts[2] == "overlay" or "ro" in parts[3].split(",")
    except OSError:
        pass
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="couch-update", description=__doc__.split("\n")[0])
    # `enabled`: exit 0 if the night runs are turned on (install.sh asks this)
    parser.add_argument("mode", choices=["nightly", "now", "status", "enabled"])
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--force", action="store_true", help="install the latest even if not newer")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (UpdateError, tomllib.TOMLDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.mode == "enabled":
        return 0 if cfg.nightly else 1
    updater = Updater(cfg)
    if args.mode == "status":
        print(json.dumps(updater.status.data, indent=2))
        return 0
    if os.geteuid() != 0:
        print("error: run as root", file=sys.stderr)
        return 2
    result = updater.run(nightly=args.mode == "nightly", force=args.force)
    print(result)
    # A rollback did its job: not a failed unit. "broken" (it couldn't go back) and "failed" are.
    ok = ("installed", "up-to-date", "skipped", "waiting", "busy", "no-access", "rolled-back")
    return 0 if result in ok else 1


if __name__ == "__main__":
    sys.exit(main())

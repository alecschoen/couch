# System files

Everything `install.sh` puts outside `/opt/couch`: systemd units, the cage/Chromium session, the hidden Kodi profile, and `config.txt`/`cmdline.txt` changes for a silent boot.

- `couchd.service`: the background service (`Type=notify`, watchdog, restarts on failure)
- `couch-session.service`: cage on tty1 as the `couch` user, through a logind session. It's an autologin without a password, and it takes tty1 from the console login.
- `couch-browser`: the Chromium command line cage runs (kiosk, Wayland, profile in `/var/lib/couch/chromium`, DevTools on 127.0.0.1:9222 for couchd)
- `couch-kodi.service`: Kodi on tty1 for Plex. Not enabled: couchd starts it, and starting it stops `couch-session.service` (they conflict). When Kodi exits the unit starts the session again, so the launcher comes back even without couchd.
- `couch-kodi`: the Kodi command line (DRM/GBM, profile in `/var/lib/couch/kodi`, filled by #22)
- `couch.rules`: polkit rules letting the `couch` user start, stop and reset those two units, and reboot or power off the Pi (Settings › Power), nothing else. Installed as `/etc/polkit-1/rules.d/50-couch.rules`.
- `config.example.toml`: every config key with its default
- `couch-update.service`, `couch-update.timer`, `couch-update-now.service`: overnight updates (#24), see below
- `update.example.toml`: couch-update's settings, installed as `/etc/couch/update.example.toml`
- `apt-couch.conf`: installed as `/etc/apt/apt.conf.d/52couch-updates`. apt still refreshes its lists by itself, but security updates are installed by couch-update at night, not by `apt-daily-upgrade`, so they never land during a film
- `wireplumber-couch.conf`: installed as `/etc/wireplumber/wireplumber.conf.d/50-couch.conf`. couchd picks the output and moves every stream itself, so WirePlumber is told not to send apps back to the output they last used.

## Sound

PipeWire runs as the `couch` user, in the session `couch-session.service` opens on tty1 (logind starts `user@<uid>.service`, which starts PipeWire, WirePlumber and pipewire-pulse). couchd is a system service, but it runs as `couch` too, so it reaches that PipeWire at `/run/user/<uid>/pulse/native` with `pactl`. It needs no linger, extra groups or socket permissions. couchd starts before the session, so it waits for PipeWire, and it reconnects if PipeWire restarts.

To look at the same thing couchd sees, on the Pi:

```bash
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) pactl list sinks short
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) wpctl status
```

## Updates

`couch-update` (`couchd/updater.py`, run as root with the system Python) keeps Couch and the
system up to date. `couch-update.timer` starts it every half hour from 01:00 to 05:30; it
does one run a night, the first time nobody is using Couch (TV off, or the launcher gone
idle) and nothing plays. Settings › Updates › Check now starts `couch-update-now.service`
(the polkit rule lets couchd start just that unit), which skips the night and idle rules but
still won't run while something plays.

A run:
1. asks couchd (`/api/state`) whether it may; couchd not answering counts as no
2. reads the latest release (`source` in `/etc/couch/update.toml`, GitHub's API by default),
   and if it's newer than `/opt/couch/VERSION` downloads and unpacks it into
   `/var/lib/couch-update/releases/<tag>` and runs its `install.sh`
3. checks couchd and the session are up, `/api/health` answers without newly failed
   services, and couchd stays up (same PID, no restarts) for 20 s, within 3 minutes. If not,
   or if `install.sh` failed, it runs the previous install's `install.sh` again
   (`install.sh` keeps a copy of its own source in `/opt/couch/source` for this) and skips
   that release from then on
4. at night only: `unattended-upgrade` (Debian's security updates), once a week a full
   `apt-get full-upgrade` (that's also how Raspberry Pi's own packages, Chromium among them,
   get updated), and a reboot if one is needed. Each step asks couchd again first.

At night couchd is told not to wake the TV when it restarts (`/var/lib/couch-update/quiet-until`).
The result is in `/var/lib/couch-update/status.json` (Settings shows it), and the last run's
output in `last-run.log` next to it. By hand:

```bash
sudo systemctl start couch-update-now.service        # what Check now does
sudo python3 /opt/couch/couchd/updater.py status
sudo python3 /opt/couch/couchd/updater.py now --force  # reinstall the latest, even if not newer
journalctl -u couch-update -u couch-update-now
```

To try it without a GitHub release, point `source` at anything serving the same JSON, e.g.
`python3 -m http.server` in a folder with a `latest.json` (`{"tag_name": "v0.2.0",
"tarball_url": "http://…/couch.tar.gz"}`) and a tarball from `git archive --prefix=couch/
-o couch.tar.gz HEAD`. `file://` URLs work too.

Not covered: a release that breaks booting (the health check runs before any reboot; the
`.couch-orig` boot files and `--revert-boot` are for that), and a release whose own
couch-update is broken while couchd is fine (the next night runs that one).

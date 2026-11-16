# System files

Everything `install.sh` puts outside `/opt/couch`: systemd units, the cage/Chromium session, the hidden Kodi profile, and `config.txt`/`cmdline.txt` changes for a silent boot.

- `couchd.service`: the background service (`Type=notify`, watchdog, restarts on failure)
- `couch-session.service`: cage on tty1 as the `couch` user, through a logind session. It's an autologin without a password, and it takes tty1 from the console login.
- `couch-session-start`: what cage runs. It starts `couch-splash`, then `couch-browser`; see [Session splash](#session-splash).
- `couch-splash`: a small Wayland client (Python's standard library only) that keeps the boot splash on screen in cage until the launcher has drawn. It draws with `couch-fbsplash`'s code, so it's installed next to it in `/opt/couch/bin`.
- `couch-browser`: the Chromium command line cage runs (kiosk, Wayland, profile in `/var/lib/couch/chromium`, DevTools on 127.0.0.1:9222 for couchd)
- `couch-kodi.service`: Kodi on tty1 for Plex. Not enabled: couchd starts it, and starting it stops `couch-session.service` (they conflict). When Kodi exits the unit starts the session again, so the launcher comes back even without couchd.
- `couch-kodi`: the Kodi command line (DRM/GBM, profile in `/var/lib/couch/kodi`, filled by #22)
- `couch.rules`: polkit rules letting the `couch` user start, stop and reset those two units, and reboot or power off the Pi (Settings › Power), nothing else. Installed as `/etc/polkit-1/rules.d/50-couch.rules`.
- `config.example.toml`: every config key with its default
- `couch-bootfiles`: edits `cmdline.txt` and `config.txt` in `/boot/firmware`, and undoes exactly those edits. Each change has an owner (`boot` for the silent boot), so other boot settings can come and go on their own. It keeps the first copy of each file as `*.couch-orig`. Installed as `/opt/couch/bin/couch-bootfiles`; run it with `show` to see what Couch changed.
- `boot-config.txt`: the silent boot's lines for `config.txt` (`disable_splash=1`, `disable_overscan=1`), in a `# >>> couch:boot` block at the end.
- `initramfs-splash`: an initramfs-tools hook, installed as `/etc/initramfs-tools/hooks/couch-splash`. It puts the splash (`/lib/firmware/couch-splash.tga`, from `ui/brand/splash.tga`) in the initramfs.
- `70-couch-no-pointer.rules`: no mouse cursor on the TV. The kernel's CEC remote inputs (`vc4-hdmi-0`, `vc4-hdmi-1`) have mouse axes, so libinput would hand them to cage as pointers and cage would draw its cursor. This makes libinput ignore them. couchd doesn't use them: it reads `/dev/cec0` and types through its own Couch remote device.
- `couch-fbsplash`, `couch-fbsplash.service`, `couch-fbsplash.rules`: draw the same splash again on vc4's framebuffer when vc4 takes the screen over, and once more at shutdown (see Boot below). The rule is installed as `/etc/udev/rules.d/70-couch-fbsplash.rules`.
- `couch-update.service`, `couch-update.timer`, `couch-update-now.service`: overnight updates (#24), see below
- `update.example.toml`: couch-update's settings, installed as `/etc/couch/update.example.toml`
- `apt-couch.conf`: installed as `/etc/apt/apt.conf.d/52couch-updates`. apt still refreshes its lists by itself, but security updates are installed by couch-update at night, not by `apt-daily-upgrade`, so they never land during a film
- Power cuts (#25, see [docs/sd-card.md](../docs/sd-card.md#power-cuts)). `install.sh --revert-sd` removes all of these:
  - `journald-couch.conf`: installed as `/etc/systemd/journald.conf.d/50-couch.conf`. The journal stays in RAM (`Storage=volatile`).
  - `var-cache-couch.mount` and `couch-cache.conf`: a 256 MB tmpfs on `/var/cache/couch`, and its `chromium` and `kodi` directories (`/etc/tmpfiles.d/couch-cache.conf`). `couch-browser` and `couch-kodi` keep their caches there when it exists.
  - `couch-tmp.conf`: turns on systemd's `tmp.mount` on a system where `/tmp` isn't in RAM yet. Trixie doesn't need it.
  - `couch-fstab`: adds `noatime` to `/` and `/boot/firmware` in `/etc/fstab`, and undoes exactly that, like `couch-bootfiles` (owner `sd-card`; original in `/etc/fstab.couch-orig`; `couch-fstab show`). It refuses any edit beyond the options column, and any edit that `findmnt --verify` likes less.
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

The night runs are opt-in. Until `/etc/couch/update.toml` says `nightly = true` (and
install.sh has run since), there's no timer, no unattended-upgrades, and nothing updates or
reboots on its own; Settings says "Not turned on yet". Check now works either way. To turn
them on, once the Pi can read the releases (see Release access below):

```bash
echo 'nightly = true' | sudo tee -a /etc/couch/update.toml
sudo /opt/couch/source/install.sh   # or install.sh from a checkout: enables the timer
```

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

### Release access

The repo is private, so the Pi needs a token to read its releases. Until there is one,
Settings › Updates says "Not set up yet" and nothing is installed (security updates still
run). The token goes in `/etc/couch/github-token`, readable by root only (couch-update refuses
it otherwise), and is sent only to `api.github.com`, `github.com` and `codeload.github.com`,
never along a redirect:

1. On GitHub: Settings › Developer settings › Fine-grained tokens › Generate. Repository
   access: only this repo. Permissions: Contents: Read-only (Metadata: Read-only comes with
   it). Nothing else. Pick an expiry you'll notice, or none.
2. On the Pi:
   `sudo sh -c 'umask 077; cat > /etc/couch/github-token'`, paste it, Enter, Ctrl-D.
3. Settings › Updates › Check now: it should say "Up to date" (or install the latest).

"The token in … can't see the repo" or "GitHub didn't accept the token" means it expired,
was revoked, or doesn't cover this repo.

To try it without a GitHub release, point `source` at anything serving the same JSON, e.g.
`python3 -m http.server` in a folder with a `latest.json` (`{"tag_name": "v0.2.0",
"tarball_url": "http://…/couch.tar.gz"}`) and a tarball from `git archive --prefix=couch/
-o couch.tar.gz HEAD`. `file://` URLs work too.

Not covered: a release that breaks booting (the health check runs before any reboot; the
`.couch-orig` boot files and `--revert-boot` are for that), and a release whose own
couch-update is broken while couchd is fine (the next night runs that one).
## Boot

From power-on the TV stays black until the Couch splash, then the launcher. The firmware's rainbow square is off (`disable_splash=1`). The kernel prints nothing on the TV: it gets `quiet loglevel=0`, no cursor, and its console moves from tty1 to tty3. Ctrl+Alt+F3 on a USB keyboard shows it.

The splash comes from the Raspberry Pi kernel's fullscreen logo (`fullscreen_logo=1 fullscreen_logo_name=couch-splash.tga`), not Plymouth. The kernel draws it as soon as the firmware's framebuffer appears, about a second after power-on, so it doesn't wait for any userspace. The kernel only draws it on that firmware framebuffer, though. About 6 s in, vc4 (the real display driver) takes the screen over with a framebuffer of its own, and the kernel won't draw a logo on a framebuffer that belongs to a module. So a udev rule starts `couch-fbsplash.service` the moment vc4's framebuffer appears, and `couch-fbsplash` draws the same TGA the same way: centred, the rest filled with its top-left pixel. The splash then stays until cage takes the screen. At a reboot or power-off the same unit stops just after couch-session, once cage has let go of the screen, and draws the splash again; it stays until the Pi resets. That redraw copies the frame kept in `/run` from the boot, so it takes a fraction of a second. The launcher's Startup screen (#19) draws the same lockup on the same pixels, so the splash seems to gain a progress bar. `logo.nologo` must stay off the command line: it hides this logo too.

The kernel uses the TV's EDID mode, 1920×1080 at 60 Hz, and adds no overscan border (`disable_overscan=1`). Set the TV not to crop either; see [the README](../README.md#the-tv). If a TV ever reports the wrong mode, `video=HDMI-A-1:1920x1080@60` on the command line forces it.

`couch-session.service` doesn't reset or clear tty1 when it starts (no `TTYReset`, no `TTYVTDisallocate`), so the splash stays up through cage's start. With the console on tty3, nothing else ever writes to tty1.

cloud-init is turned off once the card's first boot is done (`/etc/cloud/cloud-init.disabled`). It only sets up the user and SSH key on that first boot, and otherwise costs about 2.5 s of every boot. The network doesn't come from it: NetworkManager brings up eth0 by itself.

`scripts/boot-timing.sh` reads the last boot's milestones from the Pi's logs: when the splash could draw, when cage started, and when the launcher first painted. Run it with `ssh couch.local 'bash -s' < scripts/boot-timing.sh`.

To check what's set: `/opt/couch/bin/couch-bootfiles show`. `install.sh --uninstall` puts both files back the way they were. If the Pi ever doesn't boot, put the card in a PC: the boot partition has `cmdline.txt.couch-orig` and `config.txt.couch-orig`, the files as they were before Couch. Copy them over `cmdline.txt` and `config.txt`.

## Session splash

From power-on the Couch splash should stay on screen until Home appears, with no black or white in between. Before #23 it didn't. cage draws black from the moment it takes the screen until a client draws. Chromium then needs seconds to start (#4 measured cage at ~17.4 s and the launcher's first paint at ~24.7 s), and its window's first frames are white, before the page paints.

So `couch-session-start` first starts `couch-splash`, then Chromium. couch-splash is up a fraction of a second after cage. It draws exactly what couch-fbsplash drew: the frame couch-fbsplash cached in `/run/couch-fbsplash`, or the same TGA rendered the same way. So the logo doesn't move between the kernel's splash, fbsplash, cage and the launcher's Startup screen, which is `ui/brand/splash.png`. `tests/test_splash_client.py` checks that the drawn frame equals splash.png. The only difference is up to 2 levels on the lockup's anti-aliased edges, because the TGA's grey ramp has 128 steps.

cage puts each newly mapped window on top and focuses it, so Chromium's window would cover the splash. When that happens, cage takes the focus from couch-splash's window, and couch-splash hears about it at once. It then maps a second window it prepared earlier, which lands on top of Chromium, normally before cage draws its next frame. Its buffer is ARGB, with every pixel opaque and no opaque region declared. That way wlroots doesn't count Chromium as hidden, keeps sending it frame callbacks, and the launcher renders underneath as usual.

The launcher tells couchd when it has drawn its first frame (`ui.first_frame`, `ui/js/firstframe.js`). couchd ends couch-splash half a second later (`couchd/splash.py`, using the pid in `$XDG_RUNTIME_DIR/couch-splash.pid`). cage then gives the focus back to Chromium, which already shows the Startup screen: the same picture. If couchd never says so, couch-splash goes by itself after 30 s, so a broken launcher can't stay hidden. Its log lines (in the journal, from couch-session) say when each window reached the screen, from wp_presentation feedback. The same happens whenever the session starts again, after Kodi for instance.

`scripts/perf/cage-check.sh` tries all of this in a real cage 0.2.0, Debian Trixie's, headless in Docker. A fake browser maps a white window a second after the splash, and every frame is recorded. `scripts/perf/record-boot.sh` records a real boot on the Pi the same way.

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
- `wireplumber-couch.conf`: installed as `/etc/wireplumber/wireplumber.conf.d/50-couch.conf`. couchd picks the output and moves every stream itself, so WirePlumber is told not to send apps back to the output they last used.

## Sound

PipeWire runs as the `couch` user, in the session `couch-session.service` opens on tty1 (logind starts `user@<uid>.service`, which starts PipeWire, WirePlumber and pipewire-pulse). couchd is a system service, but it runs as `couch` too, so it reaches that PipeWire at `/run/user/<uid>/pulse/native` with `pactl`. It needs no linger, extra groups or socket permissions. couchd starts before the session, so it waits for PipeWire, and it reconnects if PipeWire restarts.

To look at the same thing couchd sees, on the Pi:

```bash
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) pactl list sinks short
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) wpctl status
```

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

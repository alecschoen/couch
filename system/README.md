# System files

Everything `install.sh` puts outside `/opt/couch`: systemd units, the cage/Chromium session, the hidden Kodi profile, and `config.txt`/`cmdline.txt` changes for a silent boot.

- `couchd.service`: the background service (`Type=notify`, watchdog, restarts on failure)
- `couch-session.service`: cage on tty1 as the `couch` user, through a logind session. It's an autologin without a password, and it takes tty1 from the console login.
- `couch-session-start`: what cage runs. It shows the Startup frame with swayimg, then starts `couch-browser`; see [Session splash](#session-splash). `couch-swayimg.conf` is that swayimg's config (no text, no keys), installed as `/opt/couch/share/swayimg/config`.
- `couch-browser`: the Chromium command line cage runs (kiosk, Wayland, profile in `/var/lib/couch/chromium`, DevTools on 127.0.0.1:9222 for couchd)
- `couch-kodi.service`: Kodi on tty1 for Plex. Not enabled: couchd starts it, and starting it stops `couch-session.service` (they conflict). When Kodi exits the unit starts the session again, so the launcher comes back even without couchd.
- `couch-kodi`: the Kodi command line (DRM/GBM, profile in `/var/lib/couch/kodi`, filled by #22)
- `couch.rules`: polkit rules letting the `couch` user start, stop and reset those two units, and reboot or power off the Pi (Settings › Power), nothing else. Installed as `/etc/polkit-1/rules.d/50-couch.rules`.
- `config.example.toml`: every config key with its default
- `wireplumber-couch.conf`: installed as `/etc/wireplumber/wireplumber.conf.d/50-couch.conf`. couchd picks the output and moves every stream itself, so WirePlumber is told not to send apps back to the output they last used.

## Sound

PipeWire runs as the `couch` user, in the session `couch-session.service` opens on tty1 (logind starts `user@<uid>.service`, which starts PipeWire, WirePlumber and pipewire-pulse). couchd is a system service, but it runs as `couch` too, so it reaches that PipeWire at `/run/user/<uid>/pulse/native` with `pactl`. It needs no linger, extra groups or socket permissions. couchd starts before the session, so it waits for PipeWire, and it reconnects if PipeWire restarts.

To look at the same thing couchd sees, on the Pi:

```bash
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) pactl list sinks short
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) wpctl status
```

## Session splash

cage draws black from the moment it takes the screen until a client draws, and Chromium needs several seconds on the Pi to start and paint the launcher (#4 measured cage's modeset at ~17.4 s and the launcher's first paint at ~24.7 s). So `couch-session-start` first runs swayimg with `ui/brand/splash.png`: the launcher's Startup frame, which is the boot splash's lockup on the same pixels. Then it starts Chromium. cage puts each newly mapped window on top, so Chromium's window covers the splash as soon as it maps.

The launcher tells couchd when it has drawn its first frame (`ui.first_frame`, `ui/js/firstframe.js`), and couchd ends swayimg half a second later (`couchd/splash.py`, using the pid in `$XDG_RUNTIME_DIR/couch-splash.pid`). By then the swayimg window is under Chromium's, so it can't be seen to go; it isn't left running because cage would blend it under every frame, video included. swayimg has the keyboard until Chromium maps, so its config unbinds every key. The same happens whenever the session starts again, after Kodi for instance. Without swayimg installed, the session starts Chromium straight away, as before.

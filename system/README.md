# System files

Everything `install.sh` puts outside `/opt/couch`: systemd units, the cage/Chromium session, the hidden Kodi profile, and `config.txt`/`cmdline.txt` changes for a silent boot.

- `couchd.service`: the background service (`Type=notify`, watchdog, restarts on failure)
- `couch-session.service`: cage on tty1 as the `couch` user, through a logind session. It's an autologin without a password, and it takes tty1 from the console login.
- `couch-browser`: the Chromium command line cage runs (kiosk, Wayland, profile in `/var/lib/couch/chromium`)
- `config.example.toml`: every config key with its default
- `wireplumber-couch.conf`: installed as `/etc/wireplumber/wireplumber.conf.d/50-couch.conf`. couchd picks the output and moves every stream itself, so WirePlumber is told not to send apps back to the output they last used.

## Sound

PipeWire runs as the `couch` user, in the session `couch-session.service` opens on tty1 (logind starts `user@<uid>.service`, which starts PipeWire, WirePlumber and pipewire-pulse). couchd is a system service, but it runs as `couch` too, so it reaches that PipeWire at `/run/user/<uid>/pulse/native` with `pactl`. It needs no linger, extra groups or socket permissions. couchd starts before the session, so it waits for PipeWire, and it reconnects if PipeWire restarts.

To look at the same thing couchd sees, on the Pi:

```bash
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) pactl list sinks short
sudo -u couch XDG_RUNTIME_DIR=/run/user/$(id -u couch) wpctl status
```

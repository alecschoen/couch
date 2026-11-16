# System files

Everything `install.sh` puts outside `/opt/couch`: systemd units, the cage/Chromium session, the hidden Kodi profile, and `config.txt`/`cmdline.txt` changes for a silent boot.

- `couchd.service`: the background service (`Type=notify`, watchdog, restarts on failure)
- `couch-session.service`: cage on tty1 as the `couch` user, through a logind session. It's an autologin without a password, and it takes tty1 from the console login.
- `couch-browser`: the Chromium command line cage runs (kiosk, Wayland, profile in `/var/lib/couch/chromium`, DevTools on 127.0.0.1:9222 for couchd)
- `couch-kodi.service`: Kodi on tty1 for Plex. Not enabled: couchd starts it, and starting it stops `couch-session.service` (they conflict). When Kodi exits the unit starts the session again, so the launcher comes back even without couchd.
- `couch-kodi`: the Kodi command line (DRM/GBM, profile in `/var/lib/couch/kodi`, filled by #22)
- `couch.rules`: polkit rule letting the `couch` user start, stop and reset those two units, nothing else. Installed as `/etc/polkit-1/rules.d/50-couch.rules`.
- `config.example.toml`: every config key with its default

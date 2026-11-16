<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/brand/svg/lockup-white.svg">
    <img src="ui/brand/svg/lockup-night.svg" alt="Couch" height="56">
  </picture>
</h1>

A remote-first TV launcher for the Raspberry Pi 4. It runs YouTube's TV interface and Plex, lets you choose where the sound goes (TV, the headphone jack or Bluetooth), and you control all of it with the TV remote over HDMI-CEC.

## How it fits together

- **couchd** is a Python asyncio service. It owns HDMI-CEC (`/dev/cec0`), turns remote buttons into key presses, routes audio through PipeWire, manages Bluetooth through BlueZ, and decides which app is on screen. It serves the launcher UI and a WebSocket on `127.0.0.1:8765`.
- **Launcher UI**: plain HTML/CSS/JS shown full-screen in Chromium under the `cage` kiosk compositor.
- **YouTube**: `youtube.com/tv` in Chromium with H.264 hardware decoding.
- **Plex**: Plex for Kodi, with Kodi hidden entirely. It opens straight into Plex and exits back to the launcher.

## Layout

| Path | What lives there |
| --- | --- |
| `couchd/` | The background service |
| `ui/` | Launcher web UI (evening B3 design, 1920×1080) |
| `system/` | systemd units, Chromium/Kodi config, boot files |
| `docs/` | SD card guide, TV notes, user guide |
| `tests/` | pytest suite |
| `scripts/` | `dev.sh`, `flash-sd.sh` |
| `install.sh` | Install or update Couch on a fresh Pi OS Lite |

## Development

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run couchd --dev
uv run pytest
uv run ruff check && uv run ruff format --check
```

### On your PC

```bash
./scripts/dev.sh
```

This runs couchd with fake hardware (a TV, the TV / headphone jack outputs, and Bluetooth devices that appear when you search) and opens the launcher in a 1920×1080 Chromium window. Without Chromium, it prints the URL instead: <http://127.0.0.1:8765>. Set `COUCH_BROWSER` to pick a browser, or pass `--no-browser`.

Your keyboard is the remote:

| Key | Remote |
| --- | --- |
| Arrows | Arrows |
| Enter | OK |
| Backspace / Esc | Back |
| H / Home | Home |
| Space | Play/pause |
| F1–F4 | Red, green, yellow, blue |

In the launcher these keys go through couchd like the remote's. On the Pi, couchd types the remote's buttons into YouTube and Plex on a virtual keyboard; in dev mode that keyboard only logs what it would have typed (at debug level), and the browser window gets your real keyboard instead.

The fakes take a moment like real hardware does, and "Old Speaker" always refuses to pair, so error screens can be built too. The API is described in [docs/protocol.md](docs/protocol.md).

## Install on the Pi

Start from a fresh Pi OS Lite card ([docs/sd-card.md](docs/sd-card.md)), then on the Pi:

```bash
git clone https://github.com/alecschoen/couch.git && cd couch
sudo ./install.sh
```

The script installs:
- the apt packages Couch needs
- a `couch` system user
- couchd in `/opt/couch`
- two services: `couchd` and `couch-session`, which runs cage and Chromium on tty1

The launcher comes up on the TV straight away, and again after every boot.

To update, pull and run it again. It only changes what's different, and restarts only what it changed. A run with nothing to do says so.

`sudo ./install.sh --uninstall` removes Couch but keeps `/etc/couch` and `/var/lib/couch`. `--purge` removes those and the user too. Once the repo is public, `curl -fsSL https://raw.githubusercontent.com/alecschoen/couch/main/install.sh | sudo bash` works too; it keeps its checkout in `/opt/couch/src`.

Logs: `journalctl -u couchd -u couch-session`.

It also sets up a silent boot: from power-on the TV goes from black to the Couch splash, then the launcher, and a reboot or power-off shows the splash too. There's no rainbow square and no text. It edits `cmdline.txt` and `config.txt`, and `--uninstall` undoes that; see [system/README.md](system/README.md#boot). This takes effect from the next boot, so reboot once after the first install.

### The TV

Couch draws to the edge of the screen, so turn off the TV's overscan. On the Sony Bravia, with the Pi's input showing, set Screen → Display Area to **Full Pixel**. Without it the TV crops the edges of the launcher.

### YouTube sign-in

Open YouTube from the launcher and choose **Sign in with your phone**, then go to [yt.be/activate](https://yt.be/activate) on your phone and enter the code on the TV. The sign-in is kept in Chromium's profile, `/var/lib/couch/chromium`, so it's needed once. **Watch as guest** works without an account.

## Configuration

On the Pi, couchd reads `/etc/couch/config.toml`. The file is optional, and every key has a default; see [system/config.example.toml](system/config.example.toml). Saved state, such as the preferred audio output, lives in `/var/lib/couch/state.json`. Check a config with `couchd --check-config`. If the file is broken, couchd logs the error and starts with defaults, so the TV still comes up.

## Plan

Work is tracked in [issues](https://github.com/alecschoen/couch/issues), grouped by milestone: M0 setup → M1 background service → M2 launcher UI → M3 apps → M4 polish. Issues labelled `needs-pi` can only be finished on the real Pi and TV.

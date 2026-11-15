# Couch

A remote-first TV launcher for the Raspberry Pi 4. It runs YouTube's TV interface and Plex, lets you choose where the sound goes (TV, the headphone jack or Bluetooth), and you control all of it with the TV remote over HDMI-CEC.

*Couch is a working name.*

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
| `scripts/` | `dev.sh` |
| `install.sh` | One-command install on a fresh Pi OS Lite (coming in #3) |

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

The fakes take a moment like real hardware does, and "Old Speaker" always refuses to pair, so error screens can be built too. The API is described in [docs/protocol.md](docs/protocol.md).

## Configuration

On the Pi, couchd reads `/etc/couch/config.toml`. The file is optional, and every key has a default; see [system/config.example.toml](system/config.example.toml). Saved state, such as the preferred audio output, lives in `/var/lib/couch/state.json`. Check a config with `couchd --check-config`. If the file is broken, couchd logs the error and starts with defaults, so the TV still comes up.

## Plan

Work is tracked in [issues](https://github.com/alecschoen/couch/issues), grouped by milestone: M0 setup → M1 background service → M2 launcher UI → M3 apps → M4 polish. Issues labelled `needs-pi` can only be finished on the real Pi and TV.

# couchd API

couchd listens on `http://127.0.0.1:8765`. It only answers requests whose `Host` is
loopback, and refuses any browser request with a non-loopback `Origin`. So pages like
youtube.com/tv, running in the same Chromium, can't reach it.

## WebSocket: `/ws`

The UI opens one socket. Every message is a JSON object with a `type`.

**`state`**: the live state, grouped into sections.

```json
{"type": "state", "full": true,  "data": {"system": {"version": "0.1.0", "mode": "dev"}, "services": {}}}
{"type": "state", "full": false, "data": {"audio": {"output": "bluetooth"}}}
```

The first message after connecting is always a full snapshot (`full: true`). After that come
patches. For each section in a patch, merge its keys into that section, replacing any value
already there. If the socket drops, reconnect; the next snapshot replaces everything.

| Section | Keys |
| --- | --- |
| `system` | `version`, `mode` (`device` or `dev`) |
| `services` | service name → `stopped`, `starting`, `running`, `failed` or `stopping` |
| `tv` | `power`: `on`, `standby` or `unknown`; `active`: the TV is on and showing Couch (false after the user picks another input); `standby_on_poweroff` (the setting) |
| `audio` | `outputs` (list of `{id, name, kind, available}`; kind is `tv`, `jack`, `bluetooth` or `usb`), `current` (output id), `volume` (0–100), `muted` |
| `bluetooth` | `scanning`, `devices` (list of `{address, name, audio, paired, connected}`, paired first) |

| `apps` | see below |
| `idle` | `idle` (bool), `after` (seconds without a key press, on the launcher, before `idle` turns true) |

`system` also has `url`, the launcher's own address.

`apps` says what's on screen:

```json
{"apps": {
  "list": [{"id": "launcher", "name": "Home", "kind": "launcher"},
           {"id": "youtube", "name": "YouTube", "kind": "chromium"},
           {"id": "plex", "name": "Plex", "kind": "kodi"}],
  "foreground": "youtube",
  "running": ["launcher", "youtube"],
  "opening": null,
  "error": null,
  "browser": "connected"}}
```

- `foreground`: the app in front. Starts as `launcher`.
- `running`: apps with an open tab, plus the Kodi app while it has the screen.
- `opening`: the app being started, else `null`. For Plex the launcher is in front for a
  moment with this set, then Kodi takes the screen: show the Opening screen.
- `error`: `{"app", "message"}` after an app died or didn't start, else `null`. couchd is
  back on the launcher by then. A launch or `apps.clear_error` clears it. An `error` toast
  goes out at the same time.
- `browser`: `connected` or `disconnected` (DevTools link to Chromium; it's
  `disconnected` while Kodi has the screen).

**`toast`**: a short notice to show on screen.

```json
{"type": "toast", "data": {"text": "Speaker connected", "level": "info"}}
```

`level` is `info`, `warning` or `error`.

**`key`**: a remote button for the UI to act on.

```json
{"type": "key", "data": {"key": "up"}}
```

Key names are listed in `couchd/keys.py`: `up` `down` `left` `right` `select` `back` `home`
`play` `pause` `play_pause` `stop` `rewind` `fast_forward` `red` `green` `yellow` `blue` and
`0`–`9`. Screens react only to these messages, never to raw keyboard events. A keyboard
(in dev mode, or plugged into the Pi) is turned into `remote.press` commands by
`ui/js/keys.js`, so it takes the same path through couchd as the TV remote.

When the TV goes to standby, couchd sends a `pause` key, so whatever is playing stops.

**`result`**: the reply to a command, matched by `id`.

### From the UI

**`command`**: run a command. Replies come back as `result`, possibly out of order, since
pairing takes seconds while other commands keep running.

```json
{"type": "command", "id": 7, "name": "audio.set_output", "args": {"output": "jack"}}
{"type": "result", "id": 7, "ok": true, "data": null}
{"type": "result", "id": 8, "ok": false, "error": "Old Speaker didn't accept the pairing"}
```

The text `ping` gets `pong`.

## Commands

| Command | Args |
| --- | --- |
| `remote.press` | `key` |
| `tv.power_on`, `tv.standby` | `power_on` wakes the TV and switches it to Couch |
| `tv.set_standby_on_poweroff` | `on` (bool). Saved. When on, the TV goes to standby when the Pi powers off (not on a reboot) |
| `audio.set_output` | `output` (an output id). Saved, and restored at boot if that output is around |
| `audio.set_volume` | `volume` (0–100) |
| `audio.set_muted` | `muted` (bool) |
| `bluetooth.scan` | `on` (bool) |
| `bluetooth.pair`, `.connect`, `.disconnect`, `.forget` | `address` |
| `apps.launch` | `app` (an id from `apps.list`). Starts it, or brings it to the front |
| `apps.close` | `app`. Not the launcher |
| `apps.home` | Back to the launcher, from anywhere. The `home` key does the same |
| `apps.clear_error` | Dismiss `apps.error` |
| `idle.activity` | Counts as a key press for idle detection |
| `apps.dev.kodi_exit` | Dev mode only: the pretend Kodi exits. `crashed` (bool, optional) |

A command exists only while its service runs. `GET /api/commands` lists the current ones.

If a client falls more than 256 messages behind, couchd closes it with code 1013 and it
should reconnect.

## REST

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/state` | Same object as a full `state` snapshot's `data` |
| `GET` | `/api/health` | `{"status": "ok" \| "degraded", "version", "services"}` |
| `POST` | `/api/toast` | Body `{"text": "...", "level": "info"}` → 204; shows a toast |
| `GET` | `/api/commands` | Names of the commands available right now |
| `POST` | `/api/command/{name}` | Body = args (optional) → `{"ok": true, "data"}`, or 400 with `{"ok": false, "error"}` |

```bash
curl -s localhost:8765/api/health
curl -s -XPOST localhost:8765/api/toast -d '{"text": "Hello from the shell"}'
curl -s -XPOST localhost:8765/api/command/remote.press -d '{"key": "down"}'
```

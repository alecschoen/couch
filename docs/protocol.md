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
| `tv` | `power`: `on`, `standby` or `unknown` |
| `audio` | `outputs` (list of `{id, name, kind, available}`; kind is `tv`, `jack`, `bluetooth` or `usb`), `current` (output id, or null while there's no output), `volume` (0–100, of the current output), `muted` |
| `bluetooth` | `scanning`, `devices` (list of `{address, name, audio, paired, connected}`, paired first) |

A section for the current app arrives with #13.

Output ids are stable across reboots: `tv` (HDMI), `jack` (the 3.5 mm jack), `usb:<card>`,
and `bluetooth:<address>`, the same address as in the `bluetooth` section. A second HDMI or
analog output is `tv:2` or `jack:2`. Outputs come and go (a speaker connects); an output with
`available: false` is listed but can't be picked.

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
`0`–`9`. Screens react only to these messages, never to raw keyboard events.
couchd acts on `red` itself, wherever you are, YouTube included: it moves the sound to the
next available output. Screens shouldn't give `red` another meaning. A keyboard
(in dev mode, or plugged into the Pi) is turned into `remote.press` commands by
`ui/js/keys.js`, so it takes the same path through couchd as the TV remote.

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
| `tv.power_on`, `tv.standby` | |
| `audio.set_output` | `output` (an output id). Saved, and restored at boot if that output is around |
| `audio.cycle_output` | Next available output, as the Red button does |
| `audio.set_volume` | `volume` (0–100) |
| `audio.set_muted` | `muted` (bool) |
| `bluetooth.scan` | `on` (bool) |
| `bluetooth.pair`, `.connect`, `.disconnect`, `.forget` | `address` |

couchd keeps sound on the output picked last. When that output goes away (a speaker out of
range) sound moves to the TV, and when it comes back the sound follows. Volume is remembered
per output. Every change of output, whatever caused it, shows a "Sound now on X" toast.

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

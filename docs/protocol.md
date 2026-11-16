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
| `startup` | `done` (bool), `progress` (0–1), `step`: `tv` (the TV is turning on), `services`, or `null` once done. See below |
| `services` | service name → `stopped`, `starting`, `running`, `failed` or `stopping` |
| `tv` | `power`: `on`, `standby` or `unknown`; `active`: the TV is on and showing Couch (false after the user picks another input); `standby_on_poweroff` (the setting); `system_audio`: the TV's speakers are muted and its volume buttons come to couchd (see [TV remote volume](#tv-remote-volume)) |
| `audio` | `outputs` (list of `{id, name, kind, available}`; kind is `tv`, `jack`, `bluetooth` or `usb`), `current` (output id, or null while there's no output), `volume` (0–100, of the current output), `muted` |
| `bluetooth` | `scanning`, `devices` (list of `{address, name, audio, paired, connected}`, paired first; while scanning, also named devices nearby), `auto_reconnect` (bool) |
| `apps` | see below |
| `idle` | `idle` (bool), `after` (seconds without a key press, on the launcher, before `idle` turns true; 0 = never), `tv_off_after` (seconds before the TV is turned off; 0 while `tv.off_when_idle` is off). See below |
| `evening` | `dark` (bool: the sun is down), `sunrise`, `sunset` (today, local `HH:MM`, or null in a polar day or night), `place` (the timezone the location came from, or null: then `dark` is 18:00–07:00). See below |
| `settings` | setting key → value; see [Settings](#settings) |

`system` also has `url`, the launcher's own address.

`startup` is for the Startup screen. The launcher appears while couchd is still starting
services and waking the TV over CEC. `done` turns true once every service has started (or
failed) and the TV says it's on. The TV gets 15 s to say so, and after 30 s startup is
done regardless, so nothing holds Home back for long. After a couchd restart it starts over.

`idle` drives the idle clock. When it turns true the launcher covers itself with a dark
clock and date that drifts every 3 minutes; dimming and the clock are the same step, set by
`display.dim_after`. Only the launcher goes idle, never while an app is in front. Activity is
any `key`, a change of `apps.foreground`, the TV being turned back on, or `idle.activity`.
The `key` that wakes the clock is sent before the `idle: false` patch, so the launcher drops
that press instead of acting on it. With `tv.off_when_idle` on, the TV is put in standby once
the launcher has had no activity for `tv_off_after` (2 hours), once each time.

`evening` is the sun where the Pi is, worked out offline from its timezone (tzdata's
coordinates for the zone; `timedatectl set-timezone` changes it), rechecked every minute.
With `display.evening` on `sunset` the launcher uses its deep colours while `dark` is true.

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
- `opening`: the app being started, else `null`. The launcher is in front while it's set:
  show the Opening screen. A new YouTube tab loads behind it and comes to the front once
  loaded (at most 10 s); for Plex, Kodi takes the screen after 0.8 s. Home (`apps.home`, or
  the key) while it's set gives up on opening.
- `error`: `{"app", "kind", "message"}` when an app couldn't open or stopped, else `null`.
  couchd is back on the launcher by then. `kind` says which error screen to show:
  - `offline`: no internet (YouTube), or no network at all (Plex). Checked before opening.
  - `server`: the Plex server couchd found before doesn't answer, and it isn't on the LAN
    at a new address either. Until couchd has seen a server once, Plex opens unchecked.
  - `crashed`: the app stopped unexpectedly.
  - `failed`: the app didn't start.
  - `repeated`: it crashed 3 times in 2 minutes, so launching it is refused for a while.

  A launch, `apps.home` or `apps.clear_error` clears it, and so does 2 minutes passing.
  There's no toast: the error screen says it.
- `browser`: `connected` or `disconnected` (DevTools link to Chromium; it's
  `disconnected` while Kodi has the screen).

Output ids are stable across reboots: `tv` (HDMI), `jack` (the 3.5 mm jack), `usb:<card>`,
and `bluetooth:<address>`, the same address as in the `bluetooth` section. A second HDMI or
analog output is `tv:2` or `jack:2`. Outputs come and go (a speaker connects); an output with
`available: false` is listed but can't be picked.

`updates` (#24) is what Settings > Updates shows; couch-update (couchd/updater.py), a root
service, does the work and couchd reads its status file:

```json
"updates": {"version": "v0.2.0", "state": "idle", "result": "up-to-date", "message": null,
            "last_check": 1794780000, "latest": "v0.2.0", "waiting": null,
            "system": {"at": 1794780300, "security": true, "full": true, "full_at": 1794780300},
            "announce": {"id": "installed:v0.2.0:1794780100", "kind": "installed",
                         "version": "v0.2.0", "kept": null}}
```

- `version`: which Couch is installed (`/opt/couch/VERSION`): a release's tag (`v1.2.0`), or
  for a git checkout `git describe` (`v1.2.0-3-gabc1234`, `dev-abc1234` before the first tag,
  `-dirty` with local changes); null for a copy without git.
- `state`: `idle`, `checking`, `downloading`, `installing`, `rolling-back` or `system`.
- `result`: the last run's: `installed`, `up-to-date`, `rolled-back` (the new release failed
  its health check and the previous install is back), `broken` (going back failed too),
  `no-access` (not set up: the Pi can't read the releases of the private repo, no token in
  `/etc/couch/github-token` or one GitHub refuses), `failed` (couldn't check: no network, a
  broken download), `waiting` (something was
  playing; `waiting` says what), or null before the first run. `message` says more.
- `announce`: an update the UI hasn't told anyone about yet. `ui/js/updates.js` shows it as
  a toast ("Updated to v0.2.0", or "Couldn't update to v0.3.0. Couch stayed on v0.2.0") the
  next time the launcher is on the TV, then sends `updates.announced`. Null after that.

**`toast`**: a short notice to show on screen.

```json
{"type": "toast", "data": {"text": "Speaker connected", "level": "info"}}
```

`level` is `info`, `warning` or `error`.

**`key`**: a remote button.

```json
{"type": "key", "data": {"key": "up", "repeat": false, "hold": false, "app": "launcher"}}
```

Key names are listed in `couchd/keys.py`: `up` `down` `left` `right` `select` `back` `home`
`play` `pause` `play_pause` `stop` `rewind` `fast_forward` `red` `green` `yellow` `blue` and
`0`–`9`. Screens react only to these messages, never to raw keyboard events.

`app` is the app the key was for: the one in front (`apps.foreground`). Keys for any other
app than the launcher were typed into that app on couchd's virtual keyboard; the launcher
ignores them, except `home`. `repeat` is true for the repeats of a held arrow key: couchd
repeats them itself, after 400 ms and then faster and faster. Back and OK are sent when
they're released. Held for 800 ms, Back is `home` instead, and OK is a long press: `select`
with `hold: true` (only once, and nothing on release).

Some keys are couchd's, wherever you are, and are never typed into apps: `home` goes back
to the launcher, `red` moves the sound to the next available output, `blue` asks for the
quick menu (below), and `green` and `yellow` are kept for later. Screens shouldn't give
`red` or `blue` another meaning. A keyboard (in dev mode, or plugged into the Pi) is turned
into `remote.press` commands by `ui/js/keys.js`, so it takes the same path through couchd as
the TV remote.

When the TV goes to standby, couchd sends a `pause` key, so whatever is playing stops: the
`pause` key pauses a Chromium app in front over DevTools (every `video` and `audio` on the
page) and Kodi over its JSON-RPC (127.0.0.1:9090, `Player.PlayPause` with `play: false`), as
well as being typed into the app.

**`quick_menu`**: the Blue button was pressed; show the quick menu (#18). `app` is the app
in front.

```json
{"type": "quick_menu", "data": {"app": "youtube"}}
```

**`volume_changed`**: couchd changed the volume or mute, for the volume panel (#18).

```json
{"type": "volume_changed", "data": {"output": "bluetooth:AA:BB:CC:DD:EE:FF", "kind": "bluetooth",
  "name": "JBL Flip 5", "volume": 35, "muted": false, "limit": 100, "source": "tv_remote"}}
```

`output`, `kind` and `name` are the current output's (as in `audio.outputs`), `volume` and
`muted` what it is now, and `limit` the `audio.volume_limit` setting. `source` says why:

- `tv_remote`: a volume button on the TV remote (sound on the jack, Bluetooth or USB). Sent
  for every press and every repeat of a held button, even when the volume is already at 0 or
  at the limit, so the panel can show that it's at the end. Show the panel for these.
- `command`: `audio.set_volume`, `audio.set_muted`, `audio.step_volume` or
  `audio.toggle_muted`, for instance from the quick menu.
- `limit`: the volume limit was lowered below the volume.

Live state `audio.volume` and `audio.muted` follow as usual. With sound on the TV there are
no `tv_remote` events: the TV's volume buttons are the TV's, and it shows its own bar.

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
| `remote.press` | `key`; `keyboard`, `hold` (bools, optional). Like a quick press of that button on the remote; `hold` makes it a long press (OK and Back only). With `keyboard: true`, the key was typed into the launcher and goes to the launcher whatever is in front |
| `remote.hold` | `key`, `seconds` (up to 15). Holds the button down that long, as if on the remote, for trying key repeat with curl |
| `tv.power_on`, `tv.standby` | `power_on` wakes the TV and switches it to Couch |
| `tv.set_standby_on_poweroff` | `on` (bool). Saved. When on, the TV goes to standby when the Pi powers off (not on a reboot) |
| `tv.simulate` | `frame`: CEC bytes in hex, e.g. `"05:44:41"` (TV to Audio System: Volume Up) and `"05:45"` (released). Handled as if the TV had sent it; replies go out on the bus for real. For testing: cec-ctl can't send to the adapter's own address. The dev-mode TV only takes the volume buttons |
| `audio.set_output` | `output` (an output id). Saved, and restored at boot if that output is around |
| `audio.cycle_output` | Next available output, as the Red button does |
| `audio.set_volume` | `volume` (0–100) |
| `audio.set_muted` | `muted` (bool) |
| `audio.step_volume` | `by` (-100 to 100). Up or down from where it is, at most the limit. Up unmutes |
| `audio.toggle_muted` | Mute or unmute |
| `bluetooth.scan` | `on` (bool). Scanning stops by itself after 2 minutes; send it again to keep going |
| `bluetooth.pair` | `address`. Pairs and trusts; an audio device is then connected too, so one press does it |
| `bluetooth.connect`, `.disconnect`, `.forget` | `address` |
| `bluetooth.set_auto_reconnect` | `on` (bool). Saved; on by default |
| `apps.launch` | `app` (an id from `apps.list`). Starts it, or brings it to the front |
| `apps.close` | `app`. Not the launcher |
| `apps.home` | Back to the launcher, from anywhere. The `home` key does the same |
| `apps.clear_error` | Dismiss `apps.error` |
| `idle.activity` | Counts as a key press for idle detection |
| `idle.now` | Go idle (the idle clock) at once. Only on the launcher. For trying it out; `COUCH_IDLE_AFTER=10` in couchd's environment shortens the wait instead |
| `ui.first_frame` | `at` (ms on the page's clock, optional). The launcher sends it once per page load, after its first frame is drawn. couchd then ends the session's boot splash, if it's still up (#23) |
| `apps.dev.kodi_exit` | Dev mode only: the pretend Kodi exits. `crashed` (bool, optional) |
| `apps.dev.crash` | Dev mode only: `app`. couchd acts as if its tab (or the pretend Kodi) crashed |
| `apps.dev.network` | Dev mode only: `internet` (bool), `plex` (`ok`, `unreachable` or `unknown`). Both optional |
| `settings.set` | `key`, `value`. Validated and saved; returns `{key: value}`. See [Settings](#settings) |
| `settings.reset` | `key`. Back to its default |
| `settings.describe` | Returns the schema: a list of `{key, type, default, label, choices \| min, max, step}` |
| `system.info` | Returns `{name, device, network}`: the name the TV shows, the Pi model, and `{kind: "wifi" \| "ethernet", name (SSID or null), address}` or null when offline |
| `updates.check` | Settings > Updates > Check now: starts `couch-update-now.service`, which installs a newer release if there is one (not while something plays) |
| `updates.announced` | `id` (from `updates.announce`). The toast was shown; saved |
| `system.restart`, `system.power_off` | Reboot or shut down the Pi, after a "Restarting" / "Turning off" toast. On a shutdown the TV goes to standby too if `tv.standby_on_poweroff` is on |

couchd keeps sound on the output picked last. When that output goes away (a speaker out of
range) sound moves to the TV, and when it comes back the sound follows. Volume is remembered
per output. Every change of output, whatever caused it, shows a "Sound now on X" toast.

When an audio device connects, sound moves to it as soon as its output shows up in `audio`
(that is an `audio.set_output`, so it is saved and toasted like one). When it disconnects
and sound was on it, the "Sound now on X" toast says where sound went; when sound was
elsewhere there is a "Headphones disconnected" toast instead. The last audio device
connected is remembered: with `auto_reconnect` on, couchd keeps trying to reach it whenever
no audio device is connected, at boot and after it drops out of range (backing off to once
a minute, and not while scanning). `bluetooth.disconnect` and `bluetooth.forget` make it stop.

### TV remote volume

With sound on the jack, Bluetooth or USB and `audio.tv_remote_volume` on, Couch is the TV's
HDMI-CEC Audio System (logical address 5) as well as its Playback device. While the TV shows
Couch, System Audio Mode is on: the TV mutes its own speakers and sends its volume buttons to
couchd, which changes the output's volume in steps of 2 (never above `audio.volume_limit`;
up unmutes), sends `volume_changed` and tells the TV the new volume (<Report Audio Status>).
A held button repeats: every press the TV sends is a step, and couchd repeats it every
150 ms after 500 ms if the TV doesn't. On another TV input, after moving sound back to the
TV, or with the setting off, the TV has its speakers and buttons back. `tv.system_audio` says
which it is. The Pi 4 holds one CEC address at a time, so there Couch's address becomes 5
for as long as sound is away from the TV. See docs/tv-sony-kdl40v55.md for the TV's side.

A command exists only while its service runs. `GET /api/commands` lists the current ones.

If a client falls more than 256 messages behind, couchd closes it with code 1013 and it
should reconnect.

## Settings

User settings are one flat map of `area.name` keys, in the live state section `settings`,
saved in `/var/lib/couch/settings.json` (atomic writes). Change one with `settings.set`; the
change comes back to every client as a `settings` patch, so a screen just renders from state.
A value of the wrong type or out of range is refused with an error and nothing changes.

| Key | Type | Default | |
| --- | --- | --- | --- |
| `display.evening` | `sunset`, `always`, `off` | `sunset` | Evening colours. *sunset* follows `evening.dark` |
| `display.motion` | `auto`, `reduced`, `full` | `auto` | Reduce motion; *auto* follows the system |
| `display.dim_after` | 60, 120, 300, 600, 900, 1800, 3600, 0 | 300 | Seconds before the launcher dims to the idle clock; 0 = never |
| `audio.volume_limit` | 10–100, steps of 5 | 100 | couchd never sets a volume above this |
| `audio.tv_remote_volume` | bool | true | TV remote's volume keys drive the jack, Bluetooth and USB (see [TV remote volume](#tv-remote-volume)) |
| `bluetooth.auto_reconnect` | bool | true | Reconnect the last audio device at start and when it comes back |
| `tv.off_when_idle` | bool | false | TV to standby after 2 hours without a key on the launcher |
| `tv.standby_on_poweroff` | bool | false | TV to standby when the Pi powers off (not on a reboot) |
| `updates.overnight` | bool | true | Install updates overnight (#24): read by couch-update's nightly run |

```json
{"type": "command", "id": 9, "name": "settings.set", "args": {"key": "display.evening", "value": "sunset"}}
{"type": "result", "id": 9, "ok": true, "data": {"display.evening": "sunset"}}
{"type": "state", "full": false, "data": {"settings": {"display.evening": "sunset"}}}
```

`bluetooth.set_auto_reconnect` and `tv.set_standby_on_poweroff` are the same as setting those
keys. To add a setting, add it to `SCHEMA` in `couchd/settings.py`; services read it with
`ctx.settings.get(key)` and follow changes with `ctx.settings.on_change(fn)`.

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

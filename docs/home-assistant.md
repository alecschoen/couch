# Home Assistant

Couch shows up in Home Assistant as a **Couch** device with two things on it:

- a **TV** switch (#97): on turns the TV on, off puts it in standby, and the switch follows
  the TV when you use the remote;
- a **TV** notifier (#98): messages from Home Assistant ("Washing machine finished") pop up
  on the TV over whatever is on screen.

It goes through MQTT, because couchd's own API only answers on the Pi itself
(`docs/protocol.md`).

## Setting it up

### 1. A broker

**Home Assistant OS or Supervised**: install the **Mosquitto broker** add-on (Settings →
Add-ons) and start it. Make a Home Assistant user for Couch (Settings → People → Users, e.g.
`couch`, with "Can only log in from the local network" on); the add-on accepts Home Assistant
users.

**Home Assistant Container** (no add-ons): run Mosquitto in its own container, next to Home
Assistant. A `mosquitto.conf` with two listeners: one without a login that only Home
Assistant, on the same machine, can reach, and one with a login for the Pi:

```
per_listener_settings true
persistence true
persistence_location /mosquitto/data/

listener 1884 127.0.0.1
allow_anonymous true

listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
```

Make Couch's login inside the container, typing the password when it asks:

```bash
docker exec -it mosquitto mosquitto_passwd -c /mosquitto/config/passwd couch
```

Then add the **MQTT** integration (Settings → Devices & services → Add integration → MQTT):
broker `127.0.0.1`, port `1884`, no username.

### 2. The password, on the Pi

It goes in its own file, readable by couchd only. Run this, type the password, press Enter,
then Ctrl+D:

```bash
sudo install -m 600 -o couch -g couch /dev/stdin /etc/couch/mqtt-password
```

### 3. The broker, on the Pi

Add this to `/etc/couch/config.toml` (start from `system/config.example.toml` if there's no
file yet):

```toml
[mqtt]
host = "homeassistant.local"   # or the broker's IP address
username = "couch"
password_file = "/etc/couch/mqtt-password"
```

Check it, then restart couchd:

```bash
/opt/couch/.venv/bin/couchd --check-config
```

```bash
sudo systemctl restart couchd
```

The **Couch** device turns up under the MQTT integration on its own, with `switch.couch_tv`
and `notify.couch_tv`. If it doesn't, look at what couchd says:

```bash
curl -s localhost:8765/api/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["mqtt"])'
```

`connected: false` with `error: not authorized` means a wrong username or password. No
`error` means the broker can't be reached: check `host` (and `port`, if it isn't 1883).
couchd keeps retrying either way, and the TV and remote work as always meanwhile.

## The switch

- **On / off**: on wakes the TV and switches it to Couch, the same as the remote's power button
  through Couch. Off puts it in standby.
- **Unknown**: when the TV doesn't answer over HDMI-CEC (unplugged, off at the wall, or CEC
  turned off in its settings) the switch shows unknown, not off.
- **Unavailable**: when the Pi is off or couchd has stopped.

Use it in automations like any switch, for example turning the TV off when everyone leaves, or
dimming the lights when it comes on.

## Messages on the TV

Add a **Send a notification message** action to an automation, on `notify.couch_tv`:

```yaml
- action: notify.send_message
  target:
    entity_id: notify.couch_tv
  data:
    message: "Washing machine finished — time to unload"
```

For more than one line of text, publish JSON to `couch/notify` yourself:

```yaml
- action: mqtt.publish
  data:
    topic: couch/notify
    payload: >-
      {"text": "Washing machine finished", "detail": "Time to unload", "icon": "check",
       "seconds": 15}
```

| Field | |
| --- | --- |
| `text` | the message, up to 120 characters (longer is cut) |
| `detail` | a second line, up to 200 characters (optional) |
| `icon` | `info` (the default), `check`, `update`, `speaker`, `bluetooth` or `muted` |
| `level` | `info` (the default), `warning` or `error` |
| `seconds` | how long it stays, 2–60, default 8 |

- While the TV is in **standby** a message isn't shown (nobody would see it, and it won't
  wait for later). Keep Telegram or your phone for those.
- On Home, a message **wakes the idle clock**, which would cover it otherwise. Over YouTube it
  shows in the page, and over Plex as Plex's own notification.
- The **same text twice within 10 s** shows once, so an automation that fires twice doesn't
  stack up.
- Don't publish with **retain**. A retained message shows once, but couchd skips it when it
  reconnects, or every reconnect would bring the last one back.

## The topics

For other MQTT tools, under `topic` (default `couch`):

| Topic | |
| --- | --- |
| `couch/status` | `online` / `offline`, retained; `offline` is also the last will |
| `couch/tv/state` | `on`, `standby` or `unknown`, retained |
| `couch/tv/set` | send `ON` or `OFF` |
| `couch/notify` | send text, or JSON as above |

Discovery goes to `homeassistant/switch/<hostname>/tv/config` and
`homeassistant/notify/<hostname>/tv/config`, and again whenever Home Assistant restarts.
Change `discovery` if your Home Assistant uses another prefix, and `topic` if you have two
Couches.

## Not yet

There's no TLS to the broker yet: keep it on your home network.

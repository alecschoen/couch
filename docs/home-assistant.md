# Home Assistant

Couch can show up in Home Assistant as a **Couch TV** switch (#97). On turns the TV on, off puts
it in standby, and the switch follows the TV when you use the remote. It goes through MQTT,
because couchd's own API only answers on the Pi itself (`docs/protocol.md`).

## Setting it up

1. **A broker.** In Home Assistant, install the **Mosquitto broker** add-on (Settings → Add-ons)
   and start it. Then add the **MQTT** integration (Settings → Devices & services), which
   usually offers itself once the add-on is running.
2. **A login for Couch.** Make a Home Assistant user for it (Settings → People → Users, e.g.
   `couch`, with "Can only log in from the local network" on). The Mosquitto add-on accepts
   Home Assistant users.
3. **The password, on the Pi.** It goes in its own file, readable by couchd only. Run this,
   type the password, press Enter, then Ctrl+D:

   ```bash
   sudo install -m 600 -o couch -g couch /dev/stdin /etc/couch/mqtt-password
   ```

4. **The broker, on the Pi.** Add this to `/etc/couch/config.toml` (start from
   `system/config.example.toml` if there's no file yet):

   ```toml
   [mqtt]
   host = "homeassistant.local"   # or Home Assistant's IP address
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

A **Couch** device with a **TV** switch then turns up under the MQTT integration on its own.
If it doesn't, look at what couchd says:

```bash
curl -s localhost:8765/api/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["mqtt"])'
```

`connected: false` with `error: not authorized` means a wrong username or password. No
`error` means the broker can't be reached: check `host` (and `port`, if it isn't 1883).
couchd keeps retrying either way, and the TV and remote work as always meanwhile.

## What it does

- **On / off**: on wakes the TV and switches it to Couch, the same as the remote's power button
  through Couch. Off puts it in standby.
- **Unknown**: when the TV doesn't answer over HDMI-CEC (unplugged, off at the wall, or CEC
  turned off in its settings) the switch shows unknown, not off.
- **Unavailable**: when the Pi is off or couchd has stopped.

Use it in automations like any switch, for example turning the TV off when everyone leaves, or
dimming the lights when it comes on.

## The topics

For other MQTT tools, under `topic` (default `couch`):

| Topic | |
| --- | --- |
| `couch/status` | `online` / `offline`, retained; `offline` is also the last will |
| `couch/tv/state` | `on`, `standby` or `unknown`, retained |
| `couch/tv/set` | send `ON` or `OFF` |

Discovery goes to `homeassistant/switch/<hostname>/tv/config`, and again whenever Home Assistant
restarts. Change `discovery` if your Home Assistant uses another prefix, and `topic` if you
have two Couches.

## Not yet

Messages from Home Assistant on the TV ("Someone's at the door"), over whatever is on screen,
are planned over the same connection (#98). There's no TLS to the broker yet: keep it on
your home network.

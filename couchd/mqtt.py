"""Home Assistant over MQTT (#97): the TV as a switch, and messages on the TV (#98).

couchd's own API answers on localhost only and has no login (couchd/api.py), so Home
Assistant doesn't talk to it. Both talk to an MQTT broker instead, usually the Mosquitto
add-on in Home Assistant. The broker is `[mqtt]` in /etc/couch/config.toml (couchd/config.py);
with no `host` there's no MQTT at all.

Topics, under `topic` (default "couch"):

    couch/status      "online" / "offline", retained. "offline" is also the last will, so
                      Home Assistant shows the switch unavailable when the Pi is off.
    couch/tv/state    "on" / "standby" / "unknown", retained: `tv.power` (couchd/tv.py).
    couch/tv/set      "ON" turns the TV on (tv.power_on), "OFF" puts it in standby
                      (tv.standby). Anything else is ignored.
    couch/notify      a message for the TV, as a toast over whatever is on screen: plain
                      text, or JSON {"text", "detail", "icon", "level", "seconds"}. Not
                      shown while the TV is in standby; retained messages and the same
                      text again within `REPEAT_S` s are dropped.

Home Assistant finds both by itself through MQTT discovery: retained configs at
`<discovery>/switch/<node>/tv/config` and `<discovery>/notify/<node>/tv/config`, which show up
as a "TV" switch and a "TV" notifier under a "Couch" device.
`<node>` is the Pi's hostname, so two Couches don't collide (they'd need different `topic`s
too). The config is sent again whenever Home Assistant says it's back (`<discovery>/status`
"online"), as the discovery docs ask. "unknown" (no answer from the TV) is unknown in Home
Assistant, not off.

The client is paho-mqtt (Debian's python3-paho-mqtt), imported only when MQTT is set up. It
runs its network loop in its own thread and reconnects by itself, waiting up to
`RECONNECT_MAX` s between tries, so a broker that's down or a wrong password never holds up
the TV: couchd just keeps trying, and says so in live state section `mqtt`:

    {"broker": "homeassistant.local:1883", "connected": false, "error": "not authorized"}
"""

import asyncio
import contextlib
import json
import logging
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from couchd import __version__
from couchd.bus import TOAST_ICONS, TOAST_LEVELS
from couchd.commands import CommandError
from couchd.config import MqttConfig
from couchd.services import Context, Service

log = logging.getLogger(__name__)

KEEPALIVE = 60
RECONNECT_MIN = 1
RECONNECT_MAX = 60
STOP_WAIT = 1.0  # how long stop() waits for "offline" to reach the broker

POWER_STATES = ("on", "standby")
NOTIFY_TEXT = 120  # characters: one line on the TV
NOTIFY_DETAIL = 200
NOTIFY_SECONDS = 8  # how long a message stays, unless it says
NOTIFY_RANGE = (2, 60)
REPEAT_S = 10.0  # the same text again this soon is an automation firing twice
TOAST_ID = "ha-notify"
# Home Assistant's MQTT switch: ON, OFF, or "None" for unknown.
VALUE_TEMPLATE = "{% if value == 'on' %}ON{% elif value == 'standby' %}OFF{% else %}None{% endif %}"


def make_client(client_id: str) -> Any:
    """A paho client. Imported here, so couchd runs without paho when MQTT isn't set up."""
    from paho.mqtt.client import CallbackAPIVersion, Client

    return Client(CallbackAPIVersion.VERSION2, client_id=client_id)


def node_id(hostname: str | None = None) -> str:
    """The Pi's hostname, as Home Assistant's discovery wants it: [a-zA-Z0-9_-]."""
    name = hostname or socket.gethostname().split(".")[0]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name) or "couch"


class MqttService(Service):
    name = "mqtt"

    def __init__(
        self,
        ctx: Context,
        config: MqttConfig,
        *,
        client_factory: Callable[[str], Any] = make_client,
        hostname: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.config = config
        self._client_factory = client_factory
        self._node = node_id(hostname)
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._power: str | None = None
        self._watch: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        t = config.topic
        self.status_topic = f"{t}/status"
        self.state_topic = f"{t}/tv/state"
        self.set_topic = f"{t}/tv/set"
        self.notify_topic = f"{t}/notify"
        self.ha_status_topic = f"{config.discovery}/status"
        self.discovery_topic = f"{config.discovery}/switch/{self._node}/tv/config"
        self.notify_discovery_topic = f"{config.discovery}/notify/{self._node}/tv/config"
        self._last_note: tuple[str, float] | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        password = self._password()
        client = self._client_factory(f"couch-{self._node}")
        if self.config.username:
            client.username_pw_set(self.config.username, password)
        client.will_set(self.status_topic, "offline", qos=1, retain=True)
        client.reconnect_delay_set(RECONNECT_MIN, RECONNECT_MAX)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        self._set_state(connected=False, error=None)
        # connect_async: the first try happens on paho's thread, so a broker that isn't up yet
        # (or a name that doesn't resolve yet, early in boot) doesn't fail the service.
        client.connect_async(self.config.host, self.config.port, keepalive=KEEPALIVE)
        client.loop_start()
        self._power = self._tv_power()
        self._watch = asyncio.create_task(self._watch_tv())
        log.info("MQTT started", extra={"broker": self._broker()})

    async def stop(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        for task in list(self._tasks):
            task.cancel()
        client, self._client = self._client, None
        if client is None:
            return
        if self._connected:
            # A clean disconnect doesn't send the last will, so say it ourselves.
            info = client.publish(self.status_topic, "offline", qos=1, retain=True)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(info.wait_for_publish, STOP_WAIT)
        with contextlib.suppress(Exception):
            client.disconnect()
        await asyncio.to_thread(client.loop_stop)
        self._connected = False

    # --- the broker (paho calls these on its own thread) ------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._call(self._connected_cb, not reason_code.is_failure, str(reason_code))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._call(self._disconnected_cb, str(reason_code))

    def _on_message(self, client, userdata, message) -> None:
        retained = bool(getattr(message, "retain", False))
        self._call(self._message_cb, message.topic, bytes(message.payload), retained)

    def _call(self, fn: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # the loop closed under us
                loop.call_soon_threadsafe(fn, *args)

    # --- back on couchd's loop ----------------------------------------------------------------

    def _connected_cb(self, ok: bool, reason: str) -> None:
        client = self._client
        if client is None:
            return
        if not ok:
            # paho tries again by itself; a wrong password stays wrong, so say it once.
            if self.ctx.state.get("mqtt").get("error") != reason.lower():
                log.error("MQTT broker refused us", extra={"reason": reason})
            self._set_state(connected=False, error=reason.lower())
            return
        log.info("MQTT connected", extra={"broker": self._broker()})
        self._connected = True
        self._set_state(connected=True, error=None)
        client.subscribe([(self.set_topic, 1), (self.notify_topic, 1), (self.ha_status_topic, 1)])
        client.publish(self.status_topic, "online", qos=1, retain=True)
        self._announce()

    def _disconnected_cb(self, reason: str) -> None:
        if self._client is None:
            return  # our own stop()
        if self._connected:
            log.warning("MQTT disconnected, reconnecting", extra={"reason": reason})
        self._connected = False
        self._set_state(connected=False)

    def _message_cb(self, topic: str, payload: bytes, retained: bool = False) -> None:
        text = payload.decode("utf-8", "replace").strip()
        if topic == self.notify_topic:
            if retained:
                # Old news: a retained message would pop up again on every reconnect.
                log.info("MQTT: skipped a retained message for the TV")
                return
            self._notify(text)
        elif topic == self.set_topic:
            command = {"ON": "tv.power_on", "OFF": "tv.standby"}.get(text.upper())
            if command is None:
                log.warning("MQTT: unknown TV command", extra={"payload": text[:40]})
                return
            log.info("MQTT: TV command", extra={"command": command})
            self._spawn(self._run(command))
        elif topic == self.ha_status_topic and text == "online":
            # Home Assistant restarted: it wants the discovery config again.
            self._announce()

    async def _run(self, command: str) -> None:
        try:
            await self.ctx.commands.run(command)
        except CommandError as e:
            log.warning("MQTT: TV command failed", extra={"command": command, "error": str(e)})

    def _notify(self, payload: str) -> None:
        note = parse_note(payload)
        if note is None:
            log.warning("MQTT: a message for the TV with no text", extra={"payload": payload[:40]})
            return
        if self._tv_power() == "standby":
            log.info("MQTT: TV in standby, message not shown", extra={"text": note["text"]})
            return
        now = asyncio.get_running_loop().time()
        if self._last_note is not None:
            last, at = self._last_note
            if last == note["text"] and now - at < REPEAT_S:
                log.info("MQTT: same message again, not shown", extra={"text": note["text"]})
                return
        self._last_note = (note["text"], now)
        log.info("MQTT: message on the TV", extra={"text": note["text"]})
        self.ctx.bus.publish("toast", {**note, "id": TOAST_ID})

    # --- what we tell Home Assistant ----------------------------------------------------------

    def _announce(self) -> None:
        """The discovery configs, then the TV's state, all retained."""
        if self._client is None or not self._connected:
            return
        for topic, config in self.discovery().items():
            self._client.publish(topic, json.dumps(config), qos=1, retain=True)
        self._publish_power()

    def discovery(self) -> dict[str, dict[str, Any]]:
        """Discovery topic → config: the TV switch and the TV notifier, on one device."""
        common = {
            "availability_topic": self.status_topic,
            "qos": 1,
            "device": {
                "identifiers": [f"couch_{self._node}"],
                "name": "Couch",
                "manufacturer": "Couch",
                "model": "Raspberry Pi 4",
                "sw_version": __version__,
            },
            "origin": {"name": "couchd", "sw_version": __version__},
        }
        return {
            self.discovery_topic: {
                "name": "TV",
                "unique_id": f"couch_{self._node}_tv",
                "icon": "mdi:television",
                "state_topic": self.state_topic,
                "command_topic": self.set_topic,
                "value_template": VALUE_TEMPLATE,
                **common,
            },
            self.notify_discovery_topic: {
                "name": "TV",
                "unique_id": f"couch_{self._node}_notify",
                "icon": "mdi:message-badge",
                "command_topic": self.notify_topic,
                **common,
            },
        }

    def _publish_power(self) -> None:
        if self._client is None or not self._connected:
            return
        power = self._power if self._power in POWER_STATES else "unknown"
        self._client.publish(self.state_topic, power, qos=1, retain=True)

    async def _watch_tv(self) -> None:
        """Follow `tv.power` off the bus. A subscription that falls behind is dropped by the
        bus (couchd/bus.py), so take a new one and read the state afresh."""
        while True:
            with self.ctx.bus.subscribe() as sub:
                self._tv_changed(self._tv_power())
                async for message in sub:
                    if message.get("type") != "state":
                        continue
                    tv = (message.get("data") or {}).get("tv")
                    if isinstance(tv, dict) and "power" in tv:
                        self._tv_changed(tv["power"])

    def _tv_changed(self, power: str | None) -> None:
        if power == self._power:
            return
        self._power = power
        self._publish_power()

    def _tv_power(self) -> str | None:
        return self.ctx.state.get("tv").get("power")

    # --- bits ---------------------------------------------------------------------------------

    def _password(self) -> str | None:
        if not self.config.password_file:
            return None
        path = Path(self.config.password_file)
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(f"can't read mqtt.password_file {path}: {e.strerror}") from e

    def _broker(self) -> str:
        return f"{self.config.host}:{self.config.port}"

    def _set_state(self, **values: Any) -> None:
        self.ctx.state.update("mqtt", {"broker": self._broker(), **values})

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def parse_note(payload: str) -> dict[str, Any] | None:
    """A message for the TV, as a toast: plain text, or JSON with `text` and maybe `detail`,
    `icon`, `level` and `seconds`. Unknown icons and levels fall back to info; text is cut to
    fit the screen. None when there's nothing to say."""
    fields: dict[str, Any] = {}
    if payload.startswith("{"):
        try:
            loaded = json.loads(payload)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            fields = loaded
    if not fields:
        fields = {"text": payload}
    text = _line(fields.get("text"), NOTIFY_TEXT)
    if not text:
        return None
    icon = fields.get("icon")
    level = fields.get("level")
    note: dict[str, Any] = {
        "text": text,
        "level": level if level in TOAST_LEVELS else "info",
        "icon": icon if icon in TOAST_ICONS else "info",
        "ms": _seconds(fields.get("seconds")) * 1000,
    }
    detail = _line(fields.get("detail"), NOTIFY_DETAIL)
    if detail:
        note["detail"] = detail
    return note


def _line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return NOTIFY_SECONDS
    low, high = NOTIFY_RANGE
    return int(min(max(value, low), high))

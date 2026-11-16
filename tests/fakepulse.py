"""A pretend pipewire-pulse server for testing PipewireAudio without PipeWire.

It answers the pactl calls the backend makes, with JSON shaped like `pactl -f json`, and
emits `pactl subscribe` lines. Like WirePlumber, when the default sink goes away it picks
another one by itself (the highest `priority`), which is not necessarily the TV.
"""

import asyncio
import contextlib
import json
from typing import Any

from couchd.pipewire import Pactl, PactlError

# Sink names as the Pi 4 shows them (seen on couch.local, Pi OS Trixie): vc4-hdmi-0 and the
# bcm2835 headphone card, which comes up through the firmware mailbox.
PI_HDMI0 = "alsa_output.platform-fef00700.hdmi.hdmi-stereo"
PI_HDMI1 = "alsa_output.platform-fef05700.hdmi.hdmi-stereo"
PI_JACK = "alsa_output.platform-fe00b840.mailbox.stereo-fallback"
USB_DAC = "alsa_output.usb-FiiO_K3-00.analog-stereo"
SPEAKER_ADDRESS = "AA:BB:CC:DD:EE:01"
SPEAKER = f"bluez_output.{SPEAKER_ADDRESS.replace(':', '_')}.1"


def sink(
    index: int,
    name: str,
    *,
    api: str | None = "alsa",
    bus: str = "platform",
    description: str = "Built-in Audio",
    port_type: str = "Analog",
    availability: str = "availability unknown",
    volume: int = 40,
    mute: bool = False,
    priority: int = 500,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    value = round(volume * 65536 / 100)
    props = {
        "device.api": api,
        "device.bus": bus,
        "device.description": description,
        "media.class": "Audio/Sink",
        "node.name": name,
        "priority.session": str(priority),
        **(extra or {}),
    }
    if api is None:
        del props["device.api"]
    return {
        "index": index,
        "name": name,
        "description": description,
        "mute": mute,
        "volume": {
            "front-left": {"value": value, "value_percent": f"{volume}%"},
            "front-right": {"value": value, "value_percent": f"{volume}%"},
        },
        "properties": props,
        "ports": [{"name": "out", "type": port_type, "availability": availability}],
        "active_port": "out",
    }


def pi_hdmi(index: int = 51, name: str = PI_HDMI0, **kw: Any) -> dict[str, Any]:
    kw.setdefault("priority", 600)
    return sink(index, name, port_type="HDMI", **kw)


def pi_jack(index: int = 52, **kw: Any) -> dict[str, Any]:
    kw.setdefault("priority", 700)  # higher than HDMI, as on the Pi: WirePlumber's pick
    return sink(index, PI_JACK, description="Built-in Audio", **kw)


def usb_dac(index: int = 53, **kw: Any) -> dict[str, Any]:
    return sink(
        index,
        USB_DAC,
        bus="usb",
        description="FiiO K3",
        port_type="Headphones",
        extra={"device.name": "alsa_card.usb-FiiO_K3-00"},
        **kw,
    )


def speaker(index: int = 60, **kw: Any) -> dict[str, Any]:
    return sink(
        index,
        SPEAKER,
        api="bluez5",
        bus="bluetooth",
        description="Kitchen Speaker",
        port_type="Speaker",
        priority=1000,
        extra={"api.bluez5.address": SPEAKER_ADDRESS},
        **kw,
    )


async def until(predicate) -> None:
    """Wait (up to a second) for `predicate()` to hold; the backend works in the background."""
    async with asyncio.timeout(1.0):
        while not predicate():  # noqa: ASYNC110 - polling state is the point here
            await asyncio.sleep(0.005)


class FakePulse(Pactl):
    def __init__(self, sinks: list[dict[str, Any]], default: str | None = None) -> None:
        super().__init__()
        self.sinks = {s["name"]: s for s in sinks}
        self.default = default if default is not None else (sinks[0]["name"] if sinks else None)
        self.streams: list[dict[str, Any]] = []
        self.calls: list[tuple[str, ...]] = []
        self.up = True
        self._subscribers: list[asyncio.Queue[str | None]] = []

    # --- what the test does to the "server" -----------------------------------------------

    def add_sink(self, s: dict[str, Any]) -> None:
        self.sinks[s["name"]] = s
        self._emit(f"Event 'new' on sink #{s['index']}")

    def remove_sink(self, name: str) -> None:
        s = self.sinks.pop(name)
        for stream in self.streams:
            if stream["sink"] == s["index"]:
                stream["sink"] = self._index(self._pick_default())
        if self.default == name:
            self.default = self._pick_default()
            self._emit("Event 'change' on server #0")
        self._emit(f"Event 'remove' on sink #{s['index']}")

    def play(self, index: int, on: str) -> None:
        self.streams.append({"index": index, "sink": self.sinks[on]["index"]})

    def stream_sinks(self) -> dict[int, str]:
        by_index = {s["index"]: n for n, s in self.sinks.items()}
        return {st["index"]: by_index.get(st["sink"]) for st in self.streams}

    def go_down(self) -> None:
        self.up = False
        self._emit(None)

    def come_up(self) -> None:
        self.up = True

    # --- Pactl ---------------------------------------------------------------------------

    async def run(self, *args: str) -> str:
        if not self.up:
            raise PactlError("Connection failure: Connection refused")
        self.calls.append(args)
        if args[:2] == ("-f", "json"):
            return json.dumps(self._query(args[2:]))
        match args:
            case ("set-default-sink", name):
                self._need(name)
                self.default = name
                self._emit("Event 'change' on server #0")
            case ("move-sink-input", index, name):
                target = self._need(name)["index"]
                stream = next(s for s in self.streams if s["index"] == int(index))
                stream["sink"] = target
                self._emit(f"Event 'change' on sink-input #{index}")
            case ("set-sink-volume", name, percent):
                volume = int(percent.rstrip("%"))
                value = round(volume * 65536 / 100)
                for channel in self._need(name)["volume"].values():
                    channel["value"] = value
                self._emit(f"Event 'change' on sink #{self.sinks[name]['index']}")
            case ("set-sink-mute", name, flag):
                self._need(name)["mute"] = flag == "1"
                self._emit(f"Event 'change' on sink #{self.sinks[name]['index']}")
            case _:
                raise AssertionError(f"unexpected pactl call {args}")
        return ""

    @contextlib.asynccontextmanager
    async def subscribe(self):
        if not self.up:
            raise PactlError("Connection failure: Connection refused")
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers.append(queue)

        async def lines():
            while (line := await queue.get()) is not None:
                yield line + "\n"

        try:
            yield lines()
        finally:
            self._subscribers.remove(queue)

    def _query(self, what: tuple[str, ...]) -> Any:
        match what:
            case ("list", "sinks"):
                return list(self.sinks.values())
            case ("info",):
                return {
                    "server_name": "PulseAudio (on PipeWire)",
                    "default_sink_name": self.default,
                }
            case ("list", "sink-inputs"):
                return [dict(s) for s in self.streams]
        raise AssertionError(f"unexpected query {what}")

    def _emit(self, line: str | None) -> None:
        for queue in self._subscribers:
            queue.put_nowait(line)

    def _need(self, name: str) -> dict[str, Any]:
        if name not in self.sinks:
            raise PactlError(f"Failure: No such entity: {name}")
        return self.sinks[name]

    def _pick_default(self) -> str | None:
        if not self.sinks:
            return None
        return max(self.sinks.values(), key=lambda s: int(s["properties"]["priority.session"]))[
            "name"
        ]

    def _index(self, name: str | None) -> int:
        return self.sinks[name]["index"] if name else -1

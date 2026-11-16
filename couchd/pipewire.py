"""The real AudioBackend: PipeWire, through its PulseAudio server (pipewire-pulse) and `pactl`.

Why pactl: it lists sinks as JSON with volumes, mute and port availability in one call,
`pactl subscribe` reports every change, and it moves playing streams. Kodi and Chromium
both play through the PulseAudio API too, so this is the same view of sound they have.

couchd is a system service, but PipeWire runs in the couch user's own session (cage on
tty1 starts it). couchd runs as that same user, so it only needs to find the socket:
`$XDG_RUNTIME_DIR/pulse/native`, i.e. `/run/user/<uid>`. couchd usually starts before the
session does, and PipeWire can restart, so the backend keeps retrying and reports no
outputs while it can't reach the server.

Outputs get stable ids, because the preferred one is saved across reboots:
`tv` (HDMI), `jack` (3.5 mm or other analog), `usb:<card>` and `bluetooth:<address>`.
A second HDMI or analog sink becomes `tv:2`, `jack:2`, numbered in sink-name order.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from couchd.backends import AudioBackend, BackendError, Output, OutputKind

log = logging.getLogger(__name__)

PACTL_TIMEOUT = 5.0
RETRY_MIN = 1.0
RETRY_MAX = 5.0
# Events come in bursts (a Bluetooth speaker connecting is a dozen); refresh once per burst.
SETTLE = 0.05

_EVENT = re.compile(r"Event '(?:new|change|remove)' on (?:sink|server|card) #")
_KIND_ORDER: dict[OutputKind, int] = {"tv": 0, "jack": 1, "usb": 2, "bluetooth": 3}
_NAMES = {"tv": "TV speakers", "jack": "Headphone jack"}
_SECOND_NAMES = {"tv": "TV {n}", "jack": "Headphone jack {n}"}


class PactlError(BackendError):
    pass


class Pactl:
    """Runs `pactl` against the PulseAudio server of the user couchd runs as."""

    def __init__(self) -> None:
        env = dict(os.environ)
        # A system service has no XDG_RUNTIME_DIR; the session's is /run/user/<uid>.
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env["LC_ALL"] = "C"  # port availability and errors come back as English text
        self.env = env

    async def run(self, *args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except OSError as e:
            raise PactlError(f"can't run pactl: {e}") from e
        try:
            async with asyncio.timeout(PACTL_TIMEOUT):
                out, err = await proc.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise PactlError(f"pactl {args[0]} timed out") from None
        if proc.returncode != 0:
            raise PactlError(err.decode(errors="replace").strip() or f"pactl {args[0]} failed")
        return out.decode()

    async def json(self, *args: str) -> Any:
        out = await self.run("-f", "json", *args)
        try:
            return json.loads(out)
        except ValueError as e:
            raise PactlError(f"pactl {args[0]} gave bad JSON: {e}") from e

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[str]]:
        """`async with pactl.subscribe() as events`: event lines until the server goes away.
        The subscription is running once the block is entered."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl",
                "subscribe",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
            )
        except OSError as e:
            raise PactlError(f"can't run pactl: {e}") from e
        assert proc.stdout is not None
        try:
            yield _lines(proc.stdout)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()


async def _lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    async for line in stream:
        yield line.decode(errors="replace")


@dataclass(frozen=True)
class Sink:
    output: Output
    name: str  # the PulseAudio/PipeWire sink name
    index: int
    volume: int
    muted: bool


def parse_sinks(raw: list[dict[str, Any]]) -> list[Sink]:
    """`pactl -f json list sinks` → our outputs, in display order. Sinks that aren't a
    real device (null sinks, filters, the dummy output) are left out."""
    found: list[tuple[OutputKind, str, dict[str, Any]]] = []
    for sink in raw:
        props = sink.get("properties") or {}
        api = props.get("device.api")
        if api == "bluez5":
            kind: OutputKind = "bluetooth"
        elif api == "alsa":
            kind = _alsa_kind(sink, props)
        else:
            continue
        found.append((kind, sink["name"], sink))

    found.sort(key=lambda f: (_KIND_ORDER[f[0]], f[1]))
    counts: dict[str, int] = {}
    sinks = []
    for kind, name, sink in found:
        props = sink.get("properties") or {}
        description = props.get("device.description") or sink.get("description") or name
        if kind == "bluetooth":
            address = props.get("api.bluez5.address") or _address_from_name(name)
            output_id, label = f"bluetooth:{address}", description
        elif kind == "usb":
            card = props.get("device.name") or name
            output_id = "usb:" + card.removeprefix("alsa_card.").removeprefix("usb-")
            label = description
        else:
            n = counts[kind] = counts.get(kind, 0) + 1
            output_id = kind if n == 1 else f"{kind}:{n}"
            label = _NAMES[kind] if n == 1 else _SECOND_NAMES[kind].format(n=n)
        output = Output(output_id, label, kind, available=_available(sink))
        sinks.append(
            Sink(
                output=output,
                name=name,
                index=int(sink["index"]),
                volume=_volume(sink),
                muted=bool(sink.get("mute")),
            )
        )
    return sinks


def _alsa_kind(sink: dict[str, Any], props: dict[str, Any]) -> OutputKind:
    if props.get("device.bus") == "usb":
        return "usb"
    port = _active_port(sink)
    if (port and port.get("type") == "HDMI") or "hdmi" in sink["name"].lower():
        return "tv"
    return "jack"


def _active_port(sink: dict[str, Any]) -> dict[str, Any] | None:
    active = sink.get("active_port")
    return next((p for p in sink.get("ports") or [] if p.get("name") == active), None)


def _available(sink: dict[str, Any]) -> bool:
    # "availability unknown" is normal (the Pi's jack has no detection): count it as there.
    port = _active_port(sink)
    return not (port and port.get("availability") == "not available")


def _volume(sink: dict[str, Any]) -> int:
    channels = [c.get("value", 0) for c in (sink.get("volume") or {}).values()]
    if not channels:
        return 0
    return min(100, round(sum(channels) / len(channels) / 65536 * 100))


def _address_from_name(name: str) -> str:
    # bluez_output.AA_BB_CC_DD_EE_FF.1 (or .a2dp-sink on older setups)
    parts = name.split(".")
    return parts[1].replace("_", ":") if len(parts) > 1 else name


class PipewireAudio(AudioBackend):
    def __init__(self, pactl: Pactl | None = None) -> None:
        super().__init__()
        self.pactl = pactl or Pactl()
        self._sinks: list[Sink] = []
        self._default: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.connected = asyncio.Event()  # tests wait on this

    # --- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        # Don't wait for PipeWire: the session that runs it may not be up yet.
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        delay = RETRY_MIN
        waiting_logged = False
        while True:
            try:
                await self._watch()
                delay = RETRY_MIN
                log.warning("lost PipeWire, reconnecting")
            except PactlError as e:
                if not waiting_logged:
                    log.info("waiting for PipeWire", extra={"error": str(e)})
                    waiting_logged = True
            except Exception:
                log.exception("audio watcher crashed, restarting it")
            self.connected.clear()
            self._set(sinks=[], default=None)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    async def _watch(self) -> None:
        """Follow the server until it goes away. Raises PactlError if it can't reach it."""
        # Subscribe before the first read, so nothing that happens in between is missed.
        async with self.pactl.subscribe() as events:
            await self.refresh()
            log.info("connected to PipeWire", extra={"outputs": [s.output.id for s in self._sinks]})
            self.connected.set()
            dirty = asyncio.Event()
            refresher = asyncio.create_task(self._refresh_on(dirty))
            try:
                async for line in events:
                    if _EVENT.match(line):
                        dirty.set()
            finally:
                refresher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresher

    async def _refresh_on(self, dirty: asyncio.Event) -> None:
        while True:
            await dirty.wait()
            await asyncio.sleep(SETTLE)
            dirty.clear()
            try:
                await self.refresh()
            except PactlError as e:
                log.warning("couldn't read audio outputs", extra={"error": str(e)})

    async def refresh(self) -> None:
        async with self._lock:
            raw = await self.pactl.json("list", "sinks")
            info = await self.pactl.json("info")
        self._set(sinks=parse_sinks(raw), default=info.get("default_sink_name"))

    def _set(self, *, sinks: list[Sink], default: str | None) -> None:
        if sinks == self._sinks and default == self._default:
            return
        self._sinks, self._default = sinks, default
        self.changed()

    # --- AudioBackend -------------------------------------------------------------------

    def outputs(self) -> list[Output]:
        return [s.output for s in self._sinks]

    def current(self) -> str | None:
        sink = self._current_sink()
        return sink.output.id if sink else None

    def volume(self) -> int:
        sink = self._current_sink()
        return sink.volume if sink else 0

    def muted(self) -> bool:
        sink = self._current_sink()
        return sink.muted if sink else False

    async def set_output(self, output_id: str) -> None:
        sink = next((s for s in self._sinks if s.output.id == output_id), None)
        if sink is None or not sink.output.available:
            raise BackendError(f"{output_id} isn't available")
        # New streams follow the default. Streams already playing would mostly follow it
        # too, but one that picked a sink itself wouldn't, so move every one explicitly.
        await self.pactl.run("set-default-sink", sink.name)
        streams = await self.pactl.json("list", "sink-inputs")
        for stream in streams:
            if stream.get("sink") == sink.index:
                continue
            try:
                await self.pactl.run("move-sink-input", str(stream["index"]), sink.name)
            except PactlError as e:  # it may have just ended
                log.debug("couldn't move a stream", extra={"error": str(e)})
        await self.refresh()

    async def set_volume(self, volume: int) -> None:
        sink = self._need_current()
        await self.pactl.run("set-sink-volume", sink.name, f"{max(0, min(100, volume))}%")
        await self.refresh()

    async def set_muted(self, muted: bool) -> None:
        sink = self._need_current()
        await self.pactl.run("set-sink-mute", sink.name, "1" if muted else "0")
        await self.refresh()

    def _current_sink(self) -> Sink | None:
        return next((s for s in self._sinks if s.name == self._default), None)

    def _need_current(self) -> Sink:
        sink = self._current_sink()
        if sink is None:
            raise BackendError("There's no sound output right now")
        return sink

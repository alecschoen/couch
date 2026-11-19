"""Couch as a speaker (#80): Spotify Connect (librespot) and AirPlay (shairport-sync).

Phones see a speaker named "Couch". Each way in is a receiver, a SpeakerBackend: the real
ones are couchd/librespot.py and couchd/shairport.py, the dev-mode one is in couchd/fakes.py.
Both play through PipeWire like everything else, so sound goes to the output couchd picked
(couchd/audio.py) and the TV remote's volume works on it.

This service:

- starts the receivers while the `speaker.enabled` setting is on, and stops them when it's off;
- publishes what's playing in the live state section `speaker` (see docs/protocol.md). When
  both receivers have something, the one that started playing last is the one shown;
- when playback starts, with `speaker.take_over` on (the default): wakes the TV if it isn't
  showing Couch and goes back to the launcher if YouTube or Plex is in front, the way Home
  does. Opening an app while music plays stops the music. Playing two things at once is
  never what anyone wants;
- when one receiver starts playing while the other is, stops the other: AirPlay is paused,
  Spotify (which can't be paused from here) is disconnected;
- takes the remote's media keys while the launcher is in front and something is playing to
  Couch: Play, Pause, Play/Pause, Stop (pause), Fast forward (next) and Rewind (previous);
- serves the cover art at `GET /api/file/speaker-cover`.

What can be controlled depends on the receiver: AirPlay can ask the iPhone or Mac to play,
pause and skip (DACP, couchd/shairport.py). librespot has no control interface at all, so
a Spotify session can only be disconnected (the receiver restarts). `controls` in the state
says what works now.

Commands: `speaker.play_pause`, `speaker.play`, `speaker.pause`, `speaker.next`,
`speaker.previous`, `speaker.disconnect`, and `speaker.event` for couch-speaker-event
(system/), which librespot runs for each of its events. Dev mode adds `speaker.dev.play` and
`speaker.dev.stop`.
"""

import asyncio
import logging
from collections.abc import Coroutine, Sequence
from typing import Any, get_args

from couchd.backends import BackendError, Playback, SpeakerBackend, SpeakerControl
from couchd.commands import CommandError, arg
from couchd.fakes import FakeSpeaker
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SECTION = "speaker"
NAME = "Couch"  # what phones show; the receivers' configs say the same
ENABLED = "speaker.enabled"  # settings (couchd/settings.py)
TAKE_OVER = "speaker.take_over"
COVER_FILE = "speaker-cover"
CONTROLS: tuple[str, ...] = get_args(SpeakerControl)
# The remote's media keys, on the launcher, while something plays to Couch.
KEYS: dict[str, SpeakerControl] = {
    "play": "play",
    "pause": "pause",
    "play_pause": "play_pause",
    "stop": "pause",
    "fast_forward": "next",
    "rewind": "previous",
}
LAUNCHER = "launcher"


class SpeakerService(Service):
    name = "speaker"

    def __init__(self, ctx: Context, backends: Sequence[SpeakerBackend]) -> None:
        self.ctx = ctx
        self.backends = {b.source: b for b in backends}
        self._current: str | None = None  # source shown: the one that started playing last
        self._playing: dict[str, bool] = dict.fromkeys(self.backends, False)
        self._receivers: dict[str, str] = {}
        self._enabled: bool | None = None  # what the receivers were last set to
        self._apply_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._unwatch = None
        self._foreground: str | None = None

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        for backend in self.backends.values():
            await backend.start()
            backend.on_change(self._sync)
        commands = self.ctx.commands
        for control in CONTROLS:
            commands.add(f"speaker.{control}", self._control_command(control))
        commands.add("speaker.disconnect", self._disconnect_command)
        commands.add("speaker.event", self._event_command)
        if any(isinstance(b, FakeSpeaker) for b in self.backends.values()):
            commands.add("speaker.dev.play", self._dev_play)
            commands.add("speaker.dev.stop", self._dev_stop)
        self.ctx.files.add(COVER_FILE, self._cover)
        self._unwatch = self.ctx.settings.on_change(self._setting_changed)
        self._foreground = self.ctx.state.get("apps").get("foreground")
        self._sync()
        self._spawn(self._apply_enabled())
        self._spawn(self._watch_bus())

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("speaker.")
        self.ctx.files.remove(COVER_FILE)
        if self._unwatch is not None:
            self._unwatch()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # The receivers keep running: restarting couchd shouldn't cut the music.
        for backend in self.backends.values():
            await backend.stop()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            log.error("speaker task failed", exc_info=e)

    # --- receivers on and off ---------------------------------------------------------------

    def _setting_changed(self, key: str, value: Any) -> None:
        if key == ENABLED:
            self._spawn(self._apply_enabled())

    async def _apply_enabled(self) -> None:
        async with self._apply_lock:
            on = bool(self.ctx.settings.get(ENABLED))
            if on == self._enabled:
                return
            self._enabled = on
            for source, backend in self.backends.items():
                if not backend.installed():
                    self._receivers[source] = "missing"
                    if on:
                        log.warning("receiver not installed", extra={"source": source})
                    continue
                try:
                    await backend.set_enabled(on)
                    self._receivers[source] = "on" if on else "off"
                except BackendError as e:
                    log.warning("receiver didn't switch", extra={"source": source, "error": str(e)})
                    self._receivers[source] = "failed"
            log.info("speaker on" if on else "speaker off", extra=dict(self._receivers))
            self._sync()

    # --- what's playing ------------------------------------------------------------------

    def _sync(self) -> None:
        started = []
        for source, backend in self.backends.items():
            playing = backend.playback().playing
            if playing and not self._playing[source]:
                started.append(source)
            self._playing[source] = playing
        for source in started:
            log.info("speaker playing", extra={"source": source})
            self._current = source
        current = self.backends.get(self._current) if self._current else None
        if current is None or not current.playback().active:
            # The one shown has finished: show another that has something, playing first.
            others = [b for b in self.backends.values() if b.playback().active]
            others.sort(key=lambda b: not b.playback().playing)
            self._current = others[0].source if others else None
        self._publish()
        if started:
            self._spawn(self._take_over(self._current))

    @property
    def current(self) -> SpeakerBackend | None:
        return self.backends.get(self._current) if self._current else None

    @property
    def playing(self) -> bool:
        current = self.current
        return current is not None and current.playback().playing

    def _publish(self) -> None:
        backend = self.current
        p = backend.playback() if backend is not None else Playback()
        cover = None
        if backend is not None and p.cover:
            cover = f"/api/file/{COVER_FILE}?v={backend.source}-{p.cover}"
        self.ctx.state.update(
            SECTION,
            {
                "name": NAME,
                "receivers": {s: self._receivers.get(s, "off") for s in self.backends},
                "source": backend.source if backend is not None else None,
                "playing": p.playing,
                "title": p.title,
                "artist": p.artist,
                "album": p.album,
                "device": p.device,
                "cover": cover,
                "duration_ms": p.duration_ms,
                "position_ms": p.position_ms,
                "position_at": p.position_at,
                "controls": [c for c in (*CONTROLS, "disconnect") if c in p.controls]
                if backend is not None
                else [],
            },
        )

    def _cover(self) -> tuple[bytes, str] | None:
        backend = self.current
        return backend.cover() if backend is not None else None

    # --- taking over ---------------------------------------------------------------------

    async def _take_over(self, source: str | None) -> None:
        """Music started on `source`: the other receiver stops, and (with the setting) the TV
        comes on and the launcher comes to the front."""
        if source is None or source != self._current:
            return  # another phone started since; its take-over does it
        for other in self.backends.values():
            if other.source != source and other.playback().playing:
                await self._quiet(other, "another phone started playing")
        if not self.ctx.settings.get(TAKE_OVER):
            return
        tv = self.ctx.state.get("tv")
        if tv and not tv.get("active"):
            log.info("music started, waking the TV")
            await self._run("tv.power_on")
        apps = self.ctx.state.get("apps")
        if (apps.get("foreground") or LAUNCHER) != LAUNCHER or apps.get("opening"):
            log.info("music started, going home", extra={"app": apps.get("foreground")})
            await self._run("apps.home")

    async def _quiet(self, backend: SpeakerBackend, why: str) -> None:
        """Stop `backend` playing: a pause where it can, else end its session."""
        log.info("stopping the music", extra={"source": backend.source, "why": why})
        try:
            if "pause" in backend.playback().controls:
                await backend.control("pause")
            else:
                await backend.disconnect()
        except BackendError as e:
            extra = {"source": backend.source, "error": str(e)}
            log.warning("couldn't stop the music", extra=extra)

    async def _run(self, command: str) -> None:
        try:
            await self.ctx.commands.run(command)
        except CommandError as e:
            log.warning("take-over step failed", extra={"command": command, "error": str(e)})

    async def _watch_bus(self) -> None:
        with self.ctx.bus.subscribe() as sub:
            async for message in sub:
                data = message.get("data")
                if not isinstance(data, dict):
                    continue
                if message.get("type") == "key":
                    await self._key(data)
                elif message.get("type") == "state" and "foreground" in data.get("apps", {}):
                    await self._foreground_changed(data["apps"]["foreground"])

    async def _foreground_changed(self, foreground: str | None) -> None:
        was, self._foreground = self._foreground, foreground
        opened = (foreground or LAUNCHER) != LAUNCHER and (was or LAUNCHER) == LAUNCHER
        current = self.current
        if opened and current is not None and self.playing and self.ctx.settings.get(TAKE_OVER):
            await self._quiet(current, f"{foreground} opened")

    async def _key(self, data: dict[str, Any]) -> None:
        control = KEYS.get(data.get("key"))
        if control is None or data.get("app") != LAUNCHER:
            return
        if data.get("raw") or data.get("overlay") or data.get("repeat") or data.get("hold"):
            return
        current = self.current
        if current is None or not current.playback().active:
            return
        if control == "pause" and not current.playback().playing:
            return
        try:
            await current.control(control)
        except BackendError as e:
            log.info("media key not passed on", extra={"key": data.get("key"), "error": str(e)})

    # --- commands --------------------------------------------------------------------------

    def _control_command(self, control: SpeakerControl):
        async def handler(args: dict[str, Any]) -> None:
            await self._require_current().control(control)

        return handler

    async def _disconnect_command(self, args: dict[str, Any]) -> None:
        await self._require_current().disconnect()

    def _require_current(self) -> SpeakerBackend:
        current = self.current
        if current is None or not current.playback().active:
            raise CommandError("Nothing is playing to Couch")
        return current

    async def _event_command(self, args: dict[str, Any]) -> None:
        source = arg(args, "source", str)
        event = arg(args, "event", str)
        env = args.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise CommandError("'env' must be an object of strings")
        backend = self.backends.get(source)
        if backend is None:
            raise CommandError(f"unknown source {source!r}")
        log.debug("receiver event", extra={"source": source, "event": event})
        backend.handle_event(event, env)

    def _fake(self, args: dict[str, Any]) -> FakeSpeaker:
        source = args.get("source", "airplay")
        backend = self.backends.get(source) if isinstance(source, str) else None
        if not isinstance(backend, FakeSpeaker):
            raise CommandError(f"no pretend receiver {source!r}")
        return backend

    async def _dev_play(self, args: dict[str, Any]) -> None:
        self._fake(args).play()

    async def _dev_stop(self, args: dict[str, Any]) -> None:
        await self._fake(args).disconnect()

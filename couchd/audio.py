"""Where the sound goes: one place that picks the output, and moves it when that changes.

The rule is simple, and re-applied whenever the backend reports a change:

1. the output the user last picked (saved across reboots), if it's around;
2. otherwise the TV;
3. otherwise whatever is playing now, or the first output there is.

So a Bluetooth speaker that drops out sends sound back to the TV, and when it comes back
the sound goes back to it. Volume is remembered per output and put back on each switch.
Every change of output, whatever caused it, gets one "Sound now on X" toast. A change the
user made is announced at once. One that just happened is announced once it has held for
ANNOUNCE_DELAY: the TV's HDMI sink vanishes for under a second whenever the TV switches to
our input, and that blip shouldn't produce two toasts. The Red button cycles through the
outputs that are available.

The real backend is `couchd/pipewire.py`; the dev-mode fake is in `couchd/fakes.py`.
"""

import asyncio
import contextlib
import logging
from dataclasses import asdict
from typing import Any

from couchd.backends import AudioBackend, BackendError, Output
from couchd.commands import CommandError, arg
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SAVED_OUTPUT = "audio.output"
SAVED_VOLUMES = "audio.volumes"
FALLBACK = "tv"
CYCLE_KEY = "red"
ANNOUNCE_DELAY = 1.5


class AudioService(Service):
    """Live state section `audio`: outputs, current output id, volume (0-100), muted."""

    name = "audio"

    def __init__(self, ctx: Context, backend: AudioBackend) -> None:
        self.ctx = ctx
        self.backend = backend
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # The output last announced with a toast. None until the first one is settled at
        # startup, which isn't announced (nobody needs "Sound now on TV" at every boot).
        self._announced: str | None = None
        self._announcing: tuple[str, asyncio.Task] | None = None  # a delayed toast
        self._volume_for: str | None = None  # output whose saved volume was last applied
        self._gave_up: tuple | None = None  # (target, outputs) a switch failed for

    async def start(self) -> None:
        await self.backend.start()
        self.backend.on_change(self._on_change)
        self._sync()
        await self.reconcile()
        commands = self.ctx.commands
        commands.add("audio.set_output", self._set_output)
        commands.add("audio.cycle_output", self._cycle_output)
        commands.add("audio.set_volume", self._set_volume)
        commands.add("audio.set_muted", self._set_muted)
        self._tasks = [
            asyncio.create_task(self._reconcile_loop()),
            asyncio.create_task(self._watch_keys()),
        ]

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("audio.")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self._cancel_announcement()
        await self.backend.stop()

    # --- keeping state and the output in line ----------------------------------------------

    def _on_change(self) -> None:
        self._sync()
        self._wake.set()

    def _sync(self) -> None:
        self.ctx.state.update(
            "audio",
            {
                "outputs": [asdict(o) for o in self.backend.outputs()],
                "current": self.backend.current(),
                "volume": self.backend.volume(),
                "muted": self.backend.muted(),
            },
        )

    async def _reconcile_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.reconcile()
            except Exception:
                log.exception("couldn't settle the audio output")

    async def reconcile(self, *, chosen: bool = False) -> None:
        """Put sound where the rule says, with that output's volume, and announce it:
        at once if the user `chosen` it, else once it has held for ANNOUNCE_DELAY."""
        async with self._lock:
            outputs = self.backend.outputs()
            if not outputs:
                self._announced = None  # PipeWire went away; its return isn't news
                self._volume_for = None
                return
            target = self._target(outputs)
            if target is not None and target != self.backend.current():
                await self._switch(target, outputs)
            current = self.backend.current()
            if current is None:
                return
            await self._apply_saved_volume(current)
            self._announce(current, now=chosen)

    def _target(self, outputs: list[Output]) -> str | None:
        available = [o.id for o in outputs if o.available]
        for candidate in (self.ctx.saved.get(SAVED_OUTPUT), FALLBACK, self.backend.current()):
            if candidate in available:
                return candidate
        return available[0] if available else None

    async def _switch(self, target: str, outputs: list[Output]) -> None:
        # If a switch didn't take, don't retry it on every change event until the set of
        # outputs changes, or one stuck sink would have us calling PipeWire in a loop.
        attempt = (target, tuple(outputs))
        if attempt == self._gave_up:
            return
        try:
            await self.backend.set_output(target)
        except BackendError as e:
            log.warning("couldn't switch output", extra={"output": target, "error": str(e)})
        if self.backend.current() == target:
            self._gave_up = None
        else:
            self._gave_up = attempt

    async def _apply_saved_volume(self, current: str) -> None:
        if current == self._volume_for:
            return
        self._volume_for = current
        saved = (self.ctx.saved.get(SAVED_VOLUMES) or {}).get(current)
        if isinstance(saved, int) and saved != self.backend.volume():
            try:
                await self.backend.set_volume(saved)
            except BackendError as e:
                log.warning("couldn't restore volume", extra={"output": current, "error": str(e)})

    def _announce(self, current: str, *, now: bool) -> None:
        if current == self._announced:
            self._cancel_announcement()  # it came back before anyone was told it left
            return
        if self._announced is None:
            self._announced = current
            return
        if now:
            self._cancel_announcement()
            self._toast(current)
        elif self._announcing is None or self._announcing[0] != current:
            self._cancel_announcement()
            self._announcing = (current, asyncio.create_task(self._announce_later(current)))

    async def _announce_later(self, output_id: str) -> None:
        await asyncio.sleep(ANNOUNCE_DELAY)
        self._announcing = None
        if self.backend.current() == output_id != self._announced:
            self._toast(output_id)

    def _cancel_announcement(self) -> None:
        if self._announcing is not None:
            self._announcing[1].cancel()
            self._announcing = None

    def _toast(self, output_id: str) -> None:
        self._announced = output_id
        output = self._output(output_id)
        if output is not None:
            self.ctx.bus.toast(f"Sound now on {output.name}")

    def _output(self, output_id: str) -> Output | None:
        return next((o for o in self.backend.outputs() if o.id == output_id), None)

    # --- the Red button ------------------------------------------------------------------

    async def _watch_keys(self) -> None:
        while True:
            with self.ctx.bus.subscribe() as sub:
                async for message in sub:
                    data = message.get("data")
                    if message.get("type") == "key" and data == {"key": CYCLE_KEY}:
                        await self._cycle_from_remote()

    async def _cycle_from_remote(self) -> None:
        try:
            await self._cycle_output({})
        except (CommandError, BackendError) as e:
            self.ctx.bus.toast(str(e), "error")

    # --- commands --------------------------------------------------------------------------

    async def _set_output(self, args: dict[str, Any]) -> None:
        await self.choose(arg(args, "output", str))

    async def choose(self, output_id: str) -> None:
        """The user picked `output_id`: remember it and move the sound there."""
        output = self._output(output_id)
        if output is None:
            raise CommandError(f"no output {output_id!r}")
        if not output.available:
            raise CommandError(f"{output.name} isn't available")
        previous = self.ctx.saved.get(SAVED_OUTPUT)
        self.ctx.saved.set(SAVED_OUTPUT, output_id)
        try:
            async with self._lock:
                self._gave_up = None
                await self.backend.set_output(output_id)
        except BackendError:
            self.ctx.saved.set(SAVED_OUTPUT, previous)
            raise
        await self.reconcile(chosen=True)

    async def _cycle_output(self, args: dict[str, Any]) -> None:
        available = [o for o in self.backend.outputs() if o.available]
        if not available:
            raise CommandError("There's no sound output right now")
        ids = [o.id for o in available]
        current = self.backend.current()
        if len(available) == 1 and current == ids[0]:
            self.ctx.bus.toast(f"Sound is on {available[0].name}, the only output")
            return
        nxt = ids[(ids.index(current) + 1) % len(ids)] if current in ids else ids[0]
        await self.choose(nxt)

    async def _set_volume(self, args: dict[str, Any]) -> None:
        volume = arg(args, "volume", int)
        if not 0 <= volume <= 100:
            raise CommandError("'volume' must be 0-100")
        await self.backend.set_volume(volume)
        current = self.backend.current()
        if current is not None:
            volumes = dict(self.ctx.saved.get(SAVED_VOLUMES) or {})
            volumes[current] = volume
            self.ctx.saved.set(SAVED_VOLUMES, volumes)

    async def _set_muted(self, args: dict[str, Any]) -> None:
        await self.backend.set_muted(arg(args, "muted", bool))

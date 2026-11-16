"""The TV remote's volume buttons, for sound that isn't on the TV (#11).

With sound on the TV, its volume buttons are the TV's own business. With sound on the jack,
Bluetooth or USB, the TV can't turn it up or down, so Couch acts as the TV's Audio System
over HDMI-CEC (couchd/cec_tv.py): the TV mutes its own speakers and sends its volume buttons
to couchd, which turns the current output up or down (at most `audio.volume_limit`) and
tells the TV the new volume. Moving sound back to the TV hands both back to it.

All of it follows the `audio.tv_remote_volume` setting; off, the TV keeps its buttons.

A button held down: the TV may or may not repeat it. Every press the TV sends is one step,
and if no new press comes within `repeat_delay` while it's still held, couchd repeats it
every `repeat_every` until the release.

The output has to stay put for `settle` seconds before couchd changes its CEC role: when the
TV powers on, its HDMI hotplug drops for a moment, the HDMI sink goes with it and sound
falls back to the jack for about a second (seen on the KDL-40V55, COUCH-9). Taking the
Audio System address for that second and giving it back is just noise on the bus.
"""

import asyncio
import contextlib
import logging

from couchd.audio import VOLUME_STEP, AudioService
from couchd.backends import BackendError, TvBackend, VolumeKey
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SETTING = "audio.tv_remote_volume"  # a setting (couchd/settings.py)
SOURCE = "tv_remote"  # in volume_changed messages


class TvVolumeService(Service):
    name = "tv_volume"

    def __init__(
        self,
        ctx: Context,
        tv: TvBackend,
        audio: AudioService,
        *,
        repeat_delay: float = 0.5,
        repeat_every: float = 0.15,
        settle: float = 2.0,
    ) -> None:
        self.ctx = ctx
        self.tv = tv
        self.audio = audio
        self.repeat_delay = repeat_delay
        self.repeat_every = repeat_every
        self.settle = settle
        self._wanted: bool | None = None  # what the TV backend was last asked for
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._repeat: asyncio.Task | None = None
        self._steps: set[asyncio.Task] = set()  # presses being applied; a release can't undo
        self._step_lock = asyncio.Lock()  # one change at a time, in the order pressed
        self._unwatch = None

    async def start(self) -> None:
        self.tv.on_volume_key(self._on_key)
        self._unwatch = self.ctx.settings.on_change(
            lambda key, _: key == SETTING and self._wake.set()
        )
        self._wake.set()
        self._tasks = [
            asyncio.create_task(self._apply_loop()),
            asyncio.create_task(self._watch_audio()),
        ]

    async def stop(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
        self._stop_repeat()
        tasks, self._tasks = [*self._tasks, *self._steps], []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._wanted:
            # Leave the TV with its own speakers and buttons.
            with contextlib.suppress(BackendError):
                await self.tv.set_system_audio(False)
            self._wanted = False

    # --- following the output --------------------------------------------------------------

    def wanted(self) -> bool:
        """Sound is somewhere the TV can't turn up, and the setting is on."""
        if not self.ctx.settings.get(SETTING):
            return False
        audio = self.ctx.state.get("audio")
        current = audio.get("current")
        kind = next((o["kind"] for o in audio.get("outputs", ()) if o["id"] == current), None)
        return kind is not None and kind != "tv"

    async def _watch_audio(self) -> None:
        while True:
            with self.ctx.bus.subscribe() as sub:
                async for message in sub:
                    if message.get("type") == "state" and "audio" in (message.get("data") or {}):
                        self._wake.set()

    async def _apply_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._apply()
            except Exception:
                log.exception("couldn't hand the TV's volume buttons over")

    async def _apply(self) -> None:
        wanted = self.wanted()
        if wanted != self._wanted and self._wanted is not None and self.settle:
            await asyncio.sleep(self.settle)
            wanted = self.wanted()
        if wanted != self._wanted:
            log.info("TV remote volume", extra={"couch": wanted})
            self._wanted = wanted
            if not wanted:
                self._stop_repeat()
            try:
                await self.tv.set_system_audio(wanted)
            except BackendError as e:
                log.warning("TV audio system change failed", extra={"error": str(e)})
        audio = self.ctx.state.get("audio")
        if isinstance(audio.get("volume"), int):
            self.tv.set_audio_status(audio["volume"], bool(audio.get("muted")))

    # --- the buttons -----------------------------------------------------------------------

    def _on_key(self, key: VolumeKey | None) -> None:
        self._stop_repeat()
        if key is None:
            return
        if not self._wanted:
            log.debug("TV volume button ignored, sound is on the TV", extra={"key": key})
            return
        step = asyncio.create_task(self._act(key))
        self._steps.add(step)
        step.add_done_callback(self._steps.discard)
        if key in ("up", "down"):
            self._repeat = asyncio.create_task(self._repeat_while_held(key))

    async def _repeat_while_held(self, key: VolumeKey) -> None:
        await asyncio.sleep(self.repeat_delay)
        while True:
            await self._act(key)
            await asyncio.sleep(self.repeat_every)

    async def _act(self, key: VolumeKey) -> None:
        async with self._step_lock:
            await self._change(key)

    async def _change(self, key: VolumeKey) -> None:
        try:
            if key == "up":
                await self.audio.step_volume(VOLUME_STEP, source=SOURCE)
            elif key == "down":
                await self.audio.step_volume(-VOLUME_STEP, source=SOURCE)
            else:
                muted = {"mute": not self.audio.backend.muted(), "mute_on": True}.get(key, False)
                await self.audio.set_muted(muted, source=SOURCE)
        except BackendError as e:
            log.warning("couldn't change the volume", extra={"key": key, "error": str(e)})
            return
        backend = self.audio.backend
        self.tv.set_audio_status(backend.volume(), backend.muted(), report=True)

    def _stop_repeat(self) -> None:
        if self._repeat is not None:
            self._repeat.cancel()
            self._repeat = None

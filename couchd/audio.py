"""Where the sound goes. The PipeWire backend lands with #10."""

import logging
from dataclasses import asdict
from typing import Any

from couchd.backends import AudioBackend
from couchd.commands import CommandError, arg
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SAVED_OUTPUT = "audio.output"


class AudioService(Service):
    """Live state section `audio`: outputs, current output id, volume (0-100), muted."""

    name = "audio"

    def __init__(self, ctx: Context, backend: AudioBackend) -> None:
        self.ctx = ctx
        self.backend = backend

    async def start(self) -> None:
        await self.backend.start()
        self.backend.on_change(self._sync)
        await self._restore()
        self._sync()
        commands = self.ctx.commands
        commands.add("audio.set_output", self._set_output)
        commands.add("audio.set_volume", self._set_volume)
        commands.add("audio.set_muted", self._set_muted)

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("audio.")
        await self.backend.stop()

    async def _restore(self) -> None:
        preferred = self.ctx.saved.get(SAVED_OUTPUT)
        if not preferred or preferred == self.backend.current():
            return
        if any(o.id == preferred and o.available for o in self.backend.outputs()):
            await self.backend.set_output(preferred)
        else:
            log.info("saved output not around, staying put", extra={"output": preferred})

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

    async def _set_output(self, args: dict[str, Any]) -> None:
        output_id = arg(args, "output", str)
        output = next((o for o in self.backend.outputs() if o.id == output_id), None)
        if output is None:
            raise CommandError(f"no output {output_id!r}")
        await self.backend.set_output(output_id)
        self.ctx.saved.set(SAVED_OUTPUT, output_id)
        self.ctx.bus.toast(f"Sound now on {output.name}")

    async def _set_volume(self, args: dict[str, Any]) -> None:
        volume = arg(args, "volume", int)
        if not 0 <= volume <= 100:
            raise CommandError("'volume' must be 0-100")
        await self.backend.set_volume(volume)

    async def _set_muted(self, args: dict[str, Any]) -> None:
        await self.backend.set_muted(arg(args, "muted", bool))

"""Plex's navigation sounds follow the launcher's (#49).

Kodi has clicks of its own: the resource.uisounds.kodi pack that ships with it, chosen by its
`lookandfeel.soundskin` setting. While Kodi is in front, couchd sets that to match
`audio.ui_sounds`: the pack for "on", and for "tv" while sound is on the TV speakers; no pack
otherwise. Kodi keeps the value in its guisettings.xml, so it starts the next time with the
sounds as they were left, and couchd corrects them once it's up.

Kodi's JSON-RPC comes up a few seconds after Kodi has the screen, so a set that fails is tried
again every `retry` seconds while Kodi is still in front.
"""

import asyncio
import contextlib
import logging

from couchd.kodi import Kodi, KodiError
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SETTING = "audio.ui_sounds"  # a setting (couchd/settings.py)
KODI_SETTING = "lookandfeel.soundskin"
PACK = "resource.uisounds.kodi"


class KodiSoundsService(Service):
    name = "kodi_sounds"

    def __init__(self, ctx: Context, kodi: Kodi, *, retry: float = 1.0) -> None:
        self.ctx = ctx
        self.kodi = kodi
        self.retry = retry
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._unwatch = None
        self._retry: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        self._unwatch = self.ctx.settings.on_change(
            lambda key, _: key == SETTING and self._wake.set()
        )
        self._wake.set()
        self._tasks = [
            asyncio.create_task(self._apply_loop()),
            asyncio.create_task(self._watch_state()),
        ]

    async def stop(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
        if self._retry is not None:
            self._retry.cancel()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def wanted(self) -> str:
        """The sound pack Kodi should have: PACK, or "" for none."""
        mode = self.ctx.settings.get(SETTING)
        if mode == "off":
            return ""
        if mode == "tv" and self.ctx.state.get("audio").get("current") != "tv":
            return ""
        return PACK

    def kodi_in_front(self) -> bool:
        apps = self.ctx.state.get("apps")
        front = apps.get("foreground")
        return any(a.get("id") == front and a.get("kind") == "kodi" for a in apps.get("list", ()))

    async def _watch_state(self) -> None:
        with self.ctx.bus.subscribe() as sub:
            async for message in sub:
                data = message.get("data")
                if message.get("type") != "state" or not isinstance(data, dict):
                    continue
                if "apps" in data or "audio" in data:
                    self._wake.set()

    async def _apply_loop(self) -> None:
        applied: str | None = None  # what this Kodi run was last given
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self.kodi_in_front():
                applied = None  # the next Kodi starts with whatever it saved
                continue
            want = self.wanted()
            if want == applied:
                continue
            try:
                await self.kodi.call(
                    "Settings.SetSettingValue", {"setting": KODI_SETTING, "value": want}
                )
            except KodiError as e:
                log.debug("couldn't set Plex's sounds yet", extra={"error": str(e)})
                self._try_again()
                continue
            applied = want
            log.info("Plex sounds", extra={"on": bool(want)})

    def _try_again(self) -> None:
        if self._retry is not None:
            self._retry.cancel()
        self._retry = asyncio.get_running_loop().call_later(self.retry, self._wake.set)

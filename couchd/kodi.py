"""Kodi's JSON-RPC, over its TCP port on 127.0.0.1 (9090), for the overlays over Plex (#18).

Kodi listens there while it runs (`services.esenabled`, set in its profile by
couchd/kodi_profile.py; loopback only, since `services.esallinterfaces` stays off). Each
call is one short connection: Kodi comes and goes with Plex, so there's nothing worth
keeping open. The TCP port also carries Kodi's own notifications to every client, so a
reply is picked out by its id.

What couchd uses:

- `GUI.ShowNotification`: toasts, in Kodi's own notification.
- `JSONRPC.NotifyAll`: hands overlay state to service.couch (system/kodi/service.couch),
  which draws the quick menu and the volume panel through skin.couch.
- `XBMC.GetInfoLabels`: asks whether service.couch is up to draw them.

`FakeKodi` is the dev-mode and test stand-in: it records what would have been sent.
"""

import asyncio
import codecs
import contextlib
import itertools
import json
import logging
import os
from typing import Any

from couchd.backends import BackendError

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 9090
CALL_TIMEOUT = 2.0
SENDER = "couch"  # NotifyAll's sender, which service.couch listens for
# Set by service.couch while it can draw overlays. Kodi's Home window is 10000.
HOST_PROPERTY = "Window(10000).Property(couch.overlay)"


class KodiError(BackendError):
    """Kodi isn't there, didn't answer in time, or said no."""


class Kodi:
    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    async def show_notification(self, title: str, message: str, image: str, ms: int) -> None:
        params = {"title": title, "message": message, "image": image, "displaytime": ms}
        await self.call("GUI.ShowNotification", params)

    async def notify(self, message: str, data: Any) -> None:
        """Tell Kodi's addons something: they get onNotification(SENDER, "Other.<message>")."""
        await self.call("JSONRPC.NotifyAll", {"sender": SENDER, "message": message, "data": data})

    async def can_host(self) -> bool:
        """Is service.couch up, and so able to draw the quick menu?"""
        try:
            labels = await self.call("XBMC.GetInfoLabels", {"labels": [HOST_PROPERTY]})
        except KodiError:
            return False
        return isinstance(labels, dict) and labels.get(HOST_PROPERTY) == "1"


class KodiRpc(Kodi):
    def __init__(self, port: int | None = None) -> None:
        self.port = (
            int(os.environ.get("COUCH_KODI_RPC_PORT", DEFAULT_PORT)) if port is None else port
        )
        self._ids = itertools.count(1)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        call_id = next(self._ids)
        request = {"jsonrpc": "2.0", "id": call_id, "method": method, "params": params or {}}
        writer = None
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                reader, writer = await asyncio.open_connection(HOST, self.port)
                writer.write(json.dumps(request).encode())
                await writer.drain()
                reply = await _read_reply(reader, call_id)
        except TimeoutError:
            raise KodiError(f"{method}: Kodi didn't answer") from None
        except (OSError, ValueError) as e:
            raise KodiError(f"{method}: {e}") from e
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        if "error" in reply:
            raise KodiError(f"{method}: {reply['error'].get('message', 'error')}")
        return reply.get("result")


async def _read_reply(reader: asyncio.StreamReader, call_id: int) -> dict[str, Any]:
    """Kodi's TCP JSON-RPC has no framing: objects follow one another. Skip notifications."""
    decoder = json.JSONDecoder()
    text = codecs.getincrementaldecoder("utf-8")(errors="replace")  # a chunk can split a char
    buffer = ""
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            raise ValueError("Kodi closed the connection")
        buffer += text.decode(chunk)
        while buffer:
            buffer = buffer.lstrip()
            try:
                obj, end = decoder.raw_decode(buffer)
            except ValueError:
                break  # incomplete; read more
            buffer = buffer[end:]
            if isinstance(obj, dict) and obj.get("id") == call_id:
                return obj


class FakeKodi(Kodi):
    """Records calls. `hosting` says whether service.couch is up; `up` whether Kodi is."""

    def __init__(self, *, hosting: bool = True) -> None:
        self.hosting = hosting
        self.up = True
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self.up:
            raise KodiError(f"{method}: Kodi isn't running")
        self.calls.append((method, params or {}))
        log.debug("fake Kodi call", extra={"method": method})
        if method == "XBMC.GetInfoLabels":
            return {label: "1" if self.hosting else "" for label in (params or {})["labels"]}
        return "OK"

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

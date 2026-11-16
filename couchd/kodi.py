"""What the overlays (#18) ask of Kodi, through its JSON-RPC (couchd/kodi_rpc.py).

- `GUI.ShowNotification`: toasts, in Kodi's own notification.
- `JSONRPC.NotifyAll`: hands overlay state to service.couch (system/kodi/service.couch),
  which draws the quick menu and the volume panel through skin.couch.
- `XBMC.GetInfoLabels`: asks whether service.couch is up to draw them.

`KodiClient` sends them over the real socket; `FakeKodi` is the dev-mode and test stand-in,
which records what would have been sent.
"""

import logging
from typing import Any

from couchd.kodi_rpc import KodiRpc, KodiRpcError

log = logging.getLogger(__name__)

SENDER = "couch"  # NotifyAll's sender, which service.couch listens for
# Set by service.couch while it can draw overlays. Kodi's Home window is 10000.
HOST_PROPERTY = "Window(10000).Property(couch.overlay)"

KodiError = KodiRpcError  # Kodi isn't there, didn't answer in time, or said no


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


class KodiClient(Kodi):
    def __init__(self, rpc: KodiRpc) -> None:
        self.rpc = rpc

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await self.rpc.call(method, params)


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

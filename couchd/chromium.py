"""Chromium over the DevTools Protocol (CDP), on 127.0.0.1 only.

The kiosk Chromium (`system/couch-browser`, or the dev window) listens for DevTools on
127.0.0.1:9222. couchd holds one browser-level WebSocket to it and uses it to open, focus
and close tabs ("targets"), and to run scripts in them: pausing a video when its app goes
to the background now, overlays later (#18).

Only loopback is ever dialled, and Chromium itself refuses DevTools WebSockets that carry
an Origin header, so web pages in the same browser (youtube.com/tv) can't use this port.

Protocol reference: https://chromedevtools.github.io/devtools-protocol/
"""

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from couchd.backends import BackendError

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 9222
CALL_TIMEOUT = 5.0
CONNECT_TIMEOUT = 3.0


def cdp_port() -> int:
    """The DevTools port: COUCH_CDP_PORT, else 9222. system/couch-browser reads the same."""
    return int(os.environ.get("COUCH_CDP_PORT", DEFAULT_PORT))


class CdpError(BackendError):
    """A DevTools call failed, timed out, or the browser isn't there."""


@dataclass(frozen=True)
class Page:
    id: str
    url: str


# Called with (target_id, crashed). crashed is False when the tab simply closed.
PageGone = Callable[[str, bool], None]


class Chromium:
    def __init__(self, port: int | None = None) -> None:
        self.port = cdp_port() if port is None else port
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._calls: dict[int, asyncio.Future] = {}
        self._closed = asyncio.Event()
        self._closed.set()
        self._page_gone: list[PageGone] = []

    @property
    def connected(self) -> bool:
        return not self._closed.is_set()

    def on_page_gone(self, listener: PageGone) -> None:
        self._page_gone.append(listener)

    async def connect(self) -> None:
        """Attach to the running browser. Raises CdpError if it isn't listening (yet)."""
        await self.close()
        self._http = aiohttp.ClientSession()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                async with self._http.get(f"http://{HOST}:{self.port}/json/version") as resp:
                    info = await resp.json(content_type=None)
                # Use only the path: whatever host the browser reports, we dial loopback.
                path = urlsplit(info["webSocketDebuggerUrl"]).path
                self._ws = await self._http.ws_connect(
                    f"ws://{HOST}:{self.port}{path}", max_msg_size=0
                )
        except (aiohttp.ClientError, OSError, TimeoutError, KeyError, TypeError, ValueError) as e:
            await self.close()
            raise CdpError(f"no browser on DevTools port {self.port}: {e}") from e
        self._closed.clear()
        self._reader = asyncio.create_task(self._read(self._ws))
        await self.call("Target.setDiscoverTargets", {"discover": True})
        log.info("attached to Chromium", extra={"port": self.port})

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        http, self._http = self._http, None
        if http is not None:
            await http.close()
        self._lost()

    # --- tabs ----------------------------------------------------------------------------

    async def pages(self) -> list[Page]:
        result = await self.call("Target.getTargets")
        return [
            Page(t["targetId"], t.get("url", ""))
            for t in result.get("targetInfos", [])
            if t.get("type") == "page"
        ]

    async def open(self, url: str, *, background: bool = False) -> str:
        """Open `url` in a new tab and return its target id. A `background` tab loads behind
        the one in front until it's activated."""
        params: dict[str, Any] = {"url": url}
        if background:
            params["background"] = True
        result = await self.call("Target.createTarget", params)
        return result["targetId"]

    async def activate(self, target_id: str) -> None:
        await self.call("Target.activateTarget", {"targetId": target_id})

    async def close_page(self, target_id: str) -> None:
        await self.call("Target.closeTarget", {"targetId": target_id})

    async def evaluate(self, target_id: str, expression: str) -> Any:
        """Run `expression` in the tab and return its (JSON) value."""
        params = {"targetId": target_id, "flatten": True}
        attached = await self.call("Target.attachToTarget", params)
        session = attached["sessionId"]
        try:
            result = await self.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
                session=session,
            )
        finally:
            with contextlib.suppress(CdpError):
                await self.call("Target.detachFromTarget", {"sessionId": session})
        if "exceptionDetails" in result:
            raise CdpError(f"script failed: {result['exceptionDetails'].get('text', '?')}")
        return result.get("result", {}).get("value")

    # --- protocol ------------------------------------------------------------------------

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None
    ) -> dict[str, Any]:
        ws = self._ws
        if ws is None or ws.closed:
            raise CdpError("not connected to Chromium")
        call_id = next(self._ids)
        message: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        future = asyncio.get_running_loop().create_future()
        self._calls[call_id] = future
        try:
            await ws.send_str(json.dumps(message))
            async with asyncio.timeout(CALL_TIMEOUT):
                return await future
        except TimeoutError:
            raise CdpError(f"{method} timed out") from None
        except (aiohttp.ClientError, ConnectionError) as e:
            raise CdpError(f"{method}: {e}") from e
        finally:
            self._calls.pop(call_id, None)

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if "id" in data:
                    self._resolve(data)
                elif "method" in data:
                    self._event(data["method"], data.get("params", {}))
        finally:
            if self._ws is ws:
                self._lost()

    def _resolve(self, data: dict[str, Any]) -> None:
        future = self._calls.get(data["id"])
        if future is None or future.done():
            return
        if "error" in data:
            future.set_exception(CdpError(data["error"].get("message", "DevTools error")))
        else:
            future.set_result(data.get("result", {}))

    def _event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Target.targetCrashed":
            self._emit_gone(params.get("targetId"), crashed=True)
        elif method == "Target.targetDestroyed":
            self._emit_gone(params.get("targetId"), crashed=False)

    def _emit_gone(self, target_id: Any, *, crashed: bool) -> None:
        if not isinstance(target_id, str):
            return
        for listener in list(self._page_gone):
            listener(target_id, crashed)

    def _lost(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for future in self._calls.values():
            if not future.done():
                future.set_exception(CdpError("lost the connection to Chromium"))
        log.info("detached from Chromium")

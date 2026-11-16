"""A pretend Chromium that speaks just enough DevTools for couchd: tabs, activation, scripts."""

import itertools
import json
from dataclasses import dataclass, field

from aiohttp import web

from couchd.apps import PAUSE_MEDIA, VISIBILITY


@dataclass
class Tab:
    id: str
    url: str
    paused: int = 0


@dataclass
class FakeChromium:
    tabs: dict[str, Tab] = field(default_factory=dict)
    active: str | None = None
    up: bool = True  # False: refuse connections, as while the session restarts
    calls: list[str] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _sockets: set = field(default_factory=set)
    _runner: web.AppRunner | None = None
    port: int = 0

    def add_tab(self, url: str, *, active: bool = True) -> str:
        tab = Tab(f"T{next(self._ids)}", url)
        self.tabs[tab.id] = tab
        if active or self.active is None:
            self.active = tab.id
        return tab.id

    def tab_for(self, prefix: str) -> Tab | None:
        return next((t for t in self.tabs.values() if t.url.startswith(prefix)), None)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/json/version", self._version)
        app.router.add_get("/devtools/browser/fake", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        await self.kill()
        if self._runner is not None:
            await self._runner.cleanup()

    async def kill(self) -> None:
        """The browser goes away: drop every connection and forget the tabs."""
        self.up = False
        self.tabs.clear()
        self.active = None
        for ws in list(self._sockets):
            await ws.close()

    async def crash(self, tab_id: str) -> None:
        await self._event("Target.targetCrashed", {"targetId": tab_id, "status": "crashed"})

    async def _version(self, request: web.Request) -> web.Response:
        if not self.up:
            raise web.HTTPServiceUnavailable()
        url = f"ws://localhost:{self.port}/devtools/browser/fake"
        return web.json_response({"Browser": "FakeChromium/1", "webSocketDebuggerUrl": url})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            async for msg in ws:
                data = json.loads(msg.data)
                reply = await self._handle(data["method"], data.get("params", {}), data)
                if "error" in reply:
                    await ws.send_json({"id": data["id"], "error": reply["error"]})
                else:
                    await ws.send_json({"id": data["id"], "result": reply})
        finally:
            self._sockets.discard(ws)
        return ws

    async def _event(self, method: str, params: dict) -> None:
        for ws in list(self._sockets):
            await ws.send_json({"method": method, "params": params})

    async def _handle(self, method: str, params: dict, raw: dict) -> dict:
        self.calls.append(method)
        tab = self.tabs.get(params.get("targetId", ""))
        match method:
            case "Target.setDiscoverTargets" | "Target.detachFromTarget":
                return {}
            case "Target.getTargets":
                infos = [
                    {"targetId": t.id, "type": "page", "url": t.url} for t in self.tabs.values()
                ]
                infos.append({"targetId": "W1", "type": "service_worker", "url": "https://x/"})
                return {"targetInfos": infos}
            case "Target.createTarget":
                return {"targetId": self.add_tab(params["url"])}
            case "Target.activateTarget" if tab:
                self.active = tab.id
                return {}
            case "Target.closeTarget" if tab:
                del self.tabs[tab.id]
                if self.active == tab.id:
                    self.active = next(iter(self.tabs), None)
                await self._event("Target.targetDestroyed", {"targetId": tab.id})
                return {"success": True}
            case "Target.attachToTarget" if tab:
                return {"sessionId": f"S-{tab.id}"}
            case "Runtime.evaluate":
                tab = self.tabs.get(raw.get("sessionId", "").removeprefix("S-"))
                if tab is None:
                    return {"error": {"code": -32000, "message": "No session"}}
                if params["expression"] == VISIBILITY:
                    visible = "visible" if self.active == tab.id else "hidden"
                    return {"result": {"type": "string", "value": visible}}
                if params["expression"] == PAUSE_MEDIA:
                    tab.paused += 1
                    return {"result": {"type": "undefined"}}
                return {
                    "result": {"type": "object"},
                    "exceptionDetails": {"text": "Uncaught ReferenceError"},
                }
        return {"error": {"code": -32000, "message": f"{method}: no such target"}}

"""HTTP + WebSocket API on 127.0.0.1. Serves the UI, a few REST endpoints and /ws.

The same Chromium that shows the launcher also loads youtube.com/tv, so the API must not
trust every page in the browser. Requests are refused unless the Host is loopback (which
stops DNS rebinding) and, when the browser sends an Origin, that is loopback too.
"""

import asyncio
import contextlib
import logging
import weakref
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from couchd import __version__
from couchd.bus import EventBus
from couchd.state import LiveState

log = logging.getLogger(__name__)

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
TOAST_LEVELS = ("info", "warning", "error")

BUS = web.AppKey("bus", EventBus)
STATE = web.AppKey("state", LiveState)
SOCKETS = web.AppKey("sockets", weakref.WeakSet)


def _hostname(host: str | None) -> str:
    if not host:
        return ""
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0]


@web.middleware
async def loopback_only(request: web.Request, handler):
    if _hostname(request.host) not in LOOPBACK:
        raise web.HTTPForbidden(text="couchd only answers on localhost")
    origin = request.headers.get("Origin")
    if origin is not None:
        scheme, _, rest = origin.partition("://")
        if scheme != "http" or _hostname(rest) not in LOOPBACK:
            log.warning("refused cross-origin request", extra={"origin": origin})
            raise web.HTTPForbidden(text="cross-origin requests are not allowed")
    return await handler(request)


async def _no_cache(request: web.Request, response: web.StreamResponse) -> None:
    # The UI is updated by replacing files; never let Chromium show a stale copy.
    response.headers.setdefault("Cache-Control", "no-cache")


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(request.app[STATE].snapshot())


async def get_health(request: web.Request) -> web.Response:
    services = request.app[STATE].get("services")
    healthy = all(status == "running" for status in services.values())
    return web.json_response(
        {"status": "ok" if healthy else "degraded", "version": __version__, "services": services}
    )


async def post_toast(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="body must be JSON") from None
    text = body.get("text") if isinstance(body, dict) else None
    level = body.get("level", "info") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise web.HTTPBadRequest(text="'text' must be a non-empty string")
    if level not in TOAST_LEVELS:
        raise web.HTTPBadRequest(text=f"'level' must be one of {', '.join(TOAST_LEVELS)}")
    request.app[BUS].toast(text.strip(), level)
    return web.Response(status=204)


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    log.info("ui connected", extra={"peer": request.remote})

    # Subscribe before taking the snapshot so no update can fall between the two.
    with request.app[BUS].subscribe() as sub:
        await ws.send_json({"type": "state", "full": True, "data": request.app[STATE].snapshot()})
        pump = asyncio.create_task(_pump(sub, ws))
        try:
            async for msg in ws:
                if msg.type is WSMsgType.TEXT:
                    await _handle_client_message(ws, msg.data)
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    log.info("ui disconnected", extra={"peer": request.remote})
    return ws


async def _pump(sub, ws: web.WebSocketResponse) -> None:
    async for message in sub:
        await ws.send_json(message)
    if sub.overflowed:
        await ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"client too slow")


async def _handle_client_message(ws: web.WebSocketResponse, data: str) -> None:
    # The UI will send commands here (switch output, pair, launch app) as those land.
    if data == "ping":
        await ws.send_str("pong")
        return
    log.debug("ignored message from ui", extra={"data": data[:200]})


async def _close_sockets(app: web.Application) -> None:
    for ws in set(app[SOCKETS]):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"couchd shutting down")


def _add_ui(app: web.Application, ui_dir: Path) -> None:
    index = ui_dir / "index.html"
    if not index.is_file():
        log.warning("no UI to serve", extra={"ui": str(ui_dir)})
        return

    async def get_index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(index)

    app.router.add_get("/", get_index)
    app.router.add_static("/", ui_dir, follow_symlinks=False)


def create_app(*, bus: EventBus, state: LiveState, ui_dir: Path) -> web.Application:
    app = web.Application(middlewares=[loopback_only])
    app[BUS] = bus
    app[STATE] = state
    app[SOCKETS] = weakref.WeakSet()
    app.on_response_prepare.append(_no_cache)
    app.on_shutdown.append(_close_sockets)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/health", get_health)
    app.router.add_post("/api/toast", post_toast)
    app.router.add_get("/ws", websocket)
    _add_ui(app, ui_dir)
    return app

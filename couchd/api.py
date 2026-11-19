"""HTTP + WebSocket API on 127.0.0.1: a few REST endpoints and /ws, for the launcher, the
overlays and curl.

The Chromium couchd starts for YouTube loads youtube.com/tv, so the API must not trust every
page in the browser. Requests are refused unless the Host is loopback (which
stops DNS rebinding) and, when the browser sends an Origin, that is loopback too.
"""

import asyncio
import json
import logging
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from couchd import __version__
from couchd.bus import TOAST_ICONS, TOAST_LEVELS, EventBus
from couchd.commands import CommandError, Commands
from couchd.state import LiveState

log = logging.getLogger(__name__)

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}

BUS = web.AppKey("bus", EventBus)
STATE = web.AppKey("state", LiveState)
COMMANDS = web.AppKey("commands", Commands)
SOCKETS = web.AppKey("sockets", weakref.WeakSet)
# Plex's pictures for Home (couchd/plex.py): a name → the file, or None for any name it
# didn't make.
PlexThumbs = Callable[[str], Path | None]
PLEX_THUMBS = web.AppKey("plex_thumbs", object)


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
    icon = body.get("icon")
    if icon is not None and icon not in TOAST_ICONS:
        raise web.HTTPBadRequest(text=f"'icon' must be one of {', '.join(TOAST_ICONS)}")
    request.app[BUS].toast(text.strip(), level, icon=icon)
    return web.Response(status=204)


async def post_command(request: web.Request) -> web.Response:
    """`POST /api/command/audio.set_output` with the args as the JSON body (or no body)."""
    args: Any = {}
    if request.can_read_body:
        try:
            args = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="body must be JSON") from None
    result = await run_command(request.app[COMMANDS], request.match_info["name"], args)
    return web.json_response(result, status=200 if result["ok"] else 400)


async def get_plex_thumb(request: web.Request) -> web.Response:
    """A picture for Home's Continue watching. Only names couchd/plex.py made are served; the
    name is never a path."""
    lookup = request.app[PLEX_THUMBS]
    path = lookup(request.match_info["name"]) if callable(lookup) else None
    if path is None:
        raise web.HTTPNotFound(text="no such picture")
    try:
        body = await asyncio.to_thread(path.read_bytes)
    except OSError:
        raise web.HTTPNotFound(text="no such picture") from None
    kind = "image/png" if body.startswith(b"\x89PNG") else "image/jpeg"
    return web.Response(body=body, content_type=kind)


async def get_commands(request: web.Request) -> web.Response:
    return web.json_response(request.app[COMMANDS].names())


async def run_command(commands: Commands, name: Any, args: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "data": await commands.run(name, args)}
    except CommandError as e:
        return {"ok": False, "error": str(e)}
    except Exception:
        log.exception("command crashed", extra={"command": name})
        return {"ok": False, "error": "something went wrong"}


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    log.info("ui connected", extra={"peer": request.remote})

    # Subscribe before taking the snapshot so no update can fall between the two.
    with request.app[BUS].subscribe() as sub:
        await ws.send_json({"type": "state", "full": True, "data": request.app[STATE].snapshot()})
        pump = asyncio.create_task(_pump(sub, ws))
        # Commands can take seconds (pairing), so each runs in its own task and the socket
        # keeps reading meanwhile.
        running: set[asyncio.Task] = set()
        try:
            async for msg in ws:
                if msg.type is WSMsgType.TEXT:
                    await _handle_client_message(request.app, ws, msg.data, running)
        finally:
            for task in (pump, *running):
                task.cancel()
            await asyncio.gather(pump, *running, return_exceptions=True)

    log.info("ui disconnected", extra={"peer": request.remote})
    return ws


async def _pump(sub, ws: web.WebSocketResponse) -> None:
    async for message in sub:
        await ws.send_json(message)
    if sub.overflowed:
        await ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"client too slow")


async def _handle_client_message(
    app: web.Application, ws: web.WebSocketResponse, data: str, running: set[asyncio.Task]
) -> None:
    if data == "ping":
        await ws.send_str("pong")
        return
    try:
        msg = json.loads(data)
    except ValueError:
        msg = None
    if not isinstance(msg, dict) or msg.get("type") != "command":
        log.debug("ignored message from ui", extra={"data": data[:200]})
        return
    task = asyncio.create_task(_reply_to_command(app, ws, msg))
    running.add(task)
    task.add_done_callback(running.discard)


async def _reply_to_command(app: web.Application, ws: web.WebSocketResponse, msg: dict) -> None:
    result = await run_command(app[COMMANDS], msg.get("name"), msg.get("args"))
    if not ws.closed:
        await ws.send_json({"type": "result", "id": msg.get("id"), **result})


async def _close_sockets(app: web.Application) -> None:
    for ws in set(app[SOCKETS]):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"couchd shutting down")


def create_app(
    *, bus: EventBus, state: LiveState, commands: Commands, plex_thumbs: PlexThumbs | None = None
) -> web.Application:
    app = web.Application(middlewares=[loopback_only])
    app[BUS] = bus
    app[STATE] = state
    app[COMMANDS] = commands
    app[PLEX_THUMBS] = plex_thumbs
    app[SOCKETS] = weakref.WeakSet()
    app.on_response_prepare.append(_no_cache)
    app.on_shutdown.append(_close_sockets)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/health", get_health)
    app.router.add_post("/api/toast", post_toast)
    app.router.add_get("/api/commands", get_commands)
    app.router.add_post("/api/command/{name}", post_command)
    app.router.add_get("/api/plex/thumb/{name}", get_plex_thumb)
    app.router.add_get("/ws", websocket)
    return app

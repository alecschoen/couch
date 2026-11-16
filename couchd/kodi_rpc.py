"""Kodi's JSON-RPC, over its local TCP socket (127.0.0.1:9090).

Kodi answers JSON-RPC there as long as "Allow programs on this system to control Kodi" is on,
which is Kodi's default. Messages are bare JSON objects with no framing, and Kodi also sends
notifications (`"method": "Player.OnPause"`, ...) on the same socket, so replies are picked
out by id.

    kodi = KodiRpc()
    await kodi.call("JSONRPC.Ping")      # "pong"
    await kodi.pause()                   # pauses whatever plays; nothing if nothing does
"""

import asyncio
import contextlib
import json
import logging
from typing import Any

from couchd.backends import BackendError

log = logging.getLogger(__name__)

HOST, PORT = "127.0.0.1", 9090
TIMEOUT = 2.0


class KodiRpcError(BackendError):
    pass


class KodiRpc:
    def __init__(self, host: str = HOST, port: int = PORT, *, timeout: float = TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._next_id = 1

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One request on a fresh connection. Raises KodiRpcError if Kodi isn't there or
        says no."""
        request_id = self._next_id
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params:
            request["params"] = params
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
                try:
                    writer.write(json.dumps(request).encode())
                    await writer.drain()
                    reply = await _read_reply(reader, request_id)
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except TimeoutError as e:
            raise KodiRpcError(f"Kodi didn't answer {method}") from e
        except OSError as e:
            raise KodiRpcError(f"can't reach Kodi: {e.strerror or e}") from e
        if "error" in reply:
            raise KodiRpcError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result")

    async def pause(self) -> int:
        """Pause every active player. Returns how many there were."""
        players = await self.call("Player.GetActivePlayers") or []
        for player in players:
            await self.call("Player.PlayPause", {"playerid": player["playerid"], "play": False})
        if players:
            log.info("paused Kodi", extra={"players": len(players)})
        return len(players)


async def _read_reply(reader: asyncio.StreamReader, request_id: int) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    buffer = ""
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            raise KodiRpcError("Kodi closed the connection")
        buffer += chunk.decode(errors="replace")
        while buffer:
            buffer = buffer.lstrip()
            try:
                message, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break  # the rest hasn't arrived yet
            buffer = buffer[end:]
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

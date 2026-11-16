"""A pretend Kodi JSON-RPC server on a local TCP port, like Kodi's own on 9090."""

import asyncio
import json


class FakeKodi:
    def __init__(self, players=(1,)) -> None:
        self.players = list(players)
        self.paused: list[int] = []
        self.calls: list[str] = []
        self.chatty = True  # send a notification before each reply, as Kodi does
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = json.JSONDecoder()
        buffer = ""
        try:
            while chunk := await reader.read(4096):
                buffer += chunk.decode()
                while buffer.strip():
                    try:
                        request, end = decoder.raw_decode(buffer.lstrip())
                    except json.JSONDecodeError:
                        break
                    buffer = buffer.lstrip()[end:]
                    # In two pieces, so the client has to put the reply back together.
                    data = self._answer(request)
                    writer.write(data[: len(data) // 2])
                    await writer.drain()
                    await asyncio.sleep(0.01)
                    writer.write(data[len(data) // 2 :])
                    await writer.drain()
        finally:
            writer.close()

    def _answer(self, request: dict) -> bytes:
        method, params = request["method"], request.get("params", {})
        self.calls.append(method)
        out = b""
        if self.chatty:
            note = {"jsonrpc": "2.0", "method": "GUI.OnScreensaverDeactivated", "params": {}}
            out += json.dumps(note).encode()
        if method == "JSONRPC.Ping":
            reply = {"result": "pong"}
        elif method == "Player.GetActivePlayers":
            reply = {"result": [{"playerid": p, "type": "video"} for p in self.players]}
        elif method == "Player.PlayPause" and params.get("play") is False:
            self.paused.append(params["playerid"])
            reply = {"result": {"speed": 0}}
        else:
            reply = {"error": {"code": -32601, "message": "Method not found."}}
        reply.update(jsonrpc="2.0", id=request["id"])
        return out + json.dumps(reply).encode()

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from couchd.api import create_app
from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.state import LiveState


@pytest.fixture
def ui_dir(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Couch</h1>")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "couch.js").write_text("export {};")
    return tmp_path


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def state(bus):
    state = LiveState(bus)
    state.update("system", {"version": "test", "mode": "dev"})
    return state


@pytest.fixture
def commands():
    return Commands()


@pytest.fixture
async def client(bus, state, commands, ui_dir):
    app = create_app(bus=bus, state=state, commands=commands, ui_dir=ui_dir)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        yield client


async def test_ws_sends_snapshot_then_live_updates(client, state, bus):
    async with client.ws_connect("/ws") as ws:
        first = await ws.receive_json(timeout=2)
        assert first == {
            "type": "state",
            "full": True,
            "data": {"system": {"version": "test", "mode": "dev"}},
        }
        state.update("audio", {"output": "jack"})
        assert await ws.receive_json(timeout=2) == {
            "type": "state",
            "full": False,
            "data": {"audio": {"output": "jack"}},
        }
        bus.publish("key", {"key": "up"})
        assert await ws.receive_json(timeout=2) == {"type": "key", "data": {"key": "up"}}


async def test_ws_ping(client):
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json(timeout=2)
        await ws.send_str("ping")
        assert await ws.receive_str(timeout=2) == "pong"


async def test_ws_unsubscribes_on_disconnect(client, bus):
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json(timeout=2)
        assert bus.subscriber_count == 1
    for _ in range(50):
        if bus.subscriber_count == 0:
            break
        await asyncio.sleep(0.01)
    assert bus.subscriber_count == 0


async def test_toast_endpoint_reaches_the_ui(client):
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json(timeout=2)
        resp = await client.post("/api/toast", json={"text": " Hi ", "level": "warning"})
        assert resp.status == 204
        assert await ws.receive_json(timeout=2) == {
            "type": "toast",
            "data": {"text": "Hi", "level": "warning"},
        }


@pytest.mark.parametrize(
    "body", [{}, {"text": ""}, {"text": 5}, {"text": "x", "level": "loud"}, ["text"]]
)
async def test_toast_rejects_bad_bodies(client, body):
    resp = await client.post("/api/toast", json=body)
    assert resp.status == 400


async def test_state_and_health(client, state):
    resp = await client.get("/api/state")
    assert await resp.json() == {"system": {"version": "test", "mode": "dev"}}

    resp = await client.get("/api/health")
    assert (await resp.json())["status"] == "ok"

    state.update("services", {"cec": "running", "bluetooth": "failed"})
    body = await (await client.get("/api/health")).json()
    assert body["status"] == "degraded"
    assert body["services"] == {"cec": "running", "bluetooth": "failed"}


async def test_serves_ui_without_caching(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "Couch" in await resp.text()
    assert resp.headers["Cache-Control"] == "no-cache"
    resp = await client.get("/js/couch.js")
    assert resp.status == 200


async def test_static_does_not_escape_ui_dir(client):
    resp = await client.get("/../config.toml")
    assert resp.status in (403, 404)


@pytest.mark.parametrize(
    "origin", ["https://www.youtube.com", "http://evil.example", "https://127.0.0.1:8765"]
)
async def test_foreign_origins_refused(client, origin):
    resp = await client.post("/api/toast", json={"text": "hi"}, headers={"Origin": origin})
    assert resp.status == 403
    resp = await client.get("/ws", headers={"Origin": origin})
    assert resp.status == 403


async def test_loopback_origin_allowed(client):
    port = client.server.port
    resp = await client.get("/api/state", headers={"Origin": f"http://localhost:{port}"})
    assert resp.status == 200


async def test_foreign_host_refused(client):
    # DNS rebinding: an attacker domain resolving to 127.0.0.1.
    resp = await client.get("/api/state", headers={"Host": "attacker.example:8765"})
    assert resp.status == 403


async def echo(args):
    if args.get("fail"):
        from couchd.backends import BackendError

        raise BackendError("speaker said no")
    await asyncio.sleep(args.get("delay", 0))
    return {"echo": args}


async def test_command_over_rest(client, commands):
    commands.add("test.echo", echo)
    resp = await client.post("/api/command/test.echo", json={"x": 1})
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "data": {"echo": {"x": 1}}}

    resp = await client.post("/api/command/test.echo")  # no body = no args
    assert (await resp.json())["data"] == {"echo": {}}

    resp = await client.post("/api/command/test.echo", json={"fail": True})
    assert resp.status == 400
    assert await resp.json() == {"ok": False, "error": "speaker said no"}

    resp = await client.post("/api/command/nope")
    assert resp.status == 400

    assert await (await client.get("/api/commands")).json() == ["test.echo"]


async def test_commands_over_ws_run_concurrently(client, commands):
    commands.add("test.echo", echo)
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json(timeout=2)
        slow = {"type": "command", "id": 1, "name": "test.echo", "args": {"delay": 0.2}}
        fast = {"type": "command", "id": 2, "name": "test.echo", "args": {}}
        await ws.send_json(slow)
        await ws.send_json(fast)
        first = await ws.receive_json(timeout=2)
        second = await ws.receive_json(timeout=2)
    assert (first["id"], second["id"]) == (2, 1)
    assert second == {"type": "result", "id": 1, "ok": True, "data": {"echo": {"delay": 0.2}}}


async def test_ws_command_errors(client):
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json(timeout=2)
        await ws.send_json({"type": "command", "id": "a", "name": "nope"})
        reply = await ws.receive_json(timeout=2)
    assert reply == {"type": "result", "id": "a", "ok": False, "error": "unknown command 'nope'"}

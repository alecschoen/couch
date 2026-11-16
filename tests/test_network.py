"""Internet and Plex server checks (couchd/network.py)."""

import pytest
from aiohttp import web

from couchd import network
from couchd.network import NetConnectivity, parse_gdm
from couchd.state import SavedState

GDM_REPLY = (
    b"HTTP/1.0 200 OK\r\nContent-Type: plex/media-server\r\nName: Den\r\nPort: 32400\r\n"
    b"Resource-Identifier: abc\r\n\r\n"
)


def test_parse_gdm():
    assert parse_gdm(GDM_REPLY, "192.168.1.20") == "192.168.1.20:32400"
    assert parse_gdm(GDM_REPLY.replace(b"32400", b"32401"), "10.0.0.2") == "10.0.0.2:32401"
    assert parse_gdm(b"HTTP/1.0 404 Not Found\r\n\r\n", "10.0.0.2") is None
    player = GDM_REPLY.replace(b"plex/media-server", b"plex/media-player")
    assert parse_gdm(player, "10.0.0.2") is None
    assert parse_gdm(b"\xff\xfe garbage", "10.0.0.2") is None


@pytest.fixture
async def plex_server():
    app = web.Application()

    async def identity(request):
        return web.Response(text="<MediaContainer/>")

    app.router.add_get("/identity", identity)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield f"127.0.0.1:{runner.addresses[0][1]}"
    await runner.cleanup()


@pytest.fixture
def saved(tmp_path):
    return SavedState(tmp_path / "state.json")


def discovers(monkeypatch, servers):
    async def fake_discover(wait=0):
        return list(servers)

    monkeypatch.setattr(network, "discover", fake_discover)


async def test_the_saved_server_answers(saved, plex_server, monkeypatch):
    discovers(monkeypatch, [])
    saved.set("plex.server", plex_server)
    assert await NetConnectivity(saved).plex() == "ok"


async def test_a_server_found_on_the_lan_is_remembered(saved, monkeypatch):
    discovers(monkeypatch, ["192.168.1.20:32400"])
    saved.set("plex.server", "127.0.0.1:9")  # old address, nothing there now
    assert await NetConnectivity(saved).plex() == "ok"
    assert saved.get("plex.server") == "192.168.1.20:32400"


async def test_a_known_server_gone_is_unreachable(saved, monkeypatch):
    discovers(monkeypatch, [])
    saved.set("plex.server", "127.0.0.1:9")
    assert await NetConnectivity(saved).plex() == "unreachable"


async def test_never_seen_a_server_is_unknown(saved, monkeypatch):
    discovers(monkeypatch, [])
    assert await NetConnectivity(saved).plex() == "unknown"
    assert saved.get("plex.server") is None


async def test_internet(saved, plex_server, monkeypatch):
    host, port = plex_server.split(":")
    monkeypatch.setattr(network, "INTERNET_HOST", (host, int(port)))
    assert await NetConnectivity(saved).internet()
    monkeypatch.setattr(network, "INTERNET_HOST", ("127.0.0.1", 9))
    assert not await NetConnectivity(saved).internet()


async def test_discover_gives_up_quietly():
    assert isinstance(await network.discover(wait=0.05), list)  # whatever's on this LAN

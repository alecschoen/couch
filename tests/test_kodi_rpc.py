import pytest

from couchd.kodi_rpc import KodiRpc, KodiRpcError
from tests.fake_kodi import FakeKodi


@pytest.fixture
async def kodi():
    fake = FakeKodi()
    await fake.start()
    yield fake
    await fake.stop()


async def test_call_skips_notifications_and_returns_the_result(kodi):
    assert await KodiRpc(port=kodi.port).call("JSONRPC.Ping") == "pong"


async def test_pause_pauses_every_active_player(kodi):
    kodi.players = [0, 1]
    assert await KodiRpc(port=kodi.port).pause() == 2
    assert kodi.paused == [0, 1]


async def test_pause_with_nothing_playing(kodi):
    kodi.players = []
    assert await KodiRpc(port=kodi.port).pause() == 0
    assert kodi.calls == ["Player.GetActivePlayers"]


async def test_errors_from_kodi(kodi):
    with pytest.raises(KodiRpcError, match="Method not found"):
        await KodiRpc(port=kodi.port).call("Nope.Nothing")


async def test_kodi_not_running(kodi):
    port = kodi.port
    await kodi.stop()  # the fixture stops it again, which is harmless
    with pytest.raises(KodiRpcError, match="can't reach Kodi"):
        await KodiRpc(port=port).call("JSONRPC.Ping")

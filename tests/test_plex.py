"""Plex's Continue watching (#86): Couch's PIN sign-in, its token, the server, the list in
words, the refresh, the pictures and the endpoint that serves them. Nothing here reaches
plex.tv or a Plex server: a local aiohttp server plays both."""

import asyncio
import json
import logging
import stat

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from couchd.api import create_app
from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.plex import (
    EMPTY,
    FAKE_ITEMS,
    THUMB_H,
    THUMB_W,
    FakePlexApi,
    NotFound,
    Pin,
    PlexApi,
    PlexError,
    PlexService,
    Thumbs,
    TokenStore,
    Unauthorized,
    connection_uris,
    continue_list,
    describe,
    headers,
    metadata,
    open_in_pm4k,
    pick_servers,
    time_left,
)
from couchd.services import Context
from couchd.settings import Settings
from couchd.state import LiveState, SavedState

TOKEN = "s3cret-account-token"
SERVER_TOKEN = "s3cret-server-token"
MIN = 60_000


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    state = LiveState(bus)
    return Context(bus, state, SavedState(tmp_path / "state.json"), Commands(), Settings(state))


async def until(check, within: float = 2.0) -> None:
    for _ in range(int(within / 0.005)):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("never happened")


def episode(key="1", **kw) -> dict:
    m = {"ratingKey": key, "type": "episode", "grandparentTitle": "The Long Field",
         "title": "Night Shift", "parentIndex": 2, "index": 5, "duration": 47 * MIN,
         "viewOffset": 29 * MIN + 30_000, "thumb": f"/library/metadata/{key}/thumb/9"}  # fmt: skip
    return {**m, **kw}


def movie(key="2", **kw) -> dict:
    m = {"ratingKey": key, "type": "movie", "title": "Harbour Lights", "year": 2021,
         "duration": 110 * MIN, "viewOffset": 38 * MIN, "art": f"/library/metadata/{key}/art/3",
         "thumb": f"/library/metadata/{key}/thumb/3"}  # fmt: skip
    return {**m, **kw}


# --- in words ------------------------------------------------------------------------------


def test_time_left():
    assert time_left(47 * MIN, 29 * MIN + 30_000) == "18 min left"
    assert time_left(110 * MIN, 38 * MIN) == "1 h 12 min left"
    assert time_left(150 * MIN, 30 * MIN) == "2 h left"
    assert time_left(47 * MIN, 47 * MIN - 5000) == "1 min left"  # never "0 min"
    assert time_left(47 * MIN, None) is None
    assert time_left(47 * MIN, 0) is None
    assert time_left(None, 5 * MIN) is None


def test_an_episode_half_watched():
    item = describe(episode())
    assert item == {
        "key": "1",
        "title": "The Long Field",
        "subtitle": "S2 · E5 · 18 min left",
        "detail": "Season 2 · Episode 5 · Night Shift · 18 min left",
        "progress": 0.628,
        "art": "/library/metadata/1/thumb/9",
    }


def test_the_next_episode():
    item = describe(episode(viewOffset=None, parentIndex=1, index=3, title="Signal Box"))
    assert item["subtitle"] == "S1 · E3 · Next episode"
    assert item["detail"] == "Season 1 · Episode 3 · Signal Box · Next episode"
    assert item["progress"] == 0.0


def test_an_episode_with_bits_missing():
    item = describe(episode(parentIndex=None, index=None, thumb=None, grandparentArt="/g/art"))
    assert item["subtitle"] == "18 min left"
    assert item["detail"] == "Night Shift · 18 min left"
    assert item["art"] == "/g/art"
    # No show title: the episode's own, and not twice.
    item = describe(episode(grandparentTitle=None))
    assert item["title"] == "Night Shift"
    assert item["detail"] == "Season 2 · Episode 5 · 18 min left"


def test_a_film():
    item = describe(movie())
    assert item["title"] == "Harbour Lights"
    assert item["subtitle"] == "Film · 1 h 12 min left"
    assert item["detail"] == "Film · 2021 · 1 h 12 min left"
    assert item["art"] == "/library/metadata/2/art/3"  # the backdrop: 16:9, not the poster
    assert item["progress"] == pytest.approx(38 / 110, abs=0.001)
    assert describe(movie(viewOffset=None, year=None))["subtitle"] == "Film"
    assert describe(movie(art=None))["art"] == "/library/metadata/2/thumb/3"


@pytest.mark.parametrize(
    "m",
    [
        {"ratingKey": "9", "type": "track", "title": "A song"},
        {"ratingKey": "9", "type": "clip", "title": "A trailer"},
        {"type": "movie", "title": "No key"},
        {"ratingKey": "../../etc", "type": "movie", "title": "A bad key"},
        {"ratingKey": True, "type": "movie", "title": "Not a key"},
    ],
)
def test_only_films_and_episodes_with_a_key(m):
    assert describe(m) is None


def test_art_must_be_a_path_on_the_server():
    assert describe(movie(art="https://elsewhere.example/x.jpg", thumb=None))["art"] is None


def test_both_shapes_of_answer():
    flat = {"MediaContainer": {"Metadata": [episode("1"), movie("2")]}}
    hubs = {"MediaContainer": {"Hub": [{"Metadata": [episode("1")]}, {"Metadata": [movie("2")]}]}}
    assert [m["ratingKey"] for m in metadata(flat)] == ["1", "2"]
    assert [m["ratingKey"] for m in metadata(hubs)] == ["1", "2"]
    assert metadata({"MediaContainer": {"size": 0}}) == []
    for broken in ({}, {"MediaContainer": []}, {"MediaContainer": {"Metadata": {}}}, None):
        with pytest.raises(ValueError):
            metadata(broken)


def test_the_list_is_five_once_each():
    items = [episode(str(i)) for i in range(8)]
    items.insert(1, episode("0"))  # twice, from two hubs
    items.insert(2, {"ratingKey": "99", "type": "track"})
    data = {"MediaContainer": {"Metadata": items}}
    assert [i["key"] for i in continue_list(data)] == ["0", "1", "2", "3", "4"]


def test_the_dev_items_say_what_the_design_says():
    items = continue_list({"MediaContainer": {"Metadata": FAKE_ITEMS}})
    assert [i["subtitle"] for i in items] == [
        "S2 · E5 · 18 min left",
        "Film · 1 h 12 min left",
        "S1 · E3 · Next episode",
        "S4 · E1 · 9 min left",
        "Film · 48 min left",
    ]
    assert items[0]["detail"] == "Season 2 · Episode 5 · Night Shift · 18 min left"


# --- servers -------------------------------------------------------------------------------


LAN = {"uri": "https://lan.plex.direct:32400", "address": "10.0.0.2", "port": 32400, "local": True}


def resource(name, *, owned=True, provides="server", presence=True, connections=None, **kw):
    return {
        "name": name,
        "provides": provides,
        "owned": owned,
        "presence": presence,
        "clientIdentifier": f"id-{name}",
        "accessToken": SERVER_TOKEN,
        "connections": connections if connections is not None else [LAN],
        **kw,
    }


def test_connections_local_first_relay_last():
    conns = [
        {"uri": "https://relay.plex.direct:8443", "relay": True, "local": False},
        {"uri": "https://1-2-3-4.x.plex.direct:32400", "address": "1.2.3.4", "port": 32400,
         "local": False},
        {"uri": "https://10-0-0-2.x.plex.direct:32400", "address": "10.0.0.2", "port": 32400,
         "local": True},
    ]  # fmt: skip
    assert connection_uris(conns) == (
        "https://10-0-0-2.x.plex.direct:32400",
        "http://10.0.0.2:32400",  # for a router that won't resolve plex.direct
        "https://1-2-3-4.x.plex.direct:32400",
        "https://relay.plex.direct:8443",
    )


def test_the_address_found_on_the_lan_goes_first():
    conns = [
        {"uri": "https://a.plex.direct:32400", "address": "10.0.0.2", "port": 32400, "local": True},
        {"uri": "https://b.plex.direct:32400", "address": "10.0.0.9", "port": 32400, "local": True},
        {"uri": "http://[fe80::1]:32400", "address": "fe80::1", "port": 32400, "local": True},
    ]
    uris = connection_uris(conns, known="10.0.0.9:32400")
    assert uris[:2] == ("https://b.plex.direct:32400", "http://10.0.0.9:32400")
    assert "http://[fe80::1]:32400" in uris
    assert connection_uris(None) == ()
    assert connection_uris([{"uri": "ftp://x"}, "junk"]) == ()


def test_servers_owned_and_online_first():
    resources = [
        resource("Friends", owned=False),
        resource("Phone", provides="client,player"),
        resource("Attic", presence=False),
        resource("Living room"),
        resource("Broken", connections=[]),
        {**resource("No token"), "accessToken": None},
        "junk",
    ]
    servers = pick_servers(resources)
    assert [s.name for s in servers] == ["Living room", "Attic", "Friends"]
    assert servers[0].id == "id-Living room"
    assert SERVER_TOKEN not in repr(servers[0])  # never in a log line by accident


def test_requests_name_couch_and_carry_the_token_in_a_header():
    h = headers("client-1", TOKEN)
    assert h["X-Plex-Product"] == "Couch"
    assert h["X-Plex-Client-Identifier"] == "client-1"
    assert h["X-Plex-Token"] == TOKEN
    assert "X-Plex-Token" not in headers("client-1")


# --- a pretend plex.tv and server, over HTTP -----------------------------------------------


DEAD = {"uri": "http://127.0.0.1:9", "address": "127.0.0.1", "port": 9, "local": True}


class FakePlex:
    """plex.tv's PIN and resources, and a server's identity, hubs and photo transcoder, on
    127.0.0.1. Notes what it was asked."""

    def __init__(self) -> None:
        self.base = ""
        self.requests: list[tuple[str, str, dict, dict]] = []
        self.polls_until_linked = 2
        self.pin_gone = False
        self.account_ok = True
        self.hubs = True  # an older server has no /hubs/continueWatching
        self.items = [episode("1"), movie("2"), episode("3", viewOffset=None, thumb=None)]
        self.fail_server = False

    def note(self, request: web.Request) -> None:
        self.requests.append(
            (request.method, request.path, dict(request.query), dict(request.headers))
        )

    def authorised(self, request: web.Request, token: str) -> None:
        if request.headers.get("X-Plex-Token") != token:
            raise web.HTTPUnauthorized()

    async def create_pin(self, request: web.Request) -> web.Response:
        self.note(request)
        return web.json_response({"id": 77, "code": "K7QX", "expiresIn": 900, "authToken": None})

    async def pin(self, request: web.Request) -> web.Response:
        self.note(request)
        if self.pin_gone or request.match_info["id"] != "77":
            raise web.HTTPNotFound()
        self.polls_until_linked -= 1
        token = TOKEN if self.polls_until_linked <= 0 else None
        return web.json_response({"id": 77, "code": "K7QX", "authToken": token})

    async def resources(self, request: web.Request) -> web.Response:
        self.note(request)
        if not self.account_ok:
            raise web.HTTPUnauthorized()
        self.authorised(request, TOKEN)
        port = request.url.port
        return web.json_response(
            [
                resource("Phone", provides="player"),
                resource(
                    "Living room",
                    # The first is dead: nothing listens on port 9. Probed, skipped.
                    connections=[DEAD, {"uri": f"http://127.0.0.1:{port}", "local": False}],
                ),
            ]
        )

    async def identity(self, request: web.Request) -> web.Response:
        self.note(request)
        self.authorised(request, SERVER_TOKEN)
        return web.json_response({"MediaContainer": {"machineIdentifier": "id-Living room"}})

    async def continue_watching(self, request: web.Request) -> web.Response:
        self.note(request)
        self.authorised(request, SERVER_TOKEN)
        if not self.hubs:
            raise web.HTTPNotFound()
        if self.fail_server:
            raise web.HTTPServiceUnavailable(text=f"echoing {request.headers.get('X-Plex-Token')}")
        return web.json_response({"MediaContainer": {"Hub": [{"Metadata": self.items}]}})

    async def on_deck(self, request: web.Request) -> web.Response:
        self.note(request)
        self.authorised(request, SERVER_TOKEN)
        return web.json_response({"MediaContainer": {"Metadata": self.items[:1]}})

    async def transcode(self, request: web.Request) -> web.Response:
        self.note(request)
        self.authorised(request, SERVER_TOKEN)
        if "/art/" in request.query["url"]:
            raise web.HTTPInternalServerError()  # a picture that won't come
        return web.Response(body=b"\xff\xd8jpeg " + request.query["url"].encode())

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/v2/pins", self.create_pin)
        app.router.add_get("/api/v2/pins/{id}", self.pin)
        app.router.add_get("/api/v2/resources", self.resources)
        app.router.add_get("/identity", self.identity)
        app.router.add_get("/hubs/continueWatching", self.continue_watching)
        app.router.add_get("/library/onDeck", self.on_deck)
        app.router.add_get("/photo/:/transcode", self.transcode)
        return app

    def asked(self, path: str) -> list[tuple[str, str, dict, dict]]:
        return [r for r in self.requests if r[1] == path]


@pytest.fixture
async def plex():
    fake = FakePlex()
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield fake
    await runner.cleanup()


class Launches:
    def __init__(self) -> None:
        self.apps: list[str] = []

    async def __call__(self, app_id: str) -> None:
        self.apps.append(app_id)


def service_for(ctx, plex, tmp_path, **kw) -> PlexService:
    return PlexService(
        ctx, api=PlexApi(plex.base), cache_dir=tmp_path / "thumbs", pin_poll=0.01, **kw
    )


async def test_linking_with_a_pin(ctx, plex, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    with ctx.bus.subscribe() as sub:
        try:
            assert ctx.state.get("plex")["linked"] is False
            result = await ctx.commands.run("plex.link")
            assert result == {"code": "K7QX"}
            link = ctx.state.get("plex")["link"]
            assert link["code"] == "K7QX" and link["expires_at"] > 0
            await until(lambda: ctx.state.get("plex")["linked"])
            await until(lambda: ctx.state.get("plex")["continue"])
            assert ctx.state.get("plex")["link"] is None
            assert ctx.state.get("plex")["server"] == "Living room"
        finally:
            await service.stop()
        toasts = []
        while (m := sub._queue.get_nowait() if not sub._queue.empty() else None) is not None:
            if m["type"] == "toast":
                toasts.append(m["data"]["text"])
        assert toasts == ["Plex linked"]

    # The PIN: a short one, from Couch, which then polls it with the same identifier.
    method, _, query, head = plex.asked("/api/v2/pins")[0]
    assert method == "POST" and query == {"strong": "false"}
    assert head["X-Plex-Product"] == "Couch" and "X-Plex-Token" not in head
    client_id = head["X-Plex-Client-Identifier"]
    assert ctx.saved.get("plex.client_id") == client_id
    assert len(plex.asked("/api/v2/pins/77")) == 2
    assert {r[3]["X-Plex-Client-Identifier"] for r in plex.requests} == {client_id}
    _, _, query, _ = plex.asked("/api/v2/resources")[0]
    assert query["includeHttps"] == "1"

    # The token: in its file, 0600, and nowhere else.
    token_file = tmp_path / "plex-token"
    assert token_file.read_text() == TOKEN
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    everywhere = json.dumps(ctx.state.snapshot()) + caplog.text + json.dumps(result)
    assert TOKEN not in everywhere and SERVER_TOKEN not in everywhere
    ctx.saved.flush()
    assert TOKEN not in (tmp_path / "state.json").read_text()
    assert all(TOKEN not in str(r[2]) and SERVER_TOKEN not in str(r[2]) for r in plex.requests)


async def test_a_new_couch_keeps_its_identifier(ctx, plex, tmp_path):
    ctx.saved.set("plex.client_id", "0b7ae3c4-1111-4222-8333-444455556666")
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        await ctx.commands.run("plex.link")
    finally:
        await service.stop()
    head = plex.asked("/api/v2/pins")[0][3]
    assert head["X-Plex-Client-Identifier"] == "0b7ae3c4-1111-4222-8333-444455556666"


async def test_a_code_that_runs_out(ctx, plex, tmp_path):
    plex.pin_gone = True  # plex.tv forgets a PIN once it has expired
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        await ctx.commands.run("plex.link")
        await until(lambda: ctx.state.get("plex")["link"] is None)
        assert ctx.state.get("plex")["link_error"] == "The code ran out. Try again."
        assert ctx.state.get("plex")["linked"] is False
        assert not (tmp_path / "plex-token").exists()
    finally:
        await service.stop()


async def test_cancelling_the_code(ctx, plex, tmp_path):
    plex.polls_until_linked = 10**6
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        await ctx.commands.run("plex.link")
        # Asked twice while waiting: the same code, one PIN.
        assert await ctx.commands.run("plex.link") == {"code": "K7QX"}
        assert len(plex.asked("/api/v2/pins")) == 1
        await ctx.commands.run("plex.cancel_link")
        assert ctx.state.get("plex")["link"] is None
        polls = len(plex.asked("/api/v2/pins/77"))
        await asyncio.sleep(0.05)
        assert len(plex.asked("/api/v2/pins/77")) == polls
    finally:
        await service.stop()


async def test_plex_tv_unreachable_says_so(ctx, tmp_path):
    service = PlexService(ctx, api=PlexApi("http://127.0.0.1:9"), cache_dir=tmp_path / "t")
    await service.start()
    try:
        with pytest.raises(CommandError, match=r"Couldn't reach plex\.tv"):
            await ctx.commands.run("plex.link")
    finally:
        await service.stop()


async def test_unlinking_deletes_the_token_and_the_list(ctx, plex, tmp_path):
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        assert ctx.state.get("plex")["linked"] is True
        with pytest.raises(CommandError, match="already linked"):
            await ctx.commands.run("plex.link")
        await until(lambda: ctx.state.get("plex")["continue"])
        assert any((tmp_path / "thumbs").iterdir())
        await ctx.commands.run("plex.unlink")
        assert ctx.state.get("plex") == EMPTY
        assert not (tmp_path / "plex-token").exists()
        assert not any((tmp_path / "thumbs").iterdir())
    finally:
        await service.stop()


async def test_a_token_plex_tv_refuses_is_a_sign_out(ctx, plex, tmp_path):
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    plex.account_ok = False
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        await until(lambda: not ctx.state.get("plex")["linked"])
        assert ctx.state.get("plex")["link_error"] == "Plex signed Couch out. Link it again."
        assert not (tmp_path / "plex-token").exists()
    finally:
        await service.stop()


# --- the token file ------------------------------------------------------------------------


def test_the_token_file_is_only_couchds(tmp_path):
    store = TokenStore(tmp_path / "plex-token")
    assert store.load() is None
    store.save(TOKEN)
    assert stat.S_IMODE((tmp_path / "plex-token").stat().st_mode) == 0o600
    assert store.load() == TOKEN
    (tmp_path / "plex-token").chmod(0o644)
    assert store.load() == TOKEN
    assert stat.S_IMODE((tmp_path / "plex-token").stat().st_mode) == 0o600
    (tmp_path / "plex-token").write_text("\n")
    assert store.load() is None
    store.delete()
    store.delete()  # gone already: fine
    assert not (tmp_path / "plex-token").exists()


# --- the list over HTTP --------------------------------------------------------------------


async def test_the_list_and_its_pictures(ctx, plex, tmp_path):
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    ctx.saved.set("plex.server", "127.0.0.1:9")
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        await until(lambda: ctx.state.get("plex")["continue"])
    finally:
        await service.stop()
    items = ctx.state.get("plex")["continue"]
    assert [i["key"] for i in items] == ["1", "2", "3"]
    # The dead connection was tried first (the one found on the LAN), then the next.
    probes = [r[0] for r in plex.asked("/identity")]
    assert probes == ["GET"]
    # The episode's still came, at 16:9; the film's backdrop didn't (placeholder); the next
    # episode has none.
    assert items[0]["thumb"].startswith("/api/plex/thumb/")
    assert items[1]["thumb"] is None and items[2]["thumb"] is None
    _, _, query, head = plex.asked("/photo/:/transcode")[0]
    assert (query["width"], query["height"]) == (str(THUMB_W), str(THUMB_H))
    assert query["url"] == "/library/metadata/1/thumb/9"
    assert head["X-Plex-Token"] == SERVER_TOKEN
    name = items[0]["thumb"].rsplit("/", 1)[1]
    assert (tmp_path / "thumbs" / name).read_bytes().startswith(b"\xff\xd8")
    assert "art" not in items[0]


async def test_an_older_server_has_on_deck(ctx, plex, tmp_path):
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    plex.hubs = False
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        await until(lambda: ctx.state.get("plex")["continue"])
        assert [i["key"] for i in ctx.state.get("plex")["continue"]] == ["1"]
    finally:
        await service.stop()


async def test_errors_never_carry_the_token(ctx, plex, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    plex.fail_server = True  # its error page even echoes the token
    service = service_for(ctx, plex, tmp_path)
    await service.start()
    try:
        await until(lambda: "no Plex list" in caplog.text)
    finally:
        await service.stop()
    assert TOKEN not in caplog.text and SERVER_TOKEN not in caplog.text
    assert TOKEN not in json.dumps(ctx.state.snapshot())


# --- the refresh ---------------------------------------------------------------------------


class Clock:
    def __init__(self, at: float = 1_795_000_000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class Sleeps:
    """The waits between fetches: noted, the clock moved on by each, and the loop held after
    `rounds` of them."""

    def __init__(self, clock: Clock, rounds: int) -> None:
        self.clock = clock
        self.rounds = rounds
        self.waits: list[float] = []
        self.done = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.at += seconds
        if len(self.waits) >= self.rounds:
            self.done.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)


class ScriptedPlex(PlexApi):
    """plex.tv and one server, answering the list from a script: a list of items, or an
    exception to raise."""

    def __init__(self, *script) -> None:
        super().__init__("http://plex.invalid")
        self.script = list(script)
        self.lists = 0

    async def resources(self, client_id, token):
        return [resource("Living room")]

    async def identity(self, uri, client_id, token):
        return True

    async def get(self, uri, path, client_id, token, params=None):
        self.lists += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return {"MediaContainer": {"Metadata": item}}

    async def picture(self, uri, path, client_id, token):
        return b"\xff\xd8"


async def linked_service(ctx, tmp_path, api, rounds, **kw) -> tuple[PlexService, Sleeps]:
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    clock = Clock()
    sleeps = Sleeps(clock, rounds)
    service = PlexService(
        ctx, api=api, cache_dir=tmp_path / "thumbs", clock=clock, sleep=sleeps, **kw
    )
    await service.start()
    await asyncio.wait_for(sleeps.done.wait(), 2)
    return service, sleeps


async def test_refreshes_every_five_minutes_on_home(ctx, tmp_path):
    service, sleeps = await linked_service(ctx, tmp_path, ScriptedPlex([episode()]), rounds=3)
    try:
        assert sleeps.waits == [300, 300, 300]
        assert ctx.state.get("plex")["continue"][0]["title"] == "The Long Field"
    finally:
        await service.stop()


async def test_backs_off_and_keeps_the_last_list(ctx, tmp_path):
    api = ScriptedPlex([episode()], *[PlexError("down")] * 8, [movie()])
    service, sleeps = await linked_service(ctx, tmp_path, api, rounds=10, retry_max=600)
    try:
        # Good, then failures: 30 s doubling, no longer than the cap; then good again.
        assert sleeps.waits == [300, 30, 60, 120, 240, 480, 600, 600, 600, 300]
        assert ctx.state.get("plex")["continue"][0]["title"] == "Harbour Lights"
    finally:
        await service.stop()
    api = ScriptedPlex([episode()], *[PlexError("down")] * 8)
    service, _ = await linked_service(ctx, tmp_path, api, rounds=5)
    try:
        assert ctx.state.get("plex")["continue"][0]["title"] == "The Long Field"
    finally:
        await service.stop()


async def test_a_broken_answer_counts_as_a_failure(ctx, tmp_path):
    class Broken(ScriptedPlex):
        async def get(self, uri, path, client_id, token, params=None):
            return {"oops": True}

    service, sleeps = await linked_service(ctx, tmp_path, Broken(), rounds=2)
    try:
        assert sleeps.waits == [30, 60]
        assert ctx.state.get("plex")["continue"] == []
    finally:
        await service.stop()


async def test_not_while_an_app_or_the_idle_clock_is_up_and_soon_after_plex(ctx, tmp_path):
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    api = ScriptedPlex([episode()])
    ctx.state.update("apps", {"foreground": "plex"})
    service = PlexService(ctx, api=api, cache_dir=tmp_path / "thumbs", after_plex=0.05)
    await service.start()
    try:
        await asyncio.sleep(0.05)
        assert api.lists == 0
        ctx.state.update("apps", {"foreground": "launcher"})  # Plex has gone
        await asyncio.sleep(0.02)
        assert api.lists == 0  # not at once: the server needs a moment for the progress
        await until(lambda: api.lists == 1)
        ctx.state.update("idle", {"idle": True})
        ctx.state.update("apps", {"foreground": "youtube"})
        ctx.state.update("apps", {"foreground": "launcher"})
        await asyncio.sleep(0.05)
        assert api.lists == 1  # back from YouTube to the idle clock: not due yet anyway
    finally:
        await service.stop()


async def test_the_setting_off_fetches_nothing_and_shows_nothing(ctx, tmp_path):
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    ctx.settings.set("home.plex_continue", False)
    api = ScriptedPlex([episode()])
    service = PlexService(ctx, api=api, cache_dir=tmp_path / "thumbs")
    await service.start()
    try:
        await asyncio.sleep(0.02)
        assert api.lists == 0
        ctx.settings.set("home.plex_continue", True)
        await until(lambda: ctx.state.get("plex")["continue"])
        ctx.settings.set("home.plex_continue", False)
        assert ctx.state.get("plex")["continue"] == []
        assert ctx.state.get("plex")["linked"] is True
        assert not any((tmp_path / "thumbs").iterdir())
    finally:
        await service.stop()


# --- opening an item -----------------------------------------------------------------------


async def test_ok_on_an_item_opens_plex(ctx, tmp_path):
    TokenStore(tmp_path / "plex-token").save(TOKEN)
    launches = Launches()
    targeted = []

    async def target(item):
        targeted.append(item["key"])
        return False

    api = ScriptedPlex([episode("12"), movie("13")])
    service = PlexService(
        ctx, api=api, cache_dir=tmp_path / "thumbs", launch=launches, target=target
    )
    await service.start()
    try:
        await until(lambda: ctx.state.get("plex")["continue"])
        assert await ctx.commands.run("plex.open", {"key": "13"}) == {"targeted": False}
        assert await ctx.commands.run("plex.open", {"key": "gone"}) == {"targeted": False}
        assert launches.apps == ["plex", "plex"]
        assert targeted == ["13"]  # only one it knows
        with pytest.raises(CommandError):
            await ctx.commands.run("plex.open", {})
    finally:
        await service.stop()


async def test_pm4k_cant_be_pointed_at_an_item():
    assert await open_in_pm4k({"key": "1"}) is False


# --- pictures, served ----------------------------------------------------------------------


def test_thumbs_only_by_their_own_names(tmp_path):
    thumbs = Thumbs(tmp_path)
    name = thumbs.name("server", "/library/metadata/1/thumb/9")
    assert len(name) == 24 and name == thumbs.name("server", "/library/metadata/1/thumb/9")
    assert name != thumbs.name("other", "/library/metadata/1/thumb/9")
    assert thumbs.path(name) is None  # not made yet
    thumbs.store(name, b"\xff\xd8")
    assert thumbs.path(name) == tmp_path / name
    (tmp_path / ("a" * 24)).write_bytes(b"not ours")  # there, but not stored by this one
    assert thumbs.path("a" * 24) is None
    for bad in ("../plex-token", name.upper(), name + ".jpg", "", None, 7, f"x/{name}"):
        assert thumbs.path(bad) is None
    other = thumbs.name("server", "/x")
    thumbs.store(other, b"\xff\xd8")
    thumbs.keep({other})
    assert not (tmp_path / name).exists() and (tmp_path / other).exists()
    again = Thumbs(tmp_path)
    again.load()  # after a restart: what's there and looks like ours
    assert again.path(other) == tmp_path / other


@pytest.fixture
async def api_client(tmp_path):
    thumbs = Thumbs(tmp_path / "thumbs")
    (tmp_path / "plex-token").write_text(TOKEN)
    bus = EventBus()
    app = create_app(bus=bus, state=LiveState(bus), commands=Commands(), plex_thumbs=thumbs.path)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        yield client, thumbs


async def test_the_thumb_endpoint_serves_only_what_plex_made(api_client):
    client, thumbs = api_client
    name = thumbs.name("s", "/a")
    thumbs.store(name, b"\xff\xd8jpeg")
    resp = await client.get(f"/api/plex/thumb/{name}")
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"
    assert await resp.read() == b"\xff\xd8jpeg"
    png = thumbs.name("s", "/b")
    thumbs.store(png, b"\x89PNG....")
    assert (await client.get(f"/api/plex/thumb/{png}")).content_type == "image/png"
    for bad in (
        "../plex-token",
        "..%2Fplex-token",
        "%2e%2e%2fplex-token",
        "plex-token",
        name.upper(),
        "b" * 24,
        "",
    ):
        resp = await client.get(f"/api/plex/thumb/{bad}")
        assert resp.status == 404, bad
        assert TOKEN not in await resp.text()
    (thumbs.directory / name).unlink()  # gone from under it
    assert (await client.get(f"/api/plex/thumb/{name}")).status == 404


async def test_no_plex_no_thumbs(tmp_path):
    bus = EventBus()
    app = create_app(bus=bus, state=LiveState(bus), commands=Commands())
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        assert (await client.get("/api/plex/thumb/" + "a" * 24)).status == 404


# --- dev mode ------------------------------------------------------------------------------


async def test_dev_mode_links_and_lists_without_reaching_plex(ctx, tmp_path):
    api = FakePlexApi(link_after=0.02)
    service = PlexService(ctx, api=api, cache_dir=tmp_path / "thumbs", pin_poll=0.01)
    await service.start()
    try:
        assert (await ctx.commands.run("plex.link"))["code"] == "K7QX"
        await until(lambda: len(ctx.state.get("plex")["continue"]) == 5, within=10)
    finally:
        await service.stop()
    items = ctx.state.get("plex")["continue"]
    assert [i["title"] for i in items] == [
        "The Long Field",
        "Harbour Lights",
        "North Line",
        "Paper Moons",
        "Glasswater",
    ]
    assert [bool(i["thumb"]) for i in items] == [True, True, True, True, False]
    name = items[0]["thumb"].rsplit("/", 1)[1]
    assert (tmp_path / "thumbs" / name).read_bytes().startswith(b"\x89PNG")


async def test_api_errors(plex):
    api = PlexApi(plex.base)
    with pytest.raises(NotFound):
        await api.pin_token("c", 5)
    with pytest.raises(Unauthorized):
        await api.resources("c", "wrong")
    assert await api.identity("http://127.0.0.1:9", "c", SERVER_TOKEN) is False
    pin = await api.create_pin("c")
    assert pin == Pin(77, "K7QX", 900.0)

"""Plex's Continue watching, for Home (#86): Couch's own Plex sign-in, the server, the list.

**Sign-in.** Couch links itself to a Plex account the way TV apps do, with Plex's PIN flow.
`plex.link` asks plex.tv for a PIN (`POST /api/v2/pins`, `strong=false`, so the code is four
characters), the TV shows the code, someone enters it at plex.tv/link on their phone, and
couchd polls the PIN (`GET /api/v2/pins/{id}`) until it carries a token. The token is Couch's
own: Plex for Kodi (PM4K) has its sign-in, and couchd never reads it. It's kept in one file in
couchd's state dir, `plex-token`, mode 600, and nowhere else: never in live state, the logs,
an error or the API. `plex.unlink` deletes it. plex.tv knows this Couch by a client
identifier made once and saved (`plex.client_id` in state.json), and lists it as "Couch"
among the account's devices.

**The server.** With the token, plex.tv's resources (`GET /api/v2/resources?includeHttps=1`)
say which servers the account owns or has been given, and how to reach them. Owned servers
come first; of a server's connections the local ones (the address couchd/network.py found on
the LAN first, then plain HTTP on the LAN after the plex.direct name, for routers that won't
resolve it), then remote ones, and relays last. The first that answers `/identity` is the
one, until it stops answering.

**The list.** `GET /hubs/continueWatching` (`/library/onDeck` on a server that hasn't got it)
gives what's half-watched and the next episodes. The first `ITEMS` movies and episodes become
live state `plex.continue`, with their words worked out here (`describe`). Each one's picture
(an episode's still, a film's backdrop) is fetched at 16:9 through the server's photo
transcoder into the cache dir and served at `GET /api/plex/thumb/<name>`, by names this module
made and no others (`Thumbs`). The cache is RAM on the Pi (/var/cache/couch/plex).

It's fetched every `REFRESH` while the launcher is in front and awake, a moment after Plex goes
away (it's what moves the progress on), and when couchd is linked or the setting turned on.
Not while an app is in front or the idle clock is up. A failed fetch is tried again after
`RETRY_MIN`, doubling up to `RETRY_MAX`, and the last good list stays meanwhile. A token
plex.tv refuses is a sign-out: it's deleted, and Settings > Network > Plex says so. With the
`home.plex_continue` setting off nothing is fetched and the list is empty.

**Opening an item.** `plex.open` opens Plex (couchd/apps.py, as the card does) and would then
point it at the item, but PM4K 1.3.19 can't be told to: no entry point takes an item. Its
`plugin://script.plexmod/...` endpoint only runs `RunScript(script.plexmod,fromplugin)`, the
script's arguments are kiosk flags parsed with int(), and a second RunScript, or
`NotifyAll(script.plexmod,RESTORE)`, only brings a running PM4K to the front. So Plex opens
where it was (`open_in_pm4k`). Playing the file with Kodi's own player instead would bypass
PM4K: no resume from Plex's position, no progress back to the server, and Couch's token
handed to Kodi.

Live state section `plex`:

    {"linked": true, "link": null, "link_error": null, "server": "Living room",
     "continue": [{"key": "12345", "title": "The Long Field", "subtitle": "S2 · E5 · 18 min left",
                   "detail": "Season 2 · Episode 5 · Night Shift · 18 min left",
                   "progress": 0.62, "thumb": "/api/plex/thumb/3f…"}],
     "updated_at": 1795373460}

`link` is `{"code": "K7QX", "expires_at": <unix seconds>}` while a code is waiting to be
entered. `thumb` is null until (or unless) the picture is in.
"""

import asyncio
import contextlib
import hashlib
import logging
import math
import re
import struct
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import aiohttp

from couchd import __version__
from couchd.commands import CommandError, arg
from couchd.files import fsync_dir, remove_leftovers, write_atomic
from couchd.network import SAVED_PLEX_SERVER
from couchd.services import Context, Service

log = logging.getLogger(__name__)

SECTION = "plex"
SETTING = "home.plex_continue"
PLEX_APP = "plex"  # the app id in couchd/apps.py
PLEX_TV = "https://plex.tv"
PRODUCT = "Couch"
TOKEN_FILE = "plex-token"  # in the state dir, next to state.json
SAVED_CLIENT_ID = "plex.client_id"
DEVICE_CACHE = Path("/var/cache/couch/plex")  # RAM (system/couch-cache.conf)

TIMEOUT = 10.0
PROBE_TIMEOUT = 3.0  # a connection that hasn't answered /identity by then isn't the one
PIN_POLL = 2.0
PIN_LIFETIME = 15 * 60.0  # if plex.tv doesn't say
REFRESH = 5 * 60.0
AFTER_PLEX = 3.0  # Plex has gone: by then the server has the new progress
RETRY_MIN = 30.0
RETRY_MAX = 30 * 60.0
ITEMS = 5  # Home shows five
THUMB_W, THUMB_H = 576, 324  # 16:9, twice the 288x162 Home draws them at
MAX_IMAGE = 2 * 1024 * 1024
THUMB_URL = "/api/plex/thumb/"
THUMB_NAME = re.compile(r"[0-9a-f]{24}")

DOT = " · "

EMPTY: dict[str, Any] = {
    "linked": False,
    "link": None,
    "link_error": None,
    "server": None,
    "continue": [],
    "updated_at": None,
}


class PlexError(Exception):
    """plex.tv or a server couldn't be reached, or said no. Its text never holds a token."""


class Unauthorized(PlexError):
    """The token was refused (401)."""


class NotFound(PlexError):
    """404: a PIN that's gone, a hub an older server hasn't got."""


# --- talking to Plex -----------------------------------------------------------------------


def headers(client_id: str, token: str | None = None) -> dict[str, str]:
    """What every request to plex.tv or a server carries: who's asking, and the token."""
    out = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Device-Name": PRODUCT,
    }
    if token is not None:
        out["X-Plex-Token"] = token  # a header, never the URL: URLs end up in logs
    return out


@dataclass(frozen=True)
class Pin:
    id: int
    code: str
    expires_in: float


@dataclass(frozen=True)
class Server:
    id: str  # its machineIdentifier
    name: str
    token: str = field(repr=False)  # the account's token for this server (resources' accessToken)
    uris: tuple[str, ...] = ()

    @property
    def uri(self) -> str:
        return self.uris[0]


class PlexApi:
    """plex.tv and Plex servers over HTTPS. `plex_tv` is plex.tv's address: a local fake in
    the tests, which never reach the real one."""

    def __init__(self, plex_tv: str = PLEX_TV, *, timeout: float = TIMEOUT) -> None:
        self.plex_tv = plex_tv.rstrip("/")
        self.timeout = timeout

    async def _request(
        self,
        method: str,
        url: str,
        client_id: str,
        token: str | None = None,
        *,
        params: dict[str, str] | None = None,
        limit: float | None = None,
        raw: bool = False,
    ) -> Any:
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=limit or self.timeout)
                ) as http,
                http.request(method, url, params=params, headers=headers(client_id, token)) as resp,
            ):
                if resp.status == 401:
                    raise Unauthorized("Plex refused the sign-in")
                if resp.status == 404:
                    raise NotFound(f"{resp.url.path}: not found")
                if resp.status >= 400:
                    raise PlexError(f"{resp.url.path}: HTTP {resp.status}")
                if raw:
                    data = await resp.content.read(MAX_IMAGE + 1)
                    if len(data) > MAX_IMAGE:
                        raise PlexError("picture too big")
                    return data
                return await resp.json(content_type=None)
        except PlexError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as e:
            # aiohttp's own texts name the host, never a header.
            raise PlexError(str(e) or type(e).__name__) from None

    async def create_pin(self, client_id: str) -> Pin:
        data = await self._request(
            "POST", f"{self.plex_tv}/api/v2/pins", client_id, params={"strong": "false"}
        )
        if not isinstance(data, dict):
            raise PlexError("plex.tv sent no PIN")
        pin_id, code, expires = data.get("id"), data.get("code"), data.get("expiresIn")
        if not isinstance(pin_id, int) or not isinstance(code, str) or not code:
            raise PlexError("plex.tv sent no PIN")
        if not isinstance(expires, int | float) or isinstance(expires, bool) or expires <= 0:
            expires = PIN_LIFETIME
        return Pin(pin_id, code, float(expires))

    async def pin_token(self, client_id: str, pin_id: int) -> str | None:
        """The PIN's token once someone has entered its code, else None. NotFound once the
        PIN is gone (it ran out)."""
        data = await self._request("GET", f"{self.plex_tv}/api/v2/pins/{pin_id}", client_id)
        token = data.get("authToken") if isinstance(data, dict) else None
        return token if isinstance(token, str) and token else None

    async def resources(self, client_id: str, token: str) -> list[Any]:
        data = await self._request(
            "GET",
            f"{self.plex_tv}/api/v2/resources",
            client_id,
            token,
            params={"includeHttps": "1", "includeRelay": "1"},
        )
        if not isinstance(data, list):
            raise PlexError("plex.tv sent no resources")
        return data

    async def identity(self, uri: str, client_id: str, token: str) -> bool:
        """Does a server answer at `uri`? Quickly: a dead connection is skipped, not waited on."""
        try:
            await self._request("GET", f"{uri}/identity", client_id, token, limit=PROBE_TIMEOUT)
        except PlexError:
            return False
        return True

    async def get(
        self, uri: str, path: str, client_id: str, token: str, params: dict[str, str] | None = None
    ) -> Any:
        return await self._request("GET", f"{uri}{path}", client_id, token, params=params)

    async def picture(self, uri: str, path: str, client_id: str, token: str) -> bytes:
        """The server's picture at `path`, at 16:9 from its photo transcoder."""
        params = {
            "width": str(THUMB_W),
            "height": str(THUMB_H),
            "minSize": "1",
            "upscale": "1",
            "url": path,
        }
        return await self._request(
            "GET", f"{uri}/photo/:/transcode", client_id, token, params=params, raw=True
        )


def connection_uris(connections: Any, known: str | None = None) -> tuple[str, ...]:
    """A server's ways in, best first: local, then remote, then relayed; the address couchd
    found on the LAN (`known`, "host:port") first of the local ones. A local plex.direct
    connection is followed by plain HTTP to the same address, for routers that won't resolve
    plex.direct names (DNS rebinding protection) and servers that allow it on the LAN."""
    if not isinstance(connections, list):
        return ()
    usable = [c for c in connections if isinstance(c, dict) and isinstance(c.get("uri"), str)]

    def rank(c: dict) -> tuple[bool, bool, bool]:
        at = f"{c.get('address')}:{c.get('port')}"
        return (bool(c.get("relay")), not c.get("local"), at != known)

    out: list[str] = []
    for c in sorted(usable, key=rank):
        uri = c["uri"].rstrip("/")
        if uri.startswith(("https://", "http://")) and uri not in out:
            out.append(uri)
        address, port = c.get("address"), c.get("port")
        if c.get("local") and not c.get("relay") and isinstance(address, str) and address:
            host = f"[{address}]" if ":" in address else address
            plain = f"http://{host}:{port if isinstance(port, int) else 32400}"
            if plain not in out:
                out.append(plain)
    return tuple(out)


def pick_servers(resources: list[Any], known: str | None = None) -> list[Server]:
    """The account's Plex servers from plex.tv's resources: owned first, then those that are
    online, each with its connections in order (`connection_uris`)."""
    ranked = []
    for r in resources:
        if not isinstance(r, dict) or "server" not in str(r.get("provides", "")).split(","):
            continue
        token, server_id = r.get("accessToken"), r.get("clientIdentifier")
        if not isinstance(token, str) or not token or not isinstance(server_id, str):
            continue
        uris = connection_uris(r.get("connections"), known)
        if not uris:
            continue
        name = r.get("name") if isinstance(r.get("name"), str) else "Plex"
        rank = (not r.get("owned"), r.get("presence") is False)
        ranked.append((rank, Server(server_id, name, token, uris)))
    return [s for _, s in sorted(ranked, key=lambda x: x[0])]


# --- the list, in words --------------------------------------------------------------------


def metadata(data: Any) -> list[dict]:
    """The items in a server's answer: a MediaContainer's Metadata, or its hubs' (the shape
    /hubs/continueWatching has)."""
    container = data.get("MediaContainer") if isinstance(data, dict) else None
    if not isinstance(container, dict):
        raise ValueError("not a MediaContainer")
    items = container.get("Metadata")
    if items is None:
        hubs = container.get("Hub")
        items = []
        for hub in hubs if isinstance(hubs, list) else []:
            if isinstance(hub, dict) and isinstance(hub.get("Metadata"), list):
                items.extend(hub["Metadata"])
    if not isinstance(items, list):
        raise ValueError("no Metadata")
    return [m for m in items if isinstance(m, dict)]


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return int(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def time_left(duration_ms: Any, offset_ms: Any) -> str | None:
    """ "18 min left", "1 h 12 min left", "2 h left"; None when it hasn't been started."""
    duration, offset = _whole(duration_ms), _whole(offset_ms)
    if not duration or not offset or offset <= 0:
        return None
    minutes = max(1, math.ceil((duration - offset) / 60000))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min left"
    if hours:
        return f"{hours} h left"
    return f"{minutes} min left"


def describe(m: dict) -> dict[str, Any] | None:
    """One item as Home shows it, plus `art`: the picture's path on its server. None for
    anything that isn't a movie or an episode."""
    kind = m.get("type")
    key = m.get("ratingKey")
    key = str(key) if isinstance(key, int | str) and not isinstance(key, bool) else ""
    if kind not in ("movie", "episode") or not re.fullmatch(r"[0-9A-Za-z]{1,32}", key):
        return None
    duration, offset = _whole(m.get("duration")), _whole(m.get("viewOffset"))
    left = time_left(duration, offset)
    if kind == "episode":
        title = _text(m.get("grandparentTitle")) or _text(m.get("title"))
        season, episode = _whole(m.get("parentIndex")), _whole(m.get("index"))
        status = left or "Next episode"
        short = [f"S{season}" if season is not None else "", f"E{episode}" if episode else ""]
        long = [
            f"Season {season}" if season is not None else "",
            f"Episode {episode}" if episode else "",
            _text(m.get("title")) if _text(m.get("grandparentTitle")) else "",
        ]
        art = m.get("thumb") or m.get("grandparentArt") or m.get("art")
    else:
        title = _text(m.get("title"))
        status = left or ""
        year = _whole(m.get("year"))
        short = ["Film"]
        long = ["Film", str(year) if year else ""]
        art = m.get("art") or m.get("thumb")
    progress = 0.0
    if left and duration:
        progress = round(min(max(offset / duration, 0.0), 1.0), 3)
    return {
        "key": key,
        "title": title or "Untitled",
        "subtitle": DOT.join(p for p in [*short, status] if p),
        "detail": DOT.join(p for p in [*long, status] if p),
        "progress": progress,
        "art": art if isinstance(art, str) and art.startswith("/") else None,
    }


def continue_list(data: Any, limit: int = ITEMS) -> list[dict[str, Any]]:
    items = []
    for m in metadata(data):
        item = describe(m)
        if item is not None and all(i["key"] != item["key"] for i in items):
            items.append(item)
        if len(items) >= limit:
            break
    return items


async def open_in_pm4k(item: dict[str, Any]) -> bool:
    """Point Plex (PM4K, script.plexmod) at `item` once it's open. It can't be: PM4K 1.3.19
    takes no item from outside (see this module's docstring), so this says no and Plex opens
    where it was. The one place to change if a PM4K ever can."""
    return False


# --- keeping the token and the pictures ----------------------------------------------------


class TokenStore:
    """The token, alone in a file only couchd's user can read (0600 from its first byte:
    files.write_atomic writes it to a mkstemp file, which is 0600, then renames it)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        remove_leftovers(self.path)
        try:
            token = self.path.read_text(encoding="utf-8").strip()
            if self.path.stat().st_mode & 0o077:
                self.path.chmod(0o600)  # put back if someone loosened it
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as e:
            log.warning("can't read the Plex sign-in", extra={"error": type(e).__name__})
            return None
        return token or None

    def save(self, token: str) -> None:
        write_atomic(self.path, token.encode("utf-8"), mode=0o600)

    def delete(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
            fsync_dir(self.path.parent)


class Thumbs:
    """The list's pictures, in `directory`, served at /api/plex/thumb/<name>. Names are 24
    hex digits made here (a hash of the server and the picture's path), and only names this
    has stored are served, so a request can't reach any other file."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._names: set[str] = set()

    @staticmethod
    def name(server_id: str, art: str) -> str:
        return hashlib.sha256(f"{server_id}\0{art}\0{THUMB_W}x{THUMB_H}".encode()).hexdigest()[:24]

    def load(self) -> None:
        """What a previous couchd left: still good, and found by name again."""
        with contextlib.suppress(OSError):
            self._names = {
                p.name
                for p in self.directory.iterdir()
                if THUMB_NAME.fullmatch(p.name) and p.is_file()
            }

    def has(self, name: str) -> bool:
        return name in self._names

    def path(self, name: Any) -> Path | None:
        if not isinstance(name, str) or not THUMB_NAME.fullmatch(name) or name not in self._names:
            return None
        return self.directory / name

    def store(self, name: str, data: bytes) -> None:
        assert THUMB_NAME.fullmatch(name)
        write_atomic(self.directory / name, data, mode=0o600)
        self._names.add(name)

    def keep(self, names: set[str]) -> None:
        """Delete every picture but these."""
        for name in self._names - names:
            with contextlib.suppress(OSError):
                (self.directory / name).unlink()
        self._names &= names

    def clear(self) -> None:
        self.keep(set())


def content_type(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


# --- the service ---------------------------------------------------------------------------

Launch = Callable[[str], Awaitable[None]]
Target = Callable[[dict[str, Any]], Awaitable[bool]]


class PlexService(Service):
    name = "plex"

    def __init__(
        self,
        ctx: Context,
        *,
        api: PlexApi | None = None,
        launch: Launch | None = None,
        target: Target = open_in_pm4k,
        cache_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        refresh: float = REFRESH,
        retry_min: float = RETRY_MIN,
        retry_max: float = RETRY_MAX,
        after_plex: float = AFTER_PLEX,
        pin_poll: float = PIN_POLL,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """`launch` opens an app (AppService.launch); `cache_dir` holds the pictures (left
        out: next to the state). `sleep` waits between fetches (tests): by default until then,
        or until something happens."""
        self.ctx = ctx
        self.api = api or PlexApi()
        self._launch = launch
        self._target = target
        state_dir = ctx.saved.path.parent
        self.tokens = TokenStore(state_dir / TOKEN_FILE)
        self.thumbs = Thumbs(cache_dir or state_dir / "plex-thumbs")
        self._clock = clock
        self._refresh = refresh
        self._retry_min = retry_min
        self._retry_max = retry_max
        self._after_plex = after_plex
        self._pin_poll = pin_poll
        self._sleep = sleep or self._pause
        self._token: str | None = None
        self._client_id = ""
        self._server: Server | None = None
        self._items: list[dict[str, Any]] = []
        self._next_at = 0.0
        self._retry = retry_min
        self._foreground: str | None = None
        self._idle = False
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._pin_task: asyncio.Task | None = None
        self._unsubscribe: Callable[[], None] | None = None

    async def start(self) -> None:
        self._token = self.tokens.load()
        self.thumbs.load()
        self._client_id = self._saved_client_id()
        self._foreground = self.ctx.state.get("apps").get("foreground")
        self._idle = bool(self.ctx.state.get("idle").get("idle"))
        self.ctx.state.update(SECTION, {**EMPTY, "linked": self._token is not None})
        if self._token is None:
            self.thumbs.clear()  # left from an account that's gone
        commands = self.ctx.commands
        commands.add("plex.link", self._link_command)
        commands.add("plex.cancel_link", self._cancel_link_command)
        commands.add("plex.unlink", self._unlink_command)
        commands.add("plex.open", self._open_command)
        commands.add("plex.refresh", self._refresh_command)
        self._unsubscribe = self.ctx.settings.on_change(self._setting_changed)
        self._tasks = [asyncio.create_task(self._run()), asyncio.create_task(self._watch())]

    async def stop(self) -> None:
        self.ctx.commands.remove_prefix("plex.")
        if self._unsubscribe is not None:
            self._unsubscribe()
        for task in [*self._tasks, self._pin_task]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._tasks, self._pin_task = [], None

    @property
    def linked(self) -> bool:
        return self._token is not None

    def _saved_client_id(self) -> str:
        saved = self.ctx.saved.get(SAVED_CLIENT_ID)
        if isinstance(saved, str) and re.fullmatch(r"[0-9a-f-]{16,64}", saved):
            return saved
        made = str(uuid.uuid4())
        self.ctx.saved.set(SAVED_CLIENT_ID, made)
        return made

    def _on(self) -> bool:
        return bool(self.ctx.settings.get(SETTING))

    def _wanted(self) -> bool:
        """Fetch now? Linked, the setting on, and Home is what's on the TV."""
        home = self._foreground in (None, "launcher") and not self._idle
        return self.linked and self._on() and home

    def _scrub(self, text: str) -> str:
        """Belt and braces: no token in anything that's logged or said."""
        for secret in (self._token, self._server.token if self._server else None):
            if secret:
                text = text.replace(secret, "…")
        return text

    def _soon(self, delay: float = 0.0) -> None:
        self._next_at = min(self._next_at, self._clock() + delay)
        self._wake.set()

    # --- the list ------------------------------------------------------------------------

    async def refresh(self) -> bool:
        """Fetch the list once. True if it worked; on failure the last list stays."""
        token = self._token
        if token is None:
            return False
        try:
            if self._server is None:
                self._server = await self._find_server(token)
            server = self._server
            try:
                items = await self._continue_watching(server)
                await self._pictures(server, items)
            except PlexError:
                self._server = None  # look again next time: moved, or its token changed
                raise
        except Unauthorized:
            if token == self._token:
                log.warning("plex.tv refused Couch's sign-in; unlinked")
                self._unlink(error="Plex signed Couch out. Link it again.")
            return False
        except (PlexError, ValueError) as e:
            log.info("no Plex list", extra={"error": self._scrub(str(e))})
            return False
        if token != self._token or not self._on():
            return True  # unlinked, or turned off, while it was on its way
        self._items = items
        self.ctx.state.update(
            SECTION,
            {
                "server": server.name,
                "continue": [self._public(i) for i in items],
                "updated_at": int(self._clock()),
            },
        )
        self.thumbs.keep({i["thumb"] for i in items if i["thumb"]})
        return True

    async def _find_server(self, token: str) -> Server:
        resources = await self.api.resources(self._client_id, token)
        known = self.ctx.saved.get(SAVED_PLEX_SERVER)
        servers = pick_servers(resources, known if isinstance(known, str) else None)
        for server in servers:
            for uri in server.uris:
                if await self.api.identity(uri, self._client_id, server.token):
                    log.info("Plex server", extra={"server": server.name, "uri": uri})
                    return replace(server, uris=(uri,))
        raise PlexError("no Plex server answers" if servers else "no Plex server on the account")

    async def _continue_watching(self, server: Server) -> list[dict[str, Any]]:
        paths = (
            ("/hubs/continueWatching", {"count": str(ITEMS * 2)}),
            ("/library/onDeck", {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "10"}),
        )
        for path, params in paths:
            try:
                data = await self.api.get(server.uri, path, self._client_id, server.token, params)
            except NotFound:
                continue
            return continue_list(data)
        raise PlexError("the server has no Continue watching")

    async def _pictures(self, server: Server, items: list[dict[str, Any]]) -> None:
        """Each item's picture into the cache: `thumb` is its name, or None without one."""

        async def one(item: dict[str, Any]) -> None:
            art = item.pop("art", None)
            item["thumb"] = None
            if not art:
                return
            name = self.thumbs.name(server.id, art)
            if not self.thumbs.has(name):
                try:
                    data = await self.api.picture(server.uri, art, self._client_id, server.token)
                    await asyncio.to_thread(self.thumbs.store, name, data)
                except (PlexError, OSError) as e:
                    log.info("no Plex picture", extra={"error": self._scrub(str(e))})
                    return
            item["thumb"] = name

        await asyncio.gather(*(one(i) for i in items))

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {**item, "thumb": f"{THUMB_URL}{item['thumb']}" if item["thumb"] else None}

    def _clear_list(self) -> None:
        self._items = []
        self.thumbs.clear()
        self.ctx.state.update(SECTION, {"continue": [], "updated_at": None})

    async def _run(self) -> None:
        while True:
            self._wake.clear()  # before the fetch: a change during it still wakes the wait
            if not self._wanted():
                await self._wake.wait()
                continue
            wait = self._next_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
                continue
            if await self.refresh():
                self._retry = self._retry_min
                self._next_at = self._clock() + self._refresh
            else:
                self._next_at = self._clock() + self._retry
                self._retry = min(self._retry * 2, self._retry_max)

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()

    async def _watch(self) -> None:
        """Follow what's on screen: back from Plex the list has moved on."""
        while True:
            with self.ctx.bus.subscribe() as sub:
                async for message in sub:
                    data = message.get("data")
                    if message.get("type") != "state" or not isinstance(data, dict):
                        continue
                    apps, idle = data.get("apps") or {}, data.get("idle") or {}
                    if "foreground" in apps and apps["foreground"] != self._foreground:
                        was, self._foreground = self._foreground, apps["foreground"]
                        if was == PLEX_APP:
                            # Not sooner, even if one was due: the server needs a moment
                            # to have the progress just watched.
                            self._next_at = self._clock() + self._after_plex
                            self._retry = self._retry_min
                        self._wake.set()
                    if "idle" in idle and bool(idle["idle"]) != self._idle:
                        self._idle = bool(idle["idle"])
                        self._wake.set()

    def _setting_changed(self, key: str, value: Any) -> None:
        if key != SETTING:
            return
        if value:
            self._soon()
        else:
            self._clear_list()
            self._wake.set()

    # --- linking -------------------------------------------------------------------------

    async def link(self) -> dict[str, Any]:
        """Ask plex.tv for a code to enter at plex.tv/link, and wait for it in the background."""
        current = self.ctx.state.get(SECTION).get("link")
        if self._pin_task is not None and current:
            return {"code": current["code"]}
        try:
            pin = await self.api.create_pin(self._client_id)
        except PlexError as e:
            log.info("no Plex code", extra={"error": str(e)})
            raise CommandError(
                "Couldn't reach plex.tv. Check the internet and try again."
            ) from None
        expires = self._clock() + pin.expires_in
        self.ctx.state.update(
            SECTION, {"link": {"code": pin.code, "expires_at": int(expires)}, "link_error": None}
        )
        log.info("waiting for the Plex code to be entered")
        self._pin_task = asyncio.create_task(self._wait_for(pin, expires))
        return {"code": pin.code}

    async def _wait_for(self, pin: Pin, expires: float) -> None:
        try:
            while self._clock() < expires:
                await asyncio.sleep(self._pin_poll)
                try:
                    token = await self.api.pin_token(self._client_id, pin.id)
                except NotFound:
                    break
                except PlexError as e:
                    log.info("Plex code check failed", extra={"error": str(e)})
                    continue
                if token:
                    self._linked(token)
                    return
            log.info("the Plex code ran out")
            self.ctx.state.update(
                SECTION, {"link": None, "link_error": "The code ran out. Try again."}
            )
        finally:
            if self._pin_task is asyncio.current_task():
                self._pin_task = None

    def _linked(self, token: str) -> None:
        try:
            self.tokens.save(token)
        except OSError as e:
            log.error("can't save the Plex sign-in", extra={"error": type(e).__name__})
            self.ctx.state.update(
                SECTION, {"link": None, "link_error": "Couldn't save the sign-in. Try again."}
            )
            return
        self._token = token
        self._server = None
        self._retry = self._retry_min
        log.info("linked to Plex")
        self.ctx.state.update(SECTION, {"linked": True, "link": None, "link_error": None})
        self.ctx.bus.toast("Plex linked", icon="check")
        self._soon()

    def _cancel_link(self) -> None:
        if self._pin_task is not None:
            self._pin_task.cancel()
            self._pin_task = None
        self.ctx.state.update(SECTION, {"link": None, "link_error": None})

    def _unlink(self, error: str | None = None) -> None:
        self._cancel_link()
        try:
            self.tokens.delete()
        except OSError as e:
            log.error("can't delete the Plex sign-in", extra={"error": type(e).__name__})
        self._token = None
        self._server = None
        self._items = []
        self.thumbs.clear()
        self.ctx.state.update(SECTION, {**EMPTY, "link_error": error})
        self._wake.set()

    # --- opening -------------------------------------------------------------------------

    async def open(self, key: str) -> bool:
        """Open Plex, and point it at the item if it can be. True if it was."""
        if self._launch is None:
            raise CommandError("Plex can't be opened here")
        item = next((i for i in self._items if i["key"] == key), None)
        await self._launch(PLEX_APP)
        if item is None:
            return False
        targeted = await self._target(self._public(item))
        log.info("opened Plex", extra={"item": key, "targeted": targeted})
        return targeted

    # --- commands ------------------------------------------------------------------------

    async def _link_command(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.linked:
            raise CommandError("Couch is already linked to Plex")
        return await self.link()

    async def _cancel_link_command(self, args: dict[str, Any]) -> None:
        self._cancel_link()

    async def _unlink_command(self, args: dict[str, Any]) -> None:
        if self.linked:
            log.info("unlinked from Plex")
        self._unlink()

    async def _open_command(self, args: dict[str, Any]) -> dict[str, bool]:
        return {"targeted": await self.open(arg(args, "key", str))}

    async def _refresh_command(self, args: dict[str, Any]) -> None:
        if not self.linked:
            raise CommandError("Couch isn't linked to Plex")
        self._soon()


# --- dev mode ------------------------------------------------------------------------------


def _png(width: int, height: int, top: tuple, bottom: tuple, sun: tuple) -> bytes:
    """A small made-up still: a sky from `top` to `bottom`, a low sun, dark ground."""
    cx, cy, r2 = width * 0.68, height * 0.52, (height * 0.13) ** 2
    rows = []
    for y in range(height):
        t = y / (height - 1)
        if y > height * 0.62:
            sky = tuple(int(c * 0.35) for c in bottom)
            row = bytes(sky) * width
        else:
            sky = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
            dy = (y - cy) ** 2
            row = b"".join(
                bytes(sun) if (x - cx) ** 2 + dy < r2 else bytes(sky) for x in range(width)
            )
        rows.append(b"\x00" + row)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )


# The design's five (PlexContinue.dc.html), as a server would send them.
_MIN = 60_000
FAKE_ITEMS = [
    {"ratingKey": "101", "type": "episode", "grandparentTitle": "The Long Field",
     "title": "Night Shift", "parentIndex": 2, "index": 5, "duration": 2_842_000,
     "viewOffset": 2_842_000 - 17 * _MIN - 30_000, "thumb": "/library/metadata/101/thumb/1"},
    {"ratingKey": "102", "type": "movie", "title": "Harbour Lights", "year": 2021,
     "duration": 6_650_000, "viewOffset": 6_650_000 - 71 * _MIN - 30_000,
     "art": "/library/metadata/102/art/1"},
    {"ratingKey": "103", "type": "episode", "grandparentTitle": "North Line",
     "title": "Signal Box", "parentIndex": 1, "index": 3, "duration": 2_700_000,
     "thumb": "/library/metadata/103/thumb/1"},
    {"ratingKey": "104", "type": "episode", "grandparentTitle": "Paper Moons",
     "title": "Low Tide", "parentIndex": 4, "index": 1, "duration": 2_700_000,
     "viewOffset": 2_700_000 - 8 * _MIN - 30_000, "thumb": "/library/metadata/104/thumb/1"},
    {"ratingKey": "105", "type": "movie", "title": "Glasswater", "year": 2019,
     "duration": 5_760_000, "viewOffset": 5_760_000 - 47 * _MIN - 30_000,
     "art": "/library/metadata/105/art/1"},
]  # fmt: skip

# Each still's colours; the last has none, so dev mode shows the placeholder too.
_FAKE_PICTURES = {
    "101": ((64, 92, 140), (230, 150, 90), (255, 226, 160)),
    "102": ((20, 40, 70), (90, 120, 150), (240, 240, 220)),
    "103": ((120, 140, 120), (210, 200, 150), (250, 240, 200)),
    "104": ((40, 30, 80), (200, 90, 110), (255, 200, 170)),
}


class FakePlexApi(PlexApi):
    """Dev mode's Plex: a code that's "entered" `link_after` seconds after it's shown, one
    server, the design's five items and made-up stills. Nothing leaves the PC."""

    CODE = "K7QX"

    def __init__(self, *, link_after: float = 6.0, clock: Callable[[], float] = time.monotonic):
        super().__init__("http://plex.invalid")
        self.link_after = link_after
        self._clock = clock
        self._pin_at: float | None = None

    async def create_pin(self, client_id: str) -> Pin:
        self._pin_at = self._clock()
        return Pin(1, self.CODE, PIN_LIFETIME)

    async def pin_token(self, client_id: str, pin_id: int) -> str | None:
        if self._pin_at is None:
            raise NotFound("no such PIN")
        return "dev-token" if self._clock() - self._pin_at >= self.link_after else None

    async def resources(self, client_id: str, token: str) -> list[Any]:
        return [
            {
                "name": "Living room",
                "provides": "server",
                "owned": True,
                "presence": True,
                "clientIdentifier": "dev-server",
                "accessToken": "dev-server-token",
                "connections": [
                    {"uri": "http://plex.invalid:32400", "address": "plex.invalid", "local": True}
                ],
            }
        ]

    async def identity(self, uri: str, client_id: str, token: str) -> bool:
        return True

    async def get(
        self, uri: str, path: str, client_id: str, token: str, params: dict[str, str] | None = None
    ) -> Any:
        return {"MediaContainer": {"Hub": [{"hubIdentifier": "home.continue",
                                            "Metadata": FAKE_ITEMS}]}}  # fmt: skip

    async def picture(self, uri: str, path: str, client_id: str, token: str) -> bytes:
        key = path.split("/")[3] if path.count("/") >= 3 else ""
        colours = _FAKE_PICTURES.get(key)
        if colours is None:
            raise PlexError("no picture")
        return await asyncio.to_thread(_png, THUMB_W, THUMB_H, *colours)

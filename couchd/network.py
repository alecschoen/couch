"""Is the internet there, and can we reach the Plex server? Asked before an app opens (#19).

YouTube needs the internet; when it isn't there, the launcher shows "YouTube needs the
internet" instead of handing over to a tab that can only show an error page. Plex needs its
server on the home network.

- Internet: a TCP connection to www.youtube.com:443 within a few seconds. DNS failing counts
  as no internet. Nothing is sent over the connection.
- Plex: the server couchd saw last (saved as `plex.server`, "host:port") answering
  `GET /identity`. If it doesn't, Plex's own LAN discovery (GDM: a UDP broadcast to port
  32414 that servers answer) finds it again, at a new address if DHCP moved it. Until couchd
  has seen a server once, the answer is "unknown" and Plex opens anyway, so a server that's
  off the LAN (or has discovery turned off) never locks Plex out.

`FakeConnectivity` is the dev-mode stand-in, switched with `apps.dev.network`.
"""

import asyncio
import contextlib
import logging
import socket
from abc import ABC, abstractmethod
from typing import Literal

import aiohttp

from couchd.state import SavedState

log = logging.getLogger(__name__)

PlexReach = Literal["ok", "unreachable", "unknown"]

INTERNET_HOST = ("www.youtube.com", 443)
INTERNET_TIMEOUT = 4.0
IDENTITY_TIMEOUT = 2.0
GDM_PORT = 32414
GDM_ADDRESSES = ("255.255.255.255", "239.0.0.250")
GDM_WAIT = 1.5
SAVED_PLEX_SERVER = "plex.server"


class Connectivity(ABC):
    @abstractmethod
    async def internet(self) -> bool: ...

    @abstractmethod
    async def plex(self) -> PlexReach: ...


class NetConnectivity(Connectivity):
    def __init__(self, saved: SavedState) -> None:
        self.saved = saved

    async def internet(self) -> bool:
        try:
            async with asyncio.timeout(INTERNET_TIMEOUT):
                _, writer = await asyncio.open_connection(*INTERNET_HOST)
        except (OSError, TimeoutError) as e:
            log.info("no internet", extra={"error": str(e) or type(e).__name__})
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    async def plex(self) -> PlexReach:
        known = self.saved.get(SAVED_PLEX_SERVER)
        if isinstance(known, str) and await identity(known):
            return "ok"
        found = await discover()
        if found:
            server = known if known in found else found[0]
            if server != known:
                log.info("found a Plex server", extra={"server": server})
                self.saved.set(SAVED_PLEX_SERVER, server)
            return "ok"
        if isinstance(known, str):
            log.info("Plex server unreachable", extra={"server": known})
            return "unreachable"
        return "unknown"


async def identity(server: str) -> bool:
    """Whether a Plex server answers at `server` ("host:port"). Any HTTP answer counts."""
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=IDENTITY_TIMEOUT)) as http,
            http.get(f"http://{server}/identity") as resp,
        ):
            return resp.status < 500
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
        return False


class _GdmReplies(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.servers: list[str] = []

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        server = parse_gdm(data, addr[0])
        if server and server not in self.servers:
            self.servers.append(server)

    def error_received(self, exc: Exception) -> None:
        log.debug("GDM", extra={"error": str(exc)})


def parse_gdm(data: bytes, host: str) -> str | None:
    """A GDM reply's "host:port", if it's a media server's."""
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or " 200 " not in f"{lines[0]} ":
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if headers.get("content-type", "plex/media-server") != "plex/media-server":
        return None
    port = headers.get("port", "32400")
    return f"{host}:{port}" if port.isdigit() else None


async def discover(wait: float = GDM_WAIT) -> list[str]:
    """Plex servers on the LAN that answer GDM within `wait` seconds, as "host:port"."""
    loop = asyncio.get_running_loop()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("0.0.0.0", 0))
        transport, protocol = await loop.create_datagram_endpoint(_GdmReplies, sock=sock)
    except OSError as e:
        log.info("Plex discovery unavailable", extra={"error": str(e)})
        return []
    try:
        for address in GDM_ADDRESSES:
            with contextlib.suppress(OSError):
                transport.sendto(b"M-SEARCH * HTTP/1.1\r\n\r\n", (address, GDM_PORT))
        await asyncio.sleep(wait)
        return list(protocol.servers)
    finally:
        transport.close()


class FakeConnectivity(Connectivity):
    """Dev mode and tests: answers whatever it's set to, after a moment, like the real one."""

    def __init__(self, *, speed: float = 1.0) -> None:
        self.speed = speed
        self.online = True
        self.plex_reach: PlexReach = "ok"

    async def internet(self) -> bool:
        await asyncio.sleep((0.1 if self.online else 2.0) * self.speed)
        return self.online

    async def plex(self) -> PlexReach:
        await asyncio.sleep((0.1 if self.plex_reach == "ok" else 1.5) * self.speed)
        return self.plex_reach

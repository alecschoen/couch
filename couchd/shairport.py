"""AirPlay, through shairport-sync (#80): the real SpeakerBackend for source "airplay".

shairport-sync (Debian's package) runs in `couch-airplay.service` with Couch's config
(system/shairport-sync.conf), which couchd starts while the `speaker.enabled` setting is on.
Debian builds it for classic AirPlay (AirPlay 1), which every iPhone, iPad and Mac can still
play to; it shows up in their AirPlay menu as "Couch".

What's playing comes from shairport-sync's metadata pipe, a FIFO in /run/couch-speaker that
both run as `couch` to reach. Each item on it is

    <item><type>hex</type><code>hex</code><length>n</length>
    <data encoding="base64">
    ...</data></item>

where type and code are four letters as hex ("core"/"minm" is the title, "ssnc"/"pbeg" is
play begin; the list is in shairport-sync's rtsp.c). couchd keeps the FIFO open for reading
and writing, so it never sees an end of file when shairport-sync restarts, and opens it
again if it's replaced. Items while couchd was down are lost: after a couchd restart the
title shows from the next track or pause.

Remote control is DACP: the phone runs a small HTTP server that takes
`GET /ctrl-int/1/playpause` (nextitem, previtem, play, pause) with the session's
`Active-Remote` token. shairport-sync passes on the phone's address (`clip`), its token
(`acre`) and, once Avahi has found it, the port (`dapo`). Until all three are known there's
nothing to control and `controls` says so.

Session end is `pend` (play stream end) or `aend`; the unit's ExecStopPost also sends
`receiver_stopped` through couch-speaker-event, so a crash doesn't leave "playing" behind.
"""

import asyncio
import base64
import contextlib
import logging
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from couchd.backends import BackendError, Playback, SpeakerBackend, SpeakerControl
from couchd.display import systemctl

log = logging.getLogger(__name__)

UNIT = "couch-airplay.service"
BINARY = Path("/usr/bin/shairport-sync")
PIPE = Path("/run/couch-speaker/airplay-metadata")
REOPEN_EVERY = 2.0  # seconds between looks for the pipe, and checks it's still the same one
READ_SIZE = 65536
MAX_ITEM = 16 * 1024 * 1024  # an item this big without its end is junk: start over
RTP_RATE = 44100  # AirPlay 1's RTP clock, for `prgr`
DACP_TIMEOUT = 3.0
DACP_COMMANDS: dict[str, str] = {
    "play_pause": "playpause",
    "play": "play",
    "pause": "pause",
    "next": "nextitem",
    "previous": "previtem",
}

_ITEM = re.compile(
    rb"<item><type>([0-9a-fA-F]{1,8})</type><code>([0-9a-fA-F]{1,8})</code>"
    rb"<length>(\d+)</length>\s*(?:<data encoding=\"base64\">(.*?)</data>)?\s*</item>",
    re.S,
)

# Sends one DACP command: (host, port, command, active_remote). Raises BackendError.
DacpSend = Callable[[str, int, str, str], Awaitable[None]]
Systemctl = Callable[..., Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class Item:
    type: str  # "core", "ssnc"
    code: str  # "minm", "pbeg", ...
    data: bytes

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace").strip("\0")


def _four(hex_text: bytes) -> str:
    return int(hex_text, 16).to_bytes(4, "big").decode("latin-1")


class MetadataParser:
    """Turns what comes off the pipe, in any chunks, into Items. Anything it can't read
    is skipped up to the next `<item>`."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Item]:
        self._buffer += data
        while True:
            start = self._buffer.find(b"<item>")
            if start < 0:
                del self._buffer[: max(0, len(self._buffer) - 5)]  # keep a partial "<item"
                return
            del self._buffer[:start]
            end = self._buffer.find(b"</item>")
            if end < 0:
                if len(self._buffer) > MAX_ITEM:
                    log.warning("AirPlay metadata item too big, skipped")
                    del self._buffer[:6]
                    continue
                return
            chunk = bytes(self._buffer[: end + len(b"</item>")])
            del self._buffer[: len(chunk)]
            match = _ITEM.fullmatch(chunk)
            if match is None:
                # Not one item: something got cut. Drop up to the next item start.
                log.debug("unreadable AirPlay metadata item", extra={"start": chunk[:80]})
                nxt = chunk.find(b"<item>", 1)
                if nxt > 0:
                    self._buffer[:0] = chunk[nxt:]
                continue
            kind, code, length, payload = match.groups()
            try:
                body = base64.b64decode(payload, validate=False) if payload else b""
            except ValueError:
                continue
            if len(body) != int(length):
                log.debug("AirPlay metadata length mismatch")
            yield Item(_four(kind), _four(code), body)


async def dacp_send(host: str, port: int, command: str, active_remote: str) -> None:
    address = f"[{host}]" if ":" in host else host
    url = f"http://{address}:{port}/ctrl-int/1/{command}"
    timeout = aiohttp.ClientTimeout(total=DACP_TIMEOUT)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers={"Active-Remote": active_remote}) as response,
        ):
            if response.status >= 400:
                raise BackendError(f"The phone said no ({response.status})")
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        raise BackendError("The phone didn't answer") from e


class ShairportSpeaker(SpeakerBackend):
    source = "airplay"

    def __init__(
        self,
        *,
        pipe: Path = PIPE,
        binary: Path = BINARY,
        send: DacpSend = dacp_send,
        run: Systemctl = systemctl,
        clock: Callable[[], float] = time.time,
        reopen_every: float = REOPEN_EVERY,
    ) -> None:
        super().__init__()
        self._pipe = pipe
        self._binary = binary
        self._send = send
        self._systemctl = run
        self._clock = clock
        self._reopen_every = reopen_every
        self._task: asyncio.Task | None = None
        self._parser = MetadataParser()
        self._covers = 0  # bumped with every picture, across sessions
        self._ports: dict[str, int] = {}  # DACP-ID -> port, kept: Avahi tells us once
        self._shown = Playback()
        self._clear()

    def _clear(self) -> None:
        """Forget the session (not the DACP ports)."""
        self._active = False
        self._playing = False
        self._title: str | None = None
        self._artist: str | None = None
        self._album: str | None = None
        self._duration: int | None = None
        self._position: int | None = None
        self._position_at: int | None = None
        self._bundle: dict[str, object] | None = None  # between mdst and mden
        self._cover: tuple[bytes, str] | None = None
        self._device: str | None = None
        self._client: str | None = None  # clip: the phone's address
        self._dacp_id: str | None = None
        self._remote: str | None = None  # acre: Active-Remote

    def installed(self) -> bool:
        return self._binary.is_file()

    def playback(self) -> Playback:
        return self._shown

    def cover(self) -> tuple[bytes, str] | None:
        return self._cover if self._active else None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._read_pipe())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def set_enabled(self, on: bool) -> None:
        if on:
            await self._unit("reset-failed")
            await self._unit("start")
        else:
            await self._unit("stop")
            self._end()

    async def disconnect(self) -> None:
        await self._unit("restart")
        self._end()

    async def control(self, command: SpeakerControl) -> None:
        target = self._dacp()
        if target is None:
            raise BackendError("This phone can't be controlled from Couch")
        host, port, remote = target
        await self._send(host, port, DACP_COMMANDS[command], remote)

    async def _unit(self, verb: str) -> None:
        code, out = await self._systemctl(verb, UNIT)
        if code != 0 and verb != "reset-failed":
            raise BackendError(f"Couldn't {verb} AirPlay: {out or code}")

    def handle_event(self, event: str, env: dict[str, str]) -> None:
        if event == "receiver_stopped":
            self._end()

    # --- the pipe --------------------------------------------------------------------------

    async def _read_pipe(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            fd = self._open()
            if fd is None:
                await asyncio.sleep(self._reopen_every)
                continue
            log.info("reading AirPlay metadata", extra={"pipe": str(self._pipe)})
            self._parser = MetadataParser()
            loop.add_reader(fd, self._readable, fd)
            try:
                while True:
                    await asyncio.sleep(self._reopen_every)
                    if not self._same(fd):
                        break
                log.info("AirPlay metadata pipe replaced, opening it again")
            finally:
                loop.remove_reader(fd)
                os.close(fd)

    def _open(self) -> int | None:
        try:
            if not stat.S_ISFIFO(os.stat(self._pipe).st_mode):
                return None
            # Read and write: with a writer of our own, shairport-sync going away is no EOF.
            return os.open(self._pipe, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return None

    def _same(self, fd: int) -> bool:
        try:
            return os.stat(self._pipe).st_ino == os.fstat(fd).st_ino
        except OSError:
            return False

    def _readable(self, fd: int) -> None:
        while True:
            try:
                data = os.read(fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError as e:
                log.warning("AirPlay metadata pipe failed", extra={"error": str(e)})
                return
            if not data:
                return
            self.feed(data)

    def feed(self, data: bytes) -> None:
        """Bytes off the pipe (also for tests)."""
        for item in self._parser.feed(data):
            try:
                self._item(item)
            except (ValueError, IndexError) as e:
                log.debug("bad AirPlay metadata", extra={"code": item.code, "error": str(e)})
        self._emit()

    # --- what the items mean ---------------------------------------------------------------

    def _item(self, item: Item) -> None:
        if item.type == "core":
            self._track_field(item)
            return
        if item.type != "ssnc":
            return
        code = item.code
        if code == "pbeg":
            self._active = self._playing = True
            self._position_at = self._now()
        elif code in ("pend", "aend"):
            self._end(notify=False)
        elif code in ("pfls", "paus"):
            self._freeze()
            self._playing = False
        elif code in ("prsm", "pres", "pffr"):
            if self._active and not self._playing:
                self._position_at = self._now()
            self._playing = self._active
        elif code == "mdst":
            self._bundle = {}
        elif code == "mden":
            if self._bundle is not None:
                self._title = self._bundle.get("minm")  # type: ignore[assignment]
                self._artist = self._bundle.get("asar")  # type: ignore[assignment]
                self._album = self._bundle.get("asal")  # type: ignore[assignment]
                if "astm" in self._bundle:
                    self._duration = self._bundle["astm"]  # type: ignore[assignment]
            self._bundle = None
        elif code == "PICT":
            self._picture(item.data)
        elif code == "prgr":
            self._progress(item.text())
        elif code == "snam":
            self._device = item.text() or None
        elif code == "clip":
            self._client = item.text() or None
        elif code == "daid":
            self._dacp_id = item.text().lstrip("0").upper() or None
        elif code == "acre":
            self._remote = item.text() or None
        elif code == "dapo" and self._dacp_id:
            self._ports[self._dacp_id] = int(item.text())

    def _track_field(self, item: Item) -> None:
        if item.code in ("minm", "asar", "asal"):
            value: object = item.text() or None
        elif item.code == "astm" and len(item.data) == 4:
            value = int.from_bytes(item.data, "big") or None
        else:
            return
        if self._bundle is not None:
            self._bundle[item.code] = value
        elif item.code == "minm":
            self._title = value  # type: ignore[assignment]
        elif item.code == "asar":
            self._artist = value  # type: ignore[assignment]
        elif item.code == "asal":
            self._album = value  # type: ignore[assignment]
        else:
            self._duration = value  # type: ignore[assignment]

    def _picture(self, data: bytes) -> None:
        if data.startswith(b"\xff\xd8"):
            self._cover = (data, "image/jpeg")
        elif data.startswith(b"\x89PNG"):
            self._cover = (data, "image/png")
        else:
            self._cover = None
        self._covers += 1

    def _progress(self, text: str) -> None:
        start, current, end = (int(x) for x in text.split("/"))
        self._position = ((current - start) % 2**32) * 1000 // RTP_RATE
        self._position_at = self._now()
        length = ((end - start) % 2**32) * 1000 // RTP_RATE
        self._duration = length or self._duration

    def _freeze(self) -> None:
        """Paused: the position stops where it got to."""
        if self._playing and self._position is not None and self._position_at is not None:
            now = self._now()
            moved = self._position + now - self._position_at
            self._position = min(moved, self._duration) if self._duration else moved
            self._position_at = now

    def _end(self, *, notify: bool = True) -> None:
        self._clear()
        if notify:
            self._emit()

    def _dacp(self) -> tuple[str, int, str] | None:
        port = self._ports.get(self._dacp_id) if self._dacp_id else None
        if not (self._active and self._client and self._remote and port):
            return None
        return self._client, port, self._remote

    def _emit(self) -> None:
        shown = Playback()
        if self._active:
            controls = {"disconnect"}
            if self._dacp() is not None:
                controls |= set(DACP_COMMANDS)
            shown = Playback(
                active=True,
                playing=self._playing,
                title=self._title,
                artist=self._artist,
                album=self._album,
                device=self._device,
                duration_ms=self._duration,
                position_ms=self._position,
                position_at=self._position_at if self._position is not None else None,
                cover=self._covers if self._cover is not None else 0,
                controls=frozenset(controls),
            )
        if shown != self._shown:
            self._shown = shown
            self.changed()

    def _now(self) -> int:
        return int(self._clock() * 1000)

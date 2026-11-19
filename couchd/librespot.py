"""Spotify Connect, through librespot (#80): the real SpeakerBackend for source "spotify".

librespot (built by librespot/build.sh, installed as /opt/couch/bin/librespot) runs in
`couch-spotify.service`, which couchd starts while the `speaker.enabled` setting is on. It
logs in through the phone (zeroconf, so Spotify Premium on the phone and the same network),
and keeps its own credentials cache in /var/lib/couch/spotify. Nothing of Spotify's is
stored by couchd.

librespot runs `couch-speaker-event spotify` for every player event, with the details in
environment variables (librespot's src/player_event_handler.rs); the script posts them to
couchd's `speaker.event` command, which lands in `handle_event`. The unit's ExecStopPost runs
it once more with `receiver_stopped`, so a librespot that crashes or is stopped doesn't
leave "playing" behind.

librespot has no control interface (no D-Bus, no HTTP): nothing can ask it to pause or skip.
So `controls` is just "disconnect", which restarts the receiver: the session ends and the
phone stops playing to Couch.

The cover is one of Spotify's image URLs (librespot lists the widest first); it's
downloaded here so the launcher gets it from couchd like the AirPlay one.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import aiohttp

from couchd.backends import BackendError, Playback, SpeakerBackend
from couchd.display import systemctl

log = logging.getLogger(__name__)

UNIT = "couch-spotify.service"
BINARY = Path("/opt/couch/bin/librespot")
COVER_TIMEOUT = 10.0
COVER_MAX = 4 * 1024 * 1024
CONTROLS = frozenset({"disconnect"})  # all there is: see above

# Fetches a cover: its bytes and content type. Raises OSError or aiohttp.ClientError.
CoverFetch = Callable[[str], Awaitable[tuple[bytes, str]]]
Systemctl = Callable[..., Awaitable[tuple[int, str]]]


async def fetch_cover(url: str) -> tuple[bytes, str]:
    timeout = aiohttp.ClientTimeout(total=COVER_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        response.raise_for_status()
        if not response.content_type.startswith("image/"):
            raise aiohttp.ClientError(f"not an image: {response.content_type}")
        body = await response.content.read(COVER_MAX + 1)
        if len(body) > COVER_MAX:
            raise aiohttp.ClientError("cover too big")
        return body, response.content_type


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


class LibrespotSpeaker(SpeakerBackend):
    source = "spotify"

    def __init__(
        self,
        *,
        binary: Path = BINARY,
        fetch: CoverFetch = fetch_cover,
        run: Systemctl = systemctl,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._binary = binary
        self._fetch = fetch
        self._systemctl = run
        self._clock = clock
        self._playback = Playback()
        self._track: str | None = None  # TRACK_ID the metadata is for
        self._cover: tuple[bytes, str] | None = None
        self._cover_url: str | None = None
        self._covers = 0
        self._device: str | None = None
        self._cover_task: asyncio.Task | None = None

    def installed(self) -> bool:
        return self._binary.is_file()

    def playback(self) -> Playback:
        return self._playback

    def cover(self) -> tuple[bytes, str] | None:
        return self._cover if self._playback.active else None

    async def stop(self) -> None:
        if self._cover_task is not None:
            self._cover_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cover_task

    async def set_enabled(self, on: bool) -> None:
        if on:
            await self._unit("reset-failed")
            await self._unit("start")
        else:
            await self._unit("stop")
            self._reset()

    async def disconnect(self) -> None:
        # No way to tell librespot to let go; a new librespot is a new, empty session.
        await self._unit("restart")
        self._reset()

    async def _unit(self, verb: str) -> None:
        code, out = await self._systemctl(verb, UNIT)
        if code != 0 and verb != "reset-failed":
            raise BackendError(f"Couldn't {verb} Spotify: {out or code}")

    # --- events --------------------------------------------------------------------------

    def handle_event(self, event: str, env: dict[str, str]) -> None:
        p = self._playback
        if event == "track_changed":
            self._track_changed(env)
        elif event in ("playing", "paused"):
            # After a couchd restart this comes before any track_changed: no title yet.
            self._playback = replace(
                p,
                active=True,
                playing=event == "playing",
                device=self._device,
                controls=CONTROLS,
                **self._position(env),
            )
        elif event in ("seeked", "position_correction"):
            self._playback = replace(p, **self._position(env))
        elif event == "session_client_changed":
            self._device = env.get("CLIENT_NAME") or None
            if p.active:
                self._playback = replace(p, device=self._device)
        elif event in ("stopped", "session_disconnected", "receiver_stopped"):
            if event != "stopped":
                self._device = None
            self._reset(notify=False)
        else:
            return  # loading, preloading, end_of_track, volume_changed, ...: nothing to show
        self.changed()

    def _track_changed(self, env: dict[str, str]) -> None:
        episode = env.get("ITEM_TYPE") == "Episode"
        artists = [a for a in env.get("ARTISTS", "").split("\n") if a]
        covers = [c for c in env.get("COVERS", "").split("\n") if c]
        self._track = env.get("TRACK_ID")
        p = self._playback
        self._playback = replace(
            p,
            active=True,
            title=env.get("NAME") or None,
            artist=(env.get("SHOW_NAME") if episode else ", ".join(artists)) or None,
            album=None if episode else env.get("ALBUM") or None,
            device=self._device,
            duration_ms=_int(env.get("DURATION_MS")),
            position_ms=0,
            position_at=self._now(),
            controls=CONTROLS,
        )
        url = covers[0] if covers else None
        if url != self._cover_url:
            self._cover_url = url
            self._cover = None
            self._playback = replace(self._playback, cover=0)
            if self._cover_task is not None:
                self._cover_task.cancel()
            self._cover_task = asyncio.create_task(self._load_cover(url)) if url else None

    async def _load_cover(self, url: str) -> None:
        try:
            cover = await self._fetch(url)
        except (OSError, aiohttp.ClientError, TimeoutError) as e:
            log.info("no Spotify cover", extra={"error": str(e)})
            return
        if url != self._cover_url:
            return
        self._cover = cover
        self._covers += 1
        self._playback = replace(self._playback, cover=self._covers)
        self.changed()

    def _position(self, env: dict[str, str]) -> dict:
        position = _int(env.get("POSITION_MS"))
        if position is None:
            return {}
        return {"position_ms": position, "position_at": self._now()}

    def _reset(self, *, notify: bool = True) -> None:
        was = self._playback
        self._playback = Playback()
        self._track = None
        self._cover = None
        self._cover_url = None
        if self._cover_task is not None:
            self._cover_task.cancel()
            self._cover_task = None
        if notify and was != self._playback:
            self.changed()

    def _now(self) -> int:
        return int(self._clock() * 1000)

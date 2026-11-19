"""Couch as a speaker (#80): the service with pretend receivers, and both real receivers'
event handling without librespot or shairport-sync."""

import asyncio
import base64
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

from couchd.api import create_app
from couchd.backends import BackendError
from couchd.bus import EventBus
from couchd.commands import CommandError, Commands
from couchd.fakes import FAKE_TRACKS, FakeSpeaker
from couchd.librespot import LibrespotSpeaker
from couchd.services import Context
from couchd.shairport import Item, MetadataParser, ShairportSpeaker
from couchd.speaker import SpeakerService
from couchd.state import LiveState, SavedState

ROOT = Path(__file__).resolve().parent.parent
EVENT_SCRIPT = ROOT / "system" / "couch-speaker-event"
ALL_CONTROLS = ["play_pause", "play", "pause", "next", "previous", "disconnect"]


async def until(condition):
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    ctx = Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())
    ctx.state.update("apps", {"foreground": "launcher", "opening": None})
    ctx.state.update("tv", {"power": "on", "active": True})
    return ctx


@pytest.fixture
def ran(ctx):
    """The take-over's commands, as the TV and app services would do them."""
    ran: list[str] = []

    async def power_on(args):
        ran.append("tv.power_on")
        ctx.state.update("tv", {"power": "on", "active": True})

    async def home(args):
        ran.append("apps.home")
        ctx.state.update("apps", {"foreground": "launcher", "opening": None})

    ctx.commands.add("tv.power_on", power_on)
    ctx.commands.add("apps.home", home)
    return ran


@pytest.fixture
async def receivers(ctx, ran):
    spotify, airplay = FakeSpeaker("spotify"), FakeSpeaker("airplay")
    service = SpeakerService(ctx, [spotify, airplay])
    await service.start()
    await until(lambda: ctx.state.get("speaker")["receivers"] == {"spotify": "on", "airplay": "on"})
    yield spotify, airplay
    await service.stop()


def speaker(ctx):
    return ctx.state.get("speaker")


# --- the service --------------------------------------------------------------------------


async def test_nothing_playing(ctx, receivers):
    spotify, airplay = receivers
    assert spotify.enabled and airplay.enabled
    assert speaker(ctx) == {
        "name": "Couch",
        "receivers": {"spotify": "on", "airplay": "on"},
        "source": None,
        "playing": False,
        "title": None,
        "artist": None,
        "album": None,
        "device": None,
        "cover": None,
        "duration_ms": None,
        "position_ms": None,
        "position_at": None,
        "controls": [],
    }
    assert ctx.files.get("speaker-cover") is None
    with pytest.raises(CommandError, match="Nothing is playing to Couch"):
        await ctx.commands.run("speaker.play_pause")


async def test_what_plays_is_published_with_its_cover(ctx, receivers):
    _, airplay = receivers
    airplay.play()
    s = speaker(ctx)
    title, artist, album, seconds, _ = FAKE_TRACKS[0]
    assert s["source"] == "airplay" and s["playing"]
    assert (s["title"], s["artist"], s["album"]) == (title, artist, album)
    assert s["device"] == "Pretend iPhone"
    assert s["duration_ms"] == seconds * 1000
    assert s["position_ms"] == 0 and isinstance(s["position_at"], int)
    assert s["cover"] == "/api/file/speaker-cover?v=airplay-1"
    assert s["controls"] == ALL_CONTROLS
    body, content_type = ctx.files.get("speaker-cover")
    assert content_type == "image/png" and body.startswith(b"\x89PNG")
    await ctx.commands.run("speaker.next")
    assert speaker(ctx)["title"] == FAKE_TRACKS[1][0]
    assert speaker(ctx)["cover"] == "/api/file/speaker-cover?v=airplay-2"
    await ctx.commands.run("speaker.pause")
    assert not speaker(ctx)["playing"] and speaker(ctx)["source"] == "airplay"
    await ctx.commands.run("speaker.disconnect")
    assert speaker(ctx)["source"] is None
    assert ctx.files.get("speaker-cover") is None


async def test_spotify_can_only_be_disconnected(ctx, receivers):
    spotify, _ = receivers
    spotify.play()
    assert speaker(ctx)["controls"] == ["disconnect"]
    with pytest.raises(CommandError, match="can't be controlled"):
        await ctx.commands.run("speaker.next")
    await ctx.commands.run("speaker.disconnect")
    assert not spotify.playback().active


async def test_music_wakes_the_tv_and_goes_home(ctx, ran, receivers):
    _, airplay = receivers
    ctx.state.update("tv", {"power": "standby", "active": False})
    ctx.state.update("apps", {"foreground": "youtube"})
    await settle()
    airplay.play()
    await until(lambda: len(ran) == 2)
    assert ran == ["tv.power_on", "apps.home"]
    # YouTube closing is no reason to stop the music
    assert airplay.controlled == []
    assert speaker(ctx)["playing"]


async def test_nothing_to_take_over_on_the_launcher(ctx, ran, receivers):
    _, airplay = receivers
    airplay.play()
    await settle()
    assert ran == []
    # a pause and play again is no new start either
    await ctx.commands.run("speaker.pause")
    ctx.state.update("tv", {"active": False})
    await ctx.commands.run("speaker.play")
    await until(lambda: ran == ["tv.power_on"])


async def test_an_app_still_opening_is_left_for_home(ctx, ran, receivers):
    _, airplay = receivers
    ctx.state.update("apps", {"opening": "plex"})
    airplay.play()
    await until(lambda: ran == ["apps.home"])


async def test_take_over_can_be_turned_off(ctx, ran, receivers):
    _, airplay = receivers
    ctx.settings.set("speaker.take_over", False)
    ctx.state.update("tv", {"active": False})
    ctx.state.update("apps", {"foreground": "youtube"})
    await settle()
    airplay.play()
    await settle()
    assert ran == []
    # and opening an app while music plays then leaves it playing
    ctx.state.update("apps", {"foreground": "launcher"})
    await settle()
    ctx.state.update("apps", {"foreground": "youtube"})
    await settle()
    assert airplay.controlled == []


async def test_opening_an_app_stops_the_music(ctx, receivers):
    spotify, airplay = receivers
    airplay.play()
    await settle()
    ctx.state.update("apps", {"foreground": "youtube"})
    await until(lambda: airplay.controlled == ["pause"])
    assert speaker(ctx)["source"] == "airplay" and not speaker(ctx)["playing"]
    # Spotify can't pause from here: it's disconnected
    ctx.state.update("apps", {"foreground": "launcher"})
    spotify.play()
    await settle()
    ctx.state.update("apps", {"foreground": "plex"})
    await until(lambda: not spotify.playback().active)


async def test_the_newest_phone_wins(ctx, receivers):
    spotify, airplay = receivers
    airplay.play()
    spotify.play()
    assert speaker(ctx)["source"] == "spotify"
    await until(lambda: airplay.controlled == ["pause"])
    assert airplay.playback().active  # paused, still there
    assert speaker(ctx)["source"] == "spotify"
    # AirPlay again: Spotify, which can't be paused, is disconnected
    await ctx.commands.run("speaker.disconnect")  # Spotify's gone; AirPlay is shown
    assert speaker(ctx)["source"] == "airplay" and not speaker(ctx)["playing"]
    spotify.play()
    await settle()
    airplay.play()
    await until(lambda: not spotify.playback().active)
    assert speaker(ctx)["source"] == "airplay" and speaker(ctx)["playing"]


async def test_the_remote_controls_the_music_on_the_launcher(ctx, receivers):
    _, airplay = receivers
    airplay.play()
    await settle()

    def key(name, **extra):
        ctx.bus.publish(
            "key", {"key": name, "repeat": False, "hold": False, "app": "launcher"} | extra
        )

    key("fast_forward")
    await until(lambda: airplay.controlled == ["next"])
    key("rewind")
    key("play_pause")
    await until(lambda: airplay.controlled == ["next", "previous", "play_pause"])
    assert not speaker(ctx)["playing"]
    key("pause")  # already paused: nothing to do
    key("play")
    await until(lambda: airplay.controlled[-1] == "play")
    key("stop")
    await until(lambda: airplay.controlled[-1] == "pause")
    count = len(airplay.controlled)
    # not while an app, the overlay or the launcher's raw keys have them, nor repeats
    key("play_pause", app="youtube")
    key("play_pause", overlay=True)
    key("play_pause", raw=True)
    key("play_pause", repeat=True)
    key("select")
    await settle()
    assert len(airplay.controlled) == count


async def test_media_keys_with_spotify_do_nothing(ctx, receivers):
    spotify, _ = receivers
    spotify.play()
    await settle()
    ctx.bus.publish("key", {"key": "play_pause", "app": "launcher"})
    await settle()
    assert speaker(ctx)["playing"]


async def test_the_setting_switches_the_receivers(ctx, receivers):
    spotify, airplay = receivers
    airplay.play()
    ctx.settings.set("speaker.enabled", False)
    await until(lambda: speaker(ctx)["receivers"] == {"spotify": "off", "airplay": "off"})
    assert not spotify.enabled and not airplay.enabled
    assert speaker(ctx)["source"] is None
    ctx.settings.set("speaker.enabled", True)
    await until(lambda: speaker(ctx)["receivers"] == {"spotify": "on", "airplay": "on"})
    assert spotify.enabled and airplay.enabled


class Missing(FakeSpeaker):
    def installed(self):
        return False


class Broken(FakeSpeaker):
    async def set_enabled(self, on):
        raise BackendError("no")


async def test_receivers_that_are_missing_or_fail(ctx):
    service = SpeakerService(ctx, [Missing("spotify"), Broken("airplay")])
    await service.start()
    try:
        await until(
            lambda: speaker(ctx)["receivers"] == {"spotify": "missing", "airplay": "failed"}
        )
    finally:
        await service.stop()


async def test_dev_commands_only_with_pretend_receivers(ctx, ran, receivers):
    await ctx.commands.run("speaker.dev.play", {"source": "spotify"})
    assert speaker(ctx)["source"] == "spotify"
    await ctx.commands.run("speaker.dev.stop", {"source": "spotify"})
    assert speaker(ctx)["source"] is None
    with pytest.raises(CommandError):
        await ctx.commands.run("speaker.dev.play", {"source": "radio"})


async def test_stop_leaves_the_commands_and_cover(ctx, ran):
    service = SpeakerService(ctx, [FakeSpeaker("airplay")])
    await service.start()
    await service.stop()
    assert not [n for n in ctx.commands.names() if n.startswith("speaker.")]
    assert ctx.files.get("speaker-cover") is None
    await service.start()  # and it can start again (couchd's restarts)
    await service.stop()


# --- events from librespot, through the service --------------------------------------------


class Systemctl:
    def __init__(self, fail: set[str] = frozenset()):
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail

    async def __call__(self, *args):
        self.calls.append(args)
        return (1, "nope") if args[0] in self.fail else (0, "")


class Covers:
    def __init__(self, fail=False):
        self.urls: list[str] = []
        self.fail = fail

    async def __call__(self, url):
        self.urls.append(url)
        if self.fail:
            raise OSError("offline")
        return b"\xff\xd8jpeg", "image/jpeg"


TRACK = {
    "TRACK_ID": "4uLU6hMCjMI75M1A2tKUQC",
    "NAME": "Never Gonna Give You Up",
    "ARTISTS": "Rick Astley\nSomeone Else",
    "ALBUM": "Whenever You Need Somebody",
    "COVERS": "https://i.scdn.co/image/big\nhttps://i.scdn.co/image/small",
    "DURATION_MS": "213573",
    "ITEM_TYPE": "Track",
}


@pytest.fixture
def clock():
    now = [1000.0]
    return now


@pytest.fixture
def librespot(tmp_path, clock):
    binary = tmp_path / "librespot"
    binary.write_text("")
    return LibrespotSpeaker(binary=binary, fetch=Covers(), run=Systemctl(), clock=lambda: clock[0])


async def test_spotify_events_through_the_command(ctx, ran, librespot):
    service = SpeakerService(ctx, [librespot])
    await service.start()
    try:
        await until(lambda: speaker(ctx)["receivers"] == {"spotify": "on"})
        assert librespot._systemctl.calls == [
            ("reset-failed", "couch-spotify.service"),
            ("start", "couch-spotify.service"),
        ]
        event = {"source": "spotify", "event": "session_client_changed"}
        await ctx.commands.run("speaker.event", event | {"env": {"CLIENT_NAME": "Pixel 9"}})
        await ctx.commands.run(
            "speaker.event", {"source": "spotify", "event": "track_changed", "env": TRACK}
        )
        await ctx.commands.run(
            "speaker.event",
            {"source": "spotify", "event": "playing", "env": {"POSITION_MS": "1500"}},
        )
        await until(lambda: speaker(ctx)["cover"] is not None)
        s = speaker(ctx)
        assert s["source"] == "spotify" and s["playing"]
        assert s["title"] == "Never Gonna Give You Up"
        assert s["artist"] == "Rick Astley, Someone Else"
        assert s["album"] == "Whenever You Need Somebody"
        assert s["device"] == "Pixel 9"
        assert (s["duration_ms"], s["position_ms"], s["position_at"]) == (213573, 1500, 1_000_000)
        assert s["controls"] == ["disconnect"]
        assert s["cover"] == "/api/file/speaker-cover?v=spotify-1"
        assert librespot._fetch.urls == ["https://i.scdn.co/image/big"]  # the widest
        assert ctx.files.get("speaker-cover") == (b"\xff\xd8jpeg", "image/jpeg")
        with pytest.raises(CommandError, match="unknown source"):
            await ctx.commands.run("speaker.event", {"source": "radio", "event": "playing"})
        with pytest.raises(CommandError, match="env"):
            await ctx.commands.run(
                "speaker.event", {"source": "spotify", "event": "playing", "env": {"A": 1}}
            )
        await ctx.commands.run("speaker.event", {"source": "spotify", "event": "receiver_stopped"})
        assert speaker(ctx)["source"] is None and not speaker(ctx)["playing"]
    finally:
        await service.stop()


# --- librespot ------------------------------------------------------------------------------


async def test_librespot_play_pause_seek_stop(librespot, clock):
    changes = []
    librespot.on_change(lambda: changes.append(librespot.playback()))
    librespot.handle_event("track_changed", TRACK)
    clock[0] = 1001.0
    librespot.handle_event("playing", {"POSITION_MS": "0"})
    p = librespot.playback()
    assert p.active and p.playing and p.position_at == 1_001_000
    clock[0] = 1010.0
    librespot.handle_event("paused", {"POSITION_MS": "9000"})
    p = librespot.playback()
    assert not p.playing and (p.position_ms, p.position_at) == (9000, 1_010_000)
    librespot.handle_event("seeked", {"POSITION_MS": "60000"})
    assert librespot.playback().position_ms == 60000
    librespot.handle_event("loading", {})  # nothing to show
    librespot.handle_event("volume_changed", {"VOLUME": "100"})
    assert len(changes) == 4
    librespot.handle_event("stopped", {})
    assert not librespot.playback().active
    assert librespot.cover() is None
    await librespot.stop()


async def test_librespot_podcasts(librespot):
    episode = {
        "TRACK_ID": "x",
        "NAME": "Episode 12",
        "SHOW_NAME": "A Podcast",
        "ITEM_TYPE": "Episode",
        "DURATION_MS": "3600000",
    }
    librespot.handle_event("track_changed", episode)
    p = librespot.playback()
    assert (p.title, p.artist, p.album, p.cover) == ("Episode 12", "A Podcast", None, 0)
    assert librespot._fetch.urls == []  # no cover


async def test_librespot_after_a_couchd_restart(librespot):
    # no track_changed seen: playing anyway, without a title
    librespot.handle_event("playing", {"POSITION_MS": "42000"})
    p = librespot.playback()
    assert p.active and p.playing and p.title is None and p.position_ms == 42000


async def test_librespot_cover_failures_are_no_cover(tmp_path):
    binary = tmp_path / "librespot"
    binary.write_text("")
    backend = LibrespotSpeaker(binary=binary, fetch=Covers(fail=True), run=Systemctl())
    backend.handle_event("track_changed", TRACK)
    await settle()
    assert backend.playback().cover == 0 and backend.cover() is None
    await backend.stop()


async def test_librespot_unit(tmp_path):
    run = Systemctl(fail={"start"})
    backend = LibrespotSpeaker(binary=tmp_path / "none", fetch=Covers(), run=run)
    assert not backend.installed()
    with pytest.raises(BackendError, match="Couldn't start Spotify"):
        await backend.set_enabled(True)
    backend.handle_event("playing", {})
    await backend.disconnect()  # a new librespot: the phone lets go
    assert not backend.playback().active
    await backend.set_enabled(False)
    assert [c[0] for c in run.calls] == ["reset-failed", "start", "restart", "stop"]
    assert {c[1] for c in run.calls} == {"couch-spotify.service"}
    with pytest.raises(BackendError):
        await backend.control("next")


# --- shairport-sync -------------------------------------------------------------------------


def item(kind: str, code: str, data: bytes | str | None = None) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    head = (
        f"<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code>"
        f"<length>{len(data or b'')}</length>"
    )
    if data is None:
        return f"{head}</item>\n".encode()
    payload = base64.b64encode(data).decode()
    return f'{head}\n<data encoding="base64">\n{payload}</data></item>\n'.encode()


def test_parser_takes_any_chunks_and_skips_junk():
    stream = (
        b"junk"
        + item("ssnc", "pbeg")
        + b"<item><type>zz</type></item>"
        + item("core", "minm", "Héllo")
        + item("ssnc", "PICT", b"\xff\xd8\x00")
    )
    parser = MetadataParser()
    whole = list(parser.feed(stream))
    assert whole == [
        Item("ssnc", "pbeg", b""),
        Item("core", "minm", "Héllo".encode()),
        Item("ssnc", "PICT", b"\xff\xd8\x00"),
    ]
    parser = MetadataParser()
    bytewise = [i for b in range(len(stream)) for i in parser.feed(stream[b : b + 1])]
    assert bytewise == whole


@pytest.fixture
def dacp():
    sent = []

    async def send(host, port, command, remote):
        sent.append((host, port, command, remote))

    return sent, send


@pytest.fixture
def shairport(tmp_path, dacp, clock):
    binary = tmp_path / "shairport-sync"
    binary.write_text("")
    return ShairportSpeaker(
        pipe=tmp_path / "pipe",
        binary=binary,
        send=dacp[1],
        run=Systemctl(),
        clock=lambda: clock[0],
        reopen_every=0.02,
    )


def session_start(port: bool = True) -> bytes:
    out = (
        item("ssnc", "snam", "Alec's iPhone")
        + item("ssnc", "clip", "192.168.1.20")
        + item("ssnc", "daid", "00A1B2C3D4E5F607")
        + item("ssnc", "acre", "1234567890")
        + item("ssnc", "pbeg")
    )
    if port:
        out += item("ssnc", "dapo", "49152")
    return out


TRACK_ITEMS = (
    item("ssnc", "mdst", "1146")
    + item("core", "minm", "Clair de Lune")
    + item("core", "asar", "Debussy")
    + item("core", "asal", "Suite bergamasque")
    + item("core", "astm", (300_000).to_bytes(4, "big"))
    + item("ssnc", "mden", "1146")
)


async def test_airplay_session(shairport, dacp, clock):
    sent, _ = dacp
    changes = []
    shairport.on_change(lambda: changes.append(shairport.playback()))
    shairport.feed(session_start() + TRACK_ITEMS)
    p = shairport.playback()
    assert p.active and p.playing
    assert (p.title, p.artist, p.album, p.duration_ms) == (
        "Clair de Lune",
        "Debussy",
        "Suite bergamasque",
        300_000,
    )
    assert p.device == "Alec's iPhone"
    assert p.controls == {"play_pause", "play", "pause", "next", "previous", "disconnect"}
    assert len(changes) == 1  # one chunk, one change
    # where it is: start/current/end in 44.1 kHz RTP frames
    shairport.feed(item("ssnc", "prgr", f"{2**32 - 44100}/{44100 * 9}/{44100 * 299}"))
    p = shairport.playback()
    assert (p.position_ms, p.position_at, p.duration_ms) == (10_000, 1_000_000, 300_000)
    # a picture
    shairport.feed(item("ssnc", "PICT", b"\xff\xd8cover"))
    assert shairport.playback().cover == 1
    assert shairport.cover() == (b"\xff\xd8cover", "image/jpeg")
    # paused on the phone: the position stops where it got to
    clock[0] = 1005.0
    shairport.feed(item("ssnc", "pfls"))
    p = shairport.playback()
    assert not p.playing and (p.position_ms, p.position_at) == (15_000, 1_005_000)
    clock[0] = 1100.0
    shairport.feed(item("ssnc", "prsm"))
    p = shairport.playback()
    assert p.playing and (p.position_ms, p.position_at) == (15_000, 1_100_000)
    # the remote
    await shairport.control("next")
    await shairport.control("play_pause")
    assert sent == [
        ("192.168.1.20", 49152, "nextitem", "1234567890"),
        ("192.168.1.20", 49152, "playpause", "1234567890"),
    ]
    # the phone goes
    shairport.feed(item("ssnc", "pend"))
    assert not shairport.playback().active and shairport.cover() is None
    with pytest.raises(BackendError):
        await shairport.control("pause")


async def test_airplay_without_a_dacp_port_can_only_be_disconnected(shairport):
    shairport.feed(session_start(port=False))
    assert shairport.playback().controls == {"disconnect"}
    with pytest.raises(BackendError, match="can't be controlled"):
        await shairport.control("pause")
    # the port arrives late (Avahi), and is remembered for that phone's next session
    shairport.feed(item("ssnc", "dapo", "50000"))
    assert "pause" in shairport.playback().controls
    shairport.feed(item("ssnc", "pend") + session_start(port=False))
    assert "pause" in shairport.playback().controls


async def test_airplay_receiver_stopping_ends_the_session(shairport):
    shairport.feed(session_start())
    shairport.handle_event("receiver_stopped", {})
    assert not shairport.playback().active
    shairport.feed(session_start())
    await shairport.disconnect()
    assert not shairport.playback().active
    await shairport.set_enabled(False)
    assert shairport._systemctl.calls == [
        ("restart", "couch-airplay.service"),
        ("stop", "couch-airplay.service"),
    ]


async def test_airplay_reads_the_pipe(shairport, tmp_path):
    pipe = tmp_path / "pipe"
    await shairport.start()
    try:
        await asyncio.sleep(0.05)  # no pipe yet: it keeps looking
        os.mkfifo(pipe, 0o600)
        await asyncio.sleep(0.1)
        # what shairport-sync does: open for writing without blocking
        fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, session_start() + TRACK_ITEMS[:40])
        os.write(fd, TRACK_ITEMS[40:])
        os.close(fd)  # shairport-sync going away is no end for couchd
        await until(lambda: shairport.playback().title == "Clair de Lune")
        # a new pipe (shairport-sync's unit started again): opened again
        pipe.unlink()
        os.mkfifo(pipe, 0o600)
        await asyncio.sleep(0.1)
        fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, item("ssnc", "pfls"))
        os.close(fd)
        await until(lambda: not shairport.playback().playing)
    finally:
        await shairport.stop()


# --- couch-speaker-event ----------------------------------------------------------------------


@pytest.fixture
async def couchd():
    """couchd's API on a real port, with a speaker.event that writes down what it gets."""
    got: list[dict] = []
    commands = Commands()

    async def event(args):
        got.append(args)

    commands.add("speaker.event", event)
    bus = EventBus()
    runner = web.AppRunner(create_app(bus=bus, state=LiveState(bus), commands=commands))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}", got
    await runner.cleanup()


async def hook(api: str, *args: str, **env: str) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(EVENT_SCRIPT),
        *args,
        env={"COUCH_API": api, "PATH": os.environ.get("PATH", "")} | env,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    return process.returncode


async def test_the_event_hook_posts_librespots_events(couchd):
    api, got = couchd
    env = {k: TRACK[k] for k in ("TRACK_ID", "NAME", "ARTISTS", "COVERS")}
    code = await hook(api, "spotify", PLAYER_EVENT="track_changed", SECRET="x", **env)
    assert code == 0
    assert got == [{"source": "spotify", "event": "track_changed", "env": env}]
    # the units' ExecStopPost names the event itself
    assert await hook(api, "airplay", "receiver_stopped") == 0
    assert got[-1] == {"source": "airplay", "event": "receiver_stopped", "env": {}}
    # events couchd has no use for aren't sent
    assert await hook(api, "spotify", PLAYER_EVENT="volume_changed", VOLUME="1") == 0
    assert len(got) == 2


async def test_the_event_hook_never_fails_librespot(tmp_path):
    # nothing listening there
    assert await hook("http://127.0.0.1:9", "spotify", PLAYER_EVENT="playing") == 0
    assert await hook("http://127.0.0.1:9") == 0  # no source: usage, still 0


def test_the_event_hook_uses_the_standard_library_only():
    import ast

    tree = ast.parse(EVENT_SCRIPT.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= sys.stdlib_module_names, names - sys.stdlib_module_names
    assert EVENT_SCRIPT.read_text().startswith("#!/usr/bin/python3 -I\n")

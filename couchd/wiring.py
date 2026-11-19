"""Which services run, with which backends. The only place that knows dev from device."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from couchd.apps import AppService
from couchd.audio import AudioService
from couchd.backends import BackendError
from couchd.bluetooth import BluetoothService
from couchd.bluez import BluezBluetooth
from couchd.cec_tv import CecTv
from couchd.chromium import Chromium
from couchd.display import FakeDisplay, SystemdDisplay
from couchd.evening import EveningService
from couchd.fakes import FakeAudio, FakeBluetooth, FakeKeyboard, FakeTv, FakeWifi
from couchd.idle import IdleService
from couchd.kodi import FakeKodi, KodiClient
from couchd.kodi_rpc import KodiRpc
from couchd.kodi_sounds import KodiSoundsService
from couchd.network import FakeConnectivity, NetConnectivity
from couchd.networkmanager import NmWifi
from couchd.overlay import OverlayService
from couchd.pipewire import PipewireAudio
from couchd.remote import RemoteService
from couchd.services import Context, Service
from couchd.splash import SplashService
from couchd.startup import StartupService
from couchd.system import FakeSystem, LinuxSystem, SystemService
from couchd.tv import TvService
from couchd.tv_volume import TvVolumeService
from couchd.uinput import UinputKeyboard
from couchd.updates import FakeUpdates, SystemdUpdates, UpdatesService, quiet_start
from couchd.weather import WeatherService, fake_forecast
from couchd.wifi import WifiService

log = logging.getLogger(__name__)


def build_services(ctx: Context, *, dev: bool) -> list[Service]:
    if dev:
        tv = FakeTv(power="standby", wake_time=2.0)
        audio = FakeAudio()
        bluetooth = FakeBluetooth(audio)
        apps, overlay = _apps(ctx, dev=True)
        sound = AudioService(ctx, audio)
        return [
            StartupService(ctx),
            TvService(ctx, tv),
            RemoteService(ctx, tv, FakeKeyboard(), overlay=overlay),
            sound,
            TvVolumeService(ctx, tv, sound),
            BluetoothService(ctx, bluetooth),
            WifiService(ctx, FakeWifi()),
            *apps,
            overlay,
            SystemService(ctx, FakeSystem()),
            UpdatesService(ctx, FakeUpdates()),
        ]
    tv = CecTv()
    apps, overlay = _apps(ctx, dev=False)
    sound = AudioService(ctx, PipewireAudio())
    # After an overnight update (#24) couchd restarts with the TV off; leave it off.
    quiet = quiet_start()
    if quiet:
        log.info("restarted by couch-update: not waking the TV")
    return [
        StartupService(ctx),
        TvService(ctx, tv, wake_on_start=not quiet),
        RemoteService(ctx, tv, UinputKeyboard(), overlay=overlay),
        sound,
        TvVolumeService(ctx, tv, sound),
        BluetoothService(ctx, BluezBluetooth()),
        WifiService(ctx, NmWifi()),
        *apps,
        overlay,
        SystemService(ctx, LinuxSystem()),
        UpdatesService(ctx, SystemdUpdates()),
    ]


def _apps(ctx: Context, *, dev: bool) -> tuple[list[Service], OverlayService]:
    """What's on screen (#13): the launcher, Chromium for YouTube over DevTools, plus Kodi,
    real or pretend. Before an
    app opens, the internet and the Plex server are checked (#19). The overlays (#18) draw
    over whichever is in front, Kodi's through its JSON-RPC (pretend in dev mode). The splash
    service ends the session's boot splash once the launcher has drawn (#23)."""
    display = _dev_display() if dev else SystemdDisplay()
    net = FakeConnectivity() if dev else NetConnectivity(ctx.saved)
    rpc = None if dev else KodiRpc()  # one client for both: pausing and the overlays
    chromium = Chromium()
    kodi = FakeKodi() if rpc is None else KodiClient(rpc)
    apps = AppService(
        ctx,
        display,
        chromium,
        connectivity=net,
        kodi_rpc=rpc,
    )
    overlay = OverlayService(ctx, chromium=chromium, pages=apps.page, kodi=kodi, go_home=apps.home)
    services = [
        apps,
        KodiSoundsService(ctx, kodi),  # Plex's clicks follow the UI sounds setting (#49)
        IdleService(ctx, idle_after=_idle_after()),
        EveningService(ctx),
        # The weather (#82): Open-Meteo on the Pi, a made-up forecast in dev mode.
        WeatherService(ctx, fetch=fake_forecast() if dev else None),
    ]
    return [*services, SplashService(ctx)], overlay


def _dev_display() -> FakeDisplay:
    """couchd starts Chromium only for YouTube (on the Pi, through
    couch-chromium.service). Dev mode only pretends, unless COUCH_DEV_CHROMIUM_START and
    COUCH_DEV_CHROMIUM_STOP are shell commands that do it, e.g. into a nested cage."""
    start = os.environ.get("COUCH_DEV_CHROMIUM_START")
    stop = os.environ.get("COUCH_DEV_CHROMIUM_STOP")
    if not (start and stop):
        return FakeDisplay()

    def run(command: str) -> Callable[[], Awaitable[None]]:
        async def go() -> None:
            proc = await asyncio.create_subprocess_shell(command)
            if await proc.wait() != 0:
                raise BackendError(f"{command!r} failed")

        return go

    return FakeDisplay(chromium=(run(start), run(stop)))


def _idle_after() -> float | None:
    """`COUCH_IDLE_AFTER=10 ./scripts/dev.sh` brings the idle clock up after 10 s (#20)."""
    raw = os.environ.get("COUCH_IDLE_AFTER")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        log.warning("COUCH_IDLE_AFTER isn't a number, ignored", extra={"value": raw})
        return None

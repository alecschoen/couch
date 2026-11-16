"""Which services run, with which backends. The only place that knows dev from device."""

import logging
import os

from couchd.apps import AppService
from couchd.audio import AudioService
from couchd.bluetooth import BluetoothService
from couchd.bluez import BluezBluetooth
from couchd.cec_tv import CecTv
from couchd.chromium import Chromium
from couchd.display import FakeDisplay, SystemdDisplay
from couchd.evening import EveningService
from couchd.fakes import FakeAudio, FakeBluetooth, FakeKeyboard, FakeTv, FakeWifi
from couchd.idle import IdleService
from couchd.kodi_rpc import KodiRpc
from couchd.network import FakeConnectivity, NetConnectivity
from couchd.networkmanager import NmWifi
from couchd.pipewire import PipewireAudio
from couchd.remote import RemoteService
from couchd.services import Context, Service
from couchd.startup import StartupService
from couchd.system import FakeSystem, LinuxSystem, SystemService
from couchd.tv import TvService
from couchd.tv_volume import TvVolumeService
from couchd.uinput import UinputKeyboard
from couchd.updates import FakeUpdates, SystemdUpdates, UpdatesService, quiet_start
from couchd.wifi import WifiService

log = logging.getLogger(__name__)


def build_services(ctx: Context, *, dev: bool) -> list[Service]:
    if dev:
        tv = FakeTv(power="standby", wake_time=2.0)
        audio = FakeAudio()
        bluetooth = FakeBluetooth(audio)
        sound = AudioService(ctx, audio)
        return [
            StartupService(ctx),
            TvService(ctx, tv),
            RemoteService(ctx, tv, FakeKeyboard()),
            sound,
            TvVolumeService(ctx, tv, sound),
            BluetoothService(ctx, bluetooth),
            WifiService(ctx, FakeWifi()),
            *_apps(ctx, dev=True),
            SystemService(ctx, FakeSystem()),
            UpdatesService(ctx, FakeUpdates()),
        ]
    tv = CecTv()
    sound = AudioService(ctx, PipewireAudio())
    # After an overnight update (#24) couchd restarts with the TV off; leave it off.
    quiet = quiet_start()
    if quiet:
        log.info("restarted by couch-update: not waking the TV")
    return [
        StartupService(ctx),
        TvService(ctx, tv, wake_on_start=not quiet),
        RemoteService(ctx, tv, UinputKeyboard()),
        sound,
        TvVolumeService(ctx, tv, sound),
        BluetoothService(ctx, BluezBluetooth()),
        WifiService(ctx, NmWifi()),
        *_apps(ctx, dev=False),
        SystemService(ctx, LinuxSystem()),
        UpdatesService(ctx, SystemdUpdates()),
    ]


def _apps(ctx: Context, *, dev: bool) -> list[Service]:
    """What's on screen (#13): Chromium over DevTools, plus Kodi, real or pretend. Before an
    app opens, the internet and the Plex server are checked (#19)."""
    display = FakeDisplay() if dev else SystemdDisplay()
    net = FakeConnectivity() if dev else NetConnectivity(ctx.saved)
    kodi = None if dev else KodiRpc()
    return [
        AppService(ctx, display, Chromium(), connectivity=net, kodi_rpc=kodi),
        IdleService(ctx, idle_after=_idle_after()),
        EveningService(ctx),
    ]


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

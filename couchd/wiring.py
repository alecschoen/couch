"""Which services run, with which backends. The only place that knows dev from device."""

import logging

from couchd.apps import AppService
from couchd.audio import AudioService
from couchd.bluetooth import BluetoothService
from couchd.bluez import BluezBluetooth
from couchd.cec_tv import CecTv
from couchd.chromium import Chromium
from couchd.display import FakeDisplay, SystemdDisplay
from couchd.fakes import FakeAudio, FakeBluetooth, FakeKeyboard, FakeTv
from couchd.idle import IdleService
from couchd.network import FakeConnectivity, NetConnectivity
from couchd.pipewire import PipewireAudio
from couchd.remote import RemoteService
from couchd.services import Context, Service
from couchd.startup import StartupService
from couchd.system import FakeSystem, LinuxSystem, SystemService
from couchd.tv import TvService
from couchd.uinput import UinputKeyboard
from couchd.updates import FakeUpdates, SystemdUpdates, UpdatesService, quiet_start

log = logging.getLogger(__name__)


def build_services(ctx: Context, *, dev: bool) -> list[Service]:
    if dev:
        tv = FakeTv(power="standby", wake_time=2.0)
        audio = FakeAudio()
        bluetooth = FakeBluetooth(audio)
        return [
            StartupService(ctx),
            TvService(ctx, tv),
            RemoteService(ctx, tv, FakeKeyboard()),
            AudioService(ctx, audio),
            BluetoothService(ctx, bluetooth),
            *_apps(ctx, dev=True),
            SystemService(ctx, FakeSystem()),
            UpdatesService(ctx, FakeUpdates()),
        ]
    tv = CecTv()
    # After an overnight update (#24) couchd restarts with the TV off; leave it off.
    quiet = quiet_start()
    if quiet:
        log.info("restarted by couch-update: not waking the TV")
    return [
        StartupService(ctx),
        TvService(ctx, tv, wake_on_start=not quiet),
        RemoteService(ctx, tv, UinputKeyboard()),
        AudioService(ctx, PipewireAudio()),
        BluetoothService(ctx, BluezBluetooth()),
        *_apps(ctx, dev=False),
        SystemService(ctx, LinuxSystem()),
        UpdatesService(ctx, SystemdUpdates()),
    ]


def _apps(ctx: Context, *, dev: bool) -> list[Service]:
    """What's on screen (#13): Chromium over DevTools, plus Kodi, real or pretend. Before an
    app opens, the internet and the Plex server are checked (#19)."""
    display = FakeDisplay() if dev else SystemdDisplay()
    net = FakeConnectivity() if dev else NetConnectivity(ctx.saved)
    return [AppService(ctx, display, Chromium(), connectivity=net), IdleService(ctx)]

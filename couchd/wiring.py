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
from couchd.kodi import FakeKodi, KodiRpc
from couchd.network import FakeConnectivity, NetConnectivity
from couchd.overlay import OverlayService
from couchd.pipewire import PipewireAudio
from couchd.remote import RemoteService
from couchd.services import Context, Service
from couchd.startup import StartupService
from couchd.system import FakeSystem, LinuxSystem, SystemService
from couchd.tv import TvService
from couchd.tv_volume import TvVolumeService
from couchd.uinput import UinputKeyboard

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
            *apps,
            overlay,
            SystemService(ctx, FakeSystem()),
        ]
    tv = CecTv()
    apps, overlay = _apps(ctx, dev=False)
    sound = AudioService(ctx, PipewireAudio())
    return [
        StartupService(ctx),
        TvService(ctx, tv),
        RemoteService(ctx, tv, UinputKeyboard(), overlay=overlay),
        sound,
        TvVolumeService(ctx, tv, sound),
        BluetoothService(ctx, BluezBluetooth()),
        *apps,
        overlay,
        SystemService(ctx, LinuxSystem()),
    ]


def _apps(ctx: Context, *, dev: bool) -> tuple[list[Service], OverlayService]:
    """What's on screen (#13): Chromium over DevTools, plus Kodi, real or pretend. Before an
    app opens, the internet and the Plex server are checked (#19). The overlays (#18) draw
    over whichever is in front, Kodi's through its JSON-RPC (pretend in dev mode)."""
    display = FakeDisplay() if dev else SystemdDisplay()
    net = FakeConnectivity() if dev else NetConnectivity(ctx.saved)
    chromium = Chromium()
    apps = AppService(ctx, display, chromium, connectivity=net)
    overlay = OverlayService(
        ctx,
        chromium=chromium,
        pages=apps.page,
        kodi=FakeKodi() if dev else KodiRpc(),
        go_home=apps.home,
    )
    return [apps, IdleService(ctx)], overlay

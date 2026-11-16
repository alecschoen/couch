"""Which services run, with which backends. The only place that knows dev from device."""

import logging

from couchd.apps import AppService
from couchd.audio import AudioService
from couchd.bluetooth import BluetoothService
from couchd.chromium import Chromium
from couchd.display import FakeDisplay, SystemdDisplay
from couchd.fakes import FakeAudio, FakeBluetooth, FakeTv
from couchd.idle import IdleService
from couchd.services import Context, Service
from couchd.tv import RemoteService, TvService

log = logging.getLogger(__name__)


def build_services(ctx: Context, *, dev: bool) -> list[Service]:
    if dev:
        tv = FakeTv()
        audio = FakeAudio()
        bluetooth = FakeBluetooth(audio)
        return [
            TvService(ctx, tv),
            RemoteService(ctx, tv),
            AudioService(ctx, audio),
            BluetoothService(ctx, bluetooth),
            *_apps(ctx, dev=True),
        ]
    # Real backends arrive with CEC (#7), audio (#10) and Bluetooth (#12). Until then the
    # Pi still gets the remote, so a USB keyboard can drive the launcher.
    log.info("hardware backends not built yet; running without CEC, audio or Bluetooth")
    return [RemoteService(ctx), *_apps(ctx, dev=False)]


def _apps(ctx: Context, *, dev: bool) -> list[Service]:
    """What's on screen (#13): Chromium over DevTools, plus Kodi, real or pretend."""
    display = FakeDisplay() if dev else SystemdDisplay()
    return [AppService(ctx, display, Chromium()), IdleService(ctx)]

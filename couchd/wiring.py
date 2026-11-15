"""Which services run, with which backends. The only place that knows dev from device."""

import logging

from couchd.audio import AudioService
from couchd.bluetooth import BluetoothService
from couchd.fakes import FakeAudio, FakeBluetooth, FakeTv
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
        ]
    # Real backends arrive with CEC (#7), audio (#10) and Bluetooth (#12). Until then the
    # Pi still gets the remote, so a USB keyboard can drive the launcher.
    log.info("hardware backends not built yet; running without CEC, audio or Bluetooth")
    return [RemoteService(ctx)]

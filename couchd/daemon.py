"""Wires config, state, services and the API together and runs until told to stop."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Iterable

from aiohttp import web

from couchd import __version__
from couchd.api import create_app
from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.config import Config
from couchd.devbrowser import DevBrowser
from couchd.services import Context, Service, ServiceRegistry
from couchd.state import LiveState, SavedState
from couchd.systemd import notify, watchdog_interval, watchdog_loop
from couchd.wiring import build_services

log = logging.getLogger(__name__)

HOST = "127.0.0.1"


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        dev: bool = False,
        services: Iterable[Service] | None = None,
        open_browser: bool = False,
    ):
        """`services=None` picks the standard set for the mode (fakes with `dev`)."""
        self.config = config
        self.dev = dev
        self.bus = EventBus()
        self.state = LiveState(self.bus)
        self.saved = SavedState(config.paths.state)
        self.commands = Commands()
        self.ctx = Context(self.bus, self.state, self.saved, self.commands)
        self.services = ServiceRegistry(self.state)
        if services is None:
            services = build_services(self.ctx, dev=dev)
        for service in services:
            self.services.add(service)
        self.browser = DevBrowser(config.paths.state.parent / "chromium") if open_browser else None
        self.stopping = asyncio.Event()
        self.port: int | None = None
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        try:
            await self.start()
            watchdog = self._start_watchdog()
            await self.stopping.wait()
            if watchdog:
                watchdog.cancel()
        finally:
            await self.stop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)

    async def start(self) -> None:
        mode = "dev" if self.dev else "device"
        log.info("couchd starting", extra={"version": __version__, "mode": mode})
        self.saved.load()
        self.state.update("system", {"version": __version__, "mode": mode})

        # The API comes up first and systemd is told we're ready right after, so the
        # launcher can appear while slower services (Bluetooth, CEC) are still starting.
        await self._start_api()
        # The app manager opens the launcher at this URL if its tab is ever lost.
        self.state.update("system", {"url": f"{self.url}/"})
        notify("READY=1")
        await self.services.start_all()
        log.info("couchd ready", extra={"url": self.url})
        if self.browser is not None:
            await self.browser.open(self.url)

    async def stop(self) -> None:
        notify("STOPPING=1")
        # Services first, so the app manager lets go of the dev browser before it closes.
        await self.services.stop_all()
        if self.browser is not None:
            await self.browser.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.saved.flush()
        log.info("couchd stopped")

    async def _start_api(self) -> None:
        app = create_app(
            bus=self.bus, state=self.state, commands=self.commands, ui_dir=self.config.paths.ui
        )
        self._runner = web.AppRunner(app, handle_signals=False, shutdown_timeout=2.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, HOST, self.config.server.port)
        await site.start()
        self.port = self._runner.addresses[0][1]

    def _start_watchdog(self) -> asyncio.Task | None:
        interval = watchdog_interval()
        if interval is None:
            return None
        task = asyncio.create_task(watchdog_loop(interval))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task


async def run_daemon(daemon: Daemon) -> int:
    try:
        await daemon.run()
    except OSError as e:
        log.error("couchd could not start", extra={"error": str(e)})
        return 1
    except Exception:
        log.exception("couchd crashed")
        return 1
    finally:
        with contextlib.suppress(Exception):
            daemon.saved.flush()
    return 0

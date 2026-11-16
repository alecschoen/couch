"""Service registry. CEC, input, audio, Bluetooth and the app manager each become a Service.

Services start in the order they were added and stop in reverse. A service that fails to
start is marked `failed` and the rest carry on, because a missing Bluetooth adapter shouldn't
take the whole launcher down. Only a `critical` service failing aborts startup.

Status per service is published in the `services` section of live state.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import ClassVar

from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.settings import Settings
from couchd.state import LiveState, SavedState

log = logging.getLogger(__name__)

STOP_TIMEOUT = 5.0


@dataclass(frozen=True)
class Context:
    """What every service gets: the bus, both kinds of state, the command table and the
    user's settings. Left out, `settings` is an in-memory store with the defaults."""

    bus: EventBus
    state: LiveState
    saved: SavedState
    commands: Commands
    settings: Settings = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.settings is None:
            object.__setattr__(self, "settings", Settings(self.state))


class Service:
    name: ClassVar[str]
    critical: ClassVar[bool] = False

    async def start(self) -> None:
        """Open devices, start background tasks. Return once the service is usable."""

    async def stop(self) -> None:
        """Undo start(). Must be safe to call after a partial start."""


class ServiceError(RuntimeError):
    pass


class ServiceRegistry:
    def __init__(self, state: LiveState) -> None:
        self._state = state
        self._services: list[Service] = []
        self._running: list[Service] = []

    def add(self, service: Service) -> None:
        if any(s.name == service.name for s in self._services):
            raise ValueError(f"duplicate service name {service.name!r}")
        self._services.append(service)
        self._set_status(service, "stopped")

    def get(self, name: str) -> Service:
        for service in self._services:
            if service.name == name:
                return service
        raise KeyError(name)

    def __iter__(self):
        return iter(self._services)

    async def start_all(self) -> None:
        for service in self._services:
            self._set_status(service, "starting")
            try:
                await service.start()
            except Exception as e:
                log.exception("service failed to start", extra={"service": service.name})
                self._set_status(service, "failed")
                if service.critical:
                    await self.stop_all()
                    raise ServiceError(f"critical service {service.name!r} failed: {e}") from e
                continue
            self._running.append(service)
            self._set_status(service, "running")

    async def stop_all(self) -> None:
        while self._running:
            service = self._running.pop()
            self._set_status(service, "stopping")
            try:
                async with asyncio.timeout(STOP_TIMEOUT):
                    await service.stop()
            except Exception:
                log.exception("service failed to stop cleanly", extra={"service": service.name})
            self._set_status(service, "stopped")

    def _set_status(self, service: Service, status: str) -> None:
        self._state.update("services", {service.name: status})
